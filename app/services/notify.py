"""Notifications: Home Assistant webhook, ntfy as fallback (SPEC §2)."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Household

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    url: str | None = None


class Notifier(Protocol):
    async def send(self, message: Message) -> bool: ...


# Which notifier to use for a household (by id). Jobs get one of these, so each household's
# own channels from Instellingen are respected.
NotifierFor = Callable[[int], Notifier]


class WebhookNotifier:
    """Sends to Home Assistant and/or ntfy. Explicit URLs (from the household's settings)
    win over the ones in .env."""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        ha_url: str | None = None,
        ntfy_url: str | None = None,
    ):
        self._ha = ha_url or settings.ha_webhook_url
        self._ntfy = ntfy_url or settings.ntfy_url
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._ha or self._ntfy)

    async def send(self, message: Message) -> bool:
        """True if at least one channel accepted it. Never raises: notifying must not crash jobs."""
        async with httpx.AsyncClient(timeout=10, transport=self._transport) as http:
            if self._ha:
                try:
                    r = await http.post(
                        self._ha,
                        json={"title": message.title, "message": message.body, "url": message.url},
                    )
                    if r.is_success:
                        return True
                    log.warning("HA webhook returned %s", r.status_code)
                except httpx.HTTPError as e:
                    log.warning("HA webhook failed: %s", e)
            if self._ntfy:
                try:
                    # HTTP headers must be ASCII; the body carries the full UTF-8 text.
                    headers = {"Title": message.title.encode("ascii", "replace").decode()}
                    if message.url:
                        headers["Click"] = message.url
                    r = await http.post(self._ntfy, content=message.body.encode(), headers=headers)
                    if r.is_success:
                        return True
                    log.warning("ntfy returned %s", r.status_code)
                except httpx.HTTPError as e:
                    log.warning("ntfy failed: %s", e)
        log.warning("notification not delivered: %s", message.title)
        return False


def household_notifiers(sessions: sessionmaker[Session], settings: Settings) -> NotifierFor:
    """Look the household up at send time, so changed settings apply without a restart."""

    def get(household_id: int) -> Notifier:
        with sessions() as s:
            return notifier_for(s.get(Household, household_id), settings)

    return get


def notifier_for(
    household: Household | None,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> WebhookNotifier:
    """The household's own channels (Instellingen → Meldingen), else those in .env."""
    s: dict[str, Any] = (household.settings_json if household else None) or {}
    return WebhookNotifier(
        settings,
        transport,
        ha_url=str(s.get("ha_webhook_url") or "") or None,
        ntfy_url=str(s.get("ntfy_url") or "") or None,
    )
