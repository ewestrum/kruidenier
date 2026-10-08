import pytest

from app.domain.units import BaseUnit, UnitSize, parse_unit_size

G, ML, ST = BaseUnit.GRAM, BaseUnit.MILLILITRE, BaseUnit.PIECE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # all seen in fase-0 order fixtures
        ("0,58 l", UnitSize(580, ML)),
        ("0,6 l", UnitSize(600, ML)),
        ("1 kg", UnitSize(1000, G)),
        ("10 x 25 g", UnitSize(250, G)),
        ("100 g", UnitSize(100, G)),
        ("2 stuks", UnitSize(2, ST)),
        ("6 stuks", UnitSize(6, ST)),
        ("3 stuks", UnitSize(3, ST)),
        ("200 ml", UnitSize(200, ML)),
        ("2 l", UnitSize(2000, ML)),
        ("ca. 110 g", UnitSize(110, G, approximate=True)),
        # other common forms
        ("4x1,5 l", UnitSize(6000, ML)),
        ("per 500 g", UnitSize(500, G)),
        ("per stuk", UnitSize(1, ST)),
        ("75 cl", UnitSize(750, ML)),
        ("  250  G ", UnitSize(250, G)),
        ("24 rollen", UnitSize(24, ST)),
    ],
)
def test_parse(text: str, expected: UnitSize) -> None:
    result = parse_unit_size(text)
    assert result is not None
    assert result.amount == pytest.approx(expected.amount)
    assert result.unit == expected.unit
    assert result.approximate == expected.approximate


@pytest.mark.parametrize("text", [None, "", "Tros", "los", "0 g", "12 bananen", "abc g"])
def test_unparseable_is_none(text: str | None) -> None:
    assert parse_unit_size(text) is None
