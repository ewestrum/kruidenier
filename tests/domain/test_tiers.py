import pytest

from app.domain.consumption import Confidence
from app.domain.planning import Reason
from app.domain.tiers import Tier, assign_tier, usual_packs


def test_usual_packs_is_median() -> None:
    assert usual_packs([2000, 2000, 6000, 2000], 1000) == 2
    assert usual_packs([], 1000) is None
    assert usual_packs([1000], 0) is None


@pytest.mark.parametrize(
    ("reason", "confidence", "packs", "usual", "tier"),
    [
        (Reason.DUE, Confidence.HIGH, 2, 2.0, Tier.AUTO),
        (Reason.DUE, Confidence.HIGH, 3, 2.0, Tier.AUTO),  # ceil(2 * 1.5) = 3
        (Reason.DUE, Confidence.HIGH, 4, 2.0, Tier.PROPOSE),  # unusually many
        (Reason.DUE, Confidence.HIGH, 1, 0.5, Tier.AUTO),
        (Reason.DUE, Confidence.MEDIUM, 2, 2.0, Tier.PROPOSE),
        (Reason.DUE, Confidence.LOW, 2, 2.0, Tier.PROPOSE),
        (Reason.PINNED_DUE, Confidence.HIGH, 2, 2.0, Tier.PROPOSE),
        (Reason.DUE, Confidence.HIGH, 2, None, Tier.PROPOSE),
    ],
)
def test_assign_tier(
    reason: Reason, confidence: Confidence, packs: int, usual: float | None, tier: Tier
) -> None:
    assert assign_tier(reason=reason, confidence=confidence, packs=packs, usual=usual) is tier
