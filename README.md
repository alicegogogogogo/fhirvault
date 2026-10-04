# FhirVault

FhirVault is a small clinical resource vault for a FHIR subset. It stores
`Patient`, `Observation`, and `Encounter` resources in SQLite, validates their
structure and their `reference` links to other resources, keeps every version of
every resource, answers conditional searches, and delivers `Subscription` events
when a matching resource changes.

The initial release intentionally supports a compact public contract:

- three resource types: `Patient`, `Observation`, and `Encounter`;
- full CRUD per resource, with server-managed `meta.versionId`;
- optimistic concurrency on update: responses carry an `ETag` for the current
  version and `PUT` accepts `If-Match` to reject stale writes with HTTP 412;
- every write appends an immutable version; versions never decrease and old
  versions stay readable, including after a delete;
- `reference` links must resolve to a live resource that is already stored;
- conditional search by exact value, by prefix for string fields, and by
  reference, with deterministic ordering;
- subscriptions match `created`, `updated`, and `deleted` changes and record an
  ordered delivery event each time;
- subscriptions may carry a webhook `channel`: each matching event is then also
  POSTed to the channel endpoint with persistent retries (1s, then 2s), an
  optional HMAC-SHA256 signature, and a queryable per-delivery attempt log;
- `POST /fhir` applies a transaction Bundle atomically: every entry succeeds or
  the whole batch rolls back;
- repeated state-changing requests with the same `Idempotency-Key` return the
  first response;
- every completed resource, Bundle, and Subscription request is recorded in a
  durable access audit queryable at `GET /audit`.

## Requirements

- Python 3.11 or newer
- no third-party runtime dependencies

## Run the service

```bash
PYTHONPATH=src python -m fhirvault.server --host 127.0.0.1 --port 8080 --database fhirvault.db
```

The process prints `FhirVault listening on http://127.0.0.1:8080` after it has
bound the port.

## HTTP API

All request and response bodies are JSON. Unknown fields are rejected with
`validation_error`. State-changing `POST`, `PUT`, and `DELETE` requests require
an `Idempotency-Key` header. The optional `X-FhirVault-Actor` header names the
actor recorded in the access audit; a missing or blank value is `anonymous`.

### Health

```http
GET /health
```

Returns `{"status":"ok"}`.

### Create a resource

```http
POST /fhir/Patient
Idempotency-Key: patient-1
Content-Type: application/json

{
  "resourceType": "Patient",
  "id": "p-1",
  "gender": "male",
  "birthDate": "1980-04-12",
  "name.family": "Smith",
  "name.given": "Ada",
  "identifier": [{"system": "mrn", "value": "A123"}]
}
```

Returns HTTP 201 with the stored resource, an `ETag` of `W/"1"` for the new
version, and a `Location` header pointing at `/fhir/Patient/p-1`. A duplicate
id returns HTTP 409 `conflict`; the same `Idempotency-Key` returns the
original 201 body instead.

### Read a resource

```http
GET /fhir/Patient/p-1
```

```json
{
  "resourceType": "Patient",
  "id": "p-1",
  "gender": "male",
  "birthDate": "1980-04-12",
  "name.family": "Smith",
  "name.given": "Ada",
  "identifier": [{"system": "mrn", "value": "A123"}],
  "meta": {"versionId": "1", "lastUpdated": "2026-01-05T09:00:00.002Z"}
}
```

A successful read returns an `ETag` header such as `W/"1"` identifying the
current version; clients can send it back as `If-Match` on a later update.

### Update a resource

```http
PUT /fhir/Patient/p-1
Idempotency-Key: patient-1-v2

{"resourceType":"Patient","id":"p-1","gender":"female","birthDate":"1980-04-12","name.family":"Smith"}
```

`PUT` without `If-Match` is an unconditional upsert: it creates version 1 when
the id is new and otherwise stores the next version of the same logical
resource, returning HTTP 200 with the new `meta.versionId`, an `ETag` of
`W/"<versionId>"`, and a `Location` header pointing at `/fhir/Patient/p-1`.
The body `id` must equal the path id. A resource that was deleted can be
recreated with `PUT`; the version sequence continues (`1`, `2`, `3`, …) and is
never reused.

