"""Heuristic family suggestion (SPEC §6.7): same core title and base unit -> same family.

Deliberately conservative. Merging is easy in the UI; wrongly merged families would
silently distort consumption.
"""

import re

_PACK_WORDS = re.compile(
    r"\b(grootverpakking|kleinverpakking|voordeelverpakking|voordeelpak|multipack|"
    r"\d+\s*-?\s*pack|\d+\s*stuks?|\d+\s*st|per\s+stuk)\b"
)
_SIZE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:g|gr|gram|kg|ml|cl|l|liter)\b")
_NON_WORD = re.compile(r"[^\w\s]")


def family_key(title: str, brand: str | None = None) -> str:
    """Normalised core title: lower-case, without brand, 'AH', sizes and pack words."""
    s = title.lower()
    if brand:
        s = s.replace(brand.lower(), " ")
    s = re.sub(r"^\s*ah\b", " ", s)
    s = _SIZE.sub(" ", s)
    s = _PACK_WORDS.sub(" ", s)
    s = _NON_WORD.sub(" ", s)
    return " ".join(s.split())


def family_name(key: str) -> str:
    return key[:1].upper() + key[1:] if key else "Onbekend product"
