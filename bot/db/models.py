import enum
from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Category(str, enum.Enum):
    groceries = "groceries"
    cafe = "cafe"
    pharmacy = "pharmacy"
    transport = "transport"
    electronics = "electronics"
    clothing = "clothing"
    household = "household"
    housing = "housing"
    entertainment = "entertainment"
    health = "health"
    subscriptions = "subscriptions"
    other = "other"


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    store: Mapped[Optional[str]] = mapped_column(String(255))
    date: Mapped[Optional[date]] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    total_pln: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    # Personal share after splitting a receipt with other people (Feature 3).
    # NULL means the whole receipt is personal — treat as equal to total_pln.
    personal_total_pln: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    photo_file_id: Mapped[Optional[str]] = mapped_column(String(255))
    tx_type: Mapped[Optional[str]] = mapped_column(String(20))
    source: Mapped[Optional[str]] = mapped_column(String(20))
    category: Mapped[Optional["Category"]] = mapped_column(Enum(Category), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[List["Item"]] = relationship("Item", back_populates="receipt", cascade="all, delete-orphan")

    def personal_amount(self) -> float:
        """Amount that counts toward this user's personal expense totals."""
        if self.personal_total_pln is not None:
            return float(self.personal_total_pln)
        return float(self.total_pln)

    def to_pln(self, amount: float) -> float:
        """Convert an amount in this receipt's currency (e.g. an item price) to
        PLN with the receipt's own rate — Python twin of crud._item_pln_col()."""
        if self.currency == "PLN":
            return float(amount)
        total = float(self.total)
        return float(amount) * float(self.total_pln) / total if total else 0.0

    def __repr__(self) -> str:
        return f"<Receipt id={self.id} user_id={self.user_id} store={self.store!r} total={self.total} {self.currency}>"


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(Integer, ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    quantity: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False, default=1)
    # Prices are in the receipt's currency (receipts.currency), never PLN
    # unless the receipt is; crud._item_pln_col() converts them for sums.
    unit_price: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    total_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    category: Mapped[Category] = mapped_column(Enum(Category), nullable=False, default=Category.other)
    volume_ml: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Personal-share marker after /split (Feature 3). NULL means "counts as
    # personal" so pre-split receipts behave exactly as before.
    is_personal: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    receipt: Mapped["Receipt"] = relationship("Receipt", back_populates="items")

    def __repr__(self) -> str:
        return f"<Item id={self.id} name={self.name!r} qty={self.quantity} total={self.total_price}>"


class Budget(Base):
    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    category: Mapped[Category] = mapped_column(Enum(Category), nullable=False)
    limit_pln: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    month: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    last_notified_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<Budget id={self.id} user_id={self.user_id} category={self.category} limit={self.limit_pln} month={self.month}>"


class ReportSettings(Base):
    __tablename__ = "report_settings"
    __table_args__ = (UniqueConstraint("user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    daily_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    weekly_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    monthly_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    def __repr__(self) -> str:
        return f"<ReportSettings user_id={self.user_id} daily={self.daily_enabled} weekly={self.weekly_enabled} monthly={self.monthly_enabled}>"


class User(Base):
    """Per-user settings. A row is created on the user's first update;
    migration 011 backfills every pre-existing user with language "ru"."""

    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<User user_id={self.user_id} language={self.language}>"
