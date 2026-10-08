"""Tier per plan line (SPEC §9): what the autopilot may do on its own.

`auto_bonus` follows in fase 3 (bonus assessment); until then bonus lines are plain lines.
"""

import math
import statistics
from collections.abc import Sequence
from enum import StrEnum

from app.domain.consumption import Confidence
from app.domain.planning import Reason

NORMAL_FACTOR = 1.5  # up to 1.5x the usual quantity still counts as "normal"


class Tier(StrEnum):
    AUTO = "auto"
    AUTO_BONUS = "auto_bonus"
    PROPOSE = "propose"


def usual_packs(purchase_amounts: Sequence[float], pack_size: float) -> float | None:
    """Median number of packs per purchase, or None without history."""
    if not purchase_amounts or pack_size <= 0:
        return None
    return statistics.median(a / pack_size for a in purchase_amounts)


def assign_tier(*, reason: Reason, confidence: Confidence, packs: int, usual: float | None) -> Tier:
    """Only regular, high-confidence staples in a normal quantity may go in automatically.

    Pinned-only, manual, medium/low confidence and unusually large lines stay proposals:
    when in doubt, propose (CLAUDE.md).
    """
    if reason is not Reason.DUE or confidence is not Confidence.HIGH:
        return Tier.PROPOSE
    if usual is None or packs > math.ceil(max(usual, 1.0) * NORMAL_FACTOR):
        return Tier.PROPOSE
    return Tier.AUTO
