"""How good was a draft? Compare it with what was actually delivered (SPEC §13 fase 1:
"het concept overlapt ≥ 80% met wat je zelf zou bestellen").

Everything is per family. Only families the model could have known count as "missed":
a product bought for the first time is not a miss.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

TARGET = 0.8
WEEKS_NEEDED = 2


@dataclass(frozen=True)
class Overlap:
    hits: frozenset[int]  # proposed and delivered
    missed: frozenset[int]  # delivered, known to the model, not proposed
    extra: frozenset[int]  # proposed, not delivered

    @property
    def score(self) -> float | None:
        total = len(self.hits) + len(self.missed) + len(self.extra)
        return len(self.hits) / total if total else None


def compare(planned: Iterable[int], delivered: Iterable[int], known: Iterable[int]) -> Overlap:
    p, d, k = set(planned), set(delivered), set(known)
    return Overlap(
        hits=frozenset(p & d),
        missed=frozenset((d - p) & k),
        extra=frozenset(p - d),
    )


def ready_for_autopilot(scores: Sequence[float | None]) -> bool:
    """The last `WEEKS_NEEDED` evaluated deliveries (newest first) all reached the target."""
    recent = [s for s in scores if s is not None][:WEEKS_NEEDED]
    return len(recent) == WEEKS_NEEDED and all(s >= TARGET for s in recent)
