import pytest

from app.domain.families import family_key, family_name


@pytest.mark.parametrize(
    ("a", "brand_a", "b", "brand_b"),
    [
        (
            "AH Andijvie fijngesneden grootverpakking",
            "AH",
            "AH Andijvie fijngesneden kleinverpakking",
            "AH",
        ),
        ("Heinz Tomato ketchup 2-pack", "Heinz", "Heinz Tomato ketchup", "Heinz"),
        (
            "AH Extra lang lekker zaans volkoren bol 6st",
            "AH",
            "AH Extra lang lekker zaans volkoren bol",
            "AH",
        ),
        ("Unox Knaks 4 stuks", "Unox", "Unox Knaks", "Unox"),
    ],
)
def test_same_family(a: str, brand_a: str, b: str, brand_b: str) -> None:
    assert family_key(a, brand_a) == family_key(b, brand_b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("AH Houdbare halfvolle melk", "AH Halfvolle melk"),
        ("AH Snoepgroente tomaat", "AH Snoepgroente komkommer"),
        ("AH Scharrel kipfilet plakjes gerookt", "AH Scharrel kipfilet 2 stuks"),
    ],
)
def test_different_family(a: str, b: str) -> None:
    assert family_key(a, "AH") != family_key(b, "AH")


def test_brand_is_removed_and_name_capitalised() -> None:
    key = family_key("Campina Langlekker halfvolle melk 3-pack", "Campina")
    assert key == "langlekker halfvolle melk"
    assert family_name(key) == "Langlekker halfvolle melk"
    assert family_name("") == "Onbekend product"
