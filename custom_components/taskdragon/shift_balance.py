"""PRN / shift-aware chore assignment (pure logic).

Each morning the coordinator reads every household member's work calendar for
the day, turns matching events into :class:`ShiftWindow` objects and asks
:func:`balance_chores` who should take each ``shift_aware`` chore:

1. People who are off duty today get the chores, spread by workload.
2. If everyone works, the person whose shift ends earliest gets the chore.
3. Ties are broken by current workload, then by configuration order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

DEFAULT_CHORE_WEIGHT = 15  # minutes, used when a chore has no estimate


@dataclass(frozen=True)
class ShiftWindow:
    """A work shift overlapping the day being planned."""

    start: datetime
    end: datetime
    all_day: bool = False
    summary: str = ""


@dataclass
class PersonAvailability:
    """A household member and their shifts for the day."""

    entity_id: str
    shifts: list[ShiftWindow] = field(default_factory=list)

    @property
    def working(self) -> bool:
        return bool(self.shifts)

    @property
    def shift_end(self) -> datetime | None:
        return max((shift.end for shift in self.shifts), default=None)


@dataclass(frozen=True)
class Chore:
    """A chore to assign."""

    uid: str
    estimated_minutes: int | None = None
    current_assignees: tuple[str, ...] = ()

    @property
    def weight(self) -> int:
        return int(self.estimated_minutes or DEFAULT_CHORE_WEIGHT)


def parse_keywords(raw: str | Sequence[str] | None) -> list[str]:
    """Split a comma separated keyword string into a clean list."""
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    return [item.strip().lower() for item in items if item and item.strip()]


def event_is_shift(summary: str | None, description: str | None, keywords: Sequence[str]) -> bool:
    """Return True if a calendar event counts as a work shift.

    With no keywords configured every event on the work calendar is a shift.
    """
    if not keywords:
        return True
    haystack = f"{summary or ''} {description or ''}".lower()
    return any(keyword in haystack for keyword in keywords)


def _coerce_event_time(value: Any, tz: tzinfo, *, is_end: bool) -> tuple[datetime, bool] | None:
    """Turn a calendar event start/end (date, datetime or ISO string) into a local datetime."""
    if isinstance(value, dict):
        value = value.get("dateTime") or value.get("date")
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=tz)
        return moment.astimezone(tz), False
    if isinstance(value, date):
        return datetime.combine(value, time(), tz), True
    if isinstance(value, str) and value:
        text = value.strip()
        if len(text) == 10:
            try:
                return datetime.combine(date.fromisoformat(text), time(), tz), True
            except ValueError:
                return None
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=tz)
        return moment.astimezone(tz), False
    return None


def shifts_for_day(
    events: Iterable[Mapping[str, Any]],
    keywords: Sequence[str],
    day: date,
    tz: tzinfo,
) -> list[ShiftWindow]:
    """Filter calendar events down to the work shifts that overlap ``day``."""
    day_start = datetime.combine(day, time(), tz)
    day_end = day_start + timedelta(days=1)
    windows: list[ShiftWindow] = []
    for event in events:
        if not event_is_shift(event.get("summary"), event.get("description"), keywords):
            continue
        start = _coerce_event_time(event.get("start"), tz, is_end=False)
        end = _coerce_event_time(event.get("end"), tz, is_end=True)
        if start is None:
            continue
        start_dt, all_day = start
        if end is None:
            end_dt = start_dt + (timedelta(days=1) if all_day else timedelta(hours=1))
        else:
            end_dt = end[0]
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(days=1) if all_day else start_dt
        if start_dt < day_end and end_dt > day_start:
            windows.append(ShiftWindow(start=start_dt, end=end_dt, all_day=all_day, summary=str(event.get("summary") or "")))
    return windows


def choose_assignee(
    people: Sequence[PersonAvailability],
    load: Mapping[str, int],
) -> str | None:
    """Pick who should take the next chore given current ``load`` (minutes)."""
    if not people:
        return None
    order = {person.entity_id: index for index, person in enumerate(people)}
    off_duty = [person for person in people if not person.working]
    if off_duty:
        return min(off_duty, key=lambda p: (load.get(p.entity_id, 0), order[p.entity_id])).entity_id
    return min(
        people,
        key=lambda p: (p.shift_end, load.get(p.entity_id, 0), order[p.entity_id]),
    ).entity_id


def balance_chores(
    chores: Sequence[Chore],
    people: Sequence[PersonAvailability],
    initial_load: Mapping[str, int] | None = None,
) -> dict[str, str]:
    """Assign every chore; returns ``{chore_uid: person_entity_id}``.

    Heavier chores are placed first so the greedy spread stays even.
    """
    load: dict[str, int] = {person.entity_id: 0 for person in people}
    for entity_id, minutes in (initial_load or {}).items():
        if entity_id in load:
            load[entity_id] += int(minutes)
    assignments: dict[str, str] = {}
    for chore in sorted(chores, key=lambda c: (-c.weight, c.uid)):
        chosen = choose_assignee(people, load)
        if chosen is None:
            break
        assignments[chore.uid] = chosen
        load[chosen] += chore.weight
    return assignments
