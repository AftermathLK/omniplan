"""Pure maintenance engine: schedules, triggers, status, learning, salt model, spend."""

from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta, timezone

import pytest
from conftest import TZ

from omniplan import maintenance as m

UTC = timezone.utc


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------
def test_month_arithmetic_clamps_and_anchors() -> None:
    anchor = date(2026, 1, 31)
    assert [m.nth_occurrence(anchor, 1, "months", n) for n in range(1, 4)] == [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]
    assert m.add_interval(date(2024, 2, 29), 1, "years") == date(2025, 2, 28)
    assert m.add_interval(date(2026, 10, 27), 2, "weeks") == date(2026, 11, 10)
    assert m.describe_interval(6, "months") == "6 Months" and m.describe_interval(1, "years") == "1 Year"


def test_planned_schedule_does_not_drift_but_rolling_does() -> None:
    tuesday = date(2026, 10, 27)
    common = {"value": 1, "unit": "weeks", "anchor": tuesday, "previous_due": tuesday}
    # Completed a day late on Wednesday:
    assert m.next_due_after(mode="planned", completed_on=date(2026, 10, 28), **common) == date(2026, 11, 3)
    assert m.next_due_after(mode="rolling", completed_on=date(2026, 10, 28), **common) == date(2026, 11, 4)
    # Completed early: the next cycle still follows the anchor.
    assert m.next_due_after(mode="planned", completed_on=date(2026, 10, 25), **common) == date(2026, 11, 3)
    # Missed several cycles: the next one after today, still on a Tuesday.
    late = m.next_due_after(mode="planned", completed_on=date(2026, 12, 2), **common)
    assert late == date(2026, 12, 8) and late.weekday() == 1
    assert m.next_due_after(mode="once", completed_on=tuesday, **common) is None


def test_seasonal_window_shifts_due_dates() -> None:
    lawn = [4, 5, 6, 7, 8, 9, 10]
    assert m.season_shift(date(2026, 11, 15), lawn) == date(2027, 4, 1)
    assert m.season_shift(date(2026, 6, 15), lawn) == date(2026, 6, 15)
    humidifier = [11, 12, 1, 2, 3]
    assert m.next_due_after(mode="rolling", value=1, unit="months", anchor=None, previous_due=None, completed_on=date(2026, 3, 10), active_months=humidifier) == date(2026, 11, 1)


def test_due_moment_time_of_day_flip() -> None:
    due = date(2026, 10, 28)
    assert m.due_moment(due, time(9, 0), TZ) == local(2026, 10, 28, 9, 0)
    assert m.due_moment(due, None, TZ) == local(2026, 10, 29, 0, 0)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def _task(**extra):
    base = {"schedule_mode": "rolling", "interval_value": 3, "interval_unit": "months", "warning_days": 7, "warning_percent": 10, "next_due": "2026-11-30", "last_done": "2026-08-30T10:00:00+00:00"}
    return {**base, **extra}


def test_schedule_status_ok_due_soon_overdue_with_time_flip() -> None:
    status = m.compute_status(_task(), [], now=local(2026, 10, 28, 12), tz=TZ)
    assert status.status == "ok" and status.days_left == 33 and status.countdown == "33 days" and 0.6 < status.progress < 0.7
    assert m.compute_status(_task(next_due="2026-11-03"), [], now=local(2026, 10, 28, 12), tz=TZ).status == "due_soon"
    due_today = _task(next_due="2026-10-28", schedule_time="09:00")
    assert m.compute_status(due_today, [], now=local(2026, 10, 28, 8, 59), tz=TZ).status == "due_soon"
    assert m.compute_status(due_today, [], now=local(2026, 10, 28, 9, 0), tz=TZ).status == "overdue"
    no_time = _task(next_due="2026-10-28")
    assert m.compute_status(no_time, [], now=local(2026, 10, 28, 23, 59), tz=TZ).countdown == "Due today"
    late = m.compute_status(no_time, [], now=local(2026, 10, 30, 8), tz=TZ)
    assert late.status == "overdue" and late.countdown == "2 days overdue" and late.progress == 1.0
    dormant = m.compute_status(_task(active_months=[4, 5, 6]), [], now=local(2026, 10, 28, 12), tz=TZ)
    assert dormant.status == "ok" and dormant.countdown == "Out of season"


