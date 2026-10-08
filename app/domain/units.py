"""Parse AH `salesUnitSize` strings into an amount in a base unit (SPEC §6).

Base units: grams (`g`), millilitres (`ml`) and pieces (`st`). Families are modelled in
one base unit; kg/l are only a display concern.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class BaseUnit(StrEnum):
    GRAM = "g"
    MILLILITRE = "ml"
    PIECE = "st"


@dataclass(frozen=True)
class UnitSize:
    amount: float
    unit: BaseUnit
    approximate: bool = False


_FACTORS: dict[str, tuple[float, BaseUnit]] = {
    "mg": (0.001, BaseUnit.GRAM),
    "g": (1.0, BaseUnit.GRAM),
    "gr": (1.0, BaseUnit.GRAM),
    "gram": (1.0, BaseUnit.GRAM),
    "kg": (1000.0, BaseUnit.GRAM),
    "kilo": (1000.0, BaseUnit.GRAM),
    "ml": (1.0, BaseUnit.MILLILITRE),
    "cl": (10.0, BaseUnit.MILLILITRE),
    "dl": (100.0, BaseUnit.MILLILITRE),
    "l": (1000.0, BaseUnit.MILLILITRE),
    "liter": (1000.0, BaseUnit.MILLILITRE),
    "lt": (1000.0, BaseUnit.MILLILITRE),
    "st": (1.0, BaseUnit.PIECE),
    "stuk": (1.0, BaseUnit.PIECE),
    "stuks": (1.0, BaseUnit.PIECE),
    "rol": (1.0, BaseUnit.PIECE),
    "rollen": (1.0, BaseUnit.PIECE),
    "tabletten": (1.0, BaseUnit.PIECE),
    "tabs": (1.0, BaseUnit.PIECE),
    "zakjes": (1.0, BaseUnit.PIECE),
    "plakken": (1.0, BaseUnit.PIECE),
    "wasbeurten": (1.0, BaseUnit.PIECE),
}

_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT = r"([a-z]+)"
# "10 x 25 g", "4x1,5 l", "2 x 6 stuks"
_MULTI = re.compile(rf"^{_NUM}\s*x\s*{_NUM}\s*{_UNIT}$")
# "0,58 l", "250 g", "6 stuks", "per 500 g"
_SINGLE = re.compile(rf"^(?:per\s+)?{_NUM}\s*{_UNIT}$")
_PREFIX_APPROX = re.compile(r"^(ca\.?|circa|ongeveer)\s+")


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def parse_unit_size(text: str | None) -> UnitSize | None:
    """Return the size of one sales unit, or None when it cannot be interpreted.

    Unparseable values such as "Tros" or "per stuk" without a number fall back to
    None; callers then count the product in pieces of 1.
    """
    if not text:
        return None
    s = text.strip().lower()
    approximate = False
    if m := _PREFIX_APPROX.match(s):
        approximate = True
        s = s[m.end() :]
    s = re.sub(r"\s+", " ", s)
    if s in {"per stuk", "stuk", "1 stuk"}:
        return UnitSize(1.0, BaseUnit.PIECE, approximate)

    if m := _MULTI.match(s):
        count, size, unit = _num(m.group(1)), _num(m.group(2)), m.group(3)
    elif m := _SINGLE.match(s):
        count, size, unit = 1.0, _num(m.group(1)), m.group(2)
    else:
        return None
    if unit not in _FACTORS:
        return None
    factor, base = _FACTORS[unit]
    amount = count * size * factor
    if amount <= 0:
        return None
    return UnitSize(amount, base, approximate)
