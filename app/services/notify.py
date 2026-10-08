"""Notifications: Home Assistant webhook, ntfy as fallback (SPEC §2)."""

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    url: str | None = None


class Notifier(Protocol):
    async def send(self, message: Message) -> bool: ...


class WebhookNotifier:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._ha = settings.ha_webhook_url
        self._ntfy = settings.ntfy_url
        self._transport = transport

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
