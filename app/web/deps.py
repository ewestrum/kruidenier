"""Request dependencies: database, current user, CSRF, templates."""

import secrets
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, Request, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import User
from app.db.session import session_factory

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

MONTHS = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
MONTHS_LONG = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]  # fmt: skip
DAYS = ["ma", "di", "wo", "do", "vr", "za", "zo"]
DAYS_LONG = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def euro(value: float | None) -> str:
    if value is None:
        return ""
    return "€ " + f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def day(value: date | None, long: bool = False) -> str:
    if value is None:
        return ""
    if long:
        return f"{DAYS_LONG[value.weekday()]} {value.day} {MONTHS_LONG[value.month - 1]}"
    return f"{DAYS[value.weekday()]} {value.day} {MONTHS[value.month - 1]}"


def moment(value: datetime | None) -> str:
    """UTC timestamp shown in the household's time zone."""
    if value is None:
        return ""
    local = value.astimezone(ZoneInfo(get_settings().tz))
    return f"{day(local.date())} {local:%H:%M}"


TEMPLATES.env.filters.update(euro=euro, day=day, moment=moment)


def get_sessions() -> sessionmaker[Session]:
    return session_factory()


def get_db(sessions: Annotated[sessionmaker[Session], Depends(get_sessions)]) -> Iterator[Session]:
    """One transaction per request: commit on success, roll back on error."""
    with sessions.begin() as session:
        yield session


Db = Annotated[Session, Depends(get_db)]


class LoginRequired(Exception):
    """Raised by `current_user`; turned into a redirect by the app."""


class Forbidden(Exception):
    pass


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return str(token)


async def check_csrf(request: Request) -> None:
    """Every state-changing request carries the session token (form field or htmx header)."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        form = await request.form()
        sent = str(form.get("csrf", "")) or None
    if not expected or not sent or not secrets.compare_digest(str(expected), sent):
        raise Forbidden("csrf")


def current_user(request: Request, db: Db) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None:
        request.session.pop("uid", None)
        raise LoginRequired
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def admin_user(user: CurrentUser) -> User:
    if user.role != "admin":
        raise Forbidden("admin")
    return user


AdminUser = Annotated[User, Depends(admin_user)]


def render(request: Request, template: str, user: User | None = None, **ctx: Any) -> Response:
    return TEMPLATES.TemplateResponse(
        request,
        template,
        {"user": user, "csrf": csrf_token(request), **ctx},
    )
