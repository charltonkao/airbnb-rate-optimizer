"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    # Storage
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:////data/optimizer.db")
    )

    # Scheduling
    timezone: str = field(default_factory=lambda: os.getenv("TZ", "America/New_York"))
    daily_hour: int = field(default_factory=lambda: _int("DAILY_RUN_HOUR", 8))
    daily_minute: int = field(default_factory=lambda: _int("DAILY_RUN_MINUTE", 5))
    run_on_startup: bool = field(default_factory=lambda: _bool("RUN_ON_STARTUP", False))

    # Scraping
    scrape_pages: int = field(default_factory=lambda: _int("SCRAPE_PAGES", 3))
    scrape_timeout_ms: int = field(default_factory=lambda: _int("SCRAPE_TIMEOUT_MS", 45000))
    scrape_locale: str = field(default_factory=lambda: os.getenv("SCRAPE_LOCALE", "en-US"))
    scrape_currency: str = field(default_factory=lambda: os.getenv("SCRAPE_CURRENCY", "USD"))
    # Polite delay between page loads, milliseconds.
    scrape_delay_ms: int = field(default_factory=lambda: _int("SCRAPE_DELAY_MS", 2500))

    # Email
    email_enabled: bool = field(default_factory=lambda: _bool("EMAIL_ENABLED", True))
    email_to: str = field(default_factory=lambda: os.getenv("EMAIL_TO", ""))
    email_from: str = field(default_factory=lambda: os.getenv("EMAIL_FROM", ""))
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_pass: str = field(default_factory=lambda: os.getenv("SMTP_PASS", ""))
    smtp_starttls: bool = field(default_factory=lambda: _bool("SMTP_STARTTLS", True))
    smtp_ssl: bool = field(default_factory=lambda: _bool("SMTP_SSL", False))

    # App
    base_url: str = field(default_factory=lambda: os.getenv("BASE_URL", "http://localhost:8080"))

    def email_ready(self) -> bool:
        return bool(
            self.email_enabled
            and self.smtp_host
            and self.email_to
            and (self.email_from or self.smtp_user)
        )

    def sender(self) -> str:
        return self.email_from or self.smtp_user


settings = Settings()