### Conditional update with If-Match

```http
PUT /fhir/Patient/p-1
Idempotency-Key: patient-1-v3
If-Match: W/"2"

{"resourceType":"Patient","id":"p-1","gender":"female","birthDate":"1980-04-12","name.family":"Smith"}
```

Sending `If-Match` makes the update conditional on the version the client
last saw. The server compares the header with the resource's current
`meta.versionId` and only then writes:

- `If-Match: "2"` (strong) and `If-Match: W/"2"` (weak) are equivalent; both
  match when the current version is `2`. `If-Match: *` matches any existing
  resource.
- On a match the update proceeds exactly like an unconditional `PUT`: HTTP
  200 with the updated resource, a `Location` header, and a new `ETag`.
- A missing `If-Match` is not an error; the unconditional upsert semantics
  above apply.
- A syntactically invalid `If-Match` returns HTTP 400 and changes nothing.
- If the target resource does not exist (or is deleted), the update returns
  HTTP 404.
- If the version does not match, the update returns HTTP 412 and neither the
  resource nor its version history changes.

All three failures return an `application/fhir+json` `OperationOutcome` whose
`issue.code` is `invalid`, `not-found`, or `conflict` respectively; the
`conflict` diagnostics state that the provided version is not the current
version:

```json
{
  "resourceType": "OperationOutcome",
  "issue": [{"severity": "error", "code": "conflict",
             "diagnostics": "If-Match version \"1\" is not the current version \"2\" of Patient/p-1"}]
}
```

Concurrent conditional updates are serialized: when two clients both send
`If-Match` for version 10, the first request accepted creates version 11 and
returns HTTP 200, and the second returns HTTP 412 without creating an extra
version. Validation still runs on every update, so a body that fails
structural or reference validation creates no version whether or not
`If-Match` was sent.

### Delete a resource

```http
DELETE /fhir/Encounter/e-1
Idempotency-Key: encounter-1-delete
```

```json
{"id":"e-1","resourceType":"Encounter","deleted":true,"version":2,"lastUpdated":"2026-01-05T09:00:00.005Z"}
```

The delete is logical: the resource disappears from `GET` and from search (404
afterwards), a tombstone version is appended, and `_history` stays readable.

### Transaction bundle

```http
POST /fhir
Idempotency-Key: txn-1
Content-Type: application/json

{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {"request": {"method": "POST", "url": "Patient"},
     "resource": {"resourceType": "Patient", "id": "p-1", "gender": "female"}},
    {"request": {"method": "PUT", "url": "Observation/o-1", "ifMatch": "W/\"1\""},
     "resource": {"resourceType": "Observation", "id": "o-1", "status": "final", "code.text": "HbA1c"}},
    {"request": {"method": "DELETE", "url": "Encounter/e-1"}}
  ]
}
```

`POST /fhir` applies a transaction Bundle atomically: the entries take effect in
order, and the first failing entry aborts the batch — every earlier write is
rolled back and only that first error is reported. The Bundle accepts exactly
`resourceType` (`"Bundle"`), `type` (`"transaction"`), and `entry` (1–100
items); each entry accepts exactly `request` and an optional `resource`, and
each request accepts exactly `method`, `url`, and `ifMatch`. Anything else —
malformed JSON, a wrong content type, extra fields, an unknown resource type,
an out-of-range entry count, or a missing `Idempotency-Key` — fails the whole
request with HTTP 400 and an `application/fhir+json` `OperationOutcome` whose
`issue.code` is `invalid`, and changes nothing.

Entry semantics mirror the standalone routes:

- `POST` with `url` set to a resource type creates the body's id at version 1;
  an id that already exists fails the batch with HTTP 409 `conflict`.
