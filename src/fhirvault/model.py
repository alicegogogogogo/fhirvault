from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .errors import NotFoundError, OperationOutcomeError, ValidationError

RESOURCE_TYPES: tuple[str, ...] = ("Patient", "Observation", "Encounter")
SUBSCRIPTION_TYPE = "Subscription"

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INSTANT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")
_IF_MATCH_PATTERN = re.compile(r'^(?:W/)?"([^"]*)"$')

# Every storable field: name -> (required, kind, prefix searchable).
_FIELDS: dict[str, dict[str, tuple[bool, str, bool]]] = {
    "Patient": {
        "active": (False, "boolean", False),
        "gender": (False, "code", False),
        "birthDate": (False, "date", False),
        "name.family": (False, "string", True),
        "name.given": (False, "string", True),
        "identifier": (False, "identifiers", False),
    },
    "Observation": {
        "status": (True, "code", False),
        "code.text": (True, "string", True),
        "subject": (False, "reference", False),
        "encounter": (False, "reference", False),
        "hasMember": (False, "reference", False),
        "effectiveDateTime": (False, "instant", True),
        "value": (False, "scalar", False),
    },
    "Encounter": {
        "status": (True, "code", False),
        "class.code": (True, "string", True),
        "subject": (True, "reference", False),
        "period.start": (False, "instant", True),
        "period.end": (False, "instant", True),
        "reason.text": (False, "string", True),
        "partOf": (False, "reference", False),
    },
}

_CODES: dict[tuple[str, str], tuple[str, ...]] = {
    ("Patient", "gender"): ("male", "female", "other", "unknown"),
    ("Observation", "status"): (
        "registered", "preliminary", "final", "amended", "corrected", "cancelled", "entered-in-error", "unknown",
    ),
    ("Encounter", "status"): (
        "planned", "arrived", "triaged", "in-progress", "onleave", "finished", "cancelled", "entered-in-error", "unknown",
    ),
}

REFERENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "Patient": (),
    "Observation": ("subject", "encounter", "hasMember"),
    "Encounter": ("subject", "partOf"),
}

_SEARCHABLE_STRING_FIELDS: dict[str, tuple[str, ...]] = {
    "Patient": ("name.family", "name.given"),
    "Observation": ("code.text", "effectiveDateTime"),
    "Encounter": ("class.code", "period.start", "period.end", "reason.text"),
}


def fields_for(resource_type: str) -> tuple[str, ...]:
    return tuple(_FIELDS[resource_type])


def required_fields(resource_type: str) -> tuple[str, ...]:
    return tuple(name for name, (required, _, _) in _FIELDS[resource_type].items() if required)


def prefix_fields(resource_type: str) -> tuple[str, ...]:
    return _SEARCHABLE_STRING_FIELDS[resource_type]


