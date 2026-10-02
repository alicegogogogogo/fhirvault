from __future__ import annotations

import functools
from typing import Any, Callable

from .errors import ConflictError, NotFoundError, OperationOutcomeError, ValidationError
from .model import (
    RESOURCE_TYPES,
    REFERENCE_FIELDS,
    ValidatedResource,
    criteria_match,
    fields_for,
    identifiers_of,
    parse_if_match,
    parse_resource,
    parse_subscription,
    resolve_reference,
    search_match,
    validate_path_id,
)
from .store import Store

_CONTROL_PARAMETERS = ("_count", "_offset", "_sort")
_EXPANSION_PARAMETERS = ("_include", "_revinclude")


def _serialized(method: Callable) -> Callable:
    """Run a public operation under the store lock so the shared SQLite
    connection is never used concurrently and writes stay deterministic."""

    @functools.wraps(method)
    def wrapper(self: "FhirVault", *args: Any, **kwargs: Any) -> Any:
        with self.store.lock:
            return method(self, *args, **kwargs)

    return wrapper


class FhirVault:
    def __init__(self, database: str, clock: Callable[[], Any] | None = None):
        self.store = Store(database, clock)

    # ------------------------------------------------------------------ helpers

    def _idempotent(self, key: str | None, operation: str, action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if not key:
            raise ValidationError("Idempotency-Key header is required")
        with self.store.transaction():
            existing = self.store.connection.execute(
                "SELECT operation, response FROM idempotency WHERE key = ?", (key,)
            ).fetchone()
            if existing:
                if existing["operation"] != operation:
                    raise ConflictError("idempotency key was already used for another operation")
                return self.store.decode(existing["response"])
            response = action()
            self.store.connection.execute(
                "INSERT INTO idempotency(key, operation, response) VALUES (?, ?, ?)",
                (key, operation, self.store.encode(response)),
            )
        return response

    def _require(self, resource_type: str, resource_id: str) -> Any:
        if resource_type not in RESOURCE_TYPES:
            raise NotFoundError(f"resource type {resource_type} is not supported")
        row = self.store.connection.execute(
            "SELECT document, current_version, deleted, last_updated FROM resources WHERE type = ? AND id = ?",
            (resource_type, resource_id),
        ).fetchone()
        if row is None or row["deleted"]:
            raise NotFoundError(f"{resource_type}/{resource_id} was not found")
        return row

    def _write(self, validated: ValidatedResource, *, reject_existing: bool, if_match: str | None = None) -> dict[str, Any]:
        row = self.store.connection.execute(
            "SELECT current_version, deleted FROM resources WHERE type = ? AND id = ?",
            (validated.resource_type, validated.resource_id),
        ).fetchone()
        live = row is not None and not row["deleted"]
        if if_match is not None:
            label = f"{validated.resource_type}/{validated.resource_id}"
            if not live:
                raise OperationOutcomeError(404, "not-found", f"{label} was not found")
            current = str(row["current_version"])
            if if_match != "*" and if_match != current:
                raise OperationOutcomeError(
                    412,
                    "conflict",
                    f'If-Match version "{if_match}" is not the current version "{current}" of {label}',
                )
        if live and reject_existing:
            raise ConflictError(f"{validated.resource_type}/{validated.resource_id} already exists")
        version = 1 if row is None else row["current_version"] + 1
        moment = self.store.now()
        document = self._compose(validated, version, moment)
        connection = self.store.connection
        connection.execute(
            "INSERT INTO resources(type, id, deleted, current_version, document, last_updated) "
            "VALUES (?, ?, 0, ?, ?, ?) "
            "ON CONFLICT(type, id) DO UPDATE SET deleted = 0, current_version = excluded.current_version, "
            "document = excluded.document, last_updated = excluded.last_updated",
            (validated.resource_type, validated.resource_id, version, self.store.encode(document), moment),
        )
        connection.execute(
            "INSERT INTO versions(type, id, version, document, recorded_at) VALUES (?, ?, ?, ?, ?)",
            (validated.resource_type, validated.resource_id, version, self.store.encode(document), moment),
        )
        self._record_events(validated.resource_type, validated.resource_id, "created" if version == 1 else "updated", version, document, moment)
        return document

    @staticmethod
    def _compose(validated: ValidatedResource, version: int, moment: str) -> dict[str, Any]:
        document: dict[str, Any] = {"resourceType": validated.resource_type}
        document.update(validated.document)
        document["id"] = validated.resource_id
        document["meta"] = {"versionId": str(version), "lastUpdated": moment}
        return document

    # ------------------------------------------------------------------ resources

    @_serialized
    def create(self, resource_type: str, raw: Any, key: str | None = None) -> dict[str, Any]:
        if resource_type not in RESOURCE_TYPES:
            raise NotFoundError(f"resource type {resource_type} is not supported")
        validated = parse_resource(resource_type, raw)
        self._assert_references(validated)
        return self._idempotent(
            key,
            f"create:{resource_type}/{validated.resource_id}",
            lambda: self._write(validated, reject_existing=True),
        )

    @_serialized
    def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        validate_path_id(resource_id)
        row = self._require(resource_type, resource_id)
        return self.store.decode(row["document"])

    @_serialized
    def update(
        self,
        resource_type: str,
        resource_id: str,
        raw: Any,
        key: str | None = None,
        if_match: str | None = None,
    ) -> dict[str, Any]:
        validate_path_id(resource_id)
        expected = parse_if_match(if_match)
        validated = parse_resource(resource_type, raw, expected_id=resource_id)
        self._assert_references(validated)
        return self._idempotent(
            key,
            f"update:{resource_type}/{resource_id}",
            lambda: self._write(validated, reject_existing=False, if_match=expected),
        )

    @_serialized
    def delete(self, resource_type: str, resource_id: str, key: str | None = None) -> dict[str, Any]:
        validate_path_id(resource_id)

        def apply() -> dict[str, Any]:
            row = self._require(resource_type, resource_id)
            version = row["current_version"] + 1
            moment = self.store.now()
            document = self.store.decode(row["document"])
            document["meta"] = {"versionId": str(version), "lastUpdated": moment}
            connection = self.store.connection
            connection.execute(
                "UPDATE resources SET deleted = 1, current_version = ?, document = ?, last_updated = ? "
                "WHERE type = ? AND id = ?",
                (version, self.store.encode(document), moment, resource_type, resource_id),
            )
            connection.execute(
                "INSERT INTO versions(type, id, version, document, recorded_at) VALUES (?, ?, ?, ?, ?)",
                (resource_type, resource_id, version, self.store.encode(document), moment),
            )
            self._record_events(resource_type, resource_id, "deleted", version, document, moment)
            return {"id": resource_id, "resourceType": resource_type, "deleted": True, "version": version, "lastUpdated": moment}

        return self._idempotent(key, f"delete:{resource_type}/{resource_id}", apply)

    @_serialized
    def history(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        validate_path_id(resource_id)
        if resource_type not in RESOURCE_TYPES:
            raise NotFoundError(f"resource type {resource_type} is not supported")
        head = self.store.connection.execute(
            "SELECT current_version, deleted FROM resources WHERE type = ? AND id = ?",
            (resource_type, resource_id),
        ).fetchone()
        if head is None:
            raise NotFoundError(f"{resource_type}/{resource_id} was not found")
        rows = self.store.connection.execute(
            "SELECT version, document, recorded_at FROM versions WHERE type = ? AND id = ? ORDER BY version",
            (resource_type, resource_id),
        ).fetchall()
        entries = []
        for row in rows:
            document = self.store.decode(row["document"])
            entries.append(
                {
                    "version": row["version"],
                    "recordedAt": row["recorded_at"],
                    "current": bool(row["version"] == head["current_version"] and not head["deleted"]),
                    "resource": document if not head["deleted"] or row["version"] != head["current_version"] else None,
                    "tombstone": bool(head["deleted"] and row["version"] == head["current_version"]),
                    "meta": {"versionId": str(row["version"]), "lastUpdated": row["recorded_at"]},
                }
            )
        return {"resourceType": resource_type, "id": resource_id, "deleted": bool(head["deleted"]), "entries": entries}

    @_serialized
    def search(self, resource_type: str, parameters: dict[str, list[str]]) -> dict[str, Any]:
        if resource_type not in RESOURCE_TYPES:
            raise NotFoundError(f"resource type {resource_type} is not supported")
        unknown = set(parameters) - set(fields_for(resource_type)) - set(_CONTROL_PARAMETERS) - set(_EXPANSION_PARAMETERS) - {"id"}
        if unknown:
            raise ValidationError(
                f"unknown search parameters: {', '.join(sorted(unknown))}; supported parameters are "
                f"{', '.join(sorted(set(fields_for(resource_type)) | {'id'} | set(_CONTROL_PARAMETERS) | set(_EXPANSION_PARAMETERS)))}"
            )
        includes = [
            self._parse_include(value, resource_type, reverse=False)
            for value in parameters.get("_include", [])
        ]
        revincludes = [
            self._parse_include(value, resource_type, reverse=True)
            for value in parameters.get("_revinclude", [])
        ]
        count, offset = self._paging(parameters)
        sort = parameters.get("_sort", ["_id"])[0]
        rows = self.store.connection.execute(
            "SELECT id, document FROM resources WHERE type = ? AND deleted = 0",
            (resource_type,),
        ).fetchall()
        matched: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            document = self.store.decode(row["document"])
            if "id" in parameters and row["id"] not in parameters["id"]:
                continue
            identifiers = identifiers_of(document)
            if not all(
                any(
                    search_match(resource_type, document, identifiers, field, value)
                    for value in values
                )
                for field, values in parameters.items()
                if field not in _CONTROL_PARAMETERS and field not in _EXPANSION_PARAMETERS and field != "id"
            ):
                continue
            matched.append((row["id"], document))
        matched.sort(key=lambda item: item[0], reverse=sort == "-_id")
        total = len(matched)
        page = matched[offset:] if count is None else matched[offset : offset + count]
        page_documents = [document for _, document in page]
        result: dict[str, Any] = {
            "resourceType": resource_type,
            "total": total,
            "count": len(page),
            "offset": offset,
            "sort": sort,
            "parameters": {name: sorted(values) for name, values in sorted(parameters.items())},
            "entry": [{"resource": document} for document in page_documents],
        }
        if includes or revincludes:
            included, revincluded = self._expand(resource_type, page_documents, includes, revincludes)
            result["include"] = included
            result["revinclude"] = revincluded
        return result

    @staticmethod
    def _parse_include(value: str, primary_type: str, *, reverse: bool) -> tuple[str, str]:
        """Validate an ``_include``/``_revinclude`` value of ``type:field``.

        Forward includes name the primary search type; reverse includes name the
        type that points back at the primary results. In both cases the field
        must be a reference field of the named type.
        """
        label = "_revinclude" if reverse else "_include"
        parts = value.split(":")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValidationError(f"{label} must have the form <resourceType>:<referenceField>")
        named_type, field = parts
        if named_type not in RESOURCE_TYPES:
            raise ValidationError(
                f"{label} resource type {named_type} is not supported; supported types are "
                f"{', '.join(RESOURCE_TYPES)}"
            )
        if not reverse and named_type != primary_type:
            raise ValidationError(f"_include resource type must be {primary_type}, not {named_type}")
        if field not in REFERENCE_FIELDS[named_type]:
            supported = ", ".join(REFERENCE_FIELDS[named_type]) or "none"
            raise ValidationError(
                f"{label} field {field} is not a reference field of {named_type}; "
                f"reference fields are: {supported}"
            )
        return named_type, field

    def _expand(
        self,
        primary_type: str,
        page: list[dict[str, Any]],
        includes: list[tuple[str, str]],
        revincludes: list[tuple[str, str]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Resolve one hop of forward and reverse includes over one page."""
        connection = self.store.connection
        primary_ids = {document["id"] for document in page}
        primary_keys = {(primary_type, document["id"]) for document in page}

        def load(target_type: str, target_id: str) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT document FROM resources WHERE type = ? AND id = ? AND deleted = 0",
                (target_type, target_id),
            ).fetchone()
            return self.store.decode(row["document"]) if row is not None else None

        forward: dict[tuple[str, str], dict[str, Any]] = {}
        for _, field in includes:
            for document in page:
                stored = document.get(field)
                reference = stored.get("reference") if isinstance(stored, dict) else None
                if not isinstance(reference, str):
                    continue
                target_id = self._resolve_target(connection, primary_type, field, reference)
                if target_id is None:
                    continue
                key = (reference.partition("/")[0], target_id)
                if key in primary_keys or key in forward:
                    continue
                target = load(*key)
                if target is not None:
                    forward[key] = target

        reverse: dict[tuple[str, str], dict[str, Any]] = {}
        if revincludes and primary_ids:
            for referencing_type, field in revincludes:
                rows = connection.execute(
                    "SELECT id, document FROM resources WHERE type = ? AND deleted = 0",
                    (referencing_type,),
                ).fetchall()
                for row in rows:
                    key = (referencing_type, row["id"])
                    if key in primary_keys or key in reverse:
                        continue
                    document = self.store.decode(row["document"])
                    stored = document.get(field)
                    reference = stored.get("reference") if isinstance(stored, dict) else None
                    if not isinstance(reference, str):
                        continue
                    target_type = reference.partition("/")[0]
                    if target_type != primary_type:
                        continue
                    target_id = self._resolve_target(connection, referencing_type, field, reference)
                    if target_id is not None and target_id in primary_ids:
                        reverse[key] = document

        def ordered(resolved: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
            return [resolved[key] for key in sorted(resolved)]

        return ordered(forward), ordered(reverse)

    @staticmethod
    def _resolve_target(connection: Any, owner_type: str, field: str, reference: str) -> str | None:
        """Resolve a reference to a live target id, or None when unresolvable."""
        try:
            return resolve_reference(connection, owner_type, field, reference)
        except (ValidationError, OperationOutcomeError):
            return None

    @staticmethod
    def _paging(parameters: dict[str, list[str]]) -> tuple[int | None, int]:
        count: int | None = None
        if "_count" in parameters:
            raw_count = parameters["_count"][-1]
            if not raw_count.isdigit() or int(raw_count) > 1000:
                raise ValidationError("_count must be an integer between 0 and 1000")
            count = int(raw_count)
        offset = 0
        if "_offset" in parameters:
            raw_offset = parameters["_offset"][-1]
            if not raw_offset.isdigit():
                raise ValidationError("_offset must be a non-negative integer")
            offset = int(raw_offset)
        if parameters.get("_sort", ["_id"])[0] not in ("_id", "-_id"):
            raise ValidationError("_sort must be _id or -_id")
        return count, offset

    # ------------------------------------------------------------------ subscriptions

    @_serialized
    def create_subscription(self, raw: Any, key: str | None = None, payload_id: Any = None) -> dict[str, Any]:
        subscription_id, criteria, reason = parse_subscription(raw, payload_id)

        def apply() -> dict[str, Any]:
            moment = self.store.now()
            try:
                self.store.connection.execute(
                    "INSERT INTO subscriptions(id, criteria, reason, created_at) VALUES (?, ?, ?, ?)",
                    (subscription_id, self.store.encode(criteria), reason, moment),
                )
            except Exception as error:
                if "UNIQUE constraint" in str(error):
                    raise ConflictError(f"Subscription/{subscription_id} already exists") from error
                raise
            return {"subscription_id": subscription_id, "created_at": moment, "criteria": criteria, "reason": reason}

        return self._idempotent(key, f"create-subscription:{subscription_id}", apply)

    @_serialized
    def events(self, subscription_id: str) -> dict[str, Any]:
        row = self.store.connection.execute(
            "SELECT id, criteria FROM subscriptions WHERE id = ?", (subscription_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Subscription/{subscription_id} was not found")
        rows = self.store.connection.execute(
            "SELECT payload FROM events WHERE subscription_id = ? ORDER BY sequence", (subscription_id,)
        ).fetchall()
        return {
            "subscription_id": subscription_id,
            "criteria": self.store.decode(row["criteria"]),
            "total": len(rows),
            "events": [self.store.decode(stored["payload"]) for stored in rows],
        }

    def _record_events(
        self,
        resource_type: str,
        resource_id: str,
        event: str,
        version: int,
        document: dict[str, Any],
        moment: str,
    ) -> None:
        rows = self.store.connection.execute(
            "SELECT id, criteria FROM subscriptions ORDER BY id"
        ).fetchall()
        for row in rows:
            criteria = self.store.decode(row["criteria"])
            if not criteria_match(criteria, resource_type, document):
                continue
            sequence_row = self.store.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM events WHERE subscription_id = ?",
                (row["id"],),
            ).fetchone()
            payload = {
                "sequence": sequence_row["sequence"],
                "subscription_id": row["id"],
                "event": event,
                "resource": f"{resource_type}/{resource_id}",
                "version": version,
                "occurredAt": moment,
            }
            self.store.connection.execute(
                "INSERT INTO events(subscription_id, sequence, type, payload, occurred_at) VALUES (?, ?, ?, ?, ?)",
                (row["id"], payload["sequence"], event, self.store.encode(payload), moment),
            )

    # ------------------------------------------------------------------ references

    def _assert_references(self, validated: ValidatedResource) -> None:
        for field, reference in validated.references:
            resolve_reference(self.store.connection, validated.resource_type, field, reference)
