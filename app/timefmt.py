"""Format timestamps for display in the timezone configured by TZ."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ_NAME = os.getenv("TZ", "UTC")
try:
    TIMEZONE = ZoneInfo(TZ_NAME)
except Exception:
    TIMEZONE = timezone.utc
    TZ_NAME = "UTC"

def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None

    if parsed.tzinfo is None:
        # SQLite CURRENT_TIMESTAMP and Docker timestamps without an offset are UTC.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(TIMEZONE)


def format_time(value, default: str = "-") -> str:
    """Render an ISO/SQLite timestamp as 'YYYY-MM-DD HH:MM:SS' in TZ."""
    parsed = _as_datetime(value)
    if parsed is None:
        return str(value) if value not in (None, "") else default
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def timezone_label() -> str:
    return TZ_NAME