def _identifier(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.match(value):
        raise ValidationError(
            f"{where} must match {_ID_PATTERN.pattern} (letters, digits, dot, dash, underscore; at most 100 characters)"
        )
    return value


def validate_path_id(resource_id: str) -> str:
    return _identifier(resource_id, "resource id")


def parse_if_match(value: str | None) -> str | None:
    """Normalize an If-Match header to a version id, ``*``, or None when absent.

    Strong (``"1"``) and weak (``W/"1"``) ETags both reduce to their version
    identifier; anything that is not a single entity-tag or ``*`` is a 400.
    """
    if value is None:
        return None
    text = value.strip()
    if text == "*":
        return "*"
    match = _IF_MATCH_PATTERN.match(text)
    if match is None:
        raise OperationOutcomeError(
            400, "invalid", 'If-Match must be *, "<versionId>", or W/"<versionId>"'
        )
    return match.group(1)


def _instant(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _INSTANT_PATTERN.match(value):
        raise ValidationError(f"{where} must be an RFC 3339 instant such as 2026-01-05T09:00:00Z")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValidationError(f"{where} must be a non-empty string of at most 200 characters")
    return value


def _validate(kind: str, value: Any, resource_type: str, field: str) -> Any:
    where = f"{resource_type}.{field}"
    if kind == "string":
        return _string(value, where)
    if kind == "boolean":
        if not isinstance(value, bool):
            raise ValidationError(f"{where} must be a boolean")
        return value
    if kind == "code":
        allowed = _CODES[(resource_type, field)]
        if not isinstance(value, str) or value not in allowed:
            raise ValidationError(f"{where} must be one of: {', '.join(allowed)}")
        return value
    if kind == "date":
        if not isinstance(value, str) or not _DATE_PATTERN.match(value):
            raise ValidationError(f"{where} must be a date such as 1980-04-12")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValidationError(f"{where} is not a real calendar date") from error
        return value
    if kind == "instant":
        return _instant(value, where)
    if kind == "scalar":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValidationError(f"{where} must be a number or a string")
        return value
    return value


def _identifiers(value: Any, resource_type: str, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{resource_type}.{field} must be a non-empty array of identifiers")
    entries: list[str] = []
    for index, item in enumerate(value):
        where = f"{resource_type}.{field}[{index}]"
        if not isinstance(item, dict):
            raise ValidationError(f"{where} must be an object")
        unknown = set(item) - {"system", "value"}
        if unknown:
            raise ValidationError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")
        system = _string(item.get("system"), f"{where}.system")
        identifier = _string(item.get("value"), f"{where}.value")
        if "|" in system or "|" in identifier:
            raise ValidationError(f"{where} must not contain the '|' separator")
        entries.append(f"{system}|{identifier}")
    if len(entries) != len(set(entries)):
        raise ValidationError(f"{resource_type}.{field} must not contain duplicate identifiers")
    return entries


@dataclass(frozen=True)
class ValidatedResource:
    resource_type: str
    resource_id: str
    document: dict[str, Any]
    identifiers: tuple[str, ...]
    references: tuple[tuple[str, str], ...]


def parse_resource(resource_type: str, raw: Any, *, expected_id: str | None = None) -> ValidatedResource:
    """Validate a submitted resource body, ignoring the server-managed meta field."""
    if resource_type not in RESOURCE_TYPES:
        raise NotFoundError(f"resource type {resource_type} is not supported")
    if not isinstance(raw, dict):
        raise ValidationError("resource must be a JSON object")
    body_type = raw.get("resourceType")
    if body_type is not None and body_type != resource_type:
        raise ValidationError(f"resourceType must be {resource_type}")
    resource_id = _identifier(raw.get("id"), "resource id")
    if expected_id is not None and resource_id != expected_id:
        raise ValidationError(f"resource id {resource_id} does not match the request path id {expected_id}")
    allowed = set(_FIELDS[resource_type]) | {"resourceType", "id", "meta"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValidationError(
            f"{resource_type} has unknown fields: {', '.join(sorted(unknown))}; "
            f"allowed fields are {', '.join(sorted(allowed))}"
        )

    document: dict[str, Any] = {}
    identifiers: list[str] = []
    references: list[tuple[str, str]] = []
    for field, (required, kind, _) in _FIELDS[resource_type].items():
        if field not in raw:
            if required:
                raise ValidationError(f"{resource_type}.{field} is required")
            continue
        value = raw[field]
        if kind == "identifiers":
            identifiers = _identifiers(value, resource_type, field)
        elif kind == "reference":
            references.append(_reference(value, resource_type, field))
        else:
            _validate(kind, value, resource_type, field)
        document[field] = value
    if resource_type == "Encounter" and "period.start" in document and "period.end" in document:
        if document["period.start"] > document["period.end"]:
            raise ValidationError("Encounter.period.end must not precede Encounter.period.start")
    return ValidatedResource(resource_type, resource_id, document, tuple(identifiers), tuple(references))


def _reference(value: Any, resource_type: str, field: str) -> tuple[str, str]:
    where = f"{resource_type}.{field}"
    if not isinstance(value, dict) or set(value) != {"reference"}:
        raise ValidationError(f"{where} must contain exactly the reference field")
    reference = value["reference"]
    if not isinstance(reference, str) or not reference:
        raise ValidationError(f"{where}.reference must be a non-empty string")
    target_type, _, rest = reference.partition("/")
    if target_type not in RESOURCE_TYPES:
        raise ValidationError(f"{where}.reference must start with one of: {', '.join(RESOURCE_TYPES)}")
    if rest.startswith("identifier"):
        parts = rest.split("|")
        if len(parts) != 3 or not parts[1] or not parts[2]:
            raise ValidationError(f"{where}.reference must be {target_type}/identifier|<system>|<value>")
    elif not rest:
        raise ValidationError(f"{where}.reference must be <type>/<id> or <type>/identifier|<system>|<value>")
    else:
        _identifier(rest, f"{where}.reference")
    return field, reference


def resolve_reference(connection: Any, resource_type: str, field: str, reference: str) -> str:
    """Return the id a reference points at, or raise when the target is not a live resource."""
    target_type, _, rest = reference.partition("/")
    if rest.startswith("identifier"):
        _, system, value = rest.split("|", 2)
        wanted = f"{system}|{value}"
        rows = connection.execute(
            "SELECT id, document FROM resources WHERE type = ? AND deleted = 0 ORDER BY id",
            (target_type,),
        ).fetchall()
        matches = [row["id"] for row in rows if wanted in identifiers_of(json.loads(row["document"]))]
        if not matches:
            raise ValidationError(f"{resource_type}.{field} references identifier {wanted}, which does not exist")
        if len(matches) > 1:
            raise ValidationError(
                f"{resource_type}.{field} reference {reference} is ambiguous: it matches {', '.join(matches)}"
            )
        return matches[0]
    if connection.execute(
        "SELECT id FROM resources WHERE type = ? AND id = ? AND deleted = 0", (target_type, rest)
    ).fetchone() is None:
        raise ValidationError(f"{resource_type}.{field} references {reference}, which does not exist")
    return rest


def identifiers_of(document: dict[str, Any]) -> tuple[str, ...]:
    entries = document.get("identifier")
    if not isinstance(entries, list):
        return ()
    return tuple(f"{item['system']}|{item['value']}" for item in entries if isinstance(item, dict))


def search_match(
    resource_type: str, document: dict[str, Any], identifiers: tuple[str, ...], field: str, value: str
) -> bool:
    """True when one stored field satisfies a conditional search parameter."""
    if field == "identifier":
        return value in identifiers
    stored = document.get(field)
    if field in REFERENCE_FIELDS[resource_type]:
        return isinstance(stored, dict) and stored.get("reference") == value
    if field in prefix_fields(resource_type):
        return isinstance(stored, str) and stored.startswith(value)
    return stored == value


def parse_subscription(raw: Any, payload_id: Any) -> tuple[str, dict[str, Any], str | None]:
    if not isinstance(raw, dict) or "criteria" not in raw:
        raise ValidationError("subscription must contain criteria")
    unknown = set(raw) - {"resourceType", "id", "criteria", "reason"}
    if unknown:
        raise ValidationError(f"Subscription has unknown fields: {', '.join(sorted(unknown))}")
    if raw.get("resourceType") not in (None, SUBSCRIPTION_TYPE):
        raise ValidationError(f"resourceType must be {SUBSCRIPTION_TYPE}")
    subscription_id = _identifier(payload_id if payload_id is not None else raw.get("id"), "subscription id")
    criteria = parse_criteria(raw["criteria"])
    reason = raw.get("reason")
    if reason is not None:
        _string(reason, "Subscription.reason")
    return subscription_id, criteria, reason


def parse_criteria(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("Subscription.criteria must be an object")
    unknown = set(raw) - {"type", "field", "equals", "prefix"}
    if unknown:
        raise ValidationError(f"Subscription.criteria has unknown fields: {', '.join(sorted(unknown))}")
    resource_type = raw.get("type")
    if resource_type not in RESOURCE_TYPES:
        raise ValidationError(f"Subscription.criteria.type must be one of: {', '.join(RESOURCE_TYPES)}")
    criteria: dict[str, Any] = {"type": resource_type}
    field = raw.get("field")
    if field is None:
        if "equals" in raw or "prefix" in raw:
            raise ValidationError("Subscription.criteria.field is required when equals or prefix is set")
        return criteria
    if field not in fields_for(resource_type):
        raise ValidationError(f"Subscription.criteria.field must be one of: {', '.join(fields_for(resource_type))}")
    if field in REFERENCE_FIELDS[resource_type]:
        raise ValidationError(f"Subscription.criteria.field must be a primitive field, not the reference field {field}")
    criteria["field"] = field
    if "equals" in raw and "prefix" in raw:
        raise ValidationError("Subscription.criteria must not set both equals and prefix")
    if "equals" in raw:
        criteria["equals"] = raw["equals"]
    if "prefix" in raw:
        criteria["prefix"] = _string(raw["prefix"], "Subscription.criteria.prefix")
        if field not in prefix_fields(resource_type):
            raise ValidationError(f"Subscription.criteria.prefix is not supported for {field}")
    return criteria


def criteria_match(criteria: dict[str, Any], resource_type: str, document: dict[str, Any]) -> bool:
    """True when a stored resource version satisfies a subscription's criteria."""
    if criteria["type"] != resource_type:
        return False
    field = criteria.get("field")
    if field is None:
        return True
    value = document.get(field)
    if "equals" in criteria:
        return value == criteria["equals"]
    return isinstance(value, str) and value.startswith(criteria["prefix"])
