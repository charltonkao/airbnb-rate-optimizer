"""Unit tests for the pricing math and card parser."""
from __future__ import annotations

import datetime as dt
import json
import sys
import types
from dataclasses import dataclass

import pytest

# Import the pure-logic modules without pulling in Playwright/DB at import time.
sys.modules.setdefault("playwright", types.ModuleType("playwright"))
_async_api = types.ModuleType("playwright.async_api")
_async_api.async_playwright = lambda: None  # type: ignore[attr-defined]
sys.modules.setdefault("playwright.async_api", _async_api)

from app.optimizer import (  # noqa: E402
    analyze,
    discount_for_target,
    effective_nightly,
    floor_cap_pct,
    guest_total,
    ladder_minimum,
)
from app.scraper import Listing, parse_card  # noqa: E402


@dataclass
class FakeWindow:
    """Mirrors the Window ORM surface the optimizer touches."""

    listing_id: str = "3167866"
    listing_title_match: str = "Green"
    check_in: dt.date = dt.date(2026, 8, 8)
    check_out: dt.date = dt.date(2026, 9, 1)
    max_bookings: int = 1
    nightly_price: float = 475.0
    current_discount_pct: float = 15.0
    floor_nightly: float = 250.0
    max_discount_pct: float = 60.0
    undercut_margin: float = 150.0
    ratchet_only: bool = True
    bedrooms: int = 3
    min_baths: float = 2.0
    min_reviews: int = 1
    ladder_json: str = json.dumps(
        [
            {"days_out": 10, "discount": 15},
            {"days_out": 7, "discount": 22},
            {"days_out": 4, "discount": 30},
            {"days_out": 2, "discount": 38},
        ]
    )

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def ladder(self):
        return sorted(json.loads(self.ladder_json), key=lambda r: -r["days_out"])

    def days_out(self, today=None):
        return (self.check_in - (today or dt.date.today())).days

    @property
    def recommended_min_nights(self) -> int:
        if self.max_bookings <= 1:
            return self.nights
        return max(self.nights // self.max_bookings, 1)


def mk(listing_id, name, total, nights=24, beds=3, baths=2.0, reviews=50):
    return Listing(
        listing_id=listing_id,
        name=name,
        bedrooms=beds,
        baths=baths,
        nights=nights,
        total=total,
        original_total=total,
        rating=4.8,
        reviews=reviews,
        has_discount_badge=False,
        position=0,
    )


# ---------------------------------------------------------------- primitives


def test_effective_nightly():
    assert effective_nightly(475, 15) == pytest.approx(403.75)
    assert effective_nightly(475, 0) == 475


def test_guest_total_matches_observed_airbnb_figure():
    # Real values pulled from the live listing on 2026-07-29.
    assert guest_total(475, 24, 15, 264) == pytest.approx(9954.0)


def test_discount_for_target_roundtrips():
    d = discount_for_target(475, 24, 264, 9954)
    assert d == pytest.approx(15.0, abs=0.01)


def test_discount_for_target_clamps_when_fees_exceed_target():
    assert discount_for_target(475, 24, 5000, 1000) == 100.0


def test_floor_cap_pct():
    assert floor_cap_pct(475, 250) == pytest.approx(47.368, abs=0.01)
    assert floor_cap_pct(475, 500) == 0.0
    assert floor_cap_pct(475, 0) == 100.0


def test_ladder_minimum_picks_most_aggressive_reached_rung():
    ladder = [
        {"days_out": 10, "discount": 15},
        {"days_out": 4, "discount": 30},
        {"days_out": 2, "discount": 38},
    ]
    assert ladder_minimum(ladder, 12) == 0.0
    assert ladder_minimum(ladder, 10) == 15.0
    assert ladder_minimum(ladder, 5) == 15.0
    assert ladder_minimum(ladder, 3) == 30.0
    assert ladder_minimum(ladder, 1) == 38.0


# ------------------------------------------------------------------ analyze


def test_analyze_holds_when_already_cheapest():
    w = FakeWindow()
    today = dt.date(2026, 7, 29)  # 10 days out -> ladder floor 15%
    listings = [
        mk("3167866", "Green House", 9954),
        mk("111", "Peapod 3Bd/2Ba", 10130),
        mk("222", "Bright Spacious 3BR 2BA", 10706),
    ]
    a = analyze(w, listings, today=today)
    assert a.ok
    assert a.our_rank == 1
    assert a.fees_estimate == pytest.approx(264.0, abs=1)
    # We are already below (10130 - 150); ladder floor of 15% governs.
    assert a.recommended_discount_pct == pytest.approx(15.0, abs=0.6)
    assert a.action_needed is False


def test_analyze_recommends_deeper_cut_when_undercut_by_competitor():
    w = FakeWindow()
    today = dt.date(2026, 7, 29)
    listings = [
        mk("3167866", "Green House", 9954),
        mk("111", "Rival dropped price", 8500),
    ]
    a = analyze(w, listings, today=today)
    # Target 8350 -> needs a bigger discount than the current 15%.
    assert a.recommended_discount_pct > 15.0
    assert a.action_needed is True
    assert a.projected_total == pytest.approx(8350, abs=25)


def test_analyze_respects_nightly_floor():
    w = FakeWindow(floor_nightly=400.0)  # caps discount at ~15.8%
    today = dt.date(2026, 7, 29)
    listings = [
        mk("3167866", "Green House", 9954),
        mk("111", "Very cheap rival", 5000),
    ]
    a = analyze(w, listings, today=today)
    assert a.recommended_discount_pct <= floor_cap_pct(475, 400) + 0.01
    assert "floor" in a.reason.lower()


def test_analyze_ratchet_only_never_raises_price():
    w = FakeWindow(current_discount_pct=30.0, ratchet_only=True)
    today = dt.date(2026, 7, 29)
    listings = [
        mk("3167866", "Green House", 8000),
        mk("111", "Expensive rival", 15000),
    ]
    a = analyze(w, listings, today=today)
    assert a.recommended_discount_pct == 30.0
    assert "ratchet" in a.reason.lower()


def test_analyze_allows_price_increase_when_ratchet_off():
    w = FakeWindow(current_discount_pct=30.0, ratchet_only=False)
    today = dt.date(2026, 7, 29)
    listings = [
        mk("3167866", "Green House", 8000),
        mk("111", "Expensive rival", 15000),
    ]
    a = analyze(w, listings, today=today)
    assert a.recommended_discount_pct < 30.0


def test_analyze_excludes_non_comparable_listings():
    w = FakeWindow()
    today = dt.date(2026, 7, 29)
    listings = [
        mk("3167866", "Green House", 9954),
        mk("111", "One bath, cheap", 6000, baths=1.0),      # too few baths
        mk("222", "No reviews, cheap", 6100, reviews=0),    # unreviewed
        mk("333", "Two bed, cheap", 6200, beds=2),          # too few bedrooms
        mk("444", "Legit comp", 10500),
    ]
    a = analyze(w, listings, today=today)
    assert [c.listing_id for c in a.comps] == ["444"]


def test_analyze_ignores_different_length_stays():
    w = FakeWindow()
    listings = [
        mk("3167866", "Green House", 9954, nights=24),
        mk("111", "Similar dates only", 4000, nights=16),
    ]
    a = analyze(w, listings, today=dt.date(2026, 7, 29))
    assert a.total_results == 1
    assert a.comps == []


def test_analyze_reports_missing_listing():
    w = FakeWindow()
    a = analyze(w, [mk("999", "Someone else", 9000)], today=dt.date(2026, 7, 29))
    assert a.ok is False
    assert "not found" in a.error


def test_recommended_min_nights_for_multiple_bookings():
    assert FakeWindow(max_bookings=1).recommended_min_nights == 24
    assert FakeWindow(max_bookings=2).recommended_min_nights == 12
    assert FakeWindow(max_bookings=3).recommended_min_nights == 8


# ------------------------------------------------------------------- parser


CARD_WITH_DISCOUNT = """Townhouse in Cambridge
"Green" House + parking Harvard/MIT
3 bedrooms
,
3 beds
,
2 baths
$11,664
$9,954
Show price breakdown
for 24 nights
Weekly discount
4.83 out of 5 average rating, 83 reviews"""

CARD_NO_DISCOUNT = """Apartment in Malden
Stunning 3-Bedroom Minutes From Boston & Everett
3 bedrooms
,
3 beds
,
1 bath
$6,472
Show price breakdown
for 24 nights
4.7 out of 5 average rating, 142 reviews"""


def test_parse_card_with_strikethrough():
    l = parse_card(CARD_WITH_DISCOUNT, "/rooms/3167866?foo=bar", 1)
    assert l is not None
    assert l.listing_id == "3167866"
    assert l.total == 9954.0
    assert l.original_total == 11664.0
    assert l.nights == 24
    assert l.bedrooms == 3
    assert l.baths == 2.0
    assert l.reviews == 83
    assert l.rating == 4.83
    assert l.has_discount_badge is True
    assert l.per_night == pytest.approx(414.75)


def test_parse_card_single_price():
    l = parse_card(CARD_NO_DISCOUNT, "/rooms/555", 2)
    assert l is not None
    assert l.total == 6472.0
    assert l.original_total == 6472.0
    assert l.baths == 1.0
    assert l.has_discount_badge is False


def test_parse_card_rejects_non_stay_card():
    assert parse_card("Some promo card with no price", "", 1) is None
    assert parse_card("$500 gift card", "", 1) is None
