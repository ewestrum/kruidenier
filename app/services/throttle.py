"""Slow down password guessing: too many failed logins -> wait a while.

In-memory on purpose: there is one web process, and a restart forgetting the counters is
harmless. Keys are (e-mail, client address) and the client address on its own, so one
address cannot try many accounts either.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

WINDOW_SECONDS = 15 * 60
MAX_PER_ACCOUNT = 5
MAX_PER_ADDRESS = 20


@dataclass
class LoginThrottle:
    clock: Callable[[], float] = time.monotonic
    _failures: dict[str, deque[float]] = field(default_factory=dict)

    def _recent(self, key: str) -> deque[float]:
        q = self._failures.setdefault(key, deque())
        cutoff = self.clock() - WINDOW_SECONDS
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def _keys(self, email: str, address: str) -> tuple[str, str]:
        return f"acct:{email.strip().lower()}|{address}", f"addr:{address}"

    def wait_seconds(self, email: str, address: str) -> int:
        """0 if a login attempt is allowed now, else how long to wait."""
        acct, addr = self._keys(email, address)
        waits = []
        for key, limit in ((acct, MAX_PER_ACCOUNT), (addr, MAX_PER_ADDRESS)):
            q = self._recent(key)
            if len(q) >= limit:
                waits.append(int(q[0] + WINDOW_SECONDS - self.clock()) + 1)
        return max(waits, default=0)

    def failed(self, email: str, address: str) -> None:
        now = self.clock()
        for key in self._keys(email, address):
            self._recent(key).append(now)

    def succeeded(self, email: str, address: str) -> None:
        self._failures.pop(self._keys(email, address)[0], None)


LOGIN_THROTTLE = LoginThrottle()