def test_or_and_logic_with_triggers() -> None:
    hot = m.TriggerResult(True, 1.0, "312 / 300 h")
    warm = m.TriggerResult(False, 0.95, "285 / 300 h")
    cold = m.TriggerResult(False, 0.2, "60 / 300 h")
    now = local(2026, 10, 28, 12)
    assert m.compute_status(_task(), [hot], now=now, tz=TZ).status == "triggered"
    assert m.compute_status(_task(), [warm], now=now, tz=TZ).status == "due_soon"
    assert m.compute_status(_task(), [cold], now=now, tz=TZ).status == "ok"
    # AND: the schedule isn't due yet, so a met trigger alone isn't enough.
    assert m.compute_status(_task(trigger_logic="and"), [hot], now=now, tz=TZ).status == "ok"
    overdue = _task(trigger_logic="and", next_due="2026-10-01")
    assert m.compute_status(overdue, [hot], now=now, tz=TZ).status == "triggered"
    assert m.compute_status(overdue, [cold], now=now, tz=TZ).status == "ok"
    trigger_only = _task(schedule_mode="none", next_due=None)
    result = m.compute_status(trigger_only, [hot], now=now, tz=TZ)
    assert result.status == "triggered" and result.reasons == ["312 / 300 h"]
    assert m.compute_status(trigger_only, [m.TriggerResult(False, 0, "", configured=False)], now=now, tz=TZ).countdown == "No schedule"


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def test_threshold_must_hold_for_minutes_and_latches() -> None:
    cfg = {"type": "threshold", "entity_id": "sensor.lock_battery", "below": 15, "for_minutes": 60}
    st, _ = m.trigger_update(cfg, {}, old="20", new="12", old_attrs={}, new_attrs={}, now=NOW)
    assert not m.trigger_result(cfg, st, current="12", attrs={}, now=NOW + timedelta(minutes=30)).met
    st, _ = m.trigger_update(cfg, st, old="12", new="18", old_attrs={}, new_attrs={}, now=NOW + timedelta(minutes=40))
    assert st["cond_since"] is None  # recovered before the hour was up
    st, _ = m.trigger_update(cfg, st, old="18", new="11", old_attrs={}, new_attrs={}, now=NOW + timedelta(minutes=50))
    st = m.trigger_tick(cfg, st, current="11", attrs={}, now=NOW + timedelta(minutes=111))
    assert st["latched"] is True
    st, _ = m.trigger_update(cfg, st, old="11", new="40", old_attrs={}, new_attrs={}, now=NOW + timedelta(minutes=120))
    result = m.trigger_result(cfg, st, current="40", attrs={"unit_of_measurement": "%"}, now=NOW + timedelta(minutes=120))
    assert result.met and "below 15" in result.label  # stays triggered until the battery is replaced
    fresh = m.trigger_reset(cfg, current="100", attrs={}, now=NOW)
    assert fresh == {"cond_since": None, "latched": False}


def test_counter_delta_survives_meter_resets() -> None:
    cfg = {"type": "counter", "entity_id": "sensor.water_total", "delta": 1000}
    st = m.trigger_reset(cfg, current="5000", attrs={}, now=NOW)
    for value in ("5400", "5800", "100", "350"):  # the meter was replaced at 5800
        st, _ = m.trigger_update(cfg, st, old=None, new=value, old_attrs={}, new_attrs={}, now=NOW)
    result = m.trigger_result(cfg, st, current="350", attrs={"unit_of_measurement": "gal"}, now=NOW)
    assert result.progress == pytest.approx(1050 / 1000) and result.met
    assert result.label == "1,050 / 1,000 gal"
    assert m.trigger_result(cfg, {}, current="1", attrs={}, now=NOW).label == "Waiting for the first reading"


