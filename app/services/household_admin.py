"""Household settings, holidays and members (SPEC §10 "Instellingen", §11)."""

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AhAccount, Household, Pause, User


class SettingsError(ValueError):
    """Shown to the user as-is (Dutch)."""


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    help: str
    default: float
    minimum: float
    maximum: float
    integer: bool = True


FIELDS: tuple[SettingField, ...] = (
    SettingField(
        "cadence_days", "Dagen tussen leveringen", "Meestal 7 bij wekelijks bezorgen.", 7, 1, 31
    ),
    SettingField(
        "margin_days",
        "Veiligheidsmarge in dagen",
        "Zoveel dagen extra voorraad houdt Kruidenier aan.",
        2,
        0,
        14,
    ),
    SettingField(
        "half_life_days",
        "Geheugen van het verbruik in dagen",
        "Na zoveel dagen telt een oude aankoop nog maar half mee.",
        60,
        7,
        365,
    ),
)


def settings_values(household: Household) -> dict[str, float]:
    s: dict[str, Any] = household.settings_json or {}
    return {f.key: s.get(f.key, f.default) for f in FIELDS}


def update_settings(household: Household, form: dict[str, str]) -> None:
    new = dict(household.settings_json or {})
    for f in FIELDS:
        raw = form.get(f.key, "").strip().replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            raise SettingsError(f"'{f.label}' moet een getal zijn.") from None
        if not f.minimum <= value <= f.maximum:
            raise SettingsError(f"'{f.label}' moet tussen {f.minimum:g} en {f.maximum:g} liggen.")
        new[f.key] = int(value) if f.integer else value
    household.settings_json = new  # reassign so the JSON change is persisted


def list_pauses(session: Session, household_id: int) -> list[Pause]:
    return list(
        session.scalars(
            select(Pause).where(Pause.household_id == household_id).order_by(Pause.start)
        )
    )


def add_pause(session: Session, household_id: int, start: date, end: date) -> Pause:
    if end < start:
        raise SettingsError("De einddatum ligt vóór de begindatum.")
    pause = Pause(household_id=household_id, start=start, end=end)
    session.add(pause)
    session.flush()
    return pause


def delete_pause(session: Session, household_id: int, pause_id: int) -> None:
    pause = session.get(Pause, pause_id)
    if pause is None or pause.household_id != household_id:
        raise SettingsError("Deze vakantie bestaat niet (meer).")
    session.delete(pause)


def list_users(session: Session, household_id: int) -> list[User]:
    return list(
        session.scalars(select(User).where(User.household_id == household_id).order_by(User.email))
    )


def delete_user(session: Session, household_id: int, user_id: int, *, acting_user_id: int) -> None:
    user = session.get(User, user_id)
    if user is None or user.household_id != household_id:
        raise SettingsError("Deze gebruiker bestaat niet (meer).")
    if user.id == acting_user_id:
        raise SettingsError("Je kunt jezelf niet verwijderen.")
    session.delete(user)


def list_accounts(session: Session, household_id: int) -> list[AhAccount]:
    return list(
        session.scalars(
            select(AhAccount).where(AhAccount.household_id == household_id).order_by(AhAccount.id)
        )
    )