- `PUT` with `url` set to `<resourceType>/<id>` keeps the upsert semantics of
  the standalone route: the version increments, a deleted id is recreated, and
  the body id must equal the path id. `ifMatch` accepts strong (`"1"`) and weak
  (`W/"1"`) ETags and `*`; a missing `ifMatch` is unconditional, a malformed
  one is HTTP 400 `invalid`, a missing or deleted target is HTTP 404
  `not-found`, and a version mismatch is HTTP 412 `conflict`.
- `DELETE` with `url` set to `<resourceType>/<id>` writes a tombstone; a
  missing or already deleted target is HTTP 404 `not-found`. `DELETE` entries
  carry no `resource`.

Later entries may reference resources written by earlier entries, both by id
(`Patient/p-1`) and by identifier (`Patient/identifier|<system>|<value>`); a
reference that does not exist, is deleted, is ambiguous, or names a wrong type
fails the batch with HTTP 400 `invalid`.

A successful batch returns HTTP 200 with a `transaction-response` Bundle whose
entries follow the request order. `POST` and `PUT` entries carry
`response.status` `"201"`/`"200"`, `response.location`, `response.etag`, and
the stored `resource`; `DELETE` entries carry `response.status` `"200"`,
`response.location`, and `"resource": null`. Subscription events are recorded
once per write, in entry order, when the batch commits. Replaying the same
bundle with the same `Idempotency-Key` returns the first response without
creating new versions or events; reusing the key for anything else is HTTP 409
`conflict`.

### Version history

```http
GET /fhir/Patient/p-1/_history
```

```json
{
  "resourceType": "Patient",
  "id": "p-1",
  "deleted": false,
  "entries": [
    {"version":1,"recordedAt":"2026-01-05T09:00:00.002Z","current":false,"resource":{...},"tombstone":false,"meta":{...}},
    {"version":2,"recordedAt":"2026-01-05T09:00:00.004Z","current":true,"resource":{...},"tombstone":false,"meta":{...}}
  ]
}
```

Entries are ordered by ascending `version`. The current live version has
`current: true`; a deleted resource exposes its final entry as
`"resource": null, "tombstone": true`. History is readable for deleted resources
and returns 404 for ids that never existed.

### Conditional search

```http
GET /fhir/Observation?code.text=HbA&subject=Patient/p-1&_count=10&_offset=0&_sort=_id
```

```json
{
  "resourceType": "Observation",
  "total": 2,
  "count": 2,
  "offset": 0,
  "sort": "_id",
  "parameters": {"code.text": ["HbA"], "subject": ["Patient/p-1"]},
  "entry": [{"resource": {...}}, {"resource": {...}}]
}
```

- `entry` contains only live (non-deleted) resources, sorted ascending by `id`
  (`-_id` reverses it), so the same query always returns the same order.
- Different parameters are combined with AND. A repeated parameter is combined
  with OR: `?gender=male&gender=female` matches either value.
- Exact equality applies to every field. Prefix matching applies to string
  fields: `name.family`, `name.given`, `code.text`, `effectiveDateTime`,
  `class.code`, `period.start`, `period.end`, `reason.text`.
- Reference fields (`subject`, `encounter`, `hasMember`, `partOf`) match the
  exact reference string, for example `subject=Patient/p-1`.
- `identifier` matches `system|value`, for example `identifier=mrn|A123`.
- `_count` (0–1000) limits the page, `_offset` skips leading entries, `_sort` is
  `_id` or `-_id`, and `id=...` filters by resource id. Unknown parameters are
  rejected with `validation_error`.

### One-hop include expansion

A search can carry repeated `_include` and `_revinclude` parameters to pull in
resources linked to the page's primary entries. Expansion is exactly one hop.

- `_include=<resourceType>:<referenceField>` — `resourceType` must equal the
  primary search type and `referenceField` must be one of its reference fields.
  Every referenced live target of the page's primary entries is returned, for
  example `GET /fhir/Observation?_include=Observation:subject` includes the
  referenced `Patient` resources.
- `_revinclude=<referencingType>:<referenceField>` — returns live resources of
  `referencingType` whose `referenceField` resolves to any primary result, for
  example `GET /fhir/Patient?_revinclude=Observation:subject` includes the
  related `Observation` resources.

