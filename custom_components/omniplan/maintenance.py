"""Maintenance engine: schedules, sensor triggers, status, adaptive intervals,
water-softener salt learning, regeneration detection and spend reports.

Everything here is a pure function over plain dicts, dates and datetimes. It
imports nothing from Home Assistant or the database, so the whole engine is
unit tested directly. :mod:`.maintenance_manager` persists the results and the
Home Assistant runtime feeds it sensor states.

Vocabulary:

* A *schedule* is ``none`` (trigger-only), ``once``, ``rolling`` (next due =
  actual completion + interval) or ``planned`` (anchored, so completing a
  Tuesday task on Wednesday doesn't move future cycles).
* A *trigger* watches a Home Assistant entity: ``threshold`` (above/below for
  N minutes), ``counter`` (a cumulative sensor grows by a delta), ``runtime``
  (hours spent in a state), ``state_count`` (N transitions), ``problem`` (a
  problem/moisture binary sensor) or ``salt_level`` (the softener model).
* ``or`` logic: any condition makes the task due. ``and``: all of them.
"""

from __future__ import annotations

import calendar
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from .const import (
    COST_MODE_CONSUMPTION,
    COST_MODE_PURCHASE,
    EVENT_KIND_COMPLETED,
    EVENT_KIND_PURCHASE,
    MAINT_DUE_SOON,
    MAINT_OK,
    MAINT_OVERDUE,
    MAINT_TRIGGERED,
    SALT_OBS_EMPTY,
    SALT_OBS_FILL,
    SALT_SCALE_BAGS,
    SALT_SCALE_LEVELS,
    SALT_SCALE_PERCENT,
    SCHEDULE_NONE,
    SCHEDULE_ONCE,
    SCHEDULE_PLANNED,
    SCHEDULE_ROLLING,
    TRIGGER_COUNTER,
    TRIGGER_PROBLEM,
    TRIGGER_RUNTIME,
    TRIGGER_SALT,
    TRIGGER_STATE_COUNT,
    TRIGGER_THRESHOLD,
)

UNAVAILABLE = (None, "", "unavailable", "unknown")
UNIT_DAYS = {"days": 1.0, "weeks": 7.0, "months": 30.436875, "years": 365.2425}
UNIT_SINGULAR = {"days": "Day", "weeks": "Week", "months": "Month", "years": "Year"}


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------
def to_iso(moment: datetime) -> str:
    return moment.isoformat()


def from_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def to_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def to_time(value: Any) -> time | None:
    if not value:
        return None
    if isinstance(value, time):
        return value
    match = re.match(r"^(\d{1,2}):(\d{2})", str(value))
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return time(int(match.group(1)), int(match.group(2)))