def test_runtime_counts_time_in_target_state_including_attributes() -> None:
    cfg = {"type": "runtime", "entity_id": "climate.furnace", "attribute": "hvac_action", "target_state": "heating", "target_hours": 2}
    st = m.trigger_reset(cfg, current="heat", attrs={"hvac_action": "idle"}, now=NOW)
    st, _ = m.trigger_update(cfg, st, old="heat", new="heat", old_attrs={"hvac_action": "idle"}, new_attrs={"hvac_action": "heating"}, now=NOW)
    st = m.trigger_tick(cfg, st, current="heat", attrs={"hvac_action": "heating"}, now=NOW + timedelta(minutes=60))
    st, _ = m.trigger_update(cfg, st, old="heat", new="heat", old_attrs={"hvac_action": "heating"}, new_attrs={"hvac_action": "idle"}, now=NOW + timedelta(minutes=90))
    result = m.trigger_result(cfg, st, current="heat", attrs={"hvac_action": "idle"}, now=NOW + timedelta(hours=5))
    assert result.progress == pytest.approx(0.75) and not result.met and result.label == "1.5 / 2 h"
    # A restart while heating doesn't count the downtime.
    st, _ = m.trigger_update(cfg, st, old="heat", new="heat", old_attrs={}, new_attrs={"hvac_action": "heating"}, now=NOW + timedelta(hours=6))
    st = m.trigger_startup(cfg, st, current="heat", attrs={"hvac_action": "heating"}, now=NOW + timedelta(hours=9))
    assert m.trigger_result(cfg, st, current="heat", attrs={}, now=NOW + timedelta(hours=9, minutes=30)).met


def test_state_count_debounces_and_ignores_restart_restores() -> None:
    cfg = {"type": "state_count", "entity_id": "binary_sensor.dishwasher_door", "from_state": "off", "to_state": "on", "count": 3, "debounce_seconds": 60}
    st: dict = {}
    sequence = [
        ("off", "on", 0),
        ("on", "off", 10),
        ("off", "on", 20),  # debounced (within 60 s of the last count)
        ("unavailable", "on", 200),  # restart restore: ignored
        ("off", "on", 300),
        ("on", "on", 310),
        ("off", "on", 500),
    ]
    for old, new, seconds in sequence:
        st, _ = m.trigger_update(cfg, st, old=old, new=new, old_attrs={}, new_attrs={}, now=NOW + timedelta(seconds=seconds))
    assert st["count"] == 3
    assert m.trigger_result(cfg, st, current="on", attrs={}, now=NOW).met


def test_problem_sensor_latches_and_signals_auto_recovery() -> None:
    cfg = {"type": "problem", "entity_id": "binary_sensor.leak", "auto_complete": True}
    st, signals = m.trigger_update(cfg, {}, old="off", new="on", old_attrs={}, new_attrs={}, now=NOW)
    assert st["latched"] and signals == []
    st, signals = m.trigger_update(cfg, st, old="on", new="off", old_attrs={}, new_attrs={}, now=NOW)
    assert signals == ["auto_complete"]
    assert m.trigger_result(cfg, st, current="off", attrs={}, now=NOW).met  # until completed
    assert not m.trigger_result({"type": "counter"}, {}, current="1", attrs={}, now=NOW).configured


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------
def test_ewa_and_feedback() -> None:
    samples = [{"interval_days": 90, "feedback": "too_early", "completed_on": "2026-01-01"}] * 4
    rec = m.recommend_interval(samples, 90)
    assert rec["interval_days"] > 90 and rec["suggest"] and rec["confidence"] == "medium"
    late = [{"interval_days": 90, "feedback": "too_late", "completed_on": "2026-01-01"}] * 2
    assert m.recommend_interval(late, 90)["interval_days"] < 90
    assert m.recommend_interval([], 90) is None
    steady = m.recommend_interval([{"interval_days": 91, "feedback": "just_right", "completed_on": "2026-01-01"}] * 3, 91)
    assert not steady["suggest"]


