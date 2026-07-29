"""Pricing logic.

Strategy: undercut the cheapest genuinely comparable listing by a configured
margin, with a time-based ladder acting as a floor so the price still steps
down as the window approaches even when competitors are expensive.

All money is in whole currency units. Discounts are percentages (0-100).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from .scraper import Listing

# Only recommend a change when it moves the dial by at least this much.
CHANGE_THRESHOLD_PCT = 1.0


@dataclass
class Analysis:
    ok: bool = True
    error: str = ""

    ours: Listing | None = None
    our_rank: int = 0
    total_results: int = 0

    comps: list[Listing] = field(default_factory=list)
    cheapest_comp: Listing | None = None

    fees_estimate: float = 0.0
    target_total: float = 0.0

    current_discount_pct: float = 0.0
    recommended_discount_pct: float = 0.0
    projected_total: float = 0.0
    reason: str = ""
    action_needed: bool = False
    recommended_min_nights: int = 0

    @property
    def delta_pct(self) -> float:
        return round(self.recommended_discount_pct - self.current_discount_pct, 1)


def effective_nightly(nightly: float, discount_pct: float) -> float:
    return nightly * (1 - discount_pct / 100.0)


def guest_total(nightly: float, nights: int, discount_pct: float, fees: float) -> float:
    return effective_nightly(nightly, discount_pct) * nights + fees


def ladder_minimum(ladder: list[dict], days_out: int) -> float:
    """Highest discount whose days_out threshold we have reached."""
    applicable = [
        float(rung.get("discount", 0))
        for rung in ladder
        if days_out <= int(rung.get("days_out", 10**6))
    ]
    return max(applicable) if applicable else 0.0


def discount_for_target(nightly: float, nights: int, fees: float, target_total: float) -> float:
    """Discount % that lands the guest total on target_total."""
    if nightly <= 0 or nights <= 0:
        return 0.0
    room_revenue = target_total - fees
    if room_revenue <= 0:
        return 100.0
    ratio = room_revenue / (nightly * nights)
    return max(0.0, min(100.0, (1 - ratio) * 100.0))


def floor_cap_pct(nightly: float, floor_nightly: float) -> float:
    """Largest discount that still respects the nightly floor."""
    if nightly <= 0 or floor_nightly <= 0:
        return 100.0
    if floor_nightly >= nightly:
        return 0.0
    return (1 - floor_nightly / nightly) * 100.0


def analyze(window, listings: list[Listing], today: dt.date | None = None) -> Analysis:
    today = today or dt.date.today()
    nights = window.nights
    a = Analysis(current_discount_pct=window.current_discount_pct)
    a.recommended_min_nights = window.recommended_min_nights

    exact = [l for l in listings if l.nights == nights]
    a.total_results = len(exact)

    ours = next((l for l in exact if l.listing_id == str(window.listing_id)), None)
    if ours is None and window.listing_title_match:
        needle = window.listing_title_match.lower()
        ours = next((l for l in exact if needle in l.name.lower()), None)

    if ours is None:
        a.ok = False
        a.error = (
            "Listing not found in the first "
            f"{len(exact)} exact-date results. It may be booked, unavailable, "
            "blocked by a minimum-night rule, or ranked beyond the pages scraped."
        )
        return a

    a.ours = ours
    a.our_rank = exact.index(ours) + 1

    # Self-calibrate the fee/tax wedge from observed data.
    room_revenue = effective_nightly(window.nightly_price, window.current_discount_pct) * nights
    a.fees_estimate = max(0.0, round(ours.total - room_revenue, 2))

    a.comps = [
        l
        for l in exact
        if l is not ours
        and l.bedrooms >= window.bedrooms
        and l.baths >= window.min_baths
        and l.reviews >= window.min_reviews
    ]
    a.comps.sort(key=lambda l: l.total)

    days_out = window.days_out(today)
    ladder_floor = ladder_minimum(window.ladder, days_out)
    cap = min(window.max_discount_pct, floor_cap_pct(window.nightly_price, window.floor_nightly))

    if a.comps:
        a.cheapest_comp = a.comps[0]
        a.target_total = max(0.0, a.cheapest_comp.total - window.undercut_margin)
        undercut_pct = discount_for_target(
            window.nightly_price, nights, a.fees_estimate, a.target_total
        )
        candidate = max(undercut_pct, ladder_floor)
        if undercut_pct > cap and ladder_floor <= cap:
            reason = (
                f"Undercutting {a.cheapest_comp.name} by ${window.undercut_margin:,.0f} would need "
                f"{undercut_pct:.0f}%, past your floor of ${window.floor_nightly:,.0f}/night. "
                f"Capped at {cap:.0f}%; ladder floor is {ladder_floor:.0f}%."
            )
        elif candidate == ladder_floor and ladder_floor > undercut_pct:
            reason = (
                f"You are already under the cheapest comp ({a.cheapest_comp.name} at "
                f"${a.cheapest_comp.total:,.0f}). Ladder drives this step: day {days_out} "
                f"out calls for {ladder_floor:.0f}%."
            )
        else:
            reason = (
                f"Cheapest comparable is {a.cheapest_comp.name} at ${a.cheapest_comp.total:,.0f}. "
                f"Target ${a.target_total:,.0f} (${window.undercut_margin:,.0f} under) needs "
                f"{undercut_pct:.0f}%."
            )
    else:
        candidate = ladder_floor
        reason = (
            "No comparable listings matched your filters, so the time ladder is driving this: "
            f"day {days_out} out calls for {ladder_floor:.0f}%."
        )

    candidate = max(0.0, min(candidate, cap))
    if window.ratchet_only and candidate < window.current_discount_pct:
        reason += (
            f" Holding at {window.current_discount_pct:.0f}% because ratchet-only is on "
            f"(computed {candidate:.0f}%)."
        )
        candidate = window.current_discount_pct

    # Floor rather than round: rounding up by 0.05pp could breach the nightly
    # floor the owner set, which is the one guardrail that must not move.
    a.recommended_discount_pct = math.floor(candidate * 10) / 10
    a.projected_total = round(
        guest_total(window.nightly_price, nights, a.recommended_discount_pct, a.fees_estimate), 2
    )
    a.reason = reason
    a.action_needed = abs(a.delta_pct) >= CHANGE_THRESHOLD_PCT
    return a
