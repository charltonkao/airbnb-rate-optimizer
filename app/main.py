"""FastAPI app: window CRUD, run triggers, history."""
from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import settings
from .models import SessionLocal, Snapshot, Window, init_db
from .runner import run_all, run_window

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s"
)
log = logging.getLogger("optimizer")

TEMPLATES = pathlib.Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES))


def _resolve_timezone(name: str) -> ZoneInfo:
    """Never let a bad TZ take the service down — degrade to UTC and say so."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.error(
            "Unknown timezone %r — falling back to UTC. Scheduled runs will use "
            "UTC times. Check the TZ environment variable.",
            name,
        )
        return ZoneInfo("UTC")


SCHED_TZ = _resolve_timezone(settings.timezone)

# Localise log timestamps in Python rather than relying on system tzdata,
# which is not installed in the base image (see the note in the Dockerfile).
logging.Formatter.converter = lambda *_: dt.datetime.now(SCHED_TZ).timetuple()

scheduler = BackgroundScheduler(timezone=SCHED_TZ)

DEFAULT_LADDER = [
    {"days_out": 10, "discount": 15},
    {"days_out": 7, "discount": 22},
    {"days_out": 4, "discount": 30},
    {"days_out": 2, "discount": 38},
    {"days_out": 0, "discount": 45},
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    scheduler.add_job(
        run_all,
        # Pass the timezone explicitly: CronTrigger does not inherit the
        # scheduler's, and would otherwise ask tzlocal, which reads the raw TZ
        # env var and raises on anything it cannot parse.
        CronTrigger(
            hour=settings.daily_hour, minute=settings.daily_minute, timezone=SCHED_TZ
        ),
        id="daily",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    log.info(
        "Daily run scheduled for %02d:%02d %s",
        settings.daily_hour,
        settings.daily_minute,
        SCHED_TZ,
    )
    if settings.run_on_startup:
        scheduler.add_job(
            run_all,
            "date",
            run_date=dt.datetime.now(SCHED_TZ) + dt.timedelta(seconds=15),
        )
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Airbnb Rate Optimizer", lifespan=lifespan)


def _fmt(value, spec="{:,.0f}"):
    try:
        return spec.format(value)
    except (ValueError, TypeError):
        return value


templates.env.filters["money"] = lambda v: _fmt(v)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with SessionLocal() as db:
        windows = db.query(Window).order_by(Window.check_in).all()
        latest = {}
        for w in windows:
            snap = (
                db.query(Snapshot)
                .filter(Snapshot.window_id == w.id)
                .order_by(Snapshot.taken_at.desc())
                .first()
            )
            latest[w.id] = snap
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "windows": windows,
            "latest": latest,
            "today": dt.date.today(),
            "settings": settings,
            "default_ladder": json.dumps(DEFAULT_LADDER, indent=2),
        },
    )


@app.post("/windows")
def create_window(
    name: str = Form(...),
    listing_id: str = Form(...),
    listing_title_match: str = Form(""),
    check_in: str = Form(...),
    check_out: str = Form(...),
    max_bookings: int = Form(1),
    nightly_price: float = Form(...),
    current_discount_pct: float = Form(0),
    floor_nightly: float = Form(0),
    max_discount_pct: float = Form(60),
    undercut_margin: float = Form(150),
    ratchet_only: str = Form("on"),
    market_slug: str = Form("Cambridge--MA"),
    bedrooms: int = Form(3),
    min_baths: float = Form(2),
    min_reviews: int = Form(1),
    adults: int = Form(4),
    ladder_json: str = Form(""),
):
    try:
        json.loads(ladder_json or "[]")
    except ValueError:
        ladder_json = json.dumps(DEFAULT_LADDER)

    with SessionLocal() as db:
        w = Window(
            name=name,
            listing_id=listing_id.strip(),
            listing_title_match=listing_title_match.strip(),
            check_in=dt.date.fromisoformat(check_in),
            check_out=dt.date.fromisoformat(check_out),
            max_bookings=max_bookings,
            nightly_price=nightly_price,
            current_discount_pct=current_discount_pct,
            floor_nightly=floor_nightly,
            max_discount_pct=max_discount_pct,
            undercut_margin=undercut_margin,
            ratchet_only=ratchet_only == "on",
            market_slug=market_slug.strip(),
            bedrooms=bedrooms,
            min_baths=min_baths,
            min_reviews=min_reviews,
            adults=adults,
            ladder_json=ladder_json or json.dumps(DEFAULT_LADDER),
        )
        db.add(w)
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/windows/{window_id}/discount")
def update_discount(window_id: int, current_discount_pct: float = Form(...)):
    """Record that you applied a discount in Airbnb."""
    with SessionLocal() as db:
        w = db.get(Window, window_id)
        if w:
            w.current_discount_pct = current_discount_pct
            snap = (
                db.query(Snapshot)
                .filter(Snapshot.window_id == window_id)
                .order_by(Snapshot.taken_at.desc())
                .first()
            )
            if snap:
                snap.applied = True
            db.commit()
    return RedirectResponse(f"/windows/{window_id}", status_code=303)


@app.post("/windows/{window_id}/toggle")
def toggle_window(window_id: int):
    with SessionLocal() as db:
        w = db.get(Window, window_id)
        if w:
            w.active = not w.active
            db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/windows/{window_id}/delete")
def delete_window(window_id: int):
    with SessionLocal() as db:
        w = db.get(Window, window_id)
        if w:
            db.delete(w)
            db.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/windows/{window_id}/run")
def trigger_run(window_id: int, background: BackgroundTasks, email: str = Form("on")):
    background.add_task(run_window, window_id, email == "on")
    return RedirectResponse(f"/windows/{window_id}?queued=1", status_code=303)


@app.post("/run-all")
def trigger_run_all(background: BackgroundTasks):
    background.add_task(run_all, True)
    return RedirectResponse("/?queued=1", status_code=303)


@app.get("/windows/{window_id}", response_class=HTMLResponse)
def window_detail(request: Request, window_id: int, queued: int = 0):
    with SessionLocal() as db:
        w = db.get(Window, window_id)
        if w is None:
            return RedirectResponse("/", status_code=303)
        snaps = (
            db.query(Snapshot)
            .filter(Snapshot.window_id == window_id)
            .order_by(Snapshot.taken_at.desc())
            .limit(60)
            .all()
        )
    return templates.TemplateResponse(
        request,
        "window.html",
        {
            "w": w,
            "snaps": snaps,
            "latest": snaps[0] if snaps else None,
            "queued": queued,
            "today": dt.date.today(),
        },
    )


@app.get("/healthz")
def healthz():
    return {"ok": True, "scheduler_running": scheduler.running}
