from __future__ import annotations

import re
from typing import Any, Callable

from .errors import (
    ConflictError,
    InvalidPreconditionError,
    NotFoundError,
    PreconditionFailedError,
    PreconditionTargetMissingError,
    ValidationError,
)
from .model import (
    RESOURCE_TYPES,
    ValidatedResource,
    criteria_match,
    fields_for,
    identifiers_of,
    parse_resource,
    parse_subscription,
    resolve_reference,
    search_match,
    validate_path_id,
)
from .store import Store

_CONTROL_PARAMETERS = ("_count", "_offset", "_sort")

# Bytes permitted inside an opaque-tag (RFC 7232): VCHAR minus DQUOTE, here
# restricted to ASCII (etagc also allows obs-text, which this service never emits).
_ETAGC = re.compile(r"^[\x21\x23-\x7e]*$")


def parse_if_match(header: str) -> list[tuple[bool, str]] | str:
    """Parse an If-Match header value.

    Returns ``"*"`` for the wildcard, otherwise a list of ``(weak, etag)``
    pairs where ``etag`` is the unquoted opaque tag (the resource version
    identifier). Raises InvalidPreconditionError when the header is not a
    valid ``"*"`` or comma-separated list of entity-tags.
    """
    value = header.strip(" \t")
    if value == "*":
        return "*"
    tags: list[tuple[bool, str]] = []
    for element in value.split(","):
        token = element.strip(" \t")
        weak = False
        if token[:2].upper() == "W/":
            weak = True
            token = token[2:].strip(" \t")
        if len(token) < 2 or token[0] != '"' or token[-1] != '"' or not _ETAGC.match(token[1:-1]):
            raise InvalidPreconditionError(
                "If-Match must be '*' or a comma-separated list of entity-tags such as "
                f'"10" or W/"10"; received {header!r}'
            )
        tags.append((weak, token[1:-1]))
    if not tags:
        raise InvalidPreconditionError("If-Match must contain at least one entity-tag")
    return tags


def etag_for(version_id: Any) -> str:
    """The strong ETag used for both read responses and update preconditions."""
    return f'"{version_id}"'


class FhirVault:
    def __init__(self, database: str, clock: Callable[[], Any] | None = None):
        self.store = Store(database, clock)

    # ------------------------------------------------------------------ helpers

    def _idempotent(self, key: str | None, operation: str, action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if not key:
            raise ValidationError("Idempotency-Key header is required")
        existing = self.store.connection.execute(
            "SELECT operation, response FROM idempotency WHERE key = ?", (key,)
        ).fetchone()
        if existing:
            if existing["operation"] != operation:
                raise ConflictError("idempotency key was already used for another operation")
            return self.store.decode(existing["response"])
        with self.store.transaction():
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

    def _write(self, validated: ValidatedResource, *, reject_existing: bool) -> dict[str, Any]:
        row = self.store.connection.execute(
            "SELECT current_version, deleted FROM resources WHERE type = ? AND id = ?",
            (validated.resource_type, validated.resource_id),
        ).fetchone()
        if row is not None and not row["deleted"] and reject_existing:
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

    def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        validate_path_id(resource_id)
        row = self._require(resource_type, resource_id)
        return self.store.decode(row["document"])

    def update(
        self,
        resource_type: str,
        resource_id: str,
        raw: Any,
        key: str | None = None,
        if_match: str | None = None,
    ) -> dict[str, Any]:
        validate_path_id(resource_id)
        condition = parse_if_match(if_match) if if_match is not None else None
        validated = parse_resource(resource_type, raw, expected_id=resource_id)
        self._assert_references(validated)

        def apply() -> dict[str, Any]:
            # The lock plus the BEGIN IMMEDIATE transaction make the version
            # check and the write atomic: concurrent conditional updates commit
            # in reception order, and the loser re-reads the winner's version.
            with self.store.write_lock:
                if condition is not None:
                    self._check_precondition(resource_type, resource_id, condition, if_match)
                return self._write(validated, reject_existing=False)

        return self._idempotent(key, f"update:{resource_type}/{resource_id}", apply)

    def _check_precondition(
        self,
        resource_type: str,
        resource_id: str,
        condition: Any,
        raw_header: str | None,
    ) -> None:
        row = self.store.connection.execute(
            "SELECT current_version, deleted FROM resources WHERE type = ? AND id = ?",
            (resource_type, resource_id),
        ).fetchone()
        if row is None or row["deleted"]:
            raise PreconditionTargetMissingError(resource_type, resource_id)
        current = str(row["current_version"])
        if condition != "*" and not any(tag == current for _weak, tag in condition):
            provided = (raw_header or "").strip(" \t")
            raise PreconditionFailedError(resource_type, resource_id, provided, current)

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

    def search(self, resource_type: str, parameters: dict[str, list[str]]) -> dict[str, Any]:
        if resource_type not in RESOURCE_TYPES:
            raise NotFoundError(f"resource type {resource_type} is not supported")
        unknown = set(parameters) - set(fields_for(resource_type)) - set(_CONTROL_PARAMETERS) - {"id"}
        if unknown:
            raise ValidationError(
                f"unknown search parameters: {', '.join(sorted(unknown))}; supported parameters are "
                f"{', '.join(sorted(set(fields_for(resource_type)) | {'id'} | set(_CONTROL_PARAMETERS)))}"
            )
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
                if field not in _CONTROL_PARAMETERS and field != "id"
            ):
                continue
            matched.append((row["id"], document))
        matched.sort(key=lambda item: item[0], reverse=sort == "-_id")
        total = len(matched)
        page = matched[offset:] if count is None else matched[offset : offset + count]
        return {
            "resourceType": resource_type,
            "total": total,
            "count": len(page),
            "offset": offset,
            "sort": sort,
            "parameters": {name: sorted(values) for name, values in sorted(parameters.items())},
            "entry": [{"resource": document} for _, document in page],
        }

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