Both id references (`Patient/p-1`) and identifier references
(`Patient/identifier|<system>|<value>`) follow the same resolution rules as
writes; reverse matching compares against the resolved target. Only current,
live, resolvable targets are included — deleted resources and dangling
references are skipped.

```json
{
  "resourceType": "Observation",
  "total": 1,
  "count": 1,
  "offset": 0,
  "sort": "_id",
  "parameters": {"_include": ["Observation:encounter", "Observation:subject"]},
  "entry": [{"resource": {"id": "o-1", "...": "...",
                          "subject": {"reference": "Patient/p-1"},
                          "encounter": {"reference": "Encounter/e-1"}}}],
  "include": [
    {"resourceType": "Encounter", "id": "e-1", "...": "..."},
    {"resourceType": "Patient", "id": "p-1", "...": "..."}
  ],
  "revinclude": []
}
```

- `include` and `revinclude` hold the full expanded resource documents, sorted by
  `resourceType` then `id` and deduplicated by `resourceType/id`. Repeated
  parameters are evaluated separately and their results merged.
- Expanded resources never count toward `total`, `count`, or `entry`, and primary
  entry resources are never copied into an expansion array (even when the
  referencing type equals the primary type). The same resource may appear in both
  arrays when it satisfies both kinds of request.
- Expansion runs after filtering, sorting, and paging, so only targets of the
  returned page are expanded. An empty `entry` yields empty arrays.
- When neither parameter is present the response is unchanged: the `include` and
  `revinclude` keys are omitted entirely.
- A malformed value, an unsupported `resourceType`/`referencingType`, a
  `referenceField` that does not exist or is not a reference field, or an
  `_include` whose type is not the primary type all fail the whole request with
  HTTP 400 `validation_error` — partial results are never returned.

### Create a subscription

```http
POST /Subscription
Idempotency-Key: subscription-1
Content-Type: application/json

{
  "resourceType": "Subscription",
  "id": "sub-1",
  "reason": "Watch female patients",
  "criteria": {"type": "Patient", "field": "gender", "equals": "female"}
}
```

Returns HTTP 201 with the stored subscription:

```json
{"subscription_id":"sub-1","created_at":"2026-01-05T09:00:00.006Z","criteria":{"type":"Patient","field":"gender","equals":"female"},"reason":"Watch female patients"}
```

Subscriptions are not versioned resources: `POST /fhir/Subscription` creates
exactly the same resource and returns the same body. Read a subscription's
deliveries with `GET /subscriptions/{id}/events`.

`criteria` always contains `type` (one of the three resource types). It may add
`field` plus exactly one of:

- `equals` — the stored value must be identical (`value` comparison for numbers
  and strings, so `7` and `7.0` are equal);
- `prefix` — the stored value must be a string starting with the prefix; only
  the prefix-searchable string fields above are accepted.

`field` must be a primitive field of that type (never a reference field). With
only `type` set, the subscription matches every write of that type. A matching
subscription records an event for each `created`, `updated`, and `deleted`
change; criteria are evaluated against the new stored document, and a delete
matches against the last live version.

A subscription may also carry an optional `channel` object that turns event
recording into webhook delivery:

```json
{
  "id": "sub-2",
  "criteria": {"type": "Patient"},
  "channel": {"endpoint": "https://hooks.example.com/fhir", "secret": "s3cr3t"}
}
```

`channel` accepts exactly `endpoint` (required) and `secret` (optional).
`endpoint` must be an absolute `http` or `https` URL without user info, a query
string, or a fragment; `secret`, when present, must be a non-empty string. An
invalid channel fails the whole create with HTTP 400 `validation_error` and
stores nothing. Subscriptions without a channel keep the original behavior:
matching writes only record events.

### Subscription delivery events

```http
GET /subscriptions/sub-1/events
```

```json
{
  "subscription_id": "sub-1",
  "criteria": {"type": "Patient", "field": "gender", "equals": "female"},
  "total": 2,
  "events": [
    {"sequence":1,"subscription_id":"sub-1","event":"created","resource":"Patient/p-2","version":1,"occurredAt":"2026-01-05T09:00:00.007Z"},
    {"sequence":2,"subscription_id":"sub-1","event":"updated","resource":"Patient/p-2","version":2,"occurredAt":"2026-01-05T09:00:00.008Z"}
  ]
}
```

