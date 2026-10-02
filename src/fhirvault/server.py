from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import FhirVaultError, NotFoundError, OperationOutcomeError, ValidationError
from .service import FhirVault, etag_for


def make_handler(service: FhirVault) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _json(
            self,
            status: int,
            value: Any,
            *,
            content_type: str = "application/json; charset=utf-8",
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for name, header_value in (extra_headers or {}).items():
                self.send_header(name, header_value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def _operation_outcome(error: OperationOutcomeError) -> dict[str, Any]:
            return {
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": error.severity,
                        "code": error.issue_code,
                        "diagnostics": error.diagnostics,
                    }
                ],
            }

        def _resource_headers(
            self,
            resource_type: str,
            resource_id: str,
            document: dict[str, Any],
            *,
            with_location: bool = False,
        ) -> dict[str, str]:
            headers = {"ETag": etag_for(document["meta"]["versionId"])}
            if with_location:
                host = self.headers.get("Host") or f"{self.server.server_address[0]}:{self.server.server_address[1]}"
                headers["Location"] = f"http://{host}/fhir/{resource_type}/{resource_id}"
            return headers

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

        def _dispatch(self) -> tuple[int, Any, dict[str, str]]:
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
                raise NotFoundError("route was not found")
            raise NotFoundError("route was not found")

        def _resource_routes(
            self, parts: list[str], query: dict[str, list[str]], key: str | None
        ) -> tuple[int, Any, dict[str, str]]:
            if not parts:
                raise NotFoundError("route was not found")
            resource_type = parts[0]
            if resource_type == "Subscription":
                if self.command == "POST" and len(parts) == 1:
                    return 201, service.create_subscription(self._body(), key), {}
                raise NotFoundError("route was not found")
            if len(parts) == 1:
                if self.command == "POST":
                    return 201, service.create(resource_type, self._body(), key), {}
                if self.command == "GET":
                    return 200, service.search(resource_type, query), {}
                raise NotFoundError("route was not found")
            if len(parts) == 2:
                resource_id = parts[1]
                if self.command == "GET":
                    document = service.read(resource_type, resource_id)
                    return 200, document, self._resource_headers(resource_type, resource_id, document)
                if self.command == "PUT":
                    if_match = self.headers.get("If-Match")
                    document = service.update(
                        resource_type, resource_id, self._body(), key, if_match=if_match
                    )
                    return (
                        200,
                        document,
                        self._resource_headers(resource_type, resource_id, document, with_location=True),
                    )
                if self.command == "DELETE":
                    return 200, service.delete(resource_type, resource_id, key), {}
                raise NotFoundError("route was not found")
            if len(parts) == 3 and parts[2] == "_history" and self.command == "GET":
                return 200, service.history(resource_type, parts[1]), {}
            raise NotFoundError("route was not found")

        def _handle(self) -> None:
            try:
                status, response, headers = self._dispatch()
                self._json(status, response, extra_headers=headers)
            except OperationOutcomeError as error:
                self._json(
                    error.status,
                    self._operation_outcome(error),
                    content_type="application/fhir+json",
                )
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
    server.serve_forever()


if __name__ == "__main__":
    main()
