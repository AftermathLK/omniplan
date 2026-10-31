"""Recurrence calculations (pure logic).

Two kinds of recurrence are supported:

* **calendar** – an RFC 5545 RRULE (``FREQ=WEEKLY;BYDAY=MO,TH``). The next due
  date is the first occurrence strictly after both the current due date and
  today, so finishing a chore late never schedules the next one in the past.
* **rolling** – ``N`` days after the *actual* completion date (furnace filter
  every 90 days after it was really changed).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from dateutil.rrule import rrulestr

from .const import RECURRENCE_CALENDAR, RECURRENCE_ROLLING

WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def normalize_rrule(rule: str | None) -> str | None:
    """Strip an optional ``RRULE:`` prefix and whitespace; return None for empty rules."""
    if not rule:
        return None
    text = rule.strip()
    if text.upper().startswith("RRULE:"):
        text = text[6:]
    text = ";".join(part.strip().upper() for part in text.split(";") if part.strip())
    return text or None


def validate_rrule(rule: str) -> str:
    """Return the normalized rule or raise ``ValueError``."""
    normalized = normalize_rrule(rule)
    if normalized is None:
        raise ValueError("empty recurrence rule")
    if "FREQ=" not in normalized:
        raise ValueError("recurrence rule must contain FREQ")
    rrulestr(normalized, dtstart=datetime(2020, 1, 1))
    return normalized


def next_calendar_occurrence(rule: str, anchor: date, after: date) -> date | None:
    """Return the first occurrence of ``rule`` strictly after ``after``.

    ``anchor`` is the series start (dtstart), which aligns INTERVAL>1 rules.
    """
    normalized = validate_rrule(rule)
    series = rrulestr(normalized, dtstart=datetime.combine(anchor, time()))
    occurrence = series.after(datetime.combine(after, time(23, 59, 59)), inc=False)
    return occurrence.date() if occurrence else None


def first_calendar_occurrence(rule: str, anchor: date, on_or_after: date) -> date | None:
    """Return the first occurrence on or after ``on_or_after``."""
    return next_calendar_occurrence(rule, anchor, on_or_after - timedelta(days=1))


def next_due_date(
    *,
    mode: str,
    rule: str | None,
    interval_days: int | None,
    current_due: date | None,
    anchor: date | None,
    completed_on: date,
    today: date,
) -> date | None:
    """Compute the next due date after a completion."""
    if mode == RECURRENCE_ROLLING:
        if not interval_days or interval_days < 1:
            return None
        return completed_on + timedelta(days=int(interval_days))
    if mode == RECURRENCE_CALENDAR and rule:
        reference = max(current_due or today, today)
        return next_calendar_occurrence(rule, anchor or current_due or today, reference)
    return None


def initial_due_date(
    *, mode: str, rule: str | None, interval_days: int | None, anchor: date | None, today: date
) -> date | None:
    """The first due date for a newly recurring task that has none."""
    if mode == RECURRENCE_ROLLING and interval_days:
        return today
    if mode == RECURRENCE_CALENDAR and rule:
        return first_calendar_occurrence(rule, anchor or today, today)
    return None


def describe(mode: str, rule: str | None, interval_days: int | None) -> str:
    """Human-readable description used in the UI and native todo descriptions."""
    if mode == RECURRENCE_ROLLING and interval_days:
        unit = "day" if interval_days == 1 else "days"
        return f"Every {interval_days} {unit} after completion"
    if mode != RECURRENCE_CALENDAR or not rule:
        return ""
    parts = dict(part.split("=", 1) for part in (normalize_rrule(rule) or "").split(";") if "=" in part)
    freq = parts.get("FREQ", "")
    interval = int(parts.get("INTERVAL", "1") or 1)
    names = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun"}
    if freq == "DAILY":
        return "Daily" if interval == 1 else f"Every {interval} days"
    if freq == "WEEKLY":
        days = [names.get(code[-2:], code) for code in parts.get("BYDAY", "").split(",") if code]
        base = "Weekly" if interval == 1 else f"Every {interval} weeks"
        return f"{base} on {', '.join(days)}" if days else base
    if freq == "MONTHLY":
        base = "Monthly" if interval == 1 else f"Every {interval} months"
        if parts.get("BYMONTHDAY"):
            return f"{base} on day {parts['BYMONTHDAY']}"
        return base
    if freq == "YEARLY":
        return "Yearly" if interval == 1 else f"Every {interval} years"
    return rule
