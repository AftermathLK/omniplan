"""Time helpers shared by the HA-free modules."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


def utc_now() -> datetime:
    """Return the current aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_utc_iso(value: datetime) -> str:
    """Serialize an aware datetime as a fixed-width UTC ISO string.

    A fixed width (always microseconds, always +00:00) keeps the stored strings
    lexicographically sortable, which the database relies on for comparisons.
    """
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not allowed")
    return value.astimezone(timezone.utc).strftime(ISO_FORMAT)


def utc_now_iso() -> str:
    """Return the current time as a stored timestamp string."""
    return to_utc_iso(utc_now())


def parse_datetime(value: str | datetime | None, default_tz: tzinfo | None = None) -> datetime | None:
    """Parse an ISO datetime string; naive values get ``default_tz`` (or UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_tz or timezone.utc)
    return parsed


def parse_date(value: str | date | None) -> date | None:
    """Parse an ISO date (or the date part of an ISO datetime)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_time(value: str | time | None, fallback: time) -> time:
    """Parse ``HH:MM`` or ``HH:MM:SS``."""
    if isinstance(value, time):
        return value
    if not value:
        return fallback
    try:
        return time.fromisoformat(str(value).strip())
    except ValueError:
        return fallback


def logical_today(now_local: datetime, rollover_hour: int) -> date:
    """Return the planner's "today": before the rollover hour it is still yesterday."""
    if now_local.hour < rollover_hour:
        return (now_local - timedelta(days=1)).date()
    return now_local.date()


def bucket_for_hour(hour: int) -> str:
    """Map an hour of the day to a time-of-day bucket."""
    if 4 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    return "evening"