def test_weibull_fit_recovers_parameters() -> None:
    rng = random.Random(7)
    shape, scale = 3.0, 120.0
    times = [scale * (-math.log(1 - rng.random())) ** (1 / shape) for _ in range(400)]
    fit = m.fit_weibull(times, [True] * len(times))
    assert fit is not None
    assert fit[0] == pytest.approx(shape, rel=0.15) and fit[1] == pytest.approx(scale, rel=0.08)
    # Censoring (still fine at that age) pushes the life estimate up.
    censored = m.fit_weibull([100, 110, 120, 130, 140], [True, True, False, False, False])
    uncensored = m.fit_weibull([100, 110, 120, 130, 140], [True] * 5)
    assert censored[1] > uncensored[1]
    assert m.weibull_life(3.0, 120.0) == pytest.approx(120 * 0.1053605 ** (1 / 3), rel=1e-4)
    assert m.fit_weibull([100, 100, 100], [True, True, True])[0] == 50.0  # perfectly regular


def test_seasonal_multipliers_and_unit_conversion() -> None:
    samples = [{"interval_days": 60 if month in (6, 7, 8) else 90, "feedback": "just_right", "completed_on": f"2026-{month:02d}-15"} for month in range(1, 13)]
    multipliers = m.seasonal_multipliers(samples)
    assert multipliers[6] < 1.0 < multipliers[0]
    summer = m.recommend_interval(samples, 85, next_due_month=7)
    winter = m.recommend_interval(samples, 85, next_due_month=1)
    assert summer["interval_days"] < winter["interval_days"]
    assert m.days_to_interval(183) == (6, "months")
    assert m.days_to_interval(364) == (1, "years")
    assert m.days_to_interval(100) == (100, "days")


# ---------------------------------------------------------------------------
# Salt model
# ---------------------------------------------------------------------------
SCALE = m.SaltScale(kind="levels", max_level=8, min_level=0, capacity_bags=4, bag_lb=40)


def _obs(kind: str, level: float | None, volume: float, day: int, bags: float | None = None) -> dict:
    return {
        "kind": kind,
        "fraction": SCALE.to_fraction(level) if level is not None else None,
        "bags": bags,
        "volume": volume,
        "observed_at": f"2026-10-{day:02d}T08:00:00+00:00",
    }


def test_salt_learning_converges_to_household_rate() -> None:
    # True behaviour: 1,000 gallons per level (8,000 per tank); the prior guesses 32,000.
    obs = []
    volume = 0.0
    for cycle in range(4):
        obs.append(_obs("fill", None, volume, cycle * 7 + 1))
        volume += 8000
        obs.append(_obs("empty", None, volume, cycle * 7 + 6))
    model = m.learn_salt(obs, SCALE, m.default_tank_volume({}, SCALE))
    assert len(model["segments"]) == 4 and model["segments"][0]["rate"] == 1000.0
    assert model["tank_volume"] == pytest.approx(8000, rel=0.15)


def test_salt_learning_captures_the_tapered_bottom() -> None:
    # 1,500 gal/level for levels 8→4, then only 500 gal/level for 4→0 (cone at the bottom).
    obs, volume = [], 0.0
    for cycle in range(5):
        obs.append(_obs("fill", None, volume, cycle * 6 + 1))
        volume += 6000
        obs.append(_obs("level", 4, volume, cycle * 6 + 3))
        volume += 2000
        obs.append(_obs("empty", None, volume, cycle * 6 + 5))
    model = m.learn_salt(obs, SCALE, 8000)
    top, bottom = model["band_volumes"][7], model["band_volumes"][0]
    assert top == pytest.approx(1500, rel=0.15) and bottom == pytest.approx(500, rel=0.2)
    # From full, 6,000 gallons later the estimate lands near level 4, not level 2.
    assert SCALE.from_fraction(m.estimate_fraction(model["band_volumes"], 1.0, 6000)) == pytest.approx(4, abs=0.4)


