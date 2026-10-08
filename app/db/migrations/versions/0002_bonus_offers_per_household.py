"""bonus offers per household

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08 18:51:00.195079
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # bonus_offer only holds derived data that the worker rebuilds every morning (and was
    # never filled before this revision), so clearing it makes the NOT NULL columns safe.
    op.execute("DELETE FROM bonus_offer")
    with op.batch_alter_table("bonus_offer", schema=None) as batch_op:
        batch_op.add_column(sa.Column("household_id", sa.Integer(), nullable=False))
        batch_op.add_column(sa.Column("family_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("source", sa.String(length=10), nullable=False, server_default="similar")
        )
        batch_op.add_column(sa.Column("period_end", sa.Date(), nullable=True))
        batch_op.drop_constraint(batch_op.f("uq_bonus_offer_week"), type_="unique")
        batch_op.create_unique_constraint(
            batch_op.f("uq_bonus_offer_household_id"), ["household_id", "week", "ah_product_id"]
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_bonus_offer_family_id_product_family"),
            "product_family",
            ["family_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_bonus_offer_household_id_household"),
            "household",
            ["household_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # ### end Alembic commands ###


def downgrade() -> None:
    # Per-household rows would violate the old (week, product) uniqueness; derived data only.
    op.execute("DELETE FROM bonus_offer")
    with op.batch_alter_table("bonus_offer", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_bonus_offer_household_id_household"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            batch_op.f("fk_bonus_offer_family_id_product_family"), type_="foreignkey"
        )
        batch_op.drop_constraint(batch_op.f("uq_bonus_offer_household_id"), type_="unique")
        batch_op.create_unique_constraint(
            batch_op.f("uq_bonus_offer_week"), ["week", "ah_product_id"]
        )
        batch_op.drop_column("period_end")
        batch_op.drop_column("source")
        batch_op.drop_column("family_id")
        batch_op.drop_column("household_id")

    # ### end Alembic commands ###
