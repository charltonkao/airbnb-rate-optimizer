"""Orchestrates a run: scrape -> analyze -> persist -> email."""
from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import emailer
from .config import settings
from .models import SessionLocal, Snapshot, Window
from .optimizer import analyze
from .scraper import scrape, to_dicts

log = logging.getLogger(__name__)

TEMPLATES = pathlib.Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"])
)


def run_window(window_id: int, send_email: bool = True) -> Snapshot:
    """Scrape and analyze one window. Always persists a snapshot."""
    with SessionLocal() as db:
        window = db.get(Window, window_id)
        if window is None:
            raise ValueError(f"No window with id {window_id}")

        snap = Snapshot(window_id=window.id, our_nights=window.nights)

        try:
            listings = scrape(window.search_url)
        except Exception as exc:  # noqa: BLE001
            log.exception("Scrape failed for window %s", window_id)
            snap.ok = False
            snap.error = f"Scrape failed: {exc}"
            db.add(snap)
            db.commit()
            if send_email:
                _email_failure(window, snap)
            return snap

        result = analyze(window, listings)
        snap.ok = result.ok
        snap.error = result.error
        snap.total_results = result.total_results
        snap.comps_json = json.dumps(to_dicts(result.comps)[:25])

        if result.ok and result.ours:
            snap.our_rank = result.our_rank
            snap.our_total = result.ours.total
            snap.fees_estimate = result.fees_estimate
            snap.recommended_discount_pct = result.recommended_discount_pct
            snap.recommendation_reason = result.reason
            snap.action_needed = result.action_needed
            if result.cheapest_comp:
                snap.cheapest_comp_name = result.cheapest_comp.name
                snap.cheapest_comp_total = result.cheapest_comp.total

        db.add(snap)
        db.commit()
        db.refresh(snap)

        if send_email:
            if result.ok:
                _email_digest(window, snap, result)
            else:
                _email_failure(window, snap)

        return snap


def run_all(send_email: bool = True) -> list[Snapshot]:
    with SessionLocal() as db:
        ids = [w.id for w in db.query(Window).filter(Window.active.is_(True)).all()]
    out = []
    for wid in ids:
        try:
            out.append(run_window(wid, send_email=send_email))
        except Exception:  # noqa: BLE001
            log.exception("Run failed for window %s", wid)
    return out


def _email_digest(window: Window, snap: Snapshot, result) -> None:
    tmpl = _env.get_template("email.html")
    html = tmpl.render(
        window=window,
        snap=snap,
        result=result,
        days_out=window.days_out(),
        base_url=settings.base_url,
        generated=dt.datetime.now().strftime("%A, %B %d %Y at %H:%M"),
    )
    verb = "ACTION" if snap.action_needed else "hold"
    subject = (
        f"[{verb}] {window.name}: rank #{snap.our_rank}, "
        f"{snap.recommended_discount_pct:.0f}% weekly discount"
    )
    text = (
        f"{window.name}\n"
        f"Rank #{snap.our_rank} of {snap.total_results}\n"
        f"Your total: ${snap.our_total:,.0f} for {snap.our_nights} nights\n"
        f"Cheapest comp: {snap.cheapest_comp_name} at ${snap.cheapest_comp_total:,.0f}\n"
        f"Current discount: {window.current_discount_pct:.0f}%\n"
        f"Recommended: {snap.recommended_discount_pct:.0f}%\n\n"
        f"{snap.recommendation_reason}\n\n"
        f"Change it at {window.discount_settings_url}\n"
    )
    emailer.send(subject, html, text)


def _email_failure(window: Window, snap: Snapshot) -> None:
    html = f"""
    <p style="font-family:system-ui,sans-serif">
      <strong>{window.name}</strong> could not be evaluated today.
    </p>
    <pre style="font-family:ui-monospace,monospace;white-space:pre-wrap;color:#b00">{snap.error}</pre>
    <p style="font-family:system-ui,sans-serif;color:#555">
      If the listing is now booked, that is the likely cause and no action is needed.
      Otherwise check the app at <a href="{settings.base_url}">{settings.base_url}</a>.
    </p>
    """
    emailer.send(f"[check] {window.name}: no result today", html, snap.error)
