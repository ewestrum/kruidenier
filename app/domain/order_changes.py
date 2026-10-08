"""Which quantities to set on the AH order, and how to undo that (SPEC §9). Pure rules.

AH's `PUT items` sets absolute quantities, so a change is (product, before, after).
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Change:
    product_id: int
    before: int
    after: int


def changes_to_apply(current: Mapping[int, int], wanted: Mapping[int, int]) -> list[Change]:
    """Raise each product to the wanted quantity; never lower what is already there.

    What a person put in the order themselves always wins, and running this twice
    changes nothing the second time (idempotent).
    """
    changes = []
    for product_id, qty in sorted(wanted.items()):
        before = current.get(product_id, 0)
        if qty > before:
            changes.append(Change(product_id, before, qty))
    return changes


@dataclass(frozen=True)
class UndoPlan:
    revert: list[Change]  # changes to send (after = the old quantity)
    untouched: list[int]  # products changed by someone since; left alone


def changes_to_undo(applied: list[Change], current: Mapping[int, int]) -> UndoPlan:
    """Put back the previous quantity, but only where nobody changed it after us."""
    revert, untouched = [], []
    for change in applied:
        now = current.get(change.product_id, 0)
        if now == change.after:
            revert.append(Change(change.product_id, now, change.before))
        elif now != change.before:
            untouched.append(change.product_id)
    return UndoPlan(revert, untouched)
