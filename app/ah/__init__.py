"""AH adapter: the only package in the codebase that knows about the AH API.

Everything outside `app.ah` talks to AH through the `AhClient` protocol.
"""

from app.ah.errors import (
    AhAuthError,
    AhError,
    AhGraphqlError,
    AhHttpError,
    AhSchemaError,
    ForbiddenEndpointError,
)
from app.ah.protocol import AhClient

__all__ = [
    "AhAuthError",
    "AhClient",
    "AhError",
    "AhGraphqlError",
    "AhHttpError",
    "AhSchemaError",
    "ForbiddenEndpointError",
]
