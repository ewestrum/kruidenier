"""Database schema (SPEC §5). Portable SQL so tests can run on SQLite; production is Postgres."""

from datetime import UTC, date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON}


class Household(Base):
    __tablename__ = "household"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    settings_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="household")
    ah_accounts: Mapped[list["AhAccount"]] = relationship(back_populates="household")


class User(Base):
    __tablename__ = "app_user"  # "user" is reserved in Postgres

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    pw_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="member")  # admin | member
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    household: Mapped[Household] = relationship(back_populates="users")


class AhAccount(Base):
    __tablename__ = "ah_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(100))
    tokens_enc: Mapped[bytes] = mapped_column(LargeBinary)  # Fernet, see app.ah.token_crypto
    is_order_account: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    household: Mapped[Household] = relationship(back_populates="ah_accounts")


class Product(Base):
    """Global AH catalogue entry (not household-specific)."""

    __tablename__ = "product"

    ah_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(255))
    brand: Mapped[str | None] = mapped_column(String(100))
    unit_size_text: Mapped[str | None] = mapped_column(String(50))
    unit_amount: Mapped[float | None] = mapped_column(Float)  # in `unit`
    unit: Mapped[str | None] = mapped_column(String(2))  # g | ml | st
    category: Mapped[str | None] = mapped_column(String(100))
    sub_category: Mapped[str | None] = mapped_column(String(100))
    shelf_life_days: Mapped[int | None] = mapped_column(Integer)  # manual override
    min_best_before_days: Mapped[int | None] = mapped_column(Integer)  # from AH, fresh only
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProductFamily(Base):
    __tablename__ = "product_family"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    base_unit: Mapped[str] = mapped_column(String(2))  # g | ml | st
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    # Feedback state (SPEC §6.8)
    carryover: Mapped[float] = mapped_column(Float, default=0.0)
    correction: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    members: Mapped[list["FamilyMember"]] = relationship(
        back_populates="family", cascade="all, delete-orphan"
    )


class FamilyMember(Base):
    __tablename__ = "family_member"

    family_id: Mapped[int] = mapped_column(
        ForeignKey("product_family.id", ondelete="CASCADE"), primary_key=True
    )
    ah_product_id: Mapped[int] = mapped_column(ForeignKey("product.ah_id"), primary_key=True)
    preferred: Mapped[bool] = mapped_column(Boolean, default=False)

    family: Mapped[ProductFamily] = relationship(back_populates="members")
    product: Mapped[Product] = relationship()


class Purchase(Base):
    __tablename__ = "purchase"
    __table_args__ = (UniqueConstraint("household_id", "ah_order_id", "ah_product_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    ah_order_id: Mapped[int] = mapped_column(Integer)
    ah_product_id: Mapped[int] = mapped_column(ForeignKey("product.ah_id"))
    qty: Mapped[int] = mapped_column(Integer)
    delivered_at: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(10), default="staple")  # staple|meal|manual


class PriceObservation(Base):
    __tablename__ = "price_observation"

    ah_product_id: Mapped[int] = mapped_column(ForeignKey("product.ah_id"), primary_key=True)
    observed_on: Mapped[date] = mapped_column(Date, primary_key=True)
    price: Mapped[float | None] = mapped_column(Float)  # what you pay now
    regular_price: Mapped[float | None] = mapped_column(Float)  # price before bonus
    is_bonus: Mapped[bool] = mapped_column(Boolean, default=False)
    bonus_mechanism: Mapped[str | None] = mapped_column(String(100))
    available: Mapped[bool | None] = mapped_column(Boolean)


class BonusOffer(Base):
    """A bonus product relevant to a household this bonus period.

    `source` is "own" (a product the household buys) or "similar" (found by searching the
    name of one of its families). Relevance comes from the family's purchase count.
    """

    __tablename__ = "bonus_offer"
    __table_args__ = (UniqueConstraint("household_id", "week", "ah_product_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    family_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_family.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(
        String(10), default="similar", server_default="similar"
    )  # own | similar
    week: Mapped[str] = mapped_column(String(10))  # bonus period start, e.g. 2026-10-05
    period_end: Mapped[date | None] = mapped_column(Date)
    ah_product_id: Mapped[int] = mapped_column(ForeignKey("product.ah_id"))
    mechanism: Mapped[str] = mapped_column(String(100))
    raw_json: Mapped[dict[str, Any]] = mapped_column(default=dict)


class FamilyStats(Base):
    __tablename__ = "family_stats"

    family_id: Mapped[int] = mapped_column(
        ForeignKey("product_family.id", ondelete="CASCADE"), primary_key=True
    )
    rate_per_day: Mapped[float | None] = mapped_column(Float)
    cv_interval: Mapped[float | None] = mapped_column(Float)
    n_purchases: Mapped[int] = mapped_column(Integer, default=0)
    last_purchase_at: Mapped[date | None] = mapped_column(Date)
    est_stock: Mapped[float | None] = mapped_column(Float)
    due_date: Mapped[date | None] = mapped_column(Date)
    confidence: Mapped[str] = mapped_column(String(10), default="low")
    bonus_interval_days: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("product_family.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20))  # enough_stock|not_anymore|more|less|ok
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Plan(Base):
    __tablename__ = "plan"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    ah_order_id: Mapped[int | None] = mapped_column(Integer)
    delivery_date: Mapped[date] = mapped_column(Date)
    cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), default="draft")  # draft|applied|reverted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lines: Mapped[list["PlanLine"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class PlanLine(Base):
    __tablename__ = "plan_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plan.id", ondelete="CASCADE"))
    family_id: Mapped[int] = mapped_column(ForeignKey("product_family.id", ondelete="CASCADE"))
    ah_product_id: Mapped[int] = mapped_column(ForeignKey("product.ah_id"))
    qty: Mapped[int] = mapped_column(Integer)
    reason_code: Mapped[str] = mapped_column(String(30))
    reason_text: Mapped[str] = mapped_column(Text, default="")
    tier: Mapped[str] = mapped_column(String(12), default="propose")  # auto|auto_bonus|propose
    applied: Mapped[bool] = mapped_column(Boolean, default=False)

    plan: Mapped[Plan] = relationship(back_populates="lines")


class ActionLog(Base):
    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    action: Mapped[str] = mapped_column(String(50))
    payload_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    undo_payload_json: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Pause(Base):
    """Holiday per household: no consumption, no plan (SPEC §6.9)."""

    __tablename__ = "pause"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id", ondelete="CASCADE"))
    start: Mapped[date] = mapped_column(Date)
    end: Mapped[date] = mapped_column(Date)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    detail: Mapped[str] = mapped_column(Text, default="")