def test_salt_summary_and_trigger() -> None:
    model = {"band_volumes": [1000.0] * 8, "segments": [{}, {}, {}]}
    last = _obs("fill", None, 10000, 1)
    last["fraction"] = 1.0
    summary = m.salt_summary(model, SCALE, last_observation=last, last_fill_volume=10000, effective_volume=14600, daily_use=200, warning_fraction=0.25)
    assert summary["level"] == pytest.approx(3.4) and summary["label"] == "Level 3.4 / 8"
    assert summary["remaining_to_warning"] == pytest.approx(1400) and summary["days_until_refill"] == pytest.approx(7)
    assert summary["days_until_empty"] == pytest.approx(17) and summary["confidence"] == "calibrated"
    trig = m.salt_trigger_result(summary, warning_days=7)
    assert not trig.met and trig.progress >= 0.95 and trig.label == "1,400 gal / ~7 days"
    low = m.salt_summary(model, SCALE, last_observation=last, last_fill_volume=10000, effective_volume=16500, daily_use=200, warning_fraction=0.25)
    assert m.salt_trigger_result(low, 7).met
    assert not m.salt_summary(model, SCALE, last_observation=None, last_fill_volume=None, effective_volume=0, daily_use=0, warning_fraction=0.25)["known"]
    percent = m.SaltScale(kind="percent")
    assert percent.to_fraction(50) == 0.5 and percent.label(0.42) == "42%" and percent.markers() == [100, 75, 50, 25, 0]


def test_bag_fills_add_to_the_estimated_level() -> None:
    model_obs = [_obs("fill", None, 0, 1), _obs("level", 2, 6000, 3), _obs("fill", None, 6000, 4, bags=2)]
    model = m.learn_salt(model_obs, SCALE, 8000)
    # Two of four bags on top of level 2 → level 6.
    last_fill = model_obs[-1]
    est = m.estimate_fraction(model["band_volumes"], m.estimate_fraction(model["band_volumes"], 0.25, 0) + 0.5, 0)
    assert SCALE.from_fraction(est) == pytest.approx(6)
    assert last_fill["bags"] == 2


def test_volume_meter_and_daily_average() -> None:
    live: dict = {}
    assert m.feed_volume(live, 1000, date(2026, 10, 27)) == 0
    m.feed_volume(live, 1150, date(2026, 10, 27))
    m.feed_volume(live, 30, date(2026, 10, 28))  # meter reset
    assert live["total"] == 180 and live["daily"] == {"2026-10-27": 150, "2026-10-28": 30}
    daily = {str(date(2026, 10, 28) - timedelta(days=d)): 100.0 + d for d in range(1, 15)}
    assert m.daily_average(daily, date(2026, 10, 28)) == pytest.approx(107.5)
    assert m.daily_average({"2026-10-28": 42.0}, date(2026, 10, 28)) == 42.0


def test_regeneration_detected_from_overnight_flow() -> None:
    cfg = {"regen_start": "02:00", "regen_end": "04:00", "regen_min_flow": 1.0, "regen_min_minutes": 10}
    st, event = m.regen_feed(cfg, {}, now_local=local(2026, 10, 28, 2, 10), flow=1.8, total=500)
    assert event is None and st["start_total"] == 500
    st, event = m.regen_feed(cfg, st, now_local=local(2026, 10, 28, 2, 35), flow=0.0, total=560)
    assert event == {"started_at": "2026-10-28T02:10:00-05:00", "ended_at": "2026-10-28T02:35:00-05:00", "minutes": 25.0, "volume": 60.0}
    # Daytime flow (a shower) is not a regeneration.
    st, event = m.regen_feed(cfg, {}, now_local=local(2026, 10, 28, 7, 0), flow=2.5, total=600)
    assert st == {} and event is None
    # Too short.
    st, _ = m.regen_feed(cfg, {}, now_local=local(2026, 10, 28, 3, 0), flow=2.0, total=700)
    assert m.regen_feed(cfg, st, now_local=local(2026, 10, 28, 3, 4), flow=0.0, total=705) == ({}, None)


