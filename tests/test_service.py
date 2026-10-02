import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

# Talk to the loopback server directly, never through a proxy from the environment.
_OPENER = build_opener(ProxyHandler({}))

from fhirvault.errors import ConflictError, NotFoundError, OperationOutcomeError, ValidationError
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

    # ---------------------------------------------------------------- include / revinclude

    def _setup_include_graph(self):
        self.service.create(
            "Patient",
            patient("p-2", gender="female", **{"name.family": "Jones", "identifier": [{"system": "mrn", "value": "B456"}]}),
            "i1",
        )
        self.service.create("Observation", observation("o-1", subject="Patient/p-1"), "i2")
        self.service.create("Observation", observation("o-2", subject="Patient/identifier|mrn|A123", value=1.0), "i3")
        self.service.create("Observation", observation("o-3", subject="Patient/p-2"), "i4")
        o4 = observation("o-4", status="preliminary")
        del o4["subject"]
        self.service.create("Observation", o4, "i5")
        self.service.create("Encounter", encounter("e-1", subject="Patient/p-1"), "i6")

    def test_forward_include_returns_referenced_live_resources(self):
        self._setup_include_graph()
        found = self.service.search("Observation", {"_include": ["Observation:subject"]})
        self.assertEqual(["o-1", "o-2", "o-3", "o-4"], [entry["resource"]["id"] for entry in found["entry"]])
        self.assertEqual(4, found["total"])
        self.assertEqual(4, found["count"])
        self.assertEqual(["p-1", "p-2"], [document["id"] for document in found["include"]])
        self.assertNotIn("revinclude", found)

    def test_forward_include_supports_identifier_references_and_dedup(self):
        self._setup_include_graph()
        found = self.service.search("Observation", {"id": ["o-2"], "_include": ["Observation:subject"]})
        self.assertEqual(["p-1"], [document["id"] for document in found["include"]])

    def test_reverse_include_returns_resources_pointing_at_the_page(self):
        self._setup_include_graph()
        found = self.service.search("Patient", {"_revinclude": ["Observation:subject", "Encounter:subject"]})
        self.assertEqual(["p-1", "p-2"], [entry["resource"]["id"] for entry in found["entry"]])
        self.assertEqual(
            [("Encounter", "e-1"), ("Observation", "o-1"), ("Observation", "o-2"), ("Observation", "o-3")],
            [(document["resourceType"], document["id"]) for document in found["revinclude"]],
        )
        self.assertNotIn("include", found)

    def test_include_is_evaluated_after_paging_and_not_counted(self):
        self._setup_include_graph()
        page = self.service.search("Observation", {"_sort": ["_id"], "_count": ["1"], "_offset": ["2"], "_include": ["Observation:subject"]})
        self.assertEqual(4, page["total"])
        self.assertEqual(1, page["count"])
        self.assertEqual(["o-3"], [entry["resource"]["id"] for entry in page["entry"]])
        self.assertEqual(["p-2"], [document["id"] for document in page["include"]])

    def test_include_and_revinclude_can_each_hold_the_same_resource(self):
        self._setup_include_graph()
        # Observations referencing observations via hasMember: o-10 points at o-1.
        self.service.create("Observation", observation("o-10", hasMember="Observation/o-1", **{"code.text": "Z"}), "i7")
        found = self.service.search("Observation", {"id": ["o-1"], "_include": ["Observation:hasMember"], "_revinclude": ["Observation:hasMember"]})
        self.assertEqual(["o-1"], [entry["resource"]["id"] for entry in found["entry"]])
        self.assertEqual([], found["include"])
        self.assertEqual(["o-10"], [document["id"] for document in found["revinclude"]])
        forward = self.service.search("Observation", {"id": ["o-10"], "_include": ["Observation:hasMember"]})
        self.assertEqual(["o-1"], [document["id"] for document in forward["include"]])

    def test_primary_entries_are_not_copied_into_expansion_arrays(self):
        self._setup_include_graph()
        self.service.create("Encounter", encounter("e-2", subject="Patient/p-1", partOf="Encounter/e-1"), "i8")
        found = self.service.search("Encounter", {"id": ["e-1"], "_revinclude": ["Encounter:partOf"]})
        self.assertEqual(["e-2"], [document["id"] for document in found["revinclude"]])
        self.service.create("Observation", observation("o-11", hasMember="Observation/o-1", **{"code.text": "W"}), "i9")
        forward = self.service.search("Observation", {"id": ["o-11"], "_include": ["Observation:hasMember"], "_revinclude": ["Observation:hasMember"]})
        self.assertEqual(["o-1"], [document["id"] for document in forward["include"]])
        self.assertEqual([], forward["revinclude"])

    def test_deleted_and_unresolvable_targets_are_skipped(self):
        self._setup_include_graph()
        self.service.delete("Patient", "p-2", "i10")
        found = self.service.search("Observation", {"id": ["o-3"], "_include": ["Observation:subject"]})
        self.assertEqual([], found["include"])
        self.service.delete("Observation", "o-1", "i11")
        reverse = self.service.search("Patient", {"id": ["p-1"], "_revinclude": ["Observation:subject"]})
        self.assertEqual(
            ["o-2"], [document["id"] for document in reverse["revinclude"]]
        )

    def test_empty_entry_yields_empty_expansion_arrays(self):
        self._setup_include_graph()
        found = self.service.search("Observation", {"id": ["missing"], "_include": ["Observation:subject"], "_revinclude": ["Observation:subject"]})
        self.assertEqual([], found["entry"])
        self.assertEqual([], found["include"])
        self.assertEqual([], found["revinclude"])

    def test_repeated_include_parameters_merge_and_dedup(self):
        self._setup_include_graph()
        found = self.service.search("Observation", {"_include": ["Observation:subject", "Observation:subject", "Observation:encounter"]})
        self.assertEqual(["p-1", "p-2"], [document["id"] for document in found["include"]])
        self.assertEqual(
            ["Observation:encounter", "Observation:subject", "Observation:subject"],
            found["parameters"]["_include"],
        )

    def test_search_without_include_parameters_keeps_its_shape(self):
        self._setup_include_graph()
        found = self.service.search("Observation", {})
        self.assertNotIn("include", found)
        self.assertNotIn("revinclude", found)

    def test_bad_include_parameters_are_validation_errors_without_partial_results(self):
        self._setup_include_graph()
        bad_values = [
            ("_include", "Patient:subject", "must equal the searched type"),
            ("_include", "Observation:gender", "not a reference field"),
            ("_include", "Observation:missing", "not a reference field"),
            ("_include", "Device:subject", "must be one of"),
            ("_include", "Observation", "<referenceField>"),
            ("_include", "", "<referenceField>"),
            ("_include", "Observation:subject:extra", "<referenceField>"),
            ("_revinclude", "Patient:gender", "not a reference field"),
            ("_revinclude", "Patient:subject", "no reference fields"),
            ("_revinclude", "Device:subject", "must be one of"),
        ]
        for name, value, message in bad_values:
            with self.assertRaisesRegex(ValidationError, message, msg=value):
                self.service.search("Observation", {name: [value]})
        # A good spec followed by a bad one must not return partial results.
        with self.assertRaises(ValidationError):
            self.service.search("Observation", {"_include": ["Observation:subject", "Observation:bad"]})

    def test_include_validation_runs_before_paging_validation(self):
        self._setup_include_graph()
        with self.assertRaisesRegex(ValidationError, "must equal the searched type"):
            self.service.search("Observation", {"_include": ["Patient:subject"], "_count": ["9999"]})

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
            status, body = self.request(
                "GET",
                "/fhir/Observation?_include=Observation:subject&_revinclude=Observation:subject",
            )
            self.assertEqual(200, status)
            self.assertEqual(["o-20"], [entry["resource"]["id"] for entry in body["entry"]])
            self.assertEqual(["p-20"], [document["id"] for document in body["include"]])
            self.assertEqual([], body["revinclude"])
            status, body = self.request("GET", "/fhir/Patient?_revinclude=Observation:subject")
            self.assertEqual(200, status)
            self.assertEqual(["o-20"], [document["id"] for document in body["revinclude"]])
            status, body = self.request("GET", "/fhir/Observation?_include=Patient:subject")
            self.assertEqual(400, status)
            self.assertEqual("validation_error", body["error"]["code"])
            status, body = self.request("GET", "/fhir/Observation?_include=Observation:nope")
            self.assertEqual(400, status)
            self.assertEqual("validation_error", body["error"]["code"])
            status, body = self.request("GET", "/fhir/Patient?_revinclude=Device:subject")
            self.assertEqual(400, status)
            self.assertEqual("validation_error", body["error"]["code"])
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


if __name__ == "__main__":
    unittest.main()
