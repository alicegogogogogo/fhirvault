import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fhirvault.errors import ConflictError, NotFoundError, ValidationError
from fhirvault.server import make_handler
from fhirvault.service import FhirVault


class FrozenClock:
    """A deterministic clock: every call advances one millisecond from a fixed start."""

    def __init__(self, start: str = "2026-01-05T09:00:00+00:00"):
        self.current = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        self.current += timedelta(milliseconds=1)
        return self.current


def patient(patient_id: str = "p-1", **overrides) -> dict:
    document = {
        "resourceType": "Patient",
        "id": patient_id,
        "gender": "male",
        "birthDate": "1980-04-12",
        "name.family": "Smith",
        "name.given": "Ada",
        "identifier": [{"system": "mrn", "value": "A123"}],
    }
    document.update(overrides)
    return document


def observation(observation_id: str = "o-1", subject: str = "Patient/p-1", **overrides) -> dict:
    document = {
        "resourceType": "Observation",
        "id": observation_id,
        "status": "final",
        "code.text": "HbA1c",
        "subject": {"reference": subject},
        "effectiveDateTime": "2026-01-02T08:30:00Z",
        "value": 7.2,
    }
    for field in ("subject", "encounter", "hasMember"):
        if field in overrides and isinstance(overrides[field], str):
            overrides[field] = {"reference": overrides[field]}
    document.update(overrides)
    return document


def encounter(encounter_id: str = "e-1", subject: str = "Patient/p-1", **overrides) -> dict:
    document = {
        "resourceType": "Encounter",
        "id": encounter_id,
        "status": "finished",
        "class.code": "AMB",
        "subject": {"reference": subject},
        "period.start": "2026-01-02T08:00:00Z",
        "period.end": "2026-01-02T09:00:00Z",
    }
    for field in ("subject", "partOf"):
        if field in overrides and isinstance(overrides[field], str):
            overrides[field] = {"reference": overrides[field]}
    document.update(overrides)
    return document


class FhirVaultTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = FhirVault(str(Path(self.directory.name) / "vault.db"), FrozenClock())
        self.service.create("Patient", patient(), "k-patient")
        self.port: int | None = None

    def tearDown(self):
        self.directory.cleanup()

    # ---------------------------------------------------------------- http client

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict]:
        assert self.port is not None, "start the HTTP server before issuing requests"
        request_headers = dict(headers or {})
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"null")
        except HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    # ---------------------------------------------------------------- create/read

    def test_create_returns_versioned_resource(self):
        created = self.service.create("Observation", observation(), "k-obs")
        self.assertEqual("Observation", created["resourceType"])
        self.assertEqual("o-1", created["id"])
        self.assertEqual({"versionId": "1", "lastUpdated": "2026-01-05T09:00:00.002Z"}, created["meta"])
        self.assertEqual(created, self.service.read("Observation", "o-1"))

    def test_unknown_fields_are_rejected(self):
        with self.assertRaisesRegex(ValidationError, "unknown fields: telecom"):
            self.service.create("Patient", patient("p-2", telecom="555"), "k2")

    def test_missing_required_field_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "status is required"):
            self.service.create("Observation", {"id": "o-9", "code.text": "x"}, "k3")

    def test_wrong_code_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "must be one of"):
            self.service.create("Observation", observation("o-9", status="done"), "k4")

    def test_body_id_must_match_path_id(self):
        with self.assertRaisesRegex(ValidationError, "does not match"):
            self.service.update("Patient", "p-1", patient("p-2"), "k5")

    def test_duplicate_create_conflicts(self):
        with self.assertRaisesRegex(ConflictError, "already exists"):
            self.service.create("Patient", patient(), "k6")

    def test_idempotent_create_replays_first_result(self):
        first = self.service.create("Observation", observation(), "same-key")
        repeated = self.service.create("Observation", observation(value=99), "same-key")
        self.assertEqual(first, repeated)
        self.assertEqual(1, self.service.search("Observation", {})["total"])

    def test_idempotency_key_cannot_be_reused_for_another_operation(self):
        self.service.create("Observation", observation(), "shared")
        with self.assertRaisesRegex(ConflictError, "another operation"):
            self.service.create("Encounter", encounter(), "shared")

    # ---------------------------------------------------------------- references

    def test_reference_must_exist(self):
        with self.assertRaisesRegex(ValidationError, "Patient/p-404, which does not exist"):
            self.service.create("Observation", observation("o-2", subject="Patient/p-404"), "k7")

    def test_reference_by_identifier_resolves(self):
        created = self.service.create(
            "Encounter",
            encounter("e-2", subject="Patient/identifier|mrn|A123"),
            "k8",
        )
        self.assertEqual("Patient/identifier|mrn|A123", created["subject"]["reference"])

    def test_reference_by_identifier_failure_is_explicit(self):
        with self.assertRaisesRegex(ValidationError, "which does not exist"):
            self.service.create("Encounter", encounter("e-3", subject="Patient/identifier|mrn|ZZZ"), "k9")

    def test_reference_to_deleted_resource_is_rejected(self):
        self.service.create("Observation", observation(), "k10")
        self.service.delete("Observation", "o-1", "k11")
        with self.assertRaisesRegex(ValidationError, "Observation/o-1, which does not exist"):
            self.service.create("Observation", observation("o-2", hasMember="Observation/o-1"), "k12")
        self.assertEqual([1, 2], [entry["version"] for entry in self.service.history("Observation", "o-1")["entries"]])

    def test_self_reference_is_rejected_while_the_resource_is_absent(self):
        with self.assertRaisesRegex(ValidationError, "Patient/p-8, which does not exist"):
            self.service.create("Encounter", encounter("e-5", subject="Patient/p-8"), "k13")
        self.assertEqual("1", self.service.read("Patient", "p-1")["meta"]["versionId"])

    # ---------------------------------------------------------------- versioning

    def test_update_increments_version_and_keeps_history(self):
        self.service.create("Observation", observation(), "k16")
        updated = self.service.update("Observation", "o-1", observation(value=7.4, status="amended"), "k17")
        self.assertEqual("2", updated["meta"]["versionId"])
        self.assertEqual(7.4, updated["value"])
        history = self.service.history("Observation", "o-1")
        self.assertEqual([1, 2], [entry["version"] for entry in history["entries"]])
        self.assertEqual(7.2, history["entries"][0]["resource"]["value"])
        self.assertTrue(history["entries"][1]["current"])
        self.assertFalse(history["entries"][0]["current"])

    def test_version_never_goes_backwards(self):
        self.service.create("Observation", observation(), "k18")
        for expected in ("2", "3", "4"):
            document = self.service.update("Observation", "o-1", observation(value=int(expected)), f"u{expected}")
            self.assertEqual(expected, document["meta"]["versionId"])
        versions = [entry["version"] for entry in self.service.history("Observation", "o-1")["entries"]]
        self.assertEqual([1, 2, 3, 4], versions)

    def test_history_is_readable_after_delete(self):
        self.service.create("Observation", observation(), "k19")
        self.service.update("Observation", "o-1", observation(value=7.4), "k20")
        tombstone = self.service.delete("Observation", "o-1", "k21")
        self.assertEqual(3, tombstone["version"])
        history = self.service.history("Observation", "o-1")
        self.assertTrue(history["deleted"])
        self.assertEqual([1, 2, 3], [entry["version"] for entry in history["entries"]])
        self.assertTrue(history["entries"][2]["tombstone"])
        self.assertIsNone(history["entries"][2]["resource"])
        self.assertEqual(7.2, history["entries"][0]["resource"]["value"])
        with self.assertRaises(NotFoundError):
            self.service.read("Observation", "o-1")

    def test_recreate_after_delete_continues_the_version_sequence(self):
        self.service.create("Observation", observation(), "k22")
        self.service.delete("Observation", "o-1", "k23")
        recreated = self.service.update("Observation", "o-1", observation(value=1.0), "k24")
        self.assertEqual("3", recreated["meta"]["versionId"])
        self.assertEqual(3, len(self.service.history("Observation", "o-1")["entries"]))

    def test_history_of_unknown_resource_is_missing(self):
        with self.assertRaises(NotFoundError):
            self.service.history("Patient", "p-404")

    # ---------------------------------------------------------------- search

    def test_search_is_sorted_and_supports_prefix_and_equality(self):
        self.service.create("Patient", patient("p-2", **{"name.family": "Smithers", "identifier": [{"system": "mrn", "value": "B456"}]}), "k25")
        self.service.create("Patient", patient("p-3", gender="female", **{"name.family": "Jones", "identifier": [{"system": "mrn", "value": "C789"}]}), "k26")
        found = self.service.search("Patient", {"name.family": ["Smi"]})
        self.assertEqual(["p-1", "p-2"], [entry["resource"]["id"] for entry in found["entry"]])
        self.assertEqual(2, found["total"])
        self.assertEqual(["p-3"], [entry["resource"]["id"] for entry in self.service.search("Patient", {"gender": ["female"]})["entry"]])
        self.assertEqual(["p-1"], [entry["resource"]["id"] for entry in self.service.search("Patient", {"identifier": ["mrn|A123"]})["entry"]])
        self.assertEqual([], self.service.search("Patient", {"gender": ["female"], "name.family": ["Smi"]})["entry"])

    def test_search_by_reference_is_exact(self):
        self.service.create("Observation", observation("o-1"), "k27")
        self.service.create("Observation", observation("o-2", subject="Patient/p-2".replace("p-2", "p-1"), value=1.0), "k28")
        self.service.create("Patient", patient("p-4"), "k29")
        self.service.create("Observation", observation("o-3", subject="Patient/p-4"), "k30")
        found = self.service.search("Observation", {"subject": ["Patient/p-1"]})
        self.assertEqual(["o-1", "o-2"], [entry["resource"]["id"] for entry in found["entry"]])

    def test_search_paging_and_sorting_are_deterministic(self):
        for index in range(2, 6):
            self.service.create("Patient", patient(f"p-{index}"), f"k-{index}")
        page = self.service.search("Patient", {"_count": ["2"], "_offset": ["1"]})
        self.assertEqual(5, page["total"])
        self.assertEqual(2, page["count"])
        self.assertEqual(["p-2", "p-3"], [entry["resource"]["id"] for entry in page["entry"]])
        descending = self.service.search("Patient", {"_sort": ["-_id"], "_count": ["1"]})
        self.assertEqual(["p-5"], [entry["resource"]["id"] for entry in descending["entry"]])
        self.assertEqual(page, self.service.search("Patient", {"_count": ["2"], "_offset": ["1"]}))

    def test_search_rejects_unknown_parameters(self):
        with self.assertRaisesRegex(ValidationError, "unknown search parameters: bogus"):
            self.service.search("Patient", {"bogus": ["1"]})

    def test_deleted_resources_are_not_searchable(self):
        self.service.create("Patient", patient("p-6"), "k31")
        self.service.delete("Patient", "p-6", "k32")
        self.assertEqual(["p-1"], [entry["resource"]["id"] for entry in self.service.search("Patient", {})["entry"]])

    # ---------------------------------------------------------------- subscriptions

    def test_subscription_receives_matching_events_only(self):
        self.service.create_subscription(
            {"id": "sub-crit", "criteria": {"type": "Patient", "field": "gender", "equals": "female"}},
            "s1",
        )
        self.service.create_subscription({"id": "sub-all", "criteria": {"type": "Patient"}}, "s2")
        self.service.create_subscription({"id": "sub-prefix", "criteria": {"type": "Patient", "field": "name.family", "prefix": "Sm"}}, "s3")
        self.service.create("Patient", patient("p-2"), "k33")
        self.service.update("Patient", "p-2", patient("p-2", gender="female"), "k34")
        self.service.delete("Patient", "p-2", "k35")

        all_events = self.service.events("sub-all")["events"]
        self.assertEqual(["created", "updated", "deleted"], [event["event"] for event in all_events])
        self.assertEqual([1, 2, 3], [event["sequence"] for event in all_events])
        self.assertEqual("Patient/p-2", all_events[0]["resource"])
        criteria_events = self.service.events("sub-crit")["events"]
        self.assertEqual(["updated", "deleted"], [event["event"] for event in criteria_events])
        prefix_events = self.service.events("sub-prefix")["events"]
        self.assertEqual(["created", "updated", "deleted"], [event["event"] for event in prefix_events])

    def test_subscription_ignores_other_resource_types(self):
        self.service.create_subscription({"id": "sub-obs", "criteria": {"type": "Observation"}}, "s4")
        self.service.create("Patient", patient("p-3"), "k36")
        self.assertEqual([], self.service.events("sub-obs")["events"])
        self.service.create("Observation", observation(), "k37")
        self.assertEqual(["created"], [event["event"] for event in self.service.events("sub-obs")["events"]])

    def test_subscription_criteria_validation(self):
        with self.assertRaisesRegex(ValidationError, "criteria.type must be one of"):
            self.service.create_subscription({"id": "s", "criteria": {"type": "Device"}}, "s5")
        with self.assertRaisesRegex(ValidationError, "field must be one of"):
            self.service.create_subscription({"id": "s", "criteria": {"type": "Patient", "field": "telecom"}}, "s6")
        with self.assertRaisesRegex(ValidationError, "both equals and prefix"):
            self.service.create_subscription(
                {"id": "s", "criteria": {"type": "Patient", "field": "name.family", "equals": "x", "prefix": "y"}},
                "s7",
            )
        with self.assertRaisesRegex(ValidationError, "primitive field"):
            self.service.create_subscription(
                {"id": "s", "criteria": {"type": "Observation", "field": "subject", "equals": "Patient/p-1"}},
                "s8",
            )
        with self.assertRaisesRegex(ValidationError, "prefix is not supported"):
            self.service.create_subscription(
                {"id": "s", "criteria": {"type": "Patient", "field": "gender", "prefix": "m"}},
                "s9",
            )

    def test_subscription_idempotency_and_missing_events(self):
        first = self.service.create_subscription({"id": "sub-idem", "criteria": {"type": "Patient"}}, "s10")
        repeated = self.service.create_subscription({"id": "sub-idem", "criteria": {"type": "Observation"}}, "s10")
        self.assertEqual(first, repeated)
        self.assertEqual("Patient", repeated["criteria"]["type"])
        with self.assertRaises(NotFoundError):
            self.service.events("sub-404")

    # ---------------------------------------------------------------- http surface

    def test_http_end_to_end(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = self.request("GET", "/health")
            self.assertEqual(200, status)
            self.assertEqual({"status": "ok"}, body)

            status, body = self.request("POST", "/fhir/Patient", patient("p-20", **{"name.family": "Nakamura"}), {"Idempotency-Key": "h1"})
            self.assertEqual(201, status)
            self.assertEqual("1", body["meta"]["versionId"])

            status, body = self.request("POST", "/fhir/Observation", observation("o-20", subject="Patient/p-20"), {"Idempotency-Key": "h2"})
            self.assertEqual(201, status)

            status, body = self.request("GET", "/fhir/Patient/p-20")
            self.assertEqual("Nakamura", body["name.family"])

            status, body = self.request("PUT", "/fhir/Patient/p-20", patient("p-20", gender="female", **{"name.family": "Nakamura"}), {"Idempotency-Key": "h3"})
            self.assertEqual(200, status)
            self.assertEqual("2", body["meta"]["versionId"])

            status, body = self.request("GET", "/fhir/Patient/p-20/_history")
            self.assertEqual(200, status)
            self.assertEqual([1, 2], [entry["version"] for entry in body["entries"]])

            status, body = self.request("GET", f"/fhir/Patient?{urlencode({'name.family': 'Nak', '_count': '1'})}")
            self.assertEqual(200, status)
            self.assertEqual(1, body["total"])
            self.assertEqual(["p-20"], [entry["resource"]["id"] for entry in body["entry"]])

            status, body = self.request("POST", "/Subscription", {"id": "sub-http", "criteria": {"type": "Encounter"}}, {"Idempotency-Key": "h4"})
            self.assertEqual(201, status)
            status, body = self.request("POST", "/fhir/Subscription", {"id": "sub-http-alias", "criteria": {"type": "Encounter", "field": "status", "equals": "finished"}}, {"Idempotency-Key": "h4b"})
            self.assertEqual(201, status)
            self.assertEqual("sub-http-alias", body["subscription_id"])
            status, body = self.request("POST", "/fhir/Encounter", encounter("e-20", subject="Patient/p-20"), {"Idempotency-Key": "h5"})
            self.assertEqual(201, status)
            status, body = self.request("GET", "/subscriptions/sub-http/events")
            self.assertEqual(200, status)
            self.assertEqual(["Encounter/e-20"], [event["resource"] for event in body["events"]])
            self.assertEqual(["Encounter/e-20"], [event["resource"] for event in self.request("GET", "/subscriptions/sub-http-alias/events")[1]["events"]])

            status, body = self.request("DELETE", "/fhir/Encounter/e-20", None, {"Idempotency-Key": "h6"})
            self.assertEqual(200, status)
            self.assertTrue(body["deleted"])
            self.assertEqual(404, self.request("GET", "/fhir/Encounter/e-20")[0])

            status, body = self.request("GET", "/fhir/Patient?bogus=1")
            self.assertEqual(400, status)
            self.assertEqual("validation_error", body["error"]["code"])
            self.assertEqual(404, self.request("GET", "/fhir/Patient/p-404")[0])
            self.assertEqual(404, self.request("GET", "/nope")[0])
            multi = self.request("GET", "/fhir/Patient?gender=male&gender=female")
            self.assertEqual(200, multi[0])
            self.assertEqual(["p-1", "p-20"], [entry["resource"]["id"] for entry in multi[1]["entry"]])
            self.assertEqual(400, self.request("POST", "/fhir/Patient", patient("p-30"))[0])
            self.assertEqual(201, self.request("POST", "/fhir/Patient", patient("p-31", gender="female"), {"Idempotency-Key": "h7"})[0])
            self.assertEqual(409, self.request("POST", "/fhir/Patient", patient("p-31"), {"Idempotency-Key": "h8"})[0])
        finally:
            self.port = None
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