# ---------------------------------------------------------------------------
# Spend, templates, fleet, migration helpers
# ---------------------------------------------------------------------------
def test_spend_report_by_cost_mode() -> None:
    objects = {"o1": {"name": "Furnace", "area_id": "utility"}, "o2": {"name": "Car", "area_id": None}}
    events = [
        {"kind": "completed", "cost": 50, "parts_cost": 20, "object_id": "o1", "occurred_at": "2026-03-10T15:00:00+00:00"},
        {"kind": "purchase", "cost": 40, "object_id": "o1", "occurred_at": "2026-02-01T15:00:00+00:00"},
        {"kind": "completed", "cost": 80, "parts_cost": 0, "object_id": "o2", "occurred_at": "2025-12-31T23:30:00-05:00"},
        {"kind": "skipped", "cost": 999, "object_id": "o2", "occurred_at": "2026-04-01T15:00:00+00:00"},
    ]
    consumption = m.spend_report(events, objects, {"utility": "Utility Room"}, cost_mode="consumption", year=2026, tz=TZ)
    assert consumption["total"] == 70 and consumption["by_month"][2] == 70
    assert consumption["by_area"] == [{"area": "Utility Room", "total": 70}]
    assert consumption["by_year"] == {"2025": 80, "2026": 70}
    purchase = m.spend_report(events, objects, {}, cost_mode="purchase", year=2026, tz=TZ)
    assert purchase["total"] == 90 and purchase["by_month"][1] == 40


def test_templates_and_battery_fleet() -> None:
    catalog = m.template_catalog()
    assert {"furnace", "water_softener", "vehicle"} <= {t["id"] for t in catalog}
    for template in m.TEMPLATES:
        for task in template["tasks"]:
            assert task["category"] in ("Replacement", "Service", "Cleaning", "Inspection", "Calibration", "Refill")
            assert task["schedule_mode"] in ("none", "once", "rolling", "planned")
            assert task["interval_value"] or task.get("triggers")
    states = [
        ("sensor.front_door_lock_battery", "62", {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Front Door Lock Battery", "battery_type": "AA"}),
        ("binary_sensor.leak_battery", "off", {"device_class": "battery", "friendly_name": "Leak Sensor Battery Low"}),
        ("sensor.phone_battery", "80", {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Phone Battery"}),
        ("sensor.temperature", "21", {"device_class": "temperature"}),
    ]
    plan = m.plan_battery_fleet(states, {"sensor.phone_battery"})
    assert [p["device"] for p in plan] == ["Front Door Lock", "Leak Sensor Battery Low"]
    assert plan[0]["trigger"] == {"type": "threshold", "entity_id": "sensor.front_door_lock_battery", "below": 15, "for_minutes": 60}
    assert plan[0]["battery_type"] == "AA" and plan[1]["trigger"]["type"] == "problem"


def test_migration_helpers() -> None:
    assert m.guess_category("Replace furnace filter") == "Replacement"
    assert m.guess_category("Test smoke detectors") == "Inspection"
    assert m.guess_category("Flush water heater") == "Cleaning"
    assert m.guess_category("Annual tune-up") == "Service"
    assert m.interval_from_rrule("FREQ=MONTHLY;INTERVAL=3") == (3, "months")
    assert m.interval_from_rrule("FREQ=WEEKLY;BYDAY=TU") == (1, "weeks")
    assert m.interval_from_rrule("FREQ=HOURLY") is None
