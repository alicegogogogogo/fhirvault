class FhirVaultError(Exception):
    code = "internal_error"
    status = 500


class ValidationError(FhirVaultError):
    code = "validation_error"
    status = 400


class NotFoundError(FhirVaultError):
    code = "not_found"
    status = 404


class ConflictError(FhirVaultError):
    code = "conflict"
    status = 409


class OperationOutcomeError(FhirVaultError):
    """An error rendered as a FHIR ``OperationOutcome`` (application/fhir+json)."""

    issue_code = "processing"
    severity = "error"

    def __init__(self, message: str = "", *, diagnostics: str | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or message


class InvalidPreconditionError(OperationOutcomeError):
    """A conditional header (e.g. If-Match) is syntactically invalid."""

    code = "invalid"
    issue_code = "invalid"
    status = 400


class PreconditionTargetMissingError(NotFoundError, OperationOutcomeError):
    """A conditional update names a resource that does not exist (HTTP 404)."""

    code = "not_found"
    issue_code = "not-found"
    status = 404

    def __init__(self, resource_type: str, resource_id: str) -> None:
        message = f"{resource_type}/{resource_id} was not found"
        super().__init__(message, diagnostics=message)
        self.resource_type = resource_type
        self.resource_id = resource_id


class PreconditionFailedError(OperationOutcomeError):
    """An If-Match version is no longer the current version (HTTP 412)."""

    code = "conflict"
    issue_code = "conflict"
    status = 412

    def __init__(self, resource_type: str, resource_id: str, provided: str, current: str) -> None:
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.provided = provided
        self.current = current
        message = (
            f"If-Match version {provided} is not the current version of "
            f"{resource_type}/{resource_id}; current version is {current}"
        )
        super().__init__(
            message,
            diagnostics=(
                f"the version supplied in If-Match ({provided}) is not the current version of "
                f"{resource_type}/{resource_id}; current version is {current}"
            ),
        )