Events are stored in the delivery log at the moment the write commits. Their
`sequence` numbers are per subscription, start at 1, and are strictly
increasing; a given `(subscription_id, sequence)` is never reused, so events
are readable repeatedly and never duplicated.

### Webhook deliveries

```http
GET /subscriptions/sub-2/deliveries
```

```json
{
  "subscription_id": "sub-2",
  "total": 1,
  "deliveries": [
    {"sequence":1,"deliveryId":"9f1c...","state":"delivered","attempts":[
      {"attempt":1,"attemptedAt":"2026-01-05T09:00:00.007Z","outcome":"failure","httpStatus":500,"error":"HTTP 500"},
      {"attempt":2,"attemptedAt":"2026-01-05T09:00:01.009Z","outcome":"success","httpStatus":200,"error":null}
    ]}
  ]
}
```

When a subscription has a `channel`, every matching write still records its
event and additionally enqueues a delivery task in the same commit; the write
response never waits for the webhook. A background worker POSTs the event JSON
(the exact payload shown under `events`) to the endpoint with headers:

- `X-FhirVault-Subscription` — the subscription id;
- `X-FhirVault-Sequence` — the event sequence number;
- `X-FhirVault-Delivery` — a unique delivery id;
- `X-FhirVault-Signature` — only when a `secret` is set: the lowercase hex
  HMAC-SHA256 of the raw request body bytes, keyed by the secret.

Deliveries are listed by ascending `sequence`. Each delivery is `pending`
until it finishes, `delivered` after any attempt succeeds (any 2xx status), or
`failed` after three attempts have all failed (non-2xx status or a transport
error). Failed attempts are retried 1 second after the first failure and 2
seconds after the second, measured from the end of the previous attempt. Every
attempt is logged with its `outcome` (`success`/`failure`), the response
`httpStatus` (or `null` for transport errors), and an `error` reason (or
`null`). Retries reuse the same delivery id, sequence, and signature so
receivers can deduplicate uncertain redeliveries. Delivery tasks are persisted:
restarting the service resumes pending deliveries, and querying the endpoint
never triggers a delivery. A missing subscription answers HTTP 404
`not_found`.

### Access audit

Every completed request against a resource, Bundle, or Subscription HTTP entry
point records one audit event. `GET /health`, `GET /audit` itself, and the
background webhook retries are not recorded. The actor is taken from the
`X-FhirVault-Actor` request header; when the header is missing or blank the
actor is recorded as `anonymous`. Audit events never contain request bodies,
query values, subscription secrets, webhook signatures, or idempotency keys.

```http
GET /audit?actor=alice&action=create&outcome=success&resourceType=Patient&resourceId=p-1
```

```json
{
  "total": 1,
  "count": 1,
  "offset": 0,
  "sort": "sequence",
  "entry": [
    {
      "sequence": 1,
      "occurredAt": "2026-01-05T09:00:00.010Z",
      "actor": "alice",
      "action": "create",
      "outcome": "success",
      "status": 201,
      "resourceType": "Patient",
      "resourceId": "p-1",
      "version": 1,
      "replayed": false,
      "changes": []
    }
  ]
}
```

- `sequence` is globally unique, strictly increasing, and never reused after a
  restart. Entries are returned in ascending `sequence` by default.
- `action` is the public operation: `create`, `read`, `update`, `delete`,
  `search`, `history`, `transaction`, `create-subscription`, `events`, or
  `deliveries`.
- `outcome` is `success` or `failure`; `status` is the actual HTTP status.
  `resourceType`, `resourceId`, and `version` are `null` when the request has
  no single resource or version (for example a search or a transaction).
- A successful transaction records one `transaction` event whose `changes`
  list follows entry order, each item giving `method`, `resourceType`, `id`,
  `version`, and `deleted`. A failed transaction rolls back and is then
  recorded as a `failure` event with an empty `changes`.
