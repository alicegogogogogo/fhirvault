import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

# Talk to the loopback server directly, never through a proxy from the environment.
_OPENER = build_opener(ProxyHandler({}))

from fhirvault.errors import ConflictError, NotFoundError, OperationOutcomeError, ValidationError
from fhirvault.server import make_handler
from fhirvault.service import AuditContext, FhirVault


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


def http_request(
    port: int,
    method: str,
    path: str,
    payload: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, dict, dict]:
    request_headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    request = Request(f"http://127.0.0.1:{port}{path}", data=data, headers=request_headers, method=method)
    try:
        with _OPENER.open(request, timeout=5) as response:
            headers_out = {name.lower(): value for name, value in response.headers.items()}
            return response.status, headers_out, json.loads(response.read() or b"null")
    except HTTPError as error:
        headers_out = {name.lower(): value for name, value in error.headers.items()}
        return error.code, headers_out, json.loads(error.read() or b"null")


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
        status, _, body = http_request(self.port, method, path, payload, headers)
        return status, body

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

    # ---------------------------------------------------------------- include expansion

    def test_forward_include_returns_targets_of_page_entries(self):
        self.service.create("Patient", patient("p-2"), "k60")
        self.service.create("Observation", observation("o-1"), "k61")
        self.service.create("Observation", observation("o-2", subject="Patient/p-2"), "k62")
        found = self.service.search("Observation", {"_include": ["Observation:subject"]})
        self.assertEqual(["o-1", "o-2"], [entry["resource"]["id"] for entry in found["entry"]])
        self.assertEqual(2, found["total"])
        self.assertEqual(2, found["count"])
        self.assertEqual(
            [("Patient", "p-1"), ("Patient", "p-2")],
            [(doc["resourceType"], doc["id"]) for doc in found["include"]],
        )
        self.assertEqual([], found["revinclude"])

    def test_forward_include_deduplicates_shared_targets(self):
        self.service.create("Observation", observation("o-1"), "k63")
        self.service.create("Observation", observation("o-2"), "k64")
        found = self.service.search("Observation", {"_include": ["Observation:subject"]})
        self.assertEqual(["p-1"], [doc["id"] for doc in found["include"]])

    def test_forward_include_resolves_identifier_references(self):
        self.service.create(
            "Observation", observation("o-9", subject="Patient/identifier|mrn|A123"), "k65"
        )
        found = self.service.search("Observation", {"_include": ["Observation:subject"]})
        self.assertEqual(["p-1"], [doc["id"] for doc in found["include"]])

    def test_forward_include_skips_deleted_and_unresolvable_targets(self):
        self.service.create("Patient", patient("p-2"), "k66")
        self.service.create("Observation", observation("o-1"), "k67")
        self.service.create("Observation", observation("o-2", subject="Patient/p-2"), "k68")
        self.service.delete("Patient", "p-2", "k69")
        found = self.service.search("Observation", {"_include": ["Observation:subject"]})
        self.assertEqual(["o-1", "o-2"], [entry["resource"]["id"] for entry in found["entry"]])
        self.assertEqual(["p-1"], [doc["id"] for doc in found["include"]])

    def test_include_is_one_hop_and_not_counted_in_totals(self):
        self.service.create("Patient", patient("p-2"), "k70")
        self.service.create("Encounter", encounter("e-1", subject="Patient/p-2"), "k71")
        self.service.create(
            "Observation",
            observation("o-1", subject="Patient/p-1", encounter="Encounter/e-1"),
            "k72",
        )
        found = self.service.search(
            "Observation", {"_include": ["Observation:encounter", "Observation:subject"]}
        )
        self.assertEqual(1, found["total"])
        self.assertEqual(1, found["count"])
        # The Encounter's Patient/p-2 must not be pulled in: expansion stops after one hop.
        self.assertEqual(
            [("Encounter", "e-1"), ("Patient", "p-1")],
            [(doc["resourceType"], doc["id"]) for doc in found["include"]],
        )

    def test_include_only_covers_the_current_page(self):
        self.service.create("Patient", patient("p-2"), "k73")
        self.service.create("Observation", observation("o-1"), "k74")
        self.service.create("Observation", observation("o-2", subject="Patient/p-2"), "k75")
        found = self.service.search(
            "Observation", {"_include": ["Observation:subject"], "_count": ["1"], "_offset": ["1"]}
        )
        self.assertEqual(["o-2"], [entry["resource"]["id"] for entry in found["entry"]])
        self.assertEqual(["p-2"], [doc["id"] for doc in found["include"]])

    def test_primary_entries_are_not_copied_into_include_arrays(self):
        self.service.create("Observation", observation("o-1"), "k76a")
        self.service.create("Observation", observation("o-2", hasMember="Observation/o-1"), "k76")
        found = self.service.search("Observation", {"_include": ["Observation:hasMember"]})
        self.assertEqual(["o-1", "o-2"], [entry["resource"]["id"] for entry in found["entry"]])
        # o-1 is an entry target and an include target, but stays out of the include array.
        self.assertEqual([], found["include"])

    def test_reverse_include_returns_resources_pointing_at_primary_results(self):
        self.service.create("Patient", patient("p-2"), "k77")
        self.service.create("Observation", observation("o-1"), "k78")
        self.service.create("Observation", observation("o-2", subject="Patient/p-2"), "k79")
        found = self.service.search("Patient", {"_revinclude": ["Observation:subject"]})
        self.assertEqual(["p-1", "p-2"], [entry["resource"]["id"] for entry in found["entry"]])
        self.assertEqual(
            [("Observation", "o-1"), ("Observation", "o-2")],
            [(doc["resourceType"], doc["id"]) for doc in found["revinclude"]],
        )
        self.assertEqual([], found["include"])

    def test_reverse_include_resolves_identifier_references(self):
        self.service.create(
            "Observation", observation("o-9", subject="Patient/identifier|mrn|A123"), "k80"
        )
        found = self.service.search("Patient", {"_revinclude": ["Observation:subject"]})
        self.assertEqual(["o-9"], [doc["id"] for doc in found["revinclude"]])

    def test_reverse_include_skips_deleted_targets_and_other_types(self):
        self.service.create("Patient", patient("p-2"), "k81")
        self.service.create("Observation", observation("o-1"), "k82")
        self.service.create("Observation", observation("o-2", subject="Patient/p-2"), "k83")
        self.service.delete("Patient", "p-2", "k84")
        found = self.service.search("Patient", {"_revinclude": ["Observation:subject"]})
        self.assertEqual(["p-1"], [entry["resource"]["id"] for entry in found["entry"]])
        self.assertEqual(["o-1"], [doc["id"] for doc in found["revinclude"]])

    def test_reverse_include_respects_search_filtering_and_paging(self):
        self.service.create("Patient", patient("p-2", gender="female"), "k85")
        self.service.create("Observation", observation("o-1"), "k86")
        self.service.create("Observation", observation("o-2", subject="Patient/p-2"), "k87")
        found = self.service.search(
            "Patient", {"gender": ["male"], "_revinclude": ["Observation:subject"]}
        )
        self.assertEqual(["p-1"], [entry["resource"]["id"] for entry in found["entry"]])
        self.assertEqual(["o-1"], [doc["id"] for doc in found["revinclude"]])

    def test_same_resource_can_appear_in_both_expansion_arrays(self):
        # o-1 and o-2 reference each other via hasMember; the primary search only
        # contains o-1, so o-2 is both a forward target (o-1 -> o-2) and a reverse
        # target (o-2 -> o-1) and appears in each array independently.
        self.service.create("Observation", observation("o-1"), "k88")
        self.service.create("Observation", observation("o-2", hasMember="Observation/o-1"), "k89")
        self.service.update("Observation", "o-1", observation("o-1", hasMember="Observation/o-2"), "k89b")
        found = self.service.search(
            "Observation",
            {
                "id": ["o-1"],
                "_include": ["Observation:hasMember"],
                "_revinclude": ["Observation:hasMember"],
            },
        )
        self.assertEqual(["o-1"], [entry["resource"]["id"] for entry in found["entry"]])
        self.assertEqual(["o-2"], [doc["id"] for doc in found["include"]])
        self.assertEqual(["o-2"], [doc["id"] for doc in found["revinclude"]])

    def test_empty_entry_yields_empty_expansion_arrays(self):
        self.service.create("Observation", observation("o-1"), "k90")
        found = self.service.search(
            "Patient",
            {"id": ["p-404"], "_revinclude": ["Observation:subject"]},
        )
        self.assertEqual([], found["entry"])
        self.assertEqual([], found["include"])
        self.assertEqual([], found["revinclude"])

    def test_repeated_expansion_parameters_merge_and_deduplicate(self):
        self.service.create("Patient", patient("p-2"), "k91")
        self.service.create("Observation", observation("o-1"), "k92")
        self.service.create("Observation", observation("o-2", subject="Patient/p-2"), "k93")
        self.service.create("Encounter", encounter("e-1"), "k94")
        found = self.service.search(
            "Patient",
            {
                "_revinclude": ["Observation:subject", "Observation:subject", "Encounter:subject"],
            },
        )
        self.assertEqual(
            [("Encounter", "e-1"), ("Observation", "o-1"), ("Observation", "o-2")],
            [(doc["resourceType"], doc["id"]) for doc in found["revinclude"]],
        )
        self.assertEqual(
            ["Encounter:subject", "Observation:subject", "Observation:subject"],
            found["parameters"]["_revinclude"],
        )

    def test_search_without_expansion_parameters_is_unchanged(self):
        self.service.create("Observation", observation("o-1"), "k95")
        found = self.service.search("Observation", {})
        self.assertNotIn("include", found)
        self.assertNotIn("revinclude", found)

    def test_invalid_include_parameters_are_rejected(self):
        cases = [
            ({"_include": ["Observation.subject"]}, "form"),
            ({"_include": ["Observation:subject:extra"]}, "form"),
            ({"_include": [":subject"]}, "form"),
            ({"_include": ["Device:subject"]}, "not supported"),
            ({"_include": ["Patient:subject"]}, "must be Observation"),
            ({"_include": ["Observation:status"]}, "not a reference field"),
            ({"_include": ["Observation:telecom"]}, "not a reference field"),
            ({"_revinclude": ["Device:subject"]}, "not supported"),
            ({"_revinclude": ["Observation:status"]}, "not a reference field"),
            ({"_revinclude": ["Observation:nope"]}, "not a reference field"),
            ({"_revinclude": ["Patient:active"]}, "not a reference field"),
        ]
        for parameters, fragment in cases:
            with self.subTest(parameters=parameters):
                with self.assertRaisesRegex(ValidationError, fragment):
                    self.service.search("Observation", parameters)

    def test_bad_expansion_parameter_returns_no_partial_results(self):
        self.service.create("Observation", observation("o-1"), "k96")
        with self.assertRaises(ValidationError):
            self.service.search(
                "Observation",
                {"_include": ["Observation:subject"], "_revinclude": ["Bogus:subject"]},
            )

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

    # ---------------------------------------------------------------- conditional update

    def test_update_with_matching_if_match_creates_next_version(self):
        self.service.create("Observation", observation(), "k40")
        updated = self.service.update("Observation", "o-1", observation(value=8.0), "k41", if_match='W/"1"')
        self.assertEqual("2", updated["meta"]["versionId"])
        self.assertEqual(8.0, updated["value"])

    def test_strong_and_weak_if_match_are_equivalent(self):
        self.service.create("Observation", observation(), "k42")
        self.assertEqual("2", self.service.update("Observation", "o-1", observation(value=1.0), "k43", if_match='"1"')["meta"]["versionId"])
        self.assertEqual("3", self.service.update("Observation", "o-1", observation(value=2.0), "k44", if_match='W/"2"')["meta"]["versionId"])

    def test_star_if_match_requires_an_existing_resource(self):
        self.service.create("Observation", observation(), "k45")
        updated = self.service.update("Observation", "o-1", observation(value=3.0), "k46", if_match="*")
        self.assertEqual("2", updated["meta"]["versionId"])
        with self.assertRaises(OperationOutcomeError) as caught:
            self.service.update("Observation", "o-404", observation("o-404"), "k47", if_match="*")
        self.assertEqual(404, caught.exception.status)
        self.assertEqual("not-found", caught.exception.issue_code)

    def test_if_match_on_missing_resource_is_not_found(self):
        with self.assertRaises(OperationOutcomeError) as caught:
            self.service.update("Observation", "o-404", observation("o-404"), "k48", if_match='"1"')
        self.assertEqual(404, caught.exception.status)
        self.assertEqual("not-found", caught.exception.issue_code)

    def test_stale_if_match_conflicts_and_keeps_history(self):
        self.service.create("Observation", observation(), "k49")
        self.service.update("Observation", "o-1", observation(value=7.4), "k50")
        with self.assertRaises(OperationOutcomeError) as caught:
            self.service.update("Observation", "o-1", observation(value=9.9), "k51", if_match='"1"')
        self.assertEqual(412, caught.exception.status)
        self.assertEqual("conflict", caught.exception.issue_code)
        self.assertIn("not the current version", str(caught.exception))
        history = self.service.history("Observation", "o-1")
        self.assertEqual([1, 2], [entry["version"] for entry in history["entries"]])
        self.assertEqual(7.4, self.service.read("Observation", "o-1")["value"])

    def test_invalid_if_match_is_rejected_without_touching_the_resource(self):
        self.service.create("Observation", observation(), "k52")
        for bad in ("bogus", "W/1", '"unclosed', 'W/"1" "1"', ""):
            with self.assertRaises(OperationOutcomeError) as caught:
                self.service.update("Observation", "o-1", observation(value=5.0), f"k-bad-{bad}", if_match=bad)
            self.assertEqual(400, caught.exception.status, bad)
            self.assertEqual("invalid", caught.exception.issue_code, bad)
        self.assertEqual("1", self.service.read("Observation", "o-1")["meta"]["versionId"])

    def test_missing_if_match_keeps_unconditional_upsert(self):
        created = self.service.update("Observation", "o-new", observation("o-new"), "k53")
        self.assertEqual("1", created["meta"]["versionId"])
        overwritten = self.service.update("Observation", "o-new", observation("o-new", value=4.0), "k54")
        self.assertEqual("2", overwritten["meta"]["versionId"])

    def test_validation_failure_with_if_match_creates_no_version(self):
        self.service.create("Observation", observation(), "k55")
        with self.assertRaisesRegex(ValidationError, "must be one of"):
            self.service.update("Observation", "o-1", observation(status="done"), "k56", if_match='"1"')
        self.assertEqual([1], [entry["version"] for entry in self.service.history("Observation", "o-1")["entries"]])

    def test_concurrent_conditional_updates_have_one_winner(self):
        self.service.create("Observation", observation(), "k57")
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, Any]] = []

        def attempt(key: str) -> None:
            barrier.wait(timeout=5)
            try:
                document = self.service.update("Observation", "o-1", observation(value=8.8), key, if_match='W/"1"')
                outcomes.append(("updated", document["meta"]["versionId"]))
            except OperationOutcomeError as error:
                outcomes.append(("error", error.status))

        threads = [threading.Thread(target=attempt, args=(f"race-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sorted([("error", 412), ("updated", "2")]), sorted(outcomes))
        self.assertEqual([1, 2], [entry["version"] for entry in self.service.history("Observation", "o-1")["entries"]])

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

            status, body = self.request("GET", "/fhir/Observation?_include=Observation:subject&_revinclude=Observation:hasMember")
            self.assertEqual(200, status)
            self.assertEqual(["o-20"], [entry["resource"]["id"] for entry in body["entry"]])
            self.assertEqual(["p-20"], [doc["id"] for doc in body["include"]])
            self.assertEqual([], body["revinclude"])
            self.assertEqual(
                ["Observation:subject"], body["parameters"]["_include"]
            )

            status, body = self.request("GET", "/fhir/Patient?_revinclude=Observation:subject")
            self.assertEqual(200, status)
            self.assertEqual(
                ["p-1", "p-20"], [entry["resource"]["id"] for entry in body["entry"]]
            )
            self.assertEqual(["o-20"], [doc["id"] for doc in body["revinclude"]])
            self.assertEqual([], body["include"])

            status, body = self.request("GET", "/fhir/Observation?_include=Patient:subject")
            self.assertEqual(400, status)
            self.assertEqual("validation_error", body["error"]["code"])
            status, body = self.request("GET", "/fhir/Observation?_include=Observation:status")
            self.assertEqual(400, status)
            self.assertEqual("validation_error", body["error"]["code"])
            status, body = self.request("GET", "/fhir/Observation?_revinclude=Bogus:subject")
            self.assertEqual(400, status)
            self.assertEqual("validation_error", body["error"]["code"])
            status, body = self.request("GET", "/fhir/Observation?_include=malformed")
            self.assertEqual(400, status)
            self.assertEqual("validation_error", body["error"]["code"])

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


class ConditionalUpdateHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = FhirVault(str(Path(self.directory.name) / "vault.db"), FrozenClock())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.directory.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict, dict]:
        return http_request(self.port, method, path, payload, headers)

    def create_patient(self, patient_id: str = "p-1") -> tuple[int, dict, dict]:
        return self.request("POST", "/fhir/Patient", patient(patient_id), {"Idempotency-Key": f"create-{patient_id}"})

    def test_read_and_write_responses_carry_etags(self):
        status, headers, body = self.create_patient()
        self.assertEqual(201, status)
        self.assertEqual('W/"1"', headers.get("etag"))
        self.assertEqual("/fhir/Patient/p-1", headers.get("location"))

        status, headers, body = self.request("GET", "/fhir/Patient/p-1")
        self.assertEqual(200, status)
        self.assertEqual('W/"1"', headers.get("etag"))

        status, headers, body = self.request(
            "PUT", "/fhir/Patient/p-1", patient(gender="female"),
            {"Idempotency-Key": "u1", "If-Match": headers["etag"]},
        )
        self.assertEqual(200, status)
        self.assertEqual("2", body["meta"]["versionId"])
        self.assertEqual('W/"2"', headers.get("etag"))
        self.assertEqual("/fhir/Patient/p-1", headers.get("location"))

        status, headers, body = self.request("GET", "/fhir/Patient/p-1")
        self.assertEqual('W/"2"', headers.get("etag"))

    def test_strong_and_weak_if_match_both_match(self):
        self.create_patient()
        status, _, body = self.request(
            "PUT", "/fhir/Patient/p-1", patient(gender="female"), {"Idempotency-Key": "u2", "If-Match": '"1"'}
        )
        self.assertEqual(200, status)
        self.assertEqual("2", body["meta"]["versionId"])
        status, _, body = self.request(
            "PUT", "/fhir/Patient/p-1", patient(gender="male"), {"Idempotency-Key": "u3", "If-Match": 'W/"2"'}
        )
        self.assertEqual(200, status)
        self.assertEqual("3", body["meta"]["versionId"])

    def test_star_if_match(self):
        self.create_patient()
        status, _, _ = self.request(
            "PUT", "/fhir/Patient/p-1", patient(gender="female"), {"Idempotency-Key": "u4", "If-Match": "*"}
        )
        self.assertEqual(200, status)
        status, headers, body = self.request(
            "PUT", "/fhir/Patient/p-404", patient("p-404"), {"Idempotency-Key": "u5", "If-Match": "*"}
        )
        self.assertEqual(404, status)
        self.assertEqual("application/fhir+json", headers.get("content-type"))
        self.assertEqual("OperationOutcome", body["resourceType"])
        self.assertEqual("not-found", body["issue"][0]["code"])

    def test_invalid_if_match_is_a_fhir_400(self):
        self.create_patient()
        status, headers, body = self.request(
            "PUT", "/fhir/Patient/p-1", patient(gender="female"), {"Idempotency-Key": "u6", "If-Match": "not-an-etag"}
        )
        self.assertEqual(400, status)
        self.assertEqual("application/fhir+json", headers.get("content-type"))
        self.assertEqual("OperationOutcome", body["resourceType"])
        self.assertEqual("invalid", body["issue"][0]["code"])
        _, _, current = self.request("GET", "/fhir/Patient/p-1")
        self.assertEqual("1", current["meta"]["versionId"])

    def test_stale_if_match_is_a_fhir_412(self):
        self.create_patient()
        self.request("PUT", "/fhir/Patient/p-1", patient(gender="female"), {"Idempotency-Key": "u7"})
        status, headers, body = self.request(
            "PUT", "/fhir/Patient/p-1", patient(gender="male"), {"Idempotency-Key": "u8", "If-Match": 'W/"1"'}
        )
        self.assertEqual(412, status)
        self.assertEqual("application/fhir+json", headers.get("content-type"))
        self.assertEqual("OperationOutcome", body["resourceType"])
        self.assertEqual("conflict", body["issue"][0]["code"])
        self.assertIn("not the current version", body["issue"][0]["diagnostics"])
        _, _, history = self.request("GET", "/fhir/Patient/p-1/_history")
        self.assertEqual([1, 2], [entry["version"] for entry in history["entries"]])
        _, _, current = self.request("GET", "/fhir/Patient/p-1")
        self.assertEqual("female", current["gender"])

    def test_missing_if_match_keeps_unconditional_update(self):
        self.create_patient()
        status, headers, body = self.request(
            "PUT", "/fhir/Patient/p-1", patient(gender="female"), {"Idempotency-Key": "u9"}
        )
        self.assertEqual(200, status)
        self.assertEqual("2", body["meta"]["versionId"])
        self.assertEqual('W/"2"', headers.get("etag"))

    def test_concurrent_updates_with_same_if_match_have_one_winner(self):
        self.create_patient()
        barrier = threading.Barrier(2)
        results: list[tuple[int, dict]] = []

        def attempt(key: str) -> None:
            barrier.wait(timeout=5)
            status, _, body = self.request(
                "PUT", "/fhir/Patient/p-1", patient(gender="female"),
                {"Idempotency-Key": key, "If-Match": 'W/"1"'},
            )
            results.append((status, body))

        threads = [threading.Thread(target=attempt, args=(f"race-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        statuses = sorted(status for status, _ in results)
        self.assertEqual([200, 412], statuses)
        winner = next(body for status, body in results if status == 200)
        self.assertEqual("2", winner["meta"]["versionId"])
        loser = next(body for status, body in results if status == 412)
        self.assertEqual("OperationOutcome", loser["resourceType"])
        self.assertEqual("conflict", loser["issue"][0]["code"])
        _, _, history = self.request("GET", "/fhir/Patient/p-1/_history")
        self.assertEqual([1, 2], [entry["version"] for entry in history["entries"]])


def transaction_bundle(*entries: dict) -> dict:
    return {"resourceType": "Bundle", "type": "transaction", "entry": list(entries)}


def txn_entry(method: str, url: str, resource: dict | None = None, if_match: str | None = None) -> dict:
    request: dict[str, Any] = {"method": method, "url": url}
    if if_match is not None:
        request["ifMatch"] = if_match
    result: dict[str, Any] = {"request": request}
    if resource is not None:
        result["resource"] = resource
    return result


class TransactionHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = FhirVault(str(Path(self.directory.name) / "vault.db"), FrozenClock())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.directory.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict, dict]:
        return http_request(self.port, method, path, payload, headers)

    def post_bundle(self, bundle: dict, key: str = "txn-1") -> tuple[int, dict, dict]:
        return self.request("POST", "/fhir", bundle, {"Idempotency-Key": key})

    def post_raw(self, data: bytes, headers: dict) -> tuple[int, dict, dict]:
        request = Request(f"http://127.0.0.1:{self.port}/fhir", data=data, headers=headers, method="POST")
        try:
            with _OPENER.open(request, timeout=5) as response:
                headers_out = {name.lower(): value for name, value in response.headers.items()}
                return response.status, headers_out, json.loads(response.read() or b"null")
        except HTTPError as error:
            headers_out = {name.lower(): value for name, value in error.headers.items()}
            return error.code, headers_out, json.loads(error.read() or b"null")

    def assert_outcome(self, status: int, headers: dict, body: dict, expected_status: int, code: str):
        self.assertEqual(expected_status, status)
        self.assertEqual("application/fhir+json", headers.get("content-type"))
        self.assertEqual("OperationOutcome", body["resourceType"])
        self.assertEqual(1, len(body["issue"]))
        self.assertEqual("error", body["issue"][0]["severity"])
        self.assertEqual(code, body["issue"][0]["code"])

    # ---------------------------------------------------------------- happy path

    def test_transaction_applies_entries_in_order(self):
        status, _, body = self.post_bundle(transaction_bundle(
            txn_entry("POST", "Patient", patient("p-10")),
            txn_entry("POST", "Observation", observation("o-10", subject="Patient/p-10")),
            txn_entry("PUT", "Observation/o-10", observation("o-10", subject="Patient/p-10", value=8.1), if_match='W/"1"'),
            txn_entry("DELETE", "Observation/o-10"),
        ))
        self.assertEqual(200, status)
        self.assertEqual("Bundle", body["resourceType"])
        self.assertEqual("transaction-response", body["type"])
        self.assertEqual(4, len(body["entry"]))
        self.assertEqual(["201", "201", "200", "200"], [item["response"]["status"] for item in body["entry"]])
        self.assertEqual("/fhir/Patient/p-10", body["entry"][0]["response"]["location"])
        self.assertEqual('W/"1"', body["entry"][0]["response"]["etag"])
        self.assertEqual("p-10", body["entry"][0]["resource"]["id"])
        self.assertEqual('W/"2"', body["entry"][2]["response"]["etag"])
        self.assertEqual(8.1, body["entry"][2]["resource"]["value"])
        self.assertEqual("/fhir/Observation/o-10", body["entry"][3]["response"]["location"])
        self.assertNotIn("etag", body["entry"][3]["response"])
        self.assertIsNone(body["entry"][3]["resource"])

        self.assertEqual(200, self.request("GET", "/fhir/Patient/p-10")[0])
        self.assertEqual(404, self.request("GET", "/fhir/Observation/o-10")[0])
        _, _, history = self.request("GET", "/fhir/Observation/o-10/_history")
        self.assertEqual([1, 2, 3], [item["version"] for item in history["entries"]])
        self.assertTrue(history["entries"][-1]["tombstone"])

    def test_later_entries_may_reference_earlier_writes_by_identifier(self):
        status, _, body = self.post_bundle(transaction_bundle(
            txn_entry("POST", "Patient", patient("p-11", identifier=[{"system": "mrn", "value": "B222"}])),
            txn_entry("POST", "Encounter", encounter("e-11", subject="Patient/identifier|mrn|B222")),
        ))
        self.assertEqual(200, status)
        self.assertEqual(["201", "201"], [item["response"]["status"] for item in body["entry"]])
        _, _, stored = self.request("GET", "/fhir/Encounter/e-11")
        self.assertEqual("Patient/identifier|mrn|B222", stored["subject"]["reference"])

    def test_events_are_recorded_in_order_without_duplicates(self):
        self.request("POST", "/Subscription", {"id": "sub-t", "criteria": {"type": "Patient"}}, {"Idempotency-Key": "sub-t"})
        status, _, _ = self.post_bundle(transaction_bundle(
            txn_entry("POST", "Patient", patient("p-12")),
            txn_entry("PUT", "Patient/p-12", patient("p-12", gender="female")),
        ))
        self.assertEqual(200, status)
        _, _, events = self.request("GET", "/subscriptions/sub-t/events")
        self.assertEqual([1, 2], [event["sequence"] for event in events["events"]])
        self.assertEqual(["created", "updated"], [event["event"] for event in events["events"]])
        self.assertEqual([1, 2], [event["version"] for event in events["events"]])

    # ---------------------------------------------------------------- envelope validation

    def test_missing_idempotency_key_is_a_fhir_400(self):
        status, headers, body = self.request("POST", "/fhir", transaction_bundle(txn_entry("POST", "Patient", patient("p-20"))))
        self.assert_outcome(status, headers, body, 400, "invalid")
        self.assertEqual(404, self.request("GET", "/fhir/Patient/p-20")[0])

    def test_invalid_json_is_a_fhir_400(self):
        status, headers, body = self.post_raw(b"{not json", {"Content-Type": "application/json", "Idempotency-Key": "bad-json"})
        self.assert_outcome(status, headers, body, 400, "invalid")

    def test_wrong_content_type_is_a_fhir_400(self):
        status, headers, body = self.post_raw(b"{}", {"Content-Type": "text/plain", "Idempotency-Key": "bad-ct"})
        self.assert_outcome(status, headers, body, 400, "invalid")

    def test_envelope_validation(self):
        too_many = [{"request": {"method": "DELETE", "url": f"Patient/p-{index}"}} for index in range(101)]
        invalid_bundles = [
            {},
            {"resourceType": "Patient", "type": "transaction", "entry": [txn_entry("DELETE", "Patient/p-1")]},
            {"resourceType": "Bundle", "type": "batch", "entry": [txn_entry("DELETE", "Patient/p-1")]},
            {"resourceType": "Bundle", "type": "transaction", "entry": [txn_entry("DELETE", "Patient/p-1")], "extra": 1},
            {"resourceType": "Bundle", "type": "transaction"},
            {"resourceType": "Bundle", "type": "transaction", "entry": []},
            {"resourceType": "Bundle", "type": "transaction", "entry": too_many},
            {"resourceType": "Bundle", "type": "transaction", "entry": ["not-an-object"]},
            {"resourceType": "Bundle", "type": "transaction", "entry": [{"request": {"method": "DELETE", "url": "Patient/p-1"}, "fullUrl": "x"}]},
            {"resourceType": "Bundle", "type": "transaction", "entry": [{"resource": patient("p-1")}]},
            {"resourceType": "Bundle", "type": "transaction", "entry": [{"request": {"method": "DELETE", "url": "Patient/p-1", "ifNoneMatch": "*"}}]},
        ]
        for index, bundle in enumerate(invalid_bundles):
            with self.subTest(index=index):
                status, headers, body = self.post_bundle(bundle, key=f"bad-envelope-{index}")
                self.assert_outcome(status, headers, body, 400, "invalid")
        _, _, search = self.request("GET", "/fhir/Patient")
        self.assertEqual(0, search["total"])

    def test_entry_request_validation(self):
        invalid_entries = [
            txn_entry("GET", "Patient/p-1"),
            txn_entry("PATCH", "Patient/p-1"),
            txn_entry("POST", "Patient/p-1", patient("p-1")),
            txn_entry("POST", "Bogus", patient("p-1")),
            txn_entry("PUT", "Patient", patient("p-1")),
            txn_entry("PUT", "Bogus/p-1", patient("p-1")),
            txn_entry("DELETE", "Patient/"),
            txn_entry("POST", "Patient"),  # resource required
            txn_entry("PUT", "Patient/p-1"),  # resource required
            txn_entry("POST", "Patient", patient("p-1"), if_match='W/"1"'),  # ifMatch is PUT-only
            {**txn_entry("DELETE", "Patient/p-1"), "resource": patient("p-1")},
            txn_entry("POST", "Patient", patient("p-1", telecom="555")),
            txn_entry("POST", "Patient", {**patient("p-1"), "resourceType": "Observation"}),
            txn_entry("PUT", "Patient/p-1", patient("p-2")),  # body id must match path id
            txn_entry("PUT", "Patient/p-1", patient("p-1"), if_match="junk"),
        ]
        for index, bad in enumerate(invalid_entries):
            with self.subTest(index=index):
                status, headers, body = self.post_bundle(transaction_bundle(bad), key=f"bad-entry-{index}")
                self.assert_outcome(status, headers, body, 400, "invalid")
        _, _, search = self.request("GET", "/fhir/Patient")
        self.assertEqual(0, search["total"])

    # ---------------------------------------------------------------- atomicity

    def test_first_failure_rolls_back_the_whole_batch(self):
        status, headers, body = self.post_bundle(transaction_bundle(
            txn_entry("POST", "Patient", patient("p-13")),
            txn_entry("POST", "Observation", observation("o-13", subject="Patient/p-404")),
            txn_entry("POST", "Observation", observation("o-14", subject="Patient/p-13")),
        ))
        self.assert_outcome(status, headers, body, 400, "invalid")
        self.assertIn("Patient/p-404", body["issue"][0]["diagnostics"])
        self.assertEqual(404, self.request("GET", "/fhir/Patient/p-13")[0])
        _, _, search = self.request("GET", "/fhir/Observation")
        self.assertEqual(0, search["total"])

    def test_reference_to_resource_deleted_earlier_in_the_batch_fails(self):
        self.request("POST", "/fhir/Patient", patient("p-17"), {"Idempotency-Key": "seed"})
        status, headers, body = self.post_bundle(transaction_bundle(
            txn_entry("DELETE", "Patient/p-17"),
            txn_entry("POST", "Observation", observation("o-17", subject="Patient/p-17")),
        ))
        self.assert_outcome(status, headers, body, 400, "invalid")
        self.assertEqual(200, self.request("GET", "/fhir/Patient/p-17")[0])

    def test_post_of_existing_id_conflicts_and_rolls_back(self):
        self.request("POST", "/fhir/Patient", patient("p-1"), {"Idempotency-Key": "seed"})
        status, headers, body = self.post_bundle(transaction_bundle(
            txn_entry("POST", "Patient", patient("p-14")),
            txn_entry("POST", "Patient", patient("p-1")),
        ))
        self.assert_outcome(status, headers, body, 409, "conflict")
        self.assertEqual(404, self.request("GET", "/fhir/Patient/p-14")[0])
        _, _, current = self.request("GET", "/fhir/Patient/p-1")
        self.assertEqual("1", current["meta"]["versionId"])

    # ---------------------------------------------------------------- conditional writes

    def test_put_if_match_semantics_inside_a_transaction(self):
        self.request("POST", "/fhir/Patient", patient("p-1"), {"Idempotency-Key": "seed"})

        status, headers, body = self.post_bundle(
            transaction_bundle(txn_entry("PUT", "Patient/p-1", patient("p-1", gender="female"), if_match='W/"2"')), "txn-stale"
        )
        self.assert_outcome(status, headers, body, 412, "conflict")

        status, headers, body = self.post_bundle(
            transaction_bundle(txn_entry("PUT", "Patient/p-404", patient("p-404"), if_match="*")), "txn-missing"
        )
        self.assert_outcome(status, headers, body, 404, "not-found")

        _, _, current = self.request("GET", "/fhir/Patient/p-1")
        self.assertEqual("1", current["meta"]["versionId"])

        for index, tag in enumerate(('"1"', 'W/"2"', "*")):
            status, _, body = self.post_bundle(
                transaction_bundle(txn_entry("PUT", "Patient/p-1", patient("p-1", gender="female"), if_match=tag)),
                f"txn-tag-{index}",
            )
            self.assertEqual(200, status)
            self.assertEqual("200", body["entry"][0]["response"]["status"])
        _, _, current = self.request("GET", "/fhir/Patient/p-1")
        self.assertEqual("4", current["meta"]["versionId"])

    def test_unconditional_put_upserts_and_recreates_deleted_ids(self):
        status, _, body = self.post_bundle(transaction_bundle(
            txn_entry("PUT", "Patient/p-15", patient("p-15")),
        ))
        self.assertEqual(200, status)
        self.assertEqual('W/"1"', body["entry"][0]["response"]["etag"])

        self.request("DELETE", "/fhir/Patient/p-15", None, {"Idempotency-Key": "del"})
        status, _, body = self.post_bundle(transaction_bundle(
            txn_entry("PUT", "Patient/p-15", patient("p-15", gender="female")),
        ), key="txn-recreate")
        self.assertEqual(200, status)
        self.assertEqual('W/"3"', body["entry"][0]["response"]["etag"])
        _, _, history = self.request("GET", "/fhir/Patient/p-15/_history")
        self.assertEqual([1, 2, 3], [item["version"] for item in history["entries"]])

    def test_delete_of_missing_id_is_not_found(self):
        status, headers, body = self.post_bundle(transaction_bundle(txn_entry("DELETE", "Patient/p-404")))
        self.assert_outcome(status, headers, body, 404, "not-found")

    # ---------------------------------------------------------------- idempotency

    def test_same_key_replays_the_first_response_without_duplicates(self):
        self.request("POST", "/Subscription", {"id": "sub-r", "criteria": {"type": "Patient"}}, {"Idempotency-Key": "sub-r"})
        bundle = transaction_bundle(txn_entry("POST", "Patient", patient("p-16")))
        first = self.post_bundle(bundle, key="txn-replay")
        replay = self.post_bundle(bundle, key="txn-replay")
        self.assertEqual(200, first[0])
        self.assertEqual(first, replay)
        _, _, history = self.request("GET", "/fhir/Patient/p-16/_history")
        self.assertEqual([1], [item["version"] for item in history["entries"]])
        _, _, events = self.request("GET", "/subscriptions/sub-r/events")
        self.assertEqual(1, events["total"])

    def test_same_key_with_a_different_bundle_conflicts(self):
        self.post_bundle(transaction_bundle(txn_entry("POST", "Patient", patient("p-18"))), key="txn-shared")
        status, headers, body = self.post_bundle(
            transaction_bundle(txn_entry("POST", "Patient", patient("p-19"))), key="txn-shared"
        )
        self.assert_outcome(status, headers, body, 409, "conflict")
        self.assertEqual(404, self.request("GET", "/fhir/Patient/p-19")[0])

    def test_key_reused_from_another_operation_conflicts(self):
        self.request("POST", "/fhir/Patient", patient("p-21"), {"Idempotency-Key": "plain-create"})
        status, headers, body = self.post_bundle(
            transaction_bundle(txn_entry("POST", "Patient", patient("p-22"))), key="plain-create"
        )
        self.assert_outcome(status, headers, body, 409, "conflict")


class WebhookReceiver:
    """A loopback HTTP server that captures POSTs and answers with a scripted
    sequence of status codes (defaulting to 200 once the script runs out)."""

    def __init__(self, statuses: list[int] | None = None, port: int = 0):
        self.statuses = list(statuses or [])
        self.requests: list[tuple[dict, bytes]] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                # Keep the email.Message so header lookups stay case-insensitive.
                receiver.requests.append((self.headers, body))
                status = receiver.statuses.pop(0) if receiver.statuses else 200
                payload = b"{}"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class WebhookDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = str(Path(self.directory.name) / "vault.db")
        self.service = FhirVault(self.database, FrozenClock())
        self.receiver = WebhookReceiver()

    def tearDown(self):
        self.service.close()
        self.receiver.close()
        self.directory.cleanup()

    def subscribe(self, subscription_id: str = "sub-w", **channel) -> dict:
        raw: dict[str, Any] = {"id": subscription_id, "criteria": {"type": "Patient"}}
        if channel:
            raw["channel"] = channel
        return self.service.create_subscription(raw, f"key-{subscription_id}")

    def delivery_states(self, subscription_id: str = "sub-w") -> list[str]:
        return [d["state"] for d in self.service.deliveries(subscription_id)["deliveries"]]

    # ---------------------------------------------------------------- channel validation

    def test_invalid_channels_are_rejected_without_writing(self):
        invalid = [
            {"endpoint": "ftp://example.com/hook"},
            {"endpoint": "http://example.com/hook?x=1"},
            {"endpoint": "http://example.com/hook#frag"},
            {"endpoint": "http://user@example.com/hook"},
            {"endpoint": "http://user:pass@example.com/hook"},
            {"endpoint": "example.com/hook"},
            {"endpoint": "/relative"},
            {"endpoint": ""},
            {"endpoint": 42},
            {},
            {"endpoint": self.receiver.endpoint, "secret": ""},
            {"endpoint": self.receiver.endpoint, "secret": 7},
            {"endpoint": self.receiver.endpoint, "extra": "x"},
        ]
        for index, channel in enumerate(invalid):
            with self.subTest(index=index):
                with self.assertRaises(ValidationError):
                    self.service.create_subscription(
                        {"id": f"sub-bad-{index}", "criteria": {"type": "Patient"}, "channel": channel},
                        f"key-bad-{index}",
                    )
                with self.assertRaises(NotFoundError):
                    self.service.events(f"sub-bad-{index}")

    def test_channel_must_be_an_object(self):
        with self.assertRaises(ValidationError):
            self.service.create_subscription(
                {"id": "sub-bad-channel", "criteria": {"type": "Patient"}, "channel": "http://x"}, "kbc"
            )
        with self.assertRaises(NotFoundError):
            self.service.events("sub-bad-channel")

    def test_valid_channel_is_accepted_and_echoed(self):
        created = self.subscribe("sub-ok", endpoint=self.receiver.endpoint, secret="s3cr3t")
        self.assertEqual(
            {"endpoint": self.receiver.endpoint, "secret": "s3cr3t"}, created["channel"]
        )
        bare = self.subscribe("sub-bare", endpoint="https://example.com:8443/hook/path")
        self.assertEqual({"endpoint": "https://example.com:8443/hook/path"}, bare["channel"])
        no_channel = self.service.create_subscription(
            {"id": "sub-none", "criteria": {"type": "Patient"}}, "key-sub-none"
        )
        self.assertNotIn("channel", no_channel)

    # ---------------------------------------------------------------- successful delivery

    def test_matching_write_posts_the_event_with_signature(self):
        self.subscribe("sub-w", endpoint=self.receiver.endpoint, secret="s3cr3t")
        self.service.create("Patient", patient("p-9"), "kw1")
        self.assertTrue(wait_for(lambda: self.delivery_states() == ["delivered"]))

        self.assertEqual(1, len(self.receiver.requests))
        headers, body = self.receiver.requests[0]
        self.assertEqual("sub-w", headers.get("X-Fhirvault-Subscription"))
        self.assertEqual("1", headers.get("X-Fhirvault-Sequence"))
        delivery_id = headers.get("X-Fhirvault-Delivery")
        self.assertTrue(delivery_id)
        self.assertEqual(
            hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest(),
            headers.get("X-Fhirvault-Signature"),
        )
        event = self.service.events("sub-w")["events"][0]
        self.assertEqual(event, json.loads(body))

        result = self.service.deliveries("sub-w")
        self.assertEqual("sub-w", result["subscription_id"])
        self.assertEqual(1, result["total"])
        (delivery,) = result["deliveries"]
        self.assertEqual(1, delivery["sequence"])
        self.assertEqual(delivery_id, delivery["deliveryId"])
        self.assertEqual("delivered", delivery["state"])
        self.assertEqual(1, len(delivery["attempts"]))
        attempt = delivery["attempts"][0]
        self.assertEqual(1, attempt["attempt"])
        self.assertEqual("success", attempt["outcome"])
        self.assertEqual(200, attempt["httpStatus"])
        self.assertIsNone(attempt["error"])
        self.assertIn("attemptedAt", attempt)

    def test_delivery_without_secret_omits_signature(self):
        self.subscribe("sub-w", endpoint=self.receiver.endpoint)
        self.service.create("Patient", patient("p-9"), "kw2")
        self.assertTrue(wait_for(lambda: self.delivery_states() == ["delivered"]))
        headers, _ = self.receiver.requests[0]
        self.assertNotIn("X-Fhirvault-Signature", headers)

    def test_subscription_without_channel_only_records_events(self):
        self.subscribe("sub-plain")
        self.service.create("Patient", patient("p-9"), "kw3")
        self.assertEqual(1, self.service.events("sub-plain")["total"])
        result = self.service.deliveries("sub-plain")
        self.assertEqual(0, result["total"])
        self.assertEqual([], result["deliveries"])

    def test_querying_deliveries_does_not_trigger_delivery(self):
        self.subscribe("sub-w", endpoint=self.receiver.endpoint)
        self.assertEqual([], self.service.deliveries("sub-w")["deliveries"])
        self.assertEqual([], self.receiver.requests)

    def test_idempotent_replay_creates_no_extra_task(self):
        self.subscribe("sub-w", endpoint=self.receiver.endpoint)
        first = self.service.create("Patient", patient("p-9"), "kw4")
        replay = self.service.create("Patient", patient("p-9"), "kw4")
        self.assertEqual(first, replay)
        self.assertTrue(wait_for(lambda: self.delivery_states() == ["delivered"]))
        self.assertEqual(1, self.service.deliveries("sub-w")["total"])
        self.assertEqual(1, self.service.events("sub-w")["total"])

    # ---------------------------------------------------------------- retries

    def test_failures_are_retried_then_delivered(self):
        self.receiver.statuses = [500, 500]
        self.subscribe("sub-w", endpoint=self.receiver.endpoint)
        started = time.monotonic()
        self.service.create("Patient", patient("p-9"), "kw5")
        self.assertTrue(wait_for(lambda: self.delivery_states() == ["delivered"]))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 2.5)  # 1s + 2s backoff, minus slack
        self.assertEqual(3, len(self.receiver.requests))
        # Redeliveries reuse the delivery id, sequence, and signature headers.
        first_headers = self.receiver.requests[0][0]
        for headers, _ in self.receiver.requests[1:]:
            self.assertEqual(first_headers.get("X-Fhirvault-Delivery"), headers.get("X-Fhirvault-Delivery"))
            self.assertEqual("1", headers.get("X-Fhirvault-Sequence"))
        (delivery,) = self.service.deliveries("sub-w")["deliveries"]
        self.assertEqual("delivered", delivery["state"])
        self.assertEqual([1, 2, 3], [a["attempt"] for a in delivery["attempts"]])
        self.assertEqual(["failure", "failure", "success"], [a["outcome"] for a in delivery["attempts"]])
        self.assertEqual([500, 500, 200], [a["httpStatus"] for a in delivery["attempts"]])
        self.assertIsNotNone(delivery["attempts"][0]["error"])
        self.assertIsNone(delivery["attempts"][2]["error"])

    def test_three_failures_mark_the_delivery_failed(self):
        self.receiver.statuses = [500, 500, 500, 500]
        self.subscribe("sub-w", endpoint=self.receiver.endpoint)
        self.service.create("Patient", patient("p-9"), "kw6")
        self.assertTrue(wait_for(lambda: self.delivery_states() == ["failed"]))
        self.assertEqual(3, len(self.receiver.requests))
        (delivery,) = self.service.deliveries("sub-w")["deliveries"]
        self.assertEqual("failed", delivery["state"])
        self.assertEqual(3, len(delivery["attempts"]))
        self.assertEqual(["failure"] * 3, [a["outcome"] for a in delivery["attempts"]])

    def test_transport_error_records_null_http_status(self):
        # Nothing listens on this port, so every attempt is a transport error.
        sink = WebhookReceiver()
        dead_port = sink.port
        sink.close()
        self.subscribe("sub-w", endpoint=f"http://127.0.0.1:{dead_port}/hook")
        self.service.create("Patient", patient("p-9"), "kw7")
        self.assertTrue(wait_for(lambda: self.delivery_states() == ["failed"]))
        (delivery,) = self.service.deliveries("sub-w")["deliveries"]
        self.assertEqual(3, len(delivery["attempts"]))
        for attempt in delivery["attempts"]:
            self.assertEqual("failure", attempt["outcome"])
            self.assertIsNone(attempt["httpStatus"])
            self.assertTrue(attempt["error"])

    # ---------------------------------------------------------------- restart

    def test_restart_resumes_pending_deliveries(self):
        sink = WebhookReceiver()
        dead_port = sink.port
        sink.close()
        endpoint = f"http://127.0.0.1:{dead_port}/hook"
        self.subscribe("sub-w", endpoint=endpoint, secret="topsecret")
        self.service.create("Patient", patient("p-9"), "kw8")
        # Stop the first instance while the delivery is still pending, then
        # reopen the same database: the new worker must resume the task.
        self.service.close()
        self.receiver.close()
        self.receiver = WebhookReceiver(port=dead_port)
        resumed = FhirVault(self.database, FrozenClock())
        try:
            self.assertTrue(
                wait_for(lambda: [d["state"] for d in resumed.deliveries("sub-w")["deliveries"]] == ["delivered"])
            )
        finally:
            resumed.close()
        self.assertTrue(self.receiver.requests)
        headers, body = self.receiver.requests[0]
        self.assertEqual("sub-w", headers.get("X-Fhirvault-Subscription"))
        self.assertEqual(
            hmac.new(b"topsecret", body, hashlib.sha256).hexdigest(),
            headers.get("X-Fhirvault-Signature"),
        )

    # ---------------------------------------------------------------- http surface

    def test_http_deliveries_endpoint(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, _, body = http_request(port, "GET", "/subscriptions/sub-404/deliveries")
            self.assertEqual(404, status)
            self.assertEqual("not_found", body["error"]["code"])

            status, _, _ = http_request(
                port, "POST", "/Subscription",
                {"id": "sub-http-wh", "criteria": {"type": "Patient"},
                 "channel": {"endpoint": self.receiver.endpoint}},
                {"Idempotency-Key": "wh-sub"},
            )
            self.assertEqual(201, status)
            status, _, _ = http_request(
                port, "POST", "/fhir/Patient", patient("p-9"), {"Idempotency-Key": "wh-patient"}
            )
            self.assertEqual(201, status)
            self.assertTrue(wait_for(lambda: self.delivery_states("sub-http-wh") == ["delivered"]))

            status, _, body = http_request(port, "GET", "/subscriptions/sub-http-wh/deliveries")
            self.assertEqual(200, status)
            self.assertEqual("sub-http-wh", body["subscription_id"])
            self.assertEqual(1, body["total"])
            self.assertEqual("delivered", body["deliveries"][0]["state"])

            status, _, body = http_request(
                port, "POST", "/Subscription",
                {"id": "sub-http-bad", "criteria": {"type": "Patient"},
                 "channel": {"endpoint": "http://example.com/hook?x=1"}},
                {"Idempotency-Key": "wh-bad"},
            )
            self.assertEqual(400, status)
            self.assertEqual("validation_error", body["error"]["code"])
            status, _, _ = http_request(port, "GET", "/subscriptions/sub-http-bad/events")
            self.assertEqual(404, status)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class AuditHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = str(Path(self.directory.name) / "vault.db")
        self.service = FhirVault(self.database, FrozenClock())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.service.close()
        self.directory.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict, dict]:
        return http_request(self.port, method, path, payload, headers)

    def audit(self, query: str = "") -> dict:
        status, _, body = self.request("GET", f"/audit{query}")
        self.assertEqual(200, status)
        return body

    # ---------------------------------------------------------------- recording

    def test_completed_requests_are_audited(self):
        self.assertEqual(200, self.request("GET", "/health")[0])
        self.assertEqual(0, self.audit()["total"])  # /health and /audit are not audited

        self.assertEqual(201, self.request(
            "POST", "/fhir/Patient", patient("p-1"), {"Idempotency-Key": "a1", "X-FhirVault-Actor": "dr-1"}
        )[0])
        self.assertEqual(200, self.request("GET", "/fhir/Patient/p-1")[0])
        self.assertEqual(200, self.request("GET", "/fhir/Patient?gender=male", None, {"X-FhirVault-Actor": "   "})[0])
        self.assertEqual(200, self.request(
            "PUT", "/fhir/Patient/p-1", patient("p-1", gender="female"), {"Idempotency-Key": "a2"}
        )[0])
        self.assertEqual(200, self.request("GET", "/fhir/Patient/p-1/_history")[0])
        self.assertEqual(200, self.request("DELETE", "/fhir/Patient/p-1", None, {"Idempotency-Key": "a3"})[0])

        result = self.audit()
        self.assertEqual(6, result["total"])
        self.assertEqual(6, result["count"])
        self.assertEqual(0, result["offset"])
        self.assertEqual("sequence", result["sort"])
        entries = result["entry"]
        self.assertEqual([1, 2, 3, 4, 5, 6], [entry["sequence"] for entry in entries])
        self.assertEqual(
            ["create", "read", "search", "update", "history", "delete"],
            [entry["action"] for entry in entries],
        )
        self.assertEqual(
            ["dr-1", "anonymous", "anonymous", "anonymous", "anonymous", "anonymous"],
            [entry["actor"] for entry in entries],
        )
        self.assertEqual(["success"] * 6, [entry["outcome"] for entry in entries])
        self.assertEqual([201, 200, 200, 200, 200, 200], [entry["status"] for entry in entries])
        self.assertEqual([1, 1, None, 2, None, 3], [entry["version"] for entry in entries])
        self.assertEqual([False] * 6, [entry["replayed"] for entry in entries])
        self.assertEqual([[]] * 6, [entry["changes"] for entry in entries])
        self.assertEqual("Patient", entries[0]["resourceType"])
        self.assertEqual("p-1", entries[0]["resourceId"])
        self.assertIsNone(entries[2]["resourceId"])
        for entry in entries:
            self.assertTrue(entry["occurredAt"])

    def test_failed_requests_are_audited_after_rollback(self):
        self.assertEqual(400, self.request("POST", "/fhir/Patient", patient("p-1", telecom="x"), {"Idempotency-Key": "b1"})[0])
        self.assertEqual(404, self.request("GET", "/fhir/Patient/p-404")[0])
        self.assertEqual(400, self.request("POST", "/fhir/Patient", patient("p-2"))[0])  # missing Idempotency-Key
        self.assertEqual(404, self.request("GET", "/nope")[0])  # not an audited entry point

        entries = self.audit()["entry"]
        self.assertEqual(3, len(entries))
        self.assertEqual(["create", "read", "create"], [entry["action"] for entry in entries])
        self.assertEqual(["failure"] * 3, [entry["outcome"] for entry in entries])
        self.assertEqual([400, 404, 400], [entry["status"] for entry in entries])
        self.assertEqual([None, None, None], [entry["version"] for entry in entries])
        self.assertEqual("p-404", entries[1]["resourceId"])
        # the failed writes left no resource behind
        self.assertEqual(404, self.request("GET", "/fhir/Patient/p-1")[0])
        self.assertEqual(404, self.request("GET", "/fhir/Patient/p-2")[0])

    def test_transaction_produces_one_event_with_ordered_changes(self):
        status, _, _ = self.request(
            "POST", "/fhir",
            transaction_bundle(
                txn_entry("POST", "Patient", patient("p-1")),
                txn_entry("PUT", "Patient/p-1", patient("p-1", gender="female")),
                txn_entry("DELETE", "Patient/p-1"),
            ),
            {"Idempotency-Key": "txn-1", "X-FhirVault-Actor": "batch-bot"},
        )
        self.assertEqual(200, status)
        entries = self.audit()["entry"]
        self.assertEqual(1, len(entries))
        (event,) = entries
        self.assertEqual("transaction", event["action"])
        self.assertEqual("success", event["outcome"])
        self.assertEqual(200, event["status"])
        self.assertEqual("batch-bot", event["actor"])
        self.assertIsNone(event["resourceType"])
        self.assertIsNone(event["resourceId"])
        self.assertIsNone(event["version"])
        self.assertFalse(event["replayed"])
        self.assertEqual(
            [
                {"method": "POST", "resourceType": "Patient", "id": "p-1", "version": 1, "deleted": False},
                {"method": "PUT", "resourceType": "Patient", "id": "p-1", "version": 2, "deleted": False},
                {"method": "DELETE", "resourceType": "Patient", "id": "p-1", "version": 3, "deleted": True},
            ],
            event["changes"],
        )

    def test_failed_transaction_records_failure_with_empty_changes(self):
        status, _, _ = self.request(
            "POST", "/fhir",
            transaction_bundle(
                txn_entry("POST", "Patient", patient("p-1")),
                txn_entry("POST", "Observation", observation("o-1", subject="Patient/p-404")),
            ),
            {"Idempotency-Key": "txn-bad"},
        )
        self.assertEqual(400, status)
        entries = self.audit()["entry"]
        self.assertEqual(1, len(entries))
        event = entries[0]
        self.assertEqual("transaction", event["action"])
        self.assertEqual("failure", event["outcome"])
        self.assertEqual(400, event["status"])
        self.assertEqual([], event["changes"])
        # rolled back: the first entry's write did not happen
        self.assertEqual(404, self.request("GET", "/fhir/Patient/p-1")[0])

    def test_idempotent_replay_adds_only_a_replayed_event(self):
        self.request("POST", "/Subscription", {"id": "sub-1", "criteria": {"type": "Patient"}}, {"Idempotency-Key": "s1"})
        first = self.request("POST", "/fhir/Patient", patient("p-1"), {"Idempotency-Key": "idem-1"})
        replay = self.request("POST", "/fhir/Patient", patient("p-1"), {"Idempotency-Key": "idem-1"})
        self.assertEqual(201, first[0])
        self.assertEqual(first, replay)

        entries = self.audit()["entry"]
        self.assertEqual(3, len(entries))
        self.assertEqual(["create-subscription", "create", "create"], [entry["action"] for entry in entries])
        self.assertEqual([False, False, True], [entry["replayed"] for entry in entries])
        replayed = entries[2]
        self.assertEqual(1, replayed["version"])
        self.assertEqual("p-1", replayed["resourceId"])
        self.assertEqual([], replayed["changes"])
        # the replay created no new version, subscription event, or delivery task
        _, _, history = self.request("GET", "/fhir/Patient/p-1/_history")
        self.assertEqual([1], [entry["version"] for entry in history["entries"]])
        _, _, events = self.request("GET", "/subscriptions/sub-1/events")
        self.assertEqual(1, events["total"])
        _, _, deliveries = self.request("GET", "/subscriptions/sub-1/deliveries")
        self.assertEqual(0, deliveries["total"])

    def test_transaction_replay_records_replayed_event_without_changes(self):
        bundle = transaction_bundle(txn_entry("POST", "Patient", patient("p-1")))
        first = self.request("POST", "/fhir", bundle, {"Idempotency-Key": "txn-replay"})
        replay = self.request("POST", "/fhir", bundle, {"Idempotency-Key": "txn-replay"})
        self.assertEqual(first, replay)
        entries = self.audit()["entry"]
        self.assertEqual(2, len(entries))
        self.assertEqual([False, True], [entry["replayed"] for entry in entries])
        self.assertEqual(1, len(entries[0]["changes"]))
        self.assertEqual([], entries[1]["changes"])
        _, _, history = self.request("GET", "/fhir/Patient/p-1/_history")
        self.assertEqual([1], [entry["version"] for entry in history["entries"]])

    def test_subscription_endpoints_are_audited(self):
        self.request("POST", "/Subscription", {"id": "sub-1", "criteria": {"type": "Patient"}}, {"Idempotency-Key": "s1"})
        self.request("POST", "/fhir/Subscription", {"id": "sub-2", "criteria": {"type": "Patient"}}, {"Idempotency-Key": "s2"})
        self.request("POST", "/fhir/Patient", patient("p-1"), {"Idempotency-Key": "p1"})
        self.assertEqual(200, self.request("GET", "/subscriptions/sub-1/events")[0])
        self.assertEqual(200, self.request("GET", "/subscriptions/sub-1/deliveries")[0])
        entries = self.audit()["entry"]
        self.assertEqual(
            ["create-subscription", "create-subscription", "create", "events", "deliveries"],
            [entry["action"] for entry in entries],
        )
        self.assertEqual("Subscription", entries[0]["resourceType"])
        self.assertEqual("sub-1", entries[0]["resourceId"])
        self.assertEqual("sub-2", entries[1]["resourceId"])
        self.assertEqual("sub-1", entries[3]["resourceId"])
        self.assertEqual("sub-1", entries[4]["resourceId"])

    def test_audit_records_exclude_bodies_keys_and_secrets(self):
        self.request(
            "POST", "/Subscription",
            {"id": "sub-1", "criteria": {"type": "Patient"},
             "channel": {"endpoint": "http://127.0.0.1:9/hook", "secret": "s3cr3t"}},
            {"Idempotency-Key": "secret-key-1"},
        )
        self.request("POST", "/fhir/Patient", patient("p-1"), {"Idempotency-Key": "secret-key-2"})
        self.request("GET", "/fhir/Patient?name.family=Smith&_count=5")
        serialized = json.dumps(self.audit())
        self.assertNotIn("s3cr3t", serialized)
        self.assertNotIn("secret-key-1", serialized)
        self.assertNotIn("secret-key-2", serialized)
        self.assertNotIn("Smith", serialized)
        self.assertNotIn("name.family", serialized)

    # ---------------------------------------------------------------- querying

    def test_audit_filters_paging_and_sort(self):
        self.request("POST", "/fhir/Patient", patient("p-1"), {"Idempotency-Key": "f1", "X-FhirVault-Actor": "alice"})
        self.request("POST", "/fhir/Patient", patient("p-2"), {"Idempotency-Key": "f2", "X-FhirVault-Actor": "bob"})
        self.request("GET", "/fhir/Patient/p-1", None, {"X-FhirVault-Actor": "alice"})
        self.request("GET", "/fhir/Patient/p-404")

        result = self.audit("?actor=alice")
        self.assertEqual(2, result["total"])
        self.assertEqual(["create", "read"], [entry["action"] for entry in result["entry"]])

        result = self.audit("?outcome=failure")
        self.assertEqual(1, result["total"])
        self.assertEqual(404, result["entry"][0]["status"])

        result = self.audit("?action=create&resourceType=Patient")
        self.assertEqual(2, result["total"])

        result = self.audit("?resourceId=p-2")
        self.assertEqual(1, result["total"])
        self.assertEqual("p-2", result["entry"][0]["resourceId"])

        result = self.audit("?action=bogus")
        self.assertEqual(0, result["total"])

        result = self.audit("?_sort=-sequence&_count=2")
        self.assertEqual("-sequence", result["sort"])
        self.assertEqual(4, result["total"])
        self.assertEqual(2, result["count"])
        self.assertEqual([4, 3], [entry["sequence"] for entry in result["entry"]])

        result = self.audit("?_count=1&_offset=2")
        self.assertEqual(2, result["offset"])
        self.assertEqual([3], [entry["sequence"] for entry in result["entry"]])

    def test_audit_rejects_invalid_parameters_without_partial_results(self):
        self.request("POST", "/fhir/Patient", patient("p-1"), {"Idempotency-Key": "g1"})
        for query in (
            "?bogus=1", "?_count=abc", "?_count=1001", "?_count=-1", "?_offset=-1",
            "?_offset=x", "?_sort=bogus", "?_sort=_id", "?version=1",
        ):
            with self.subTest(query=query):
                status, _, body = self.request("GET", f"/audit{query}")
                self.assertEqual(400, status)
                self.assertEqual("validation_error", body["error"]["code"])
        # failed /audit queries are not themselves audited
        self.assertEqual(1, self.audit()["total"])

    # ---------------------------------------------------------------- restart

    def test_audit_survives_restart_and_never_reuses_sequence(self):
        self.request("POST", "/fhir/Patient", patient("p-1"), {"Idempotency-Key": "r1"})
        self.assertEqual([1], [entry["sequence"] for entry in self.audit()["entry"]])

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.service.close()
        self.service = FhirVault(self.database, FrozenClock())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self.assertEqual([1], [entry["sequence"] for entry in self.audit()["entry"]])
        self.request("POST", "/fhir/Patient", patient("p-2"), {"Idempotency-Key": "r2"})
        entries = self.audit()["entry"]
        self.assertEqual([1, 2], [entry["sequence"] for entry in entries])
        self.assertEqual("p-2", entries[1]["resourceId"])


class AuditServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = FhirVault(str(Path(self.directory.name) / "vault.db"), FrozenClock())

    def tearDown(self):
        self.service.close()
        self.directory.cleanup()

    def test_successful_write_and_audit_commit_atomically(self):
        context = AuditContext("dr-1", "create", 201, "Patient")
        self.service.create("Patient", patient("p-1"), "k1", audit=context)
        self.assertTrue(context.recorded)
        entries = self.service.audit_events({})["entry"]
        self.assertEqual(1, len(entries))
        self.assertEqual(1, entries[0]["version"])
        self.assertEqual("p-1", entries[0]["resourceId"])
        self.assertEqual("1", self.service.read("Patient", "p-1")["meta"]["versionId"])

    def test_failed_write_records_nothing_until_the_failure_is_reported(self):
        context = AuditContext("dr-1", "create", 201, "Patient")
        with self.assertRaises(ValidationError):
            self.service.create("Patient", patient("p-1", telecom="x"), "k2", audit=context)
        self.assertFalse(context.recorded)
        self.assertEqual(0, self.service.audit_events({})["total"])
        self.service.record_audit_failure(context, 400)
        entries = self.service.audit_events({})["entry"]
        self.assertEqual(1, len(entries))
        self.assertEqual("failure", entries[0]["outcome"])
        self.assertEqual(400, entries[0]["status"])
        # reporting the same request twice is not possible
        self.service.record_audit_failure(context, 500)
        self.assertEqual(1, self.service.audit_events({})["total"])

    def test_calls_without_an_audit_context_record_nothing(self):
        self.service.create("Patient", patient("p-1"), "k3")
        self.service.read("Patient", "p-1")
        self.service.search("Patient", {})
        self.assertEqual(0, self.service.audit_events({})["total"])


if __name__ == "__main__":
    unittest.main()