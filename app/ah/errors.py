class AhError(Exception):
    """Base class for everything that goes wrong in the AH adapter."""


class ForbiddenEndpointError(AhError):
    """A call was attempted to an endpoint that is not on the allowlist.

    This is a programming error, never a runtime condition to recover from.
    """


class AhHttpError(AhError):
    def __init__(self, endpoint: str, status_code: int, body: str) -> None:
        super().__init__(f"{endpoint}: HTTP {status_code}: {body[:300]}")
        self.endpoint = endpoint
        self.status_code = status_code
        self.body = body


class AhGraphqlError(AhError):
    def __init__(self, operation: str, errors: object) -> None:
        super().__init__(f"graphql {operation}: {errors!r}"[:500])
        self.operation = operation
        self.errors = errors


class AhAuthError(AhError):
    """Tokens are missing, expired beyond refresh, or rejected."""


class AhSchemaError(AhError):
    """A response did not validate against its pydantic model.

    Callers must treat this as "adapter broken": log, notify, take no action.
    """

    def __init__(self, endpoint: str, detail: str) -> None:
        super().__init__(f"{endpoint}: response failed validation: {detail}")
        self.endpoint = endpoint
        self.detail = detail
