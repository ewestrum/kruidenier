"""Contract tests: every recorded fixture (from `make probe`) must validate against its model.

When AH changes a response shape, re-running the probe makes these fail, which is
exactly the "adapter broken" signal we want before any autopilot action.
"""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from app.ah.models import (
    ActiveOrderSummary,
    BonusMetadata,
    FulfillmentsData,
    OrderDetails,
    Product,
    ProductDetailResponse,
    SearchResponse,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ah"

MODELS: dict[str, TypeAdapter[object]] = {
    name: TypeAdapter(tp)
    for name, tp in {
        "product.search": SearchResponse,
        "product.by_ids": list[Product],
        "product.detail": ProductDetailResponse,
        "order.active_summary": ActiveOrderSummary,
        "order.set_items": ActiveOrderSummary,
        "order.details": OrderDetails,
        "bonus.metadata": BonusMetadata,
        "graphql.OrderFulfillments": FulfillmentsData,
        "graphql.OrderFulfillmentsAll": FulfillmentsData,
        "graphql.OrderFulfillmentsClosed": FulfillmentsData,
    }.items()
}


def _cases(folder: Path) -> list[tuple[Path, TypeAdapter[object]]]:
    cases = []
    for path in sorted(folder.glob("*.json")):
        base = path.stem.split(".")
        name = ".".join(base[:2])
        if name in MODELS:
            cases.append((path, MODELS[name]))
    return cases


def _payload(path: Path) -> object:
    data = json.loads(path.read_text(encoding="utf-8"))
    if path.stem.startswith("graphql.") and isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


@pytest.mark.parametrize(
    ("path", "model"), _cases(FIXTURES / "synthetic"), ids=lambda v: getattr(v, "name", "")
)
def test_synthetic_fixture_validates(path: Path, model: TypeAdapter[object]) -> None:
    model.validate_python(_payload(path))


RECORDED = _cases(FIXTURES)


@pytest.mark.skipif(not RECORDED, reason="no recorded fixtures yet: run `make probe` (fase 0)")
@pytest.mark.parametrize(("path", "model"), RECORDED, ids=lambda v: getattr(v, "name", ""))
def test_recorded_fixture_validates(path: Path, model: TypeAdapter[object]) -> None:
    model.validate_python(_payload(path))
