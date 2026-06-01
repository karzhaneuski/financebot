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
    photo_file_id: Mapped[Optional[str]] = mapped_column(String(255))
    tx_type: Mapped[Optional[str]] = mapped_column(String(20))
    source: Mapped[Optional[str]] = mapped_column(String(20))
    category: Mapped[Optional["Category"]] = mapped_column(Enum(Category), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[List["Item"]] = relationship("Item", back_populates="receipt", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Receipt id={self.id} user_id={self.user_id} store={self.store!r} total={self.total} {self.currency}>"


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(Integer, ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    quantity: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False, default=1)
    unit_price: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    total_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    category: Mapped[Category] = mapped_column(Enum(Category), nullable=False, default=Category.other)
    volume_ml: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

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
