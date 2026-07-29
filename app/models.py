"""SQLAlchemy models."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


class Window(Base):
    """An availability window the owner wants to fill."""

    __tablename__ = "windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))

    # Listing identity
    listing_id: Mapped[str] = mapped_column(String(32))
    listing_title_match: Mapped[str] = mapped_column(String(200), default="")

    # Dates
    check_in: Mapped[dt.date] = mapped_column(Date)
    check_out: Mapped[dt.date] = mapped_column(Date)

    # Goal
    max_bookings: Mapped[int] = mapped_column(Integer, default=1)

    # Current live pricing (mirrors what is set in Airbnb)
    nightly_price: Mapped[float] = mapped_column(Float, default=0.0)
    current_discount_pct: Mapped[float] = mapped_column(Float, default=0.0)

    # Guardrails
    floor_nightly: Mapped[float] = mapped_column(Float, default=0.0)
    max_discount_pct: Mapped[float] = mapped_column(Float, default=60.0)
    undercut_margin: Mapped[float] = mapped_column(Float, default=150.0)
    ratchet_only: Mapped[bool] = mapped_column(Boolean, default=True)

    # Comparable-set definition
    market_slug: Mapped[str] = mapped_column(String(80), default="Cambridge--MA")
    bedrooms: Mapped[int] = mapped_column(Integer, default=3)
    min_baths: Mapped[float] = mapped_column(Float, default=2.0)
    min_reviews: Mapped[int] = mapped_column(Integer, default=1)
    adults: Mapped[int] = mapped_column(Integer, default=4)

    # Fallback time ladder, JSON list of {"days_out": int, "discount": float}
    ladder_json: Mapped[str] = mapped_column(Text, default="[]")

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.timezone.utc)
    )

    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="window", cascade="all, delete-orphan", order_by="Snapshot.taken_at.desc()"
    )

    @property
    def nights(self) -> int:
        return max((self.check_out - self.check_in).days, 0)

    @property
    def ladder(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.ladder_json or "[]")
            return sorted(data, key=lambda r: -int(r.get("days_out", 0)))
        except (ValueError, TypeError):
            return []

    def days_out(self, today: dt.date | None = None) -> int:
        today = today or dt.date.today()
        return (self.check_in - today).days

    @property
    def recommended_min_nights(self) -> int:
        """Splitting the window across max_bookings implies this minimum."""
        if self.max_bookings <= 1:
            return self.nights
        return max(self.nights // self.max_bookings, 1)

    @property
    def search_url(self) -> str:
        return (
            f"https://www.airbnb.com/s/{self.market_slug}/homes"
            f"?checkin={self.check_in.isoformat()}"
            f"&checkout={self.check_out.isoformat()}"
            f"&adults={self.adults}"
            f"&min_bedrooms={self.bedrooms}"
            "&room_types%5B%5D=Entire%20home%2Fapt"
        )

    @property
    def discount_settings_url(self) -> str:
        return f"https://www.airbnb.com/multicalendar/{self.listing_id}/discounts"


class Snapshot(Base):
    """One scrape + recommendation for a window."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    window_id: Mapped[int] = mapped_column(ForeignKey("windows.id", ondelete="CASCADE"))
    taken_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.timezone.utc)
    )

    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")

    our_rank: Mapped[int] = mapped_column(Integer, default=0)
    total_results: Mapped[int] = mapped_column(Integer, default=0)
    our_total: Mapped[float] = mapped_column(Float, default=0.0)
    our_nights: Mapped[int] = mapped_column(Integer, default=0)
    fees_estimate: Mapped[float] = mapped_column(Float, default=0.0)

    cheapest_comp_name: Mapped[str] = mapped_column(String(200), default="")
    cheapest_comp_total: Mapped[float] = mapped_column(Float, default=0.0)
    comps_json: Mapped[str] = mapped_column(Text, default="[]")

    recommended_discount_pct: Mapped[float] = mapped_column(Float, default=0.0)
    recommendation_reason: Mapped[str] = mapped_column(Text, default="")
    action_needed: Mapped[bool] = mapped_column(Boolean, default=False)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)

    window: Mapped[Window] = relationship(back_populates="snapshots")

    @property
    def comps(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.comps_json or "[]")
        except (ValueError, TypeError):
            return []


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
