"""Local users (SPEC §3): password hashing with stdlib scrypt, no extra dependency."""

import base64
import hashlib
import hmac
import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Household, User

_N, _R, _P = 2**14, 8, 1
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(digest_b64)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=len(expected))
    return hmac.compare_digest(digest, expected)


def normalise_email(email: str) -> str:
    return email.strip().lower()


def has_users(session: Session) -> bool:
    return bool(session.scalar(select(func.count()).select_from(User)))


def authenticate(session: Session, email: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.email == normalise_email(email)))
    if user is None:
        verify_password(password, hash_password("timing-equaliser"))
        return None
    return user if verify_password(password, user.pw_hash) else None


class UserError(ValueError):
    """Shown to the user as-is (Dutch)."""


def create_user(
    session: Session, *, household: Household, email: str, password: str, role: str
) -> User:
    email = normalise_email(email)
    if "@" not in email:
        raise UserError("Vul een geldig e-mailadres in.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"Kies een wachtwoord van minstens {MIN_PASSWORD_LENGTH} tekens.")
    if role not in {"admin", "member"}:
        raise UserError("Onbekende rol.")
    if session.scalar(select(User).where(User.email == email)):
        raise UserError("Er bestaat al een gebruiker met dit e-mailadres.")
    user = User(household_id=household.id, email=email, pw_hash=hash_password(password), role=role)
    session.add(user)
    session.flush()
    return user
