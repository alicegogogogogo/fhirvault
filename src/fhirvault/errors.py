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