- A successful write's audit event commits atomically with its resource
  versions, subscription events, delivery tasks, and transaction result. A
  failed request is rolled back first and then recorded independently.
- Replaying an idempotent request still records a new event with
  `"replayed": true`, but creates no new version, subscription event, delivery
  task, or change, and its `changes` is empty.

The query supports exact filters `actor`, `action`, `outcome`,
`resourceType`, and `resourceId` (repeated values of one filter combine with
OR; different filters combine with AND), plus `_count` (0–1000), `_offset`,
and `_sort=sequence` (default) or `-sequence`. Unknown parameters, an
out-of-range or non-integer page, or a bad sort fail the whole request with
HTTP 400 `validation_error` and return no partial results. The audit log is
durable: events recorded before a restart stay queryable afterwards.

### Field reference

Every resource accepts exactly `resourceType` (optional), `id`, `meta`, and the
fields below. `meta` is server-managed: any client value is replaced by
`{"versionId": ..., "lastUpdated": ...}`. Anything else is a `validation_error`.

`Patient` — all fields optional:

| field | type |
| --- | --- |
| `active` | boolean |
| `gender` | `male`, `female`, `other`, `unknown` |
| `birthDate` | `YYYY-MM-DD` |
| `name.family`, `name.given` | non-empty string, prefix searchable |
| `identifier` | non-empty array of `{"system": "...", "value": "..."}` |

`Observation`:

| field | type |
| --- | --- |
| `status` | required: `registered`, `preliminary`, `final`, `amended`, `corrected`, `cancelled`, `entered-in-error`, `unknown` |
| `code.text` | required: non-empty string, prefix searchable |
| `subject` | reference, `Patient/...` |
| `encounter` | reference, `Encounter/...` |
| `hasMember` | reference, `Observation/...` |
| `effectiveDateTime` | RFC 3339 instant, prefix searchable |
| `value` | number or string |

`Encounter`:

| field | type |
| --- | --- |
| `status` | required: `planned`, `arrived`, `triaged`, `in-progress`, `onleave`, `finished`, `cancelled`, `entered-in-error`, `unknown` |
| `class.code` | required: non-empty string, prefix searchable |
| `subject` | required reference, `Patient/...` |
| `period.start`, `period.end` | RFC 3339 instants, prefix searchable; `end` must not precede `start` |
| `reason.text` | non-empty string, prefix searchable |
| `partOf` | reference, `Encounter/...` |

Identifiers are validated on write: `identifier` must be unique within the
resource, and a reference by identifier is rejected as ambiguous when more than
one live resource matches `system|value`.

### References

A reference is exactly `{"reference": "<type>/<id-or-identifier>"}`:

- `<type>/<id>` — for example `Patient/p-1`; the target resource must exist and
  must not be deleted, otherwise the write fails with `validation_error` 400;
- `<type>/identifier|<system>|<value>` — resolves a single live resource by one
  of its identifiers; no match, or more than one match, is a 400.

The referenced type must be one of the three resource types, so a reference can
never point at a resource type the vault does not store.

## Errors

```json
{"error":{"code":"validation_error","message":"Observation.subject references Patient/p-404, which does not exist"}}
```

| code | status | meaning |
| --- | --- | --- |
| `validation_error` | 400 | malformed body, unknown field, type or code violation, unresolved/ambiguous reference, missing idempotency key, unknown search parameter |
| `not_found` | 404 | unknown resource id, unknown resource type, unknown route, unknown subscription |
| `conflict` | 409 | duplicate create, or an idempotency key reused for another operation |
| `internal_error` | 500 | unexpected failure |

Failed conditional updates are reported instead as an `application/fhir+json`
`OperationOutcome`: HTTP 400 with `issue.code` `invalid` for a malformed
`If-Match`, HTTP 404 with `not-found` when the target resource does not
exist, and HTTP 412 with `conflict` when the provided version is not the
current version. Every failure of `POST /fhir` is reported the same way, with
`issue.code` `invalid`, `not-found`, or `conflict` as described above.

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```
