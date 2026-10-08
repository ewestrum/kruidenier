"""What a feedback button does to a family and its plan line (SPEC §6.8). Pure rules."""

from dataclasses import dataclass
from enum import StrEnum

MORE_FACTOR = 1.15
LESS_FACTOR = 1 / MORE_FACTOR  # "more" then "less" returns to the same rate
MIN_CORRECTION = 0.25
MAX_CORRECTION = 4.0


class FeedbackKind(StrEnum):
    OK = "ok"
    MORE = "more"
    LESS = "less"
    ENOUGH_STOCK = "enough_stock"
    NOT_ANYMORE = "not_anymore"


@dataclass(frozen=True)
class FamilyState:
    correction: float
    carryover: float
    excluded: bool


@dataclass(frozen=True)
class FeedbackEffect:
    family: FamilyState
    line_qty: int  # 0 means: remove the line from the draft


def apply_feedback(
    kind: FeedbackKind,
    state: FamilyState,
    *,
    line_qty: int,
    pack_size: float,
    enough_stock_extra: float | None = None,
) -> FeedbackEffect:
    """New family state and plan-line quantity after a feedback button.

    `enough_stock_extra` is the stock (base units) needed to get past the next planning
    horizon; the caller derives it from the consumption model. Without it we fall back to
    the line itself.
    """
    match kind:
        case FeedbackKind.OK:
            return FeedbackEffect(state, line_qty)
        case FeedbackKind.MORE:
            corr = min(state.correction * MORE_FACTOR, MAX_CORRECTION)
            return FeedbackEffect(FamilyState(corr, state.carryover, state.excluded), line_qty + 1)
        case FeedbackKind.LESS:
            corr = max(state.correction * LESS_FACTOR, MIN_CORRECTION)
            return FeedbackEffect(
                FamilyState(corr, state.carryover, state.excluded), max(line_qty - 1, 0)
            )
        case FeedbackKind.ENOUGH_STOCK:
            # "Still enough": at least what we would have ordered is still in the cupboard.
            extra = max(line_qty, 1) * max(pack_size, 0.0)
            if enough_stock_extra is not None:
                extra = max(extra, enough_stock_extra)
            return FeedbackEffect(
                FamilyState(state.correction, state.carryover + extra, state.excluded), 0
            )
        case FeedbackKind.NOT_ANYMORE:
            return FeedbackEffect(FamilyState(state.correction, state.carryover, True), 0)
