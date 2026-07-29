# Airbnb Rate Optimizer

A small self-hosted service that watches the public Airbnb search results for
your dates, works out what weekly discount would put you just under the
cheapest genuinely comparable listing, and emails you a daily digest.

It **recommends**. It never touches your listing.

---

## What it does

Every morning, for each availability window you define:

1. Loads the public Airbnb search page for your market and exact dates.
2. Finds your listing, records your **search rank** and all-in guest total.
3. Builds a comparable set — same bedroom count or more, minimum baths, has reviews — and finds the cheapest.
4. Computes the weekly discount % that lands you a configurable margin under that comp, respecting a nightly floor you set.
5. Emails you the rank, the comp table, and the one number to change.

### What it deliberately does not do

**It cannot report your view count, and it will not log in as you.** Views and
impressions live in Airbnb's host dashboard behind authentication, and there is
no host API. Getting them would mean storing your Airbnb password and
automating a login — that violates Airbnb's Terms of Service and risks your
account, which is the thing this tool exists to protect.

Search rank is the substitute, and arguably the better metric: it is the thing
that *causes* views, and it is visible without credentials.

**It does not change your prices.** Applying the recommendation is two clicks in
Airbnb, and the email deep-links you straight to the settings page. Keeping a
human in the loop is deliberate: an automated pricing bug on a live listing is
expensive, and automated writes need host authentication anyway.

### A word on scraping

This reads only anonymous, publicly visible search pages — the same HTML any
visitor gets. It ships with a deliberate delay between page loads and defaults
to three pages a day per window. That is a rounding error against Airbnb's
traffic. Even so, automated access sits against the letter of Airbnb's ToS, and
you should decide for yourself whether you are comfortable. Raising
`SCRAPE_PAGES` or lowering `SCRAPE_DELAY_MS` increases your odds of being
rate-limited or IP-blocked; the defaults are chosen to keep you well clear.

---

## Deploying on Synology via Portainer

### 1. Create a data folder

In File Station, make a folder for the SQLite database, e.g.
`/volume1/docker/airbnb-optimizer`.

### 2. Add the stack

Portainer → **Stacks** → **Add stack** → **Repository**.

| Field | Value |
|---|---|
| Repository URL | `https://github.com/<you>/airbnb-rate-optimizer` |
| Reference | `refs/heads/main` |
| Compose path | `docker-compose.yml` |

Or pick **Web editor** and paste the contents of `docker-compose.yml`.

### 3. Set environment variables

In the stack's **Environment variables** section:

| Variable | Example | Notes |
|---|---|---|
| `DATA_PATH` | `/volume1/docker/airbnb-optimizer` | Must exist |
| `HOST_PORT` | `8080` | Change if taken |
| `TZ` | `America/New_York` | Drives the daily run time |
| `BASE_URL` | `http://synology.local:8080` | Used for links in emails |
| `DAILY_RUN_HOUR` | `8` | Local time |
| `EMAIL_TO` | `you@example.com` | |
| `EMAIL_FROM` | `you@example.com` | |
| `SMTP_HOST` | `smtp.gmail.com` | |
| `SMTP_PORT` | `587` | |
| `SMTP_USER` | `you@gmail.com` | |
| `SMTP_PASS` | *app password* | **Not** your account password |

Gmail requires an [App Password](https://myaccount.google.com/apppasswords)
with 2FA enabled. Fastmail and most hosts work the same way. If your ISP blocks
port 587, set `SMTP_PORT=465` and `SMTP_SSL=true`.

### 4. Deploy, then open `http://<synology>:8080`

First build pulls the Playwright image and takes a few minutes.

> **Memory:** Chromium needs room. The compose file requests a 1 GB shm and a
> 2 GB limit. On a 2 GB NAS, lower `SCRAPE_PAGES` to `1` and drop the limit to
> `1g`, or scrapes will be killed mid-run.

---

## Configuring a window

| Field | Meaning |
|---|---|
| **Listing ID** | The number in `airbnb.com/rooms/<id>` |
| **Title contains** | Fallback match if the ID isn't in the card markup |
| **Check-in / Check-out** | The window to fill. Check-out is the day guests leave |
| **Max bookings** | How many separate reservations you'll accept. Drives the recommended minimum-nights |
| **Nightly price** | What you currently have set in Airbnb for these dates. If you use a per-date custom price, enter that — **not** your base price |
| **Current weekly discount** | What's set today, so the app can measure the delta |
| **Floor nightly** | Hard stop. The recommendation never implies an effective rate below this |
| **Undercut margin** | Dollars below the cheapest comp to target |
| **Never raise price back** | Ratchet mode. Recommendations only ever go down |
| **Min baths / Min reviews** | Keeps the comp set honest. An unreviewed 1-bath is not your competitor |

### The fallback ladder

Comp-undercutting alone can leave you static if every competitor is expensive.
The ladder guarantees the price still steps down as the date approaches:

```json
[
  {"days_out": 10, "discount": 15},
  {"days_out": 7,  "discount": 22},
  {"days_out": 4,  "discount": 30},
  {"days_out": 2,  "discount": 38},
  {"days_out": 0,  "discount": 45}
]
```

The recommendation is the **more aggressive** of the ladder and the comp
undercut, then clamped by your floor and max discount.

---

## Two Airbnb quirks the app knows about

**Weekly discount cannot exceed monthly.** Airbnb rejects the save outright. If
the app recommends 30% weekly and your monthly sits at 20%, raise monthly
first. Every email says this.

**Per-date prices override the base price.** If you set a custom price on
specific calendar dates, changing your listing's base price does nothing for
those dates. Enter the per-date price in the **Nightly price** field.

---

## How the math works

The fee-and-tax wedge between your nightly rate and the guest's total is
inferred from live data rather than guessed:

```
fees = observed_guest_total − nightly × nights × (1 − current_discount)
```

Then, to land on a target total:

```
discount = 1 − (target_total − fees) / (nightly × nights)
```

Clamped to `[0, min(max_discount, floor_cap)]` where
`floor_cap = 1 − floor_nightly / nightly`. The final value is **floored** to one
decimal, never rounded, so rounding can't breach your floor.

---

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env    # then edit

DATABASE_URL=sqlite:///./dev.db uvicorn app.main:app --reload --port 8080
pytest -q
```

18 unit tests cover the pricing math, guardrails, comp filtering, and the
search-card parser. They run without Playwright or a database.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Listing not found in the first N results" | Booked, unavailable, blocked by a min-night rule, or ranked past `SCRAPE_PAGES`. Check the listing manually first — if it's booked, you're done |
| Scrape times out | Raise `SCRAPE_TIMEOUT_MS`. Airbnb is slow to render |
| Container OOM-killed | Chromium needs memory. Lower `SCRAPE_PAGES`, raise the limit |
| No email | Check `/healthz` and container logs. The app logs SMTP failures but never crashes a run over them |
| Comps look wrong | Tighten `min_baths` / `min_reviews`, or narrow `market_slug` |

Airbnb changes its markup without warning. If parsing degrades, the selector
lives in `app/scraper.py` (`[data-testid="card-container"]`) and the field
regexes are right below it — that is the one file likely to need occasional
maintenance.

---

## License

MIT — see `LICENSE`.
