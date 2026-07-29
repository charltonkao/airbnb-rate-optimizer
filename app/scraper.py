"""Scrapes public Airbnb search results.

Only anonymous, publicly visible search pages are read. No login, no host
dashboard, no credentials. See README for the rationale and the rate-limiting
behaviour this module deliberately keeps.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass

from playwright.async_api import async_playwright

from .config import settings

log = logging.getLogger(__name__)

PAGE_SIZE = 18

_RE_BEDROOMS = re.compile(r"(\d+)\s+bedrooms?", re.I)
_RE_BATHS = re.compile(r"([\d.]+)\s+(?:shared\s+|private\s+)?baths?", re.I)
_RE_NIGHTS = re.compile(r"for\s+(\d+)\s+nights?", re.I)
_RE_MONEY = re.compile(r"\$([\d,]+)")
_RE_RATING = re.compile(r"([\d.]+)\s+out of 5 average rating,\s+([\d,]+)\s+reviews?", re.I)
_RE_ROOM_ID = re.compile(r"/rooms/(\d+)")


@dataclass
class Listing:
    listing_id: str
    name: str
    bedrooms: int
    baths: float
    nights: int
    total: float
    original_total: float
    rating: float
    reviews: int
    has_discount_badge: bool
    position: int

    @property
    def per_night(self) -> float:
        return round(self.total / self.nights, 2) if self.nights else 0.0


def _money(raw: str) -> float:
    return float(raw.replace(",", ""))


def parse_card(text: str, href: str, position: int) -> Listing | None:
    """Parse one search-result card's innerText into a Listing.

    Returns None when the card is not a priced stay result (ads, spacers).
    """
    nights_m = _RE_NIGHTS.search(text)
    money = _RE_MONEY.findall(text)
    if not nights_m or not money:
        return None

    room_m = _RE_ROOM_ID.search(href or "")
    listing_id = room_m.group(1) if room_m else ""

    # Airbnb renders the struck-through original first, then the effective
    # total. With no discount there is a single figure.
    amounts = [_money(m) for m in money]
    total = amounts[-1]
    original = amounts[0] if len(amounts) > 1 else amounts[-1]
    # Guard against a stray larger figure appearing after the real total.
    if len(amounts) > 1 and total > original:
        total, original = original, total

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Line 0 is "<Property type> in <City>"; line 1 is the title.
    name = ""
    for ln in lines[1:4]:
        if not re.match(r"^\d", ln) and "bedroom" not in ln.lower():
            name = ln
            break
    if not name and lines:
        name = lines[0]

    bed_m = _RE_BEDROOMS.search(text)
    bath_m = _RE_BATHS.search(text)
    rate_m = _RE_RATING.search(text)

    return Listing(
        listing_id=listing_id,
        name=name[:200],
        bedrooms=int(bed_m.group(1)) if bed_m else 0,
        baths=float(bath_m.group(1)) if bath_m else 0.0,
        nights=int(nights_m.group(1)),
        total=total,
        original_total=original,
        rating=float(rate_m.group(1)) if rate_m else 0.0,
        reviews=int(rate_m.group(2).replace(",", "")) if rate_m else 0,
        has_discount_badge=bool(re.search(r"(weekly|monthly|extended stay) discount", text, re.I)),
        position=position,
    )


async def _scrape_async(search_url: str, pages: int) -> list[Listing]:
    results: list[Listing] = []
    seen: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        try:
            context = await browser.new_context(
                locale=settings.scrape_locale,
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            for page_index in range(pages):
                url = search_url
                if page_index:
                    url = f"{search_url}&items_offset={page_index * PAGE_SIZE}"

                log.info("Fetching %s", url)
                await page.goto(url, timeout=settings.scrape_timeout_ms, wait_until="domcontentloaded")
                try:
                    await page.wait_for_selector(
                        '[data-testid="card-container"]', timeout=settings.scrape_timeout_ms
                    )
                except Exception:  # noqa: BLE001 - empty page is a valid outcome
                    log.warning("No cards rendered on page %s", page_index + 1)
                    break

                cards = await page.query_selector_all('[data-testid="card-container"]')
                if not cards:
                    break

                for card in cards:
                    text = (await card.inner_text()) or ""
                    href = ""
                    link = await card.query_selector("a[href*='/rooms/']")
                    if link:
                        href = await link.get_attribute("href") or ""

                    listing = parse_card(text, href, position=len(results) + 1)
                    if listing is None:
                        continue
                    key = listing.listing_id or f"{listing.name}|{listing.total}"
                    if key in seen:
                        continue
                    seen.add(key)
                    listing.position = len(results) + 1
                    results.append(listing)

                if settings.scrape_delay_ms:
                    await asyncio.sleep(settings.scrape_delay_ms / 1000)
        finally:
            await browser.close()

    return results


def scrape(search_url: str, pages: int | None = None) -> list[Listing]:
    """Synchronous wrapper. Returns listings in search-result order."""
    pages = pages or settings.scrape_pages
    return asyncio.run(_scrape_async(search_url, pages))


def to_dicts(listings: list[Listing]) -> list[dict]:
    return [asdict(l) | {"per_night": l.per_night} for l in listings]
