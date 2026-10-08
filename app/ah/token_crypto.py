"""Fernet encryption of AH tokens at rest (CLAUDE.md rule 5). The key comes from env."""

from cryptography.fernet import Fernet, InvalidToken

from app.ah.errors import AhAuthError
from app.ah.models import Tokens


class TokenCipher:
    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("FERNET_KEY is not set")
        self._fernet = Fernet(key.encode())

    def encrypt(self, tokens: Tokens) -> bytes:
        return self._fernet.encrypt(tokens.model_dump_json().encode())

    def decrypt(self, blob: bytes) -> Tokens:
        try:
            raw = self._fernet.decrypt(blob)
        except InvalidToken as e:
            # Lost or rotated FERNET_KEY: the account has to be linked again (SPEC §15).
            raise AhAuthError("cannot decrypt AH tokens; relink the AH account") from e
        return Tokens.model_validate_json(raw)
