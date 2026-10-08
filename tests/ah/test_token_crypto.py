from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from app.ah.errors import AhAuthError
from app.ah.models import Tokens
from app.ah.token_crypto import TokenCipher

TOKENS = Tokens(access_token="a", refresh_token="r", expires_at=datetime(2026, 1, 1, tzinfo=UTC))


def test_roundtrip() -> None:
    cipher = TokenCipher(Fernet.generate_key().decode())
    blob = cipher.encrypt(TOKENS)
    assert b"refresh" not in blob
    assert cipher.decrypt(blob) == TOKENS


def test_wrong_key_requires_relink() -> None:
    blob = TokenCipher(Fernet.generate_key().decode()).encrypt(TOKENS)
    with pytest.raises(AhAuthError):
        TokenCipher(Fernet.generate_key().decode()).decrypt(blob)


def test_missing_key() -> None:
    with pytest.raises(ValueError):
        TokenCipher("")
