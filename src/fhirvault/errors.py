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
    """An error reported as a FHIR OperationOutcome with application/fhir+json."""

    def __init__(self, status: int, issue_code: str, diagnostics: str):
        super().__init__(diagnostics)
        self.status = status
        self.code = issue_code
        self.issue_code = issue_code
