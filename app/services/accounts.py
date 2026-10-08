"""Linking AH accounts and building clients with encrypted, self-refreshing tokens."""

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ah.client import HttpAhClient
from app.ah.models import Tokens
from app.ah.ratelimit import RateLimiter
from app.ah.token_crypto import TokenCipher
from app.config import Settings
from app.db.models import AhAccount, Household

# One limiter per process: every client shares it, so there is never parallel fan-out
# to AH even with several households or accounts (CLAUDE.md rule 3).
_LIMITER: RateLimiter | None = None


def shared_limiter(settings: Settings) -> RateLimiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = RateLimiter(max(settings.ah_min_request_interval, 1.0))
    return _LIMITER


def get_or_create_household(session: Session, name: str) -> Household:
    household = session.scalar(select(Household).where(Household.name == name))
    if household is None:
        household = Household(name=name, settings_json={})
        session.add(household)
        session.flush()
    return household


def save_account(
    session: Session,
    *,
    household: Household,
    label: str,
    tokens: Tokens,
    cipher: TokenCipher,
    is_order_account: bool = True,
) -> AhAccount:
    """Create or update the account with this label in the household."""
    account = session.scalar(
        select(AhAccount).where(AhAccount.household_id == household.id, AhAccount.label == label)
    )
    if account is None:
        account = AhAccount(household_id=household.id, label=label, tokens_enc=b"")
        session.add(account)
    account.tokens_enc = cipher.encrypt(tokens)
    account.is_order_account = is_order_account
    session.flush()
    return account


def client_for(
    account: AhAccount,
    *,
    settings: Settings,
    cipher: TokenCipher,
    sessions: sessionmaker[Session],
) -> HttpAhClient:
    """A client whose refreshed tokens are written back to the database immediately."""
    account_id = account.id

    async def persist(tokens: Tokens) -> None:
        with sessions.begin() as s:
            row = s.get(AhAccount, account_id)
            if row is not None:
                row.tokens_enc = cipher.encrypt(tokens)

    return HttpAhClient(
        client_id=settings.ah_client_id,
        client_version=settings.ah_client_version,
        tokens=cipher.decrypt(account.tokens_enc),
        on_tokens_refreshed=persist,
        limiter=shared_limiter(settings),
    )
