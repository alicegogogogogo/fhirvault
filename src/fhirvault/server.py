from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import FhirVaultError, NotFoundError, OperationOutcomeError, ValidationError
from .service import FhirVault

Response = tuple[int, Any, dict[str, str]]


def _etag(document: dict[str, Any]) -> str:
    return f'W/"{document["meta"]["versionId"]}"'


def _read_headers(document: dict[str, Any]) -> dict[str, str]:
    return {"ETag": _etag(document)}


def _write_headers(resource_type: str, resource_id: str, document: dict[str, Any]) -> dict[str, str]:
    return {
        "ETag": _etag(document),
        "Location": f"/fhir/{resource_type}/{resource_id}",
    }


def make_handler(service: FhirVault) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _json(self, status: int, value: Any, headers: dict[str, str] | None = None, content_type: str = "application/json; charset=utf-8") -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for name, header_value in (headers or {}).items():
                self.send_header(name, header_value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Any:
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise ValidationError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 1_000_000:
                    raise ValueError
                return json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError) as error:
                raise ValidationError("request body must be valid JSON") from error

        def _segments(self) -> tuple[list[str], dict[str, list[str]]]:
            split = urlsplit(self.path)
            parts = [unquote(part) for part in split.path.split("/") if part]
            return parts, parse_qs(split.query, keep_blank_values=True)

        def _dispatch(self) -> Response:
            parts, query = self._segments()
            key = self.headers.get("Idempotency-Key")
            if self.command == "GET" and parts == ["health"]:
                return 200, {"status": "ok"}, {}
            if parts and parts[0] == "fhir":
                return self._resource_routes(parts[1:], query, key)
            if parts[:1] == ["Subscription"]:
                if self.command == "POST" and len(parts) == 1:
                    return 201, service.create_subscription(self._body(), key), {}
                raise NotFoundError("route was not found")
            if parts[:1] == ["subscriptions"]:
                if self.command == "GET" and len(parts) == 3 and parts[2] == "events":
                    return 200, service.events(parts[1]), {}
                if self.command == "GET" and len(parts) == 3 and parts[2] == "deliveries":
                    return 200, service.deliveries(parts[1]), {}
                raise NotFoundError("route was not found")
            raise NotFoundError("route was not found")

        def _resource_routes(self, parts: list[str], query: dict[str, list[str]], key: str | None) -> Response:
            if not parts:
                if self.command == "POST":
                    try:
                        bundle = self._body()
                    except ValidationError as error:
                        raise OperationOutcomeError(400, "invalid", str(error)) from error
                    return 200, service.transaction(bundle, key), {}
                raise NotFoundError("route was not found")
            resource_type = parts[0]
            if resource_type == "Subscription":
                if self.command == "POST" and len(parts) == 1:
                    return 201, service.create_subscription(self._body(), key), {}
                raise NotFoundError("route was not found")
            if len(parts) == 1:
                if self.command == "POST":
                    document = service.create(resource_type, self._body(), key)
                    return 201, document, _write_headers(resource_type, document["id"], document)
                if self.command == "GET":
                    return 200, service.search(resource_type, query), {}
                raise NotFoundError("route was not found")
            if len(parts) == 2:
                resource_id = parts[1]
                if self.command == "GET":
                    document = service.read(resource_type, resource_id)
                    return 200, document, _read_headers(document)
                if self.command == "PUT":
                    document = service.update(
                        resource_type, resource_id, self._body(), key, if_match=self.headers.get("If-Match")
                    )
                    return 200, document, _write_headers(resource_type, resource_id, document)
                if self.command == "DELETE":
                    return 200, service.delete(resource_type, resource_id, key), {}
                raise NotFoundError("route was not found")
            if len(parts) == 3 and parts[2] == "_history" and self.command == "GET":
                return 200, service.history(resource_type, parts[1]), {}
            raise NotFoundError("route was not found")

        def _handle(self) -> None:
            try:
                status, response, headers = self._dispatch()
                self._json(status, response, headers)
            except OperationOutcomeError as error:
                outcome = {
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {"severity": "error", "code": error.issue_code, "diagnostics": str(error)}
                    ],
                }
                self._json(error.status, outcome, content_type="application/fhir+json")
            except FhirVaultError as error:
                self._json(error.status, {"error": {"code": error.code, "message": str(error)}})
            except Exception:
                self._json(500, {"error": {"code": "internal_error", "message": "internal server error"}})

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_DELETE = _handle

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the FhirVault HTTP service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--database", default="fhirvault.db")
    arguments = parser.parse_args()
    service = FhirVault(arguments.database)
    server = ThreadingHTTPServer((arguments.host, arguments.port), make_handler(service))
    print(f"FhirVault listening on http://{arguments.host}:{arguments.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