def num(value: Any) -> float | None:
    if value in UNAVAILABLE:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def fmt_number(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "?"
    if digits == 0 or abs(value - round(value)) < 1e-9:
        return f"{round(value):,}"
    return f"{value:,.{digits}f}"


# ---------------------------------------------------------------------------
# Intervals & schedules
# ---------------------------------------------------------------------------
def add_months(start: date, months: int) -> date:
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def nth_occurrence(anchor: date, value: int, unit: str, n: int) -> date:
    """``anchor`` + n intervals, always counted from the anchor (so Jan 31 stays the month's end)."""
    if unit == "days":
        return anchor + timedelta(days=value * n)
    if unit == "weeks":
        return anchor + timedelta(weeks=value * n)
    if unit == "months":
        return add_months(anchor, value * n)
    if unit == "years":
        return add_months(anchor, 12 * value * n)
    raise ValueError(f"unknown interval unit {unit}")


def add_interval(start: date, value: int, unit: str) -> date:
    return nth_occurrence(start, value, unit, 1)


def interval_days(value: float | None, unit: str) -> float:
    return float(value or 0) * UNIT_DAYS.get(unit, 1.0)


def describe_interval(value: int | None, unit: str) -> str:
    if not value:
        return "—"
    word = UNIT_SINGULAR.get(unit, unit)
    return f"{value} {word}" + ("" if value == 1 else "s")


def in_season(day: date, active_months: Sequence[int] | None) -> bool:
    return not active_months or day.month in active_months


def season_shift(day: date, active_months: Sequence[int] | None) -> date:
    """Move a due date that falls outside the active months to the 1st of the next active month."""
    if in_season(day, active_months):
        return day
    probe = date(day.year, day.month, 1)
    for _ in range(12):
        probe = add_months(probe, 1)
        if probe.month in active_months:  # type: ignore[operator]
            return probe
    return day


def next_due_after(
    *,
    mode: str,
    value: int | None,
    unit: str,
    anchor: date | None,
    previous_due: date | None,
    completed_on: date,
    active_months: Sequence[int] | None = None,
) -> date | None:
    """The next due date once a task was completed (or skipped) on ``completed_on``."""
    if mode in (SCHEDULE_NONE, SCHEDULE_ONCE) or not value:
        return None
    if mode == SCHEDULE_ROLLING:
        upcoming = add_interval(completed_on, value, unit)
    else:
        anchor = anchor or previous_due or completed_on
        floor = max(day for day in (previous_due, completed_on) if day)
        step = max(interval_days(value, unit), 1.0)
        n = max(0, int((floor - anchor).days // step) - 1)
        while nth_occurrence(anchor, value, unit, n) <= floor:
            n += 1
        upcoming = nth_occurrence(anchor, value, unit, n)
    return season_shift(upcoming, active_months)


def first_due(
    *, mode: str, value: int | None, unit: str, start: date, active_months: Sequence[int] | None = None
) -> date | None:
    if mode in (SCHEDULE_NONE, SCHEDULE_ONCE) or not value:
        return None
    return season_shift(add_interval(start, value, unit), active_months)


def due_moment(due: date, schedule_time: time | None, tz: tzinfo) -> datetime:
    """When a task flips to Overdue: at ``schedule_time`` on the due date, else when the day ends."""
    if schedule_time is not None:
        return datetime.combine(due, schedule_time, tz)
    return datetime.combine(due + timedelta(days=1), time(0, 0), tz)


def countdown(days_left: int) -> str:
    if days_left > 1:
        return f"{days_left:,} days"
    if days_left == 1:
        return "1 day"
    if days_left == 0:
        return "Due today"
    return "1 day overdue" if days_left == -1 else f"{-days_left:,} days overdue"


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------
def _matches(value: Any, target: Any) -> bool:
    wanted = [part.strip().lower() for part in str(target if target not in (None, "") else "on").split(",") if part.strip()]
    return str(value).strip().lower() in wanted


def in_target(cfg: Mapping[str, Any], state: Any, attrs: Mapping[str, Any] | None) -> bool:
    """Runtime triggers: is the entity (or one of its attributes) in the counted state?"""
    attribute = cfg.get("attribute")
    value = (attrs or {}).get(attribute) if attribute else state
    if value in UNAVAILABLE:
        return False
    return _matches(value, cfg.get("target_state"))


def threshold_condition(cfg: Mapping[str, Any], value: Any) -> bool:
    current = num(value)
    if current is None:
        return False
    above, below = num(cfg.get("above")), num(cfg.get("below"))
    return (above is not None and current > above) or (below is not None and current < below)


def _counter_feed(state: dict[str, Any], value: float) -> dict[str, Any]:
    if state.get("baseline") is None:
        return {"baseline": value, "offset": 0.0, "last": value}
    last = float(state.get("last", value))
    if value < last - 1e-9:
        # The meter reset (or was replaced): keep what was already counted.
        state["offset"] = float(state.get("offset") or 0.0) + last - float(state["baseline"])
        state["baseline"] = value
    state["last"] = value
    return state


def _latch_threshold(cfg: Mapping[str, Any], state: dict[str, Any], now: datetime) -> dict[str, Any]:
    since = from_iso(state.get("cond_since"))
    if since is not None and (now - since).total_seconds() >= float(cfg.get("for_minutes") or 0) * 60:
        state["latched"] = True
    return state


def trigger_update(
    cfg: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    *,
    old: Any,
    new: Any,
    old_attrs: Mapping[str, Any] | None,
    new_attrs: Mapping[str, Any] | None,
    now: datetime,
) -> tuple[dict[str, Any], list[str]]:
    """Fold one state change into a trigger's persisted state. Returns (state, signals)."""
    kind = cfg.get("type")
    st: dict[str, Any] = dict(state or {})
    signals: list[str] = []
    if kind == TRIGGER_THRESHOLD:
        if threshold_condition(cfg, new):
            st["cond_since"] = st.get("cond_since") or to_iso(now)
        elif not st.get("latched"):
            st["cond_since"] = None
        st = _latch_threshold(cfg, st, now)
    elif kind == TRIGGER_COUNTER:
        value = num(new)
        if value is not None:
            st = _counter_feed(st, value)
    elif kind == TRIGGER_RUNTIME:
        active = from_iso(st.get("active_since"))
        now_in = in_target(cfg, new, new_attrs)
        if active is not None and not now_in:
            st["accumulated"] = float(st.get("accumulated") or 0.0) + max(0.0, (now - active).total_seconds())
            st["active_since"] = None
        elif active is None and now_in:
            st["active_since"] = to_iso(now)
    elif kind == TRIGGER_STATE_COUNT:
        # unavailable → x is how Home Assistant restores states after a restart: never count it.
        if old not in UNAVAILABLE and new not in UNAVAILABLE and old != new:
            from_ok = not cfg.get("from_state") or _matches(old, cfg.get("from_state"))
            if from_ok and _matches(new, cfg.get("to_state")):
                last = from_iso(st.get("last_counted_at"))
                debounce = float(cfg.get("debounce_seconds") or 0)
                if last is None or (now - last).total_seconds() >= debounce:
                    st["count"] = int(st.get("count") or 0) + 1
                    st["last_counted_at"] = to_iso(now)
    elif kind == TRIGGER_PROBLEM:
        if new == "on":
            st["latched"] = True
        elif new == "off" and old == "on" and cfg.get("auto_complete"):
            signals.append("auto_complete")
    return st, signals


def trigger_tick(
    cfg: Mapping[str, Any], state: Mapping[str, Any] | None, *, current: Any, attrs: Mapping[str, Any] | None, now: datetime
) -> dict[str, Any]:
    """Periodic upkeep: threshold timers, runtime folding (so progress survives restarts)."""
    kind = cfg.get("type")
    st: dict[str, Any] = dict(state or {})
    if kind == TRIGGER_THRESHOLD:
        if threshold_condition(cfg, current):
            st["cond_since"] = st.get("cond_since") or to_iso(now)
        elif not st.get("latched"):
            st["cond_since"] = None
        st = _latch_threshold(cfg, st, now)
    elif kind == TRIGGER_RUNTIME:
        active = from_iso(st.get("active_since"))
        now_in = in_target(cfg, current, attrs)
        if active is not None:
            st["accumulated"] = float(st.get("accumulated") or 0.0) + max(0.0, (now - active).total_seconds())
            st["active_since"] = to_iso(now) if now_in else None
        elif now_in:
            st["active_since"] = to_iso(now)
    elif kind == TRIGGER_COUNTER:
        value = num(current)
        if value is not None:
            st = _counter_feed(st, value)
    elif kind == TRIGGER_PROBLEM and current == "on":
        st["latched"] = True
    return st


def trigger_startup(
    cfg: Mapping[str, Any], state: Mapping[str, Any] | None, *, current: Any, attrs: Mapping[str, Any] | None, now: datetime
) -> dict[str, Any]:
    """After a restart: runtime spent while Home Assistant was down is unknown, so it isn't counted."""
    st: dict[str, Any] = dict(state or {})
    if cfg.get("type") == TRIGGER_RUNTIME:
        st["active_since"] = to_iso(now) if in_target(cfg, current, attrs) else None
    return st


def trigger_reset(cfg: Mapping[str, Any], *, current: Any, attrs: Mapping[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Fresh state for a new cycle (after completion or skip)."""
    kind = cfg.get("type")
    if kind == TRIGGER_THRESHOLD:
        return {"cond_since": to_iso(now) if threshold_condition(cfg, current) else None, "latched": False}
    if kind == TRIGGER_COUNTER:
        value = num(current)
        return {"baseline": value, "offset": 0.0, "last": value} if value is not None else {}
    if kind == TRIGGER_RUNTIME:
        return {"accumulated": 0.0, "active_since": to_iso(now) if in_target(cfg, current, attrs) else None}
    if kind == TRIGGER_STATE_COUNT:
        return {"count": 0, "last_counted_at": None}
    if kind == TRIGGER_PROBLEM:
        return {"latched": current == "on"}
    return {}


@dataclass
class TriggerResult:
    met: bool
    progress: float
    label: str
    remaining: float | None = None
    unit: str = ""
    configured: bool = True


def trigger_result(
    cfg: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    *,
    current: Any,
    attrs: Mapping[str, Any] | None,
    now: datetime,
) -> TriggerResult:
    kind = cfg.get("type")
    st = state or {}
    unit = str((attrs or {}).get("unit_of_measurement") or cfg.get("unit") or "")
    if not cfg.get("entity_id") and kind != TRIGGER_SALT:
        return TriggerResult(False, 0.0, "Pick an entity to watch", configured=False)
    if kind == TRIGGER_THRESHOLD:
        since = from_iso(st.get("cond_since"))
        minutes = float(cfg.get("for_minutes") or 0)
        held = (now - since).total_seconds() / 60 if since else 0.0
        met = bool(st.get("latched")) or (since is not None and held >= minutes)
        progress = 1.0 if met else (min(0.99, held / minutes) if since and minutes else 0.0)
        bound = f"below {fmt_number(num(cfg.get('below')), 2)}" if cfg.get("below") is not None else f"above {fmt_number(num(cfg.get('above')), 2)}"
        value = num(current)
        label = f"{fmt_number(value, 1)}{(' ' + unit) if unit else ''} · trigger {bound}" + (f" for {minutes:g} min" if minutes else "")
        return TriggerResult(met, progress, label, unit=unit)
    if kind == TRIGGER_COUNTER:
        delta = num(cfg.get("delta")) or 0.0
        if st.get("baseline") is None or delta <= 0:
            return TriggerResult(False, 0.0, "Waiting for the first reading", unit=unit)
        used = float(st.get("offset") or 0.0) + float(st.get("last", st["baseline"])) - float(st["baseline"])
        return TriggerResult(
            used >= delta,
            used / delta,
            f"{fmt_number(used)} / {fmt_number(delta)}{(' ' + unit) if unit else ''}",
            remaining=max(0.0, delta - used),
            unit=unit,
        )
    if kind == TRIGGER_RUNTIME:
        hours = num(cfg.get("target_hours")) or 0.0
        active = from_iso(st.get("active_since"))
        total = float(st.get("accumulated") or 0.0) + ((now - active).total_seconds() if active else 0.0)
        if hours <= 0:
            return TriggerResult(False, 0.0, "Set the runtime hours")
        return TriggerResult(
            total >= hours * 3600,
            total / (hours * 3600),
            f"{total / 3600:,.1f} / {hours:g} h",
            remaining=max(0.0, hours - total / 3600),
            unit="h",
        )
    if kind == TRIGGER_STATE_COUNT:
        target = int(num(cfg.get("count")) or 0)
        count = int(st.get("count") or 0)
        if target <= 0:
            return TriggerResult(False, 0.0, "Set how many changes")
        return TriggerResult(count >= target, count / target, f"{count} / {target} × {cfg.get('to_state') or 'on'}", remaining=target - count)
    if kind == TRIGGER_PROBLEM:
        met = bool(st.get("latched")) or current == "on"
        return TriggerResult(met, 1.0 if met else 0.0, "Problem detected" if met else "No problem reported")
    return TriggerResult(False, 0.0, "")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
@dataclass
class TaskStatus:
    status: str
    due_date: date | None
    days_left: int | None
    progress: float
    countdown: str
    in_season: bool = True
    reasons: list[str] = field(default_factory=list)
    due_at: datetime | None = None


def compute_status(
    task: Mapping[str, Any],
    triggers: Sequence[TriggerResult],
    *,
    now: datetime,
    tz: tzinfo,
    extra_countdown: str | None = None,
) -> TaskStatus:
    """OK / Due Soon / Overdue / Triggered from the schedule and the trigger results.

    Conditions are the schedule (if any) plus every configured trigger. With
    ``or`` logic one met condition is enough, with ``and`` all must be met.
    A met trigger makes the task *Triggered*; a met schedule alone makes it
    *Overdue*. Inside the warning window (``warning_days`` for dates,
    ``warning_percent`` of the way for sensors) it is *Due Soon*.
    """
    local_now = now.astimezone(tz)
    today = local_now.date()
    active_months = task.get("active_months") or []
    next_due = to_date(task.get("next_due"))
    mode = task.get("schedule_mode") or SCHEDULE_NONE
    warning_days = int(task.get("warning_days") if task.get("warning_days") is not None else 7)
    warning_fraction = float(task.get("warning_percent") if task.get("warning_percent") is not None else 10) / 100
    if not in_season(today, active_months):
        return TaskStatus(MAINT_OK, next_due, (next_due - today).days if next_due else None, 0.0, "Out of season", in_season=False)

    conditions: list[tuple[str, bool, bool, float, str]] = []
    days_left = None
    due_at = None
    if mode != SCHEDULE_NONE and next_due is not None:
        due_at = due_moment(next_due, to_time(task.get("schedule_time")), tz)
        days_left = (next_due - today).days
        met = local_now >= due_at
        last = from_iso(task.get("last_done"))
        start = last.astimezone(tz).date() if last else None
        if start is None or start >= next_due:
            span = interval_days(task.get("interval_value"), task.get("interval_unit") or "days") or 30.0
            start = next_due - timedelta(days=max(1, round(span)))
        total = max(1, (next_due - start).days)
        progress = 1.0 if met else max(0.0, min(0.999, (today - start).days / total))
        conditions.append(("schedule", met, days_left <= warning_days, progress, countdown(days_left)))
    for result in triggers:
        if not result.configured:
            continue
        warning = result.met or result.progress >= 1 - warning_fraction
        conditions.append(("trigger", result.met, warning, result.progress, result.label))

    if not conditions:
        return TaskStatus(MAINT_OK, next_due, days_left, 0.0, extra_countdown or "No schedule", due_at=due_at)
    logic_and = task.get("trigger_logic") == "and"
    mets = [c[1] for c in conditions]
    warns = [c[2] or c[1] for c in conditions]
    met = all(mets) if logic_and else any(mets)
    warn = all(warns) if logic_and else any(c[2] for c in conditions)
    dominant = (min if logic_and else max)(conditions, key=lambda c: c[3])
    reasons = [c[4] for c in conditions if c[0] == "trigger" and c[1]]
    if met:
        status = MAINT_TRIGGERED if any(c[0] == "trigger" and c[1] for c in conditions) else MAINT_OVERDUE
    elif warn:
        status = MAINT_DUE_SOON
    else:
        status = MAINT_OK
    progress = 1.0 if met else dominant[3]
    label = extra_countdown or (dominant[4] if dominant[0] == "trigger" or days_left is None else countdown(days_left))
    return TaskStatus(status, next_due, days_left, progress, label, reasons=reasons, due_at=due_at)


# ---------------------------------------------------------------------------
# Adaptive intervals: EWA + censored Weibull + seasonal multipliers
# ---------------------------------------------------------------------------
FEEDBACK_FACTORS = {"too_early": 1.2, "just_right": 1.0, "too_late": 0.8}


def ewa(values: Sequence[float], prior: float, alpha: float = 0.3) -> float:
    estimate = float(prior)
    for value in values:
        estimate = alpha * float(value) + (1 - alpha) * estimate
    return estimate


def fit_weibull(times: Sequence[float], events: Sequence[bool]) -> tuple[float, float] | None:
    """Maximum-likelihood Weibull (shape k, scale λ) with right-censoring.

    ``events[i]`` is True when the component needed maintenance at ``times[i]``
    and False when it was still fine (censored). Needs at least two events.
    """
    pairs = [(float(t), bool(e)) for t, e in zip(times, events) if t and t > 0]
    events_count = sum(1 for _, e in pairs if e)
    if len(pairs) < 3 or events_count < 2:
        return None
    mean = sum(t for t, _ in pairs) / len(pairs)
    ts = [t / mean for t, _ in pairs]
    logs = [math.log(t) for t in ts]
    event_log_mean = sum(lg for lg, (_, e) in zip(logs, pairs) if e) / events_count

    def profile(k: float) -> float:
        weights = [t**k for t in ts]
        return sum(w * lg for w, lg in zip(weights, logs)) / sum(weights) - 1 / k - event_log_mean

    low, high = 0.05, 50.0
    if profile(high) < 0:
        shape = high  # (nearly) identical intervals: an extremely regular component
    elif profile(low) > 0:
        return None
    else:
        for _ in range(100):
            mid = (low + high) / 2
            if profile(mid) > 0:
                high = mid
            else:
                low = mid
        shape = (low + high) / 2
    scale = (sum(t**shape for t in ts) / events_count) ** (1 / shape) * mean
    return shape, scale


def weibull_life(shape: float, scale: float, reliability: float = 0.9) -> float:
    """Age at which ``reliability`` of components still don't need maintenance (B10 for 0.9)."""
    return scale * (-math.log(reliability)) ** (1 / shape)


def seasonal_multipliers(samples: Sequence[Mapping[str, Any]], shrink: float = 3.0) -> list[float] | None:
    """Per-month interval multipliers, shrunk toward 1.0; None until there are 6 samples."""
    values = [(_adjusted(s), to_date(s.get("completed_on"))) for s in samples]
    values = [(v, d) for v, d in values if v and d]
    if len(values) < 6:
        return None
    mean = sum(v for v, _ in values) / len(values)
    sums, counts = [0.0] * 12, [0] * 12
    for value, day in values:
        sums[day.month - 1] += value / mean
        counts[day.month - 1] += 1
    return [round((sums[m] + shrink) / (counts[m] + shrink), 3) for m in range(12)]


def _adjusted(sample: Mapping[str, Any]) -> float:
    return float(sample.get("interval_days") or 0) * FEEDBACK_FACTORS.get(sample.get("feedback") or "just_right", 1.0)


def recommend_interval(
    samples: Sequence[Mapping[str, Any]],
    current_days: float,
    *,
    next_due_month: int | None = None,
    alpha: float = 0.3,
    reliability: float = 0.9,
) -> dict[str, Any] | None:
    """Suggest an interval from actual completion intervals and "too early / just right / too late" feedback.

    * EWA of the feedback-adjusted intervals, starting from the current setting.
    * A Weibull fit treating "too early" as still-fine (censored) and "too late"
      as needed a bit before it was done; its B10 life (90 % still fine).
    * Both are blended, then scaled by the learned multiplier for the month the
      next cycle ends in.
    """
    usable = [s for s in samples if (s.get("interval_days") or 0) > 0]
    if not usable or current_days <= 0:
        return None
    adjusted = [_adjusted(s) for s in usable]
    ewa_days = ewa(adjusted, current_days, alpha)
    times, events = [], []
    for sample in usable:
        feedback = sample.get("feedback")
        interval = float(sample["interval_days"])
        times.append(interval * (0.85 if feedback == "too_late" else 1.0))
        events.append(feedback != "too_early")
    fit = fit_weibull(times, events)
    weibull_days = weibull_life(*fit, reliability=reliability) if fit else None
    base = ewa_days if weibull_days is None else 0.5 * ewa_days + 0.5 * weibull_days
    seasonal = seasonal_multipliers(usable)
    multiplier = seasonal[next_due_month - 1] if seasonal and next_due_month else 1.0
    recommended = max(1, round(base * multiplier))
    count = len(usable)
    change = (recommended - current_days) / current_days
    return {
        "interval_days": recommended,
        "ewa_days": round(ewa_days, 1),
        "weibull": {"shape": round(fit[0], 2), "scale_days": round(fit[1], 1), "b10_days": round(weibull_days or 0, 1)} if fit else None,
        "seasonal_multiplier": multiplier,
        "seasonal": seasonal,
        "samples": count,
        "confidence": "low" if count < 3 else "medium" if count < 6 else "high",
        "change_percent": round(change * 100),
        "suggest": count >= 2 and abs(change) >= 0.05,
    }


def days_to_interval(days: float) -> tuple[int, str]:
    """Express a day count in the largest unit it is a whole number of (within 5 % of one unit), else days."""
    for unit in ("years", "months", "weeks"):
        count = days / UNIT_DAYS[unit]
        whole = round(count)
        if whole >= 1 and abs(count - whole) <= 0.05:
            return whole, unit
    return max(1, round(days)), "days"


# ---------------------------------------------------------------------------
# Water softener salt model
# ---------------------------------------------------------------------------
@dataclass
class SaltScale:
    """How a brine tank's level is read: printed levels, a percentage or bags."""

    kind: str = SALT_SCALE_LEVELS
    max_level: float = 8.0
    min_level: float = 0.0
    capacity_bags: float = 4.0
    bag_lb: float = 40.0

    @property
    def bands(self) -> int:
        if self.kind == SALT_SCALE_PERCENT:
            return 10
        if self.kind == SALT_SCALE_BAGS:
            return max(1, round(self.capacity_bags))
        return max(1, round(self.max_level - self.min_level))

    def to_fraction(self, value: float) -> float:
        if self.kind == SALT_SCALE_PERCENT:
            fraction = value / 100
        elif self.kind == SALT_SCALE_BAGS:
            fraction = value / max(self.capacity_bags, 1e-9)
        else:
            fraction = (value - self.min_level) / max(self.max_level - self.min_level, 1e-9)
        return min(1.0, max(0.0, fraction))

    def from_fraction(self, fraction: float) -> float:
        if self.kind == SALT_SCALE_PERCENT:
            return fraction * 100
        if self.kind == SALT_SCALE_BAGS:
            return fraction * self.capacity_bags
        return self.min_level + fraction * (self.max_level - self.min_level)

    def label(self, fraction: float) -> str:
        value = self.from_fraction(fraction)
        if self.kind == SALT_SCALE_PERCENT:
            return f"{round(value)}%"
        if self.kind == SALT_SCALE_BAGS:
            return f"{value:.1f} bags"
        return f"Level {value:.1f} / {self.max_level:g}"

    def markers(self) -> list[float]:
        if self.kind == SALT_SCALE_PERCENT:
            return [100, 75, 50, 25, 0]
        if self.kind == SALT_SCALE_BAGS:
            return [float(b) for b in range(round(self.capacity_bags), -1, -1)]
        return [float(v) for v in range(round(self.max_level), round(self.min_level) - 1, -1)]


def scale_from_config(cfg: Mapping[str, Any]) -> SaltScale:
    return SaltScale(
        kind=cfg.get("scale") if cfg.get("scale") in (SALT_SCALE_LEVELS, SALT_SCALE_PERCENT, SALT_SCALE_BAGS) else SALT_SCALE_LEVELS,
        max_level=float(num(cfg.get("max_level")) or 8),
        min_level=float(num(cfg.get("min_level")) or 0),
        capacity_bags=float(num(cfg.get("capacity_bags")) or 4),
        bag_lb=float(num(cfg.get("bag_lb")) or 40),
    )


def default_tank_volume(cfg: Mapping[str, Any], scale: SaltScale) -> float:
    """Starting guess before any calibration: ~200 gallons of water per pound of salt."""
    configured = num(cfg.get("initial_tank_volume"))
    if configured and configured > 0:
        return configured
    return 200.0 * scale.capacity_bags * scale.bag_lb


def _overlaps(bands: int, low: float, high: float) -> list[tuple[int, float]]:
    out = []
    for index in range(bands):
        overlap = (min(high, (index + 1) / bands) - max(low, index / bands)) * bands
        if overlap > 1e-9:
            out.append((index, min(1.0, overlap)))
    return out


def volume_between(band_volumes: Sequence[float], high: float, low: float) -> float:
    """Water that dissolves the salt between two fractions of the tank."""
    if high <= low:
        return 0.0
    return sum(band_volumes[i] * o for i, o in _overlaps(len(band_volumes), low, high))


def estimate_fraction(band_volumes: Sequence[float], start: float, volume: float) -> float:
    """Walk down the learned depletion curve from ``start`` after ``volume`` of water."""
    bands = len(band_volumes)
    fraction, remaining = min(1.0, max(0.0, start)), max(0.0, volume)
    while remaining > 1e-9 and fraction > 1e-9:
        index = min(bands - 1, max(0, math.ceil(fraction * bands) - 1))
        portion = fraction * bands - index
        available = band_volumes[index] * portion
        if band_volumes[index] <= 0 or remaining <= available:
            if band_volumes[index] > 0:
                fraction -= remaining / band_volumes[index] / bands
            return max(0.0, fraction)
        remaining -= available
        fraction = index / bands
    return max(0.0, fraction)


def learn_salt(
    observations: Sequence[Mapping[str, Any]], scale: SaltScale, prior_tank_volume: float, alpha: float = 0.35
) -> dict[str, Any]:
    """Learn gallons-per-band from level observations.

    Every drop from a known level to a lower one is a calibration segment:
    rate = water passed / level drop. The water is shared out across the bands
    the drop covered in proportion to the current estimates, and each band is
    updated with an EWMA (the first observation of a band outweighs the prior
    guess). Logging intermediate levels (8 → 6 → 4 → 2 → empty) therefore
    learns the narrower cone at the bottom of a brine tank.
    """
    bands = scale.bands
    volumes = [prior_tank_volume / bands] * bands
    counts = [0] * bands
    segments: list[dict[str, Any]] = []
    previous: tuple[float, float] | None = None
    for obs in sorted(observations, key=lambda o: str(o.get("observed_at"))):
        volume = float(obs.get("volume") or 0.0)
        if obs.get("kind") == SALT_OBS_FILL:
            if obs.get("bags") and previous is not None:
                current = estimate_fraction(volumes, previous[0], volume - previous[1])
                fraction = min(1.0, current + float(obs["bags"]) / max(scale.capacity_bags, 1e-9))
            else:
                fraction = float(obs.get("fraction") if obs.get("fraction") is not None else 1.0)
            previous = (fraction, volume)
            continue
        fraction = 0.0 if obs.get("kind") == SALT_OBS_EMPTY else float(obs.get("fraction") or 0.0)
        if previous is not None and fraction < previous[0] - 1e-6 and volume > previous[1] + 1e-9:
            used = volume - previous[1]
            covered = _overlaps(bands, fraction, previous[0])
            expected = sum(volumes[i] * o for i, o in covered)
            if expected > 0:
                ratio = used / expected
                for index, overlap in covered:
                    # The prior is a guess, so the first real measurement of a band dominates it.
                    weight = max(alpha, 1.0 / (counts[index] + 1.25)) * overlap
                    volumes[index] = (1 - weight) * volumes[index] + weight * volumes[index] * ratio
                    counts[index] += 1
                segments.append(
                    {
                        "from": round(previous[0], 4),
                        "to": round(fraction, 4),
                        "volume": round(used, 1),
                        "rate": round(used / ((previous[0] - fraction) * bands), 1),
                    }
                )
        previous = (fraction, volume)
    # Bands never observed yet follow the household's measured rate instead of the raw guess.
    prior_band = prior_tank_volume / bands
    seen = [(volumes[i] / prior_band, counts[i]) for i in range(bands) if counts[i]]
    if seen and prior_band > 0:
        ratio = sum(r * c for r, c in seen) / sum(c for _, c in seen)
        volumes = [v if counts[i] else prior_band * ratio for i, v in enumerate(volumes)]
    return {"band_volumes": [round(v, 2) for v in volumes], "segments": segments, "tank_volume": round(sum(volumes), 1)}


def daily_average(daily: Mapping[str, float], today: date, days: int = 14) -> float:
    """Average daily water use over the last ``days`` complete days (today's partial day if nothing else)."""
    window = [float(daily[key]) for key in (str(today - timedelta(days=d)) for d in range(1, days + 1)) if key in daily]
    if window:
        return sum(window) / len(window)
    return float(daily.get(str(today), 0.0))


def salt_summary(
    model: Mapping[str, Any],
    scale: SaltScale,
    *,
    last_observation: Mapping[str, Any] | None,
    last_fill_volume: float | None,
    effective_volume: float,
    daily_use: float,
    warning_fraction: float,
    unit: str = "gal",
) -> dict[str, Any]:
    volumes = list(model.get("band_volumes") or [])
    if last_observation is None or not volumes:
        return {"known": False, "message": "Log a fill or the current level to start tracking", "unit": unit}
    start = 0.0 if last_observation.get("kind") == SALT_OBS_EMPTY else float(last_observation.get("fraction") or 0.0)
    fraction = estimate_fraction(volumes, start, effective_volume - float(last_observation.get("volume") or 0.0))
    to_warning = volume_between(volumes, fraction, warning_fraction)
    to_empty = volume_between(volumes, fraction, 0.0)
    segments = len(model.get("segments") or [])
    units = max(scale.max_level - scale.min_level, 1e-9) if scale.kind == SALT_SCALE_LEVELS else (100 if scale.kind == SALT_SCALE_PERCENT else scale.capacity_bags)
    return {
        "known": True,
        "fraction": round(fraction, 4),
        "level": round(scale.from_fraction(fraction), 2),
        "label": scale.label(fraction),
        "since_refill": round(effective_volume - last_fill_volume, 1) if last_fill_volume is not None else None,
        "remaining_to_warning": round(to_warning, 1),
        "remaining_to_empty": round(to_empty, 1),
        "daily_use": round(daily_use, 1),
        "days_until_refill": round(to_warning / daily_use, 1) if daily_use > 0 else None,
        "days_until_empty": round(to_empty / daily_use, 1) if daily_use > 0 else None,
        "per_unit": round(sum(volumes) / units, 1),
        "tank_volume": round(sum(volumes), 1),
        "segments": segments,
        "confidence": "estimate" if segments == 0 else "learning" if segments < 3 else "calibrated",
        "below_warning": fraction <= warning_fraction + 1e-9,
        "warning_fraction": warning_fraction,
        "unit": unit,
    }


def salt_trigger_result(summary: Mapping[str, Any], warning_days: int) -> TriggerResult:
    if not summary.get("known"):
        return TriggerResult(False, 0.0, summary.get("message", ""), configured=False)
    warning = float(summary["warning_fraction"])
    fraction = float(summary["fraction"])
    progress = 1.0 if fraction <= warning else max(0.0, min(0.999, (1 - fraction) / max(1 - warning, 1e-9)))
    days = summary.get("days_until_refill")
    if not summary["below_warning"] and days is not None and days <= warning_days:
        progress = max(progress, 0.95)  # inside the warning window: Due Soon
    unit = summary.get("unit") or "gal"
    label = f"{fmt_number(summary['remaining_to_warning'])} {unit}" + (f" / ~{fmt_number(days)} days" if days is not None else "")
    return TriggerResult(bool(summary["below_warning"]), progress, label, remaining=summary["remaining_to_warning"], unit=unit)


def feed_volume(live: dict[str, Any], raw: float, day: date) -> float:
    """Add a total-increasing meter reading to the softener's own monotonic counter. Returns the delta."""
    last = live.get("last_raw")
    live["last_raw"] = raw
    if last is None:
        live.setdefault("total", 0.0)
        return 0.0
    delta = raw - float(last) if raw >= float(last) else raw  # the meter reset to zero
    if delta <= 0:
        return 0.0
    live["total"] = float(live.get("total") or 0.0) + delta
    daily = live.setdefault("daily", {})
    daily[str(day)] = round(float(daily.get(str(day), 0.0)) + delta, 3)
    for key in sorted(daily)[:-45]:
        daily.pop(key, None)
    return delta


def _in_window(now_local: datetime, start: time, end: time) -> bool:
    current = now_local.time()
    return start <= current < end if start <= end else current >= start or current < end


def regen_feed(
    cfg: Mapping[str, Any], state: Mapping[str, Any] | None, *, now_local: datetime, flow: float | None, total: float
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Detect a regeneration cycle: sustained flow inside the configured overnight window."""
    st = dict(state or {})
    start = to_time(cfg.get("regen_start")) or time(2, 0)
    end = to_time(cfg.get("regen_end")) or time(4, 0)
    min_flow = float(num(cfg.get("regen_min_flow")) or 0.5)
    min_minutes = float(num(cfg.get("regen_min_minutes")) or 8)
    active = from_iso(st.get("active_start"))
    if _in_window(now_local, start, end) and flow is not None and flow >= min_flow:
        if active is None:
            st = {"active_start": to_iso(now_local), "start_total": total}
        return st, None
    if active is not None:
        minutes = (now_local - active).total_seconds() / 60
        event = None
        if minutes >= min_minutes:
            event = {
                "started_at": to_iso(active),
                "ended_at": to_iso(now_local),
                "minutes": round(minutes, 1),
                "volume": round(max(0.0, total - float(st.get("start_total") or total)), 1),
            }
        return {}, event
    return st, None


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------
def event_amount(event: Mapping[str, Any], cost_mode: str) -> float:
    if event.get("kind") == EVENT_KIND_COMPLETED:
        amount = float(event.get("cost") or 0.0)
        if cost_mode == COST_MODE_CONSUMPTION:
            amount += float(event.get("parts_cost") or 0.0)
        return amount
    if event.get("kind") == EVENT_KIND_PURCHASE and cost_mode == COST_MODE_PURCHASE:
        return float(event.get("cost") or 0.0)
    return 0.0


def spend_report(
    events: Iterable[Mapping[str, Any]],
    objects: Mapping[str, Mapping[str, Any]],
    area_names: Mapping[str, str],
    *,
    cost_mode: str,
    year: int,
    tz: tzinfo,
) -> dict[str, Any]:
    by_month = [0.0] * 12
    by_year: dict[int, float] = {}
    by_area: dict[str, float] = {}
    by_object: dict[str, float] = {}
    for event in events:
        amount = event_amount(event, cost_mode)
        moment = from_iso(event.get("occurred_at"))
        if not amount or moment is None:
            continue
        local = moment.astimezone(tz)
        by_year[local.year] = by_year.get(local.year, 0.0) + amount
        if local.year != year:
            continue
        by_month[local.month - 1] += amount
        obj = objects.get(event.get("object_id") or "") or {}
        area = area_names.get(obj.get("area_id") or "", "No area")
        by_area[area] = by_area.get(area, 0.0) + amount
        by_object[obj.get("name") or "Other"] = by_object.get(obj.get("name") or "Other", 0.0) + amount
    return {
        "year": year,
        "mode": cost_mode,
        "total": round(sum(by_month), 2),
        "by_month": [round(v, 2) for v in by_month],
        "by_year": {str(y): round(v, 2) for y, v in sorted(by_year.items())},
        "by_area": [{"area": k, "total": round(v, 2)} for k, v in sorted(by_area.items(), key=lambda kv: -kv[1])],
        "by_object": [{"object": k, "total": round(v, 2)} for k, v in sorted(by_object.items(), key=lambda kv: -kv[1])][:10],
    }


# ---------------------------------------------------------------------------
# Templates, battery fleet and migration helpers
# ---------------------------------------------------------------------------
def _task(name: str, category: str, value: int | None, unit: str = "months", **extra: Any) -> dict[str, Any]:
    mode = extra.pop("schedule_mode", SCHEDULE_ROLLING if value else SCHEDULE_NONE)
    return {"name": name, "category": category, "schedule_mode": mode, "interval_value": value, "interval_unit": unit, **extra}


TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "furnace",
        "object": {"name": "Furnace", "icon": "mdi:hvac"},
        "tasks": [
            _task("Replace Filter", "Replacement", 3, spec_note="16x25x4 Air Filter", parts=[{"part_name": "16x25x4 Air Filter", "quantity": 1, "reorder_threshold": 1, "restock_quantity": 2}]),
            _task("Annual Service", "Service", 1, "years", schedule_mode=SCHEDULE_PLANNED),
        ],
    },
    {
        "id": "water_filter",
        "object": {"name": "Whole House Water Filter", "icon": "mdi:water-check"},
        "tasks": [
            _task("Replace Sediment Cartridge", "Replacement", 6, parts=[{"part_name": "Whole House Sediment Filter Cartridge", "quantity": 1, "reorder_threshold": 1, "restock_quantity": 2}]),
        ],
    },
    {
        "id": "water_softener",
        "object": {"name": "Water Softener", "icon": "mdi:water-opacity", "object_type": "water_softener"},
        "tasks": [
            _task("Refill Water Softener Salt", "Refill", None, warning_days=5, triggers=[{"type": TRIGGER_SALT}], parts=[{"part_name": "40lb Solar Salt Bag", "quantity": 1, "reorder_threshold": 2, "restock_quantity": 4}]),
            _task("Check for Salt Bridges", "Inspection", 2),
            _task("Clean Brine Tank", "Cleaning", 1, "years"),
        ],
    },
    {
        "id": "water_heater",
        "object": {"name": "Water Heater", "icon": "mdi:water-boiler"},
        "tasks": [_task("Flush Tank", "Service", 1, "years", schedule_mode=SCHEDULE_PLANNED), _task("Inspect Anode Rod", "Inspection", 3, "years")],
    },
    {
        "id": "smoke_detectors",
        "object": {"name": "Smoke & CO Detectors", "icon": "mdi:smoke-detector-variant"},
        "tasks": [_task("Test Detectors", "Inspection", 1, schedule_mode=SCHEDULE_PLANNED), _task("Replace Detectors", "Replacement", 10, "years")],
    },
    {
        "id": "refrigerator",
        "object": {"name": "Refrigerator", "icon": "mdi:fridge-outline"},
        "tasks": [_task("Replace Water Filter", "Replacement", 6), _task("Clean Condenser Coils", "Cleaning", 1, "years")],
    },
    {
        "id": "dryer",
        "object": {"name": "Clothes Dryer", "icon": "mdi:tumble-dryer"},
        "tasks": [_task("Clean Dryer Vent", "Cleaning", 1, "years")],
    },
    {
        "id": "dishwasher",
        "object": {"name": "Dishwasher", "icon": "mdi:dishwasher"},
        "tasks": [_task("Clean Filter", "Cleaning", 1)],
    },
    {
        "id": "vehicle",
        "object": {"name": "Vehicle", "icon": "mdi:car", "object_type": "vehicle"},
        "tasks": [
            _task("Oil Change", "Service", 6, triggers=[{"type": TRIGGER_COUNTER, "entity_id": None, "delta": 5000, "unit": "mi"}]),
            _task("Rotate Tires", "Service", None, triggers=[{"type": TRIGGER_COUNTER, "entity_id": None, "delta": 7500, "unit": "mi"}]),
        ],
    },
    {
        "id": "printer_3d",
        "object": {"name": "3D Printer", "icon": "mdi:printer-3d"},
        "tasks": [
            _task("Lubricate Rods", "Service", None, triggers=[{"type": TRIGGER_RUNTIME, "entity_id": None, "target_state": "printing", "target_hours": 200}]),
            _task("Clean Nozzle", "Cleaning", 1),
        ],
    },
    {
        "id": "hvac_heating",
        "object": {"name": "Heating System", "icon": "mdi:fire"},
        "tasks": [_task("Replace Filter (runtime)", "Replacement", None, triggers=[{"type": TRIGGER_RUNTIME, "entity_id": None, "attribute": "hvac_action", "target_state": "heating", "target_hours": 300}])],
    },
    {
        "id": "gutters",
        "object": {"name": "Gutters", "icon": "mdi:home-roof"},
        "tasks": [_task("Clean Gutters", "Cleaning", 6, active_months=[4, 5, 6, 7, 8, 9, 10, 11])],
    },
    {
        "id": "humidifier",
        "object": {"name": "Humidifier", "icon": "mdi:air-humidifier"},
        "tasks": [_task("Replace Humidifier Pad", "Replacement", 1, "years", active_months=[11, 12, 1, 2, 3]), _task("Clean Humidifier", "Cleaning", 1, active_months=[11, 12, 1, 2, 3])],
    },
    {
        "id": "lawn_mower",
        "object": {"name": "Lawn Mower", "icon": "mdi:mower"},
        "tasks": [_task("Sharpen Blade", "Service", 1, "years", active_months=[4, 5, 6, 7, 8, 9, 10]), _task("Change Oil", "Service", 1, "years", active_months=[4, 5, 6, 7, 8, 9, 10])],
    },
    {
        "id": "sump_pump",
        "object": {"name": "Sump Pump", "icon": "mdi:water-pump"},
        "tasks": [_task("Test Sump Pump", "Inspection", 3), _task("Investigate Water Alarm", "Inspection", None, triggers=[{"type": TRIGGER_PROBLEM, "entity_id": None, "auto_complete": True}])],
    },
)


def template_catalog() -> list[dict[str, Any]]:
    return [
        {"id": t["id"], "name": t["object"]["name"], "icon": t["object"].get("icon"), "tasks": [task["name"] for task in t["tasks"]]}
        for t in TEMPLATES
    ]


def get_template(template_id: str) -> dict[str, Any] | None:
    return next((t for t in TEMPLATES if t["id"] == template_id), None)


def plan_battery_fleet(
    states: Iterable[tuple[str, Any, Mapping[str, Any]]], existing: set[str], *, threshold: float = 15, for_minutes: float = 60
) -> list[dict[str, Any]]:
    """One "Replace battery" task per battery entity not already tracked."""
    planned = []
    for entity_id, state, attrs in states:
        if entity_id in existing or attrs.get("device_class") != "battery":
            continue
        domain = entity_id.split(".", 1)[0]
        name = str(attrs.get("friendly_name") or entity_id.split(".", 1)[-1].replace("_", " ").title())
        device = re.sub(r"\s*battery( level)?\s*$", "", name, flags=re.IGNORECASE).strip() or name
        battery_type = attrs.get("battery_type") or attrs.get("Battery type") or attrs.get("battery_type_and_quantity")
        if domain == "sensor" and attrs.get("unit_of_measurement") == "%":
            trigger = {"type": TRIGGER_THRESHOLD, "entity_id": entity_id, "below": threshold, "for_minutes": for_minutes}
        elif domain == "binary_sensor":
            trigger = {"type": TRIGGER_PROBLEM, "entity_id": entity_id, "auto_complete": False}
        else:
            continue
        planned.append({"entity_id": entity_id, "device": device, "battery_type": str(battery_type) if battery_type else None, "state": state, "trigger": trigger})
    return sorted(planned, key=lambda p: p["device"].lower())


_CATEGORY_WORDS = (
    ("Replacement", ("replace", "change", "swap", "new ")),
    ("Cleaning", ("clean", "flush", "wash", "descale", "vacuum")),
    ("Inspection", ("test", "check", "inspect", "look")),
    ("Refill", ("refill", "salt", "top up", "add ")),
    ("Calibration", ("calibrat", "level", "adjust")),
)


def guess_category(name: str) -> str:
    lowered = f"{name.lower()} "
    for category, words in _CATEGORY_WORDS:
        if any(word in lowered for word in words):
            return category
    return "Service"


def interval_from_rrule(rule: str | None) -> tuple[int, str] | None:
    """Best-effort FREQ/INTERVAL → (value, unit) for migrating 1.x maintenance tasks."""
    if not rule:
        return None
    parts = dict(part.split("=", 1) for part in str(rule).upper().split(";") if "=" in part)
    unit = {"DAILY": "days", "WEEKLY": "weeks", "MONTHLY": "months", "YEARLY": "years"}.get(parts.get("FREQ", ""))
    if unit is None:
        return None
    try:
        value = max(1, int(parts.get("INTERVAL", "1")))
    except ValueError:
        value = 1
    return value, unit


# ---------------------------------------------------------------------------
# Document files
# ---------------------------------------------------------------------------
def sniff_document_type(data: bytes, filename: str | None = None) -> str | None:
    """The real type of an uploaded file from its first bytes (the declared type is never trusted).

    Only PDFs, raster images and plain text are accepted. HTML, SVG and anything
    else that a browser could execute is refused.
    """
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heim", b"heis", b"hevc"):
        return "image/heic"
    if (filename or "").lower().endswith(".txt"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        lowered = text[:2048].lower()
        if "\x00" in text or "<script" in lowered or "<html" in lowered or "<svg" in lowered:
            return None
        return "text/plain"
    return None


def safe_filename(name: str | None, extension: str) -> str:
    """A download name made only of harmless characters, with the extension of the sniffed type."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", str(name or "document"))
    stem = re.sub(r"[^A-Za-z0-9 ._-]", "_", stem).strip(" ._") or "document"
    return f"{stem[:80]}.{extension}"
