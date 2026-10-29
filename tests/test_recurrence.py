"""Calendar and rolling recurrence, rollover, streaks and the audit trail."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import plan_by_name

from omniplan.planner import Actor, PlannerError
from omniplan.recurrence import describe, next_calendar_occurrence, next_due_date, validate_rrule

ALEX = Actor(user_id="u1", user_name="Alex", person="person.alex")
SAM = Actor(user_id="u2", user_name="Sam", person="person.sam")


def test_validate_rrule() -> None:
    assert validate_rrule("rrule:freq=weekly; byday=mo,th") == "FREQ=WEEKLY;BYDAY=MO,TH"
    with pytest.raises(ValueError):
        validate_rrule("BYDAY=MO")


def test_next_weekly_occurrence() -> None:
    # 2026-10-27 is a Tuesday
    assert next_calendar_occurrence("FREQ=WEEKLY;BYDAY=TU,FR", date(2026, 10, 6), date(2026, 10, 27)) == date(2026, 10, 30)


def test_calendar_completion_late_never_schedules_in_the_past() -> None:
    nxt = next_due_date(
        mode="calendar",
        rule="FREQ=DAILY",
        interval_days=None,
        current_due=date(2026, 10, 20),
        anchor=date(2026, 10, 1),
        completed_on=date(2026, 10, 27),
        today=date(2026, 10, 27),
    )
    assert nxt == date(2026, 10, 28)


def test_calendar_completion_early_moves_to_the_following_occurrence() -> None:
    nxt = next_due_date(
        mode="calendar",
        rule="FREQ=WEEKLY;BYDAY=TH",
        interval_days=None,
        current_due=date(2026, 10, 29),
        anchor=date(2026, 10, 1),
        completed_on=date(2026, 10, 27),
        today=date(2026, 10, 27),
    )
    assert nxt == date(2026, 11, 5)


def test_biweekly_interval_respects_anchor() -> None:
    nxt = next_calendar_occurrence("FREQ=WEEKLY;INTERVAL=2;BYDAY=TU", date(2026, 10, 20), date(2026, 10, 27))
    assert nxt == date(2026, 11, 3)


def test_rolling_counts_from_actual_completion() -> None:
    nxt = next_due_date(
        mode="rolling",
        rule=None,
        interval_days=90,
        current_due=date(2026, 9, 1),
        anchor=None,
        completed_on=date(2026, 10, 27),
        today=date(2026, 10, 27),
    )
    assert nxt == date(2027, 1, 25)


def test_describe() -> None:
    assert describe("calendar", "FREQ=WEEKLY;BYDAY=MO,TH", None) == "Weekly on Mon, Thu"
    assert describe("rolling", None, 90) == "Every 90 days after completion"
    assert describe("calendar", "FREQ=MONTHLY;BYMONTHDAY=1", None) == "Monthly on day 1"


async def test_recurring_chore_lifecycle(planner, clock) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    task = await planner.save_task(
        {"plan_id": chores["id"], "summary": "Feed the cat", "recurrence_mode": "calendar", "recurrence_rule": "FREQ=DAILY"}
    )
    assert task["due_date"] == "2026-10-27"  # first occurrence is today

    result = await planner.set_completed(task["uid"], True, ALEX)
    assert result["next_due"] == "2026-10-28"
    assert result["task"]["status"] == "completed"
    assert result["task"]["streak"] == 1

    # Still "done for today" until the rollover hour (03:00) tomorrow.
    clock.set_local(2026, 10, 28, 2, 30)
    assert await planner.process_rollover() == 0
    clock.set_local(2026, 10, 28, 3, 1)
    assert await planner.process_rollover() == 1
    reopened = await planner.db.get_task(task["uid"])
    assert reopened["status"] == "needs_action"
    assert reopened["completed_by"] is None

    await planner.set_completed(task["uid"], True, SAM)
    assert (await planner.db.get_task(task["uid"]))["streak"] == 2

    board = await planner.get_board()
    row = next(t for t in board["tasks"] if t["uid"] == task["uid"])
    assert row["last_done"]["person_entity_id"] == "person.sam"
    history = await planner.task_history(task["uid"])
    assert [h["person_entity_id"] for h in history] == ["person.sam", "person.alex"]


async def test_missed_day_breaks_the_streak(planner, clock) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    task = await planner.save_task(
        {"plan_id": chores["id"], "summary": "Walk dog", "recurrence_mode": "calendar", "recurrence_rule": "FREQ=DAILY"}
    )
    await planner.set_completed(task["uid"], True, ALEX)
    clock.set_local(2026, 10, 30, 9, 0)  # skipped the 28th and 29th
    await planner.process_rollover()
    assert (await planner.db.get_task(task["uid"]))["streak"] == 0
    result = await planner.set_completed(task["uid"], True, ALEX)
    assert result["task"]["streak"] == 1
    assert result["next_due"] == "2026-10-31"


async def test_undo_completion_restores_due_date_and_log(planner) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    task = await planner.save_task(
        {
            "plan_id": chores["id"],
            "summary": "Furnace filter",
            "recurrence_mode": "rolling",
            "recurrence_interval_days": 90,
            "due_date": "2026-10-20",
        }
    )
    await planner.set_completed(task["uid"], True, ALEX)
    assert (await planner.db.get_task(task["uid"]))["due_date"] == "2027-01-25"
    await planner.set_completed(task["uid"], False, ALEX)
    restored = await planner.db.get_task(task["uid"])
    assert restored["due_date"] == "2026-10-20"
    assert restored["status"] == "needs_action"
    assert await planner.task_history(task["uid"]) == []


async def test_timed_recurrence_keeps_time_of_day(planner) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    task = await planner.save_task(
        {
            "plan_id": chores["id"],
            "summary": "Meds",
            "recurrence_mode": "calendar",
            "recurrence_rule": "FREQ=DAILY",
            "due_datetime": "2026-10-27T20:00:00-05:00",
        }
    )
    assert task["time_of_day_bucket"] == "evening"
    result = await planner.set_completed(task["uid"], True, ALEX)
    assert result["task"]["due_datetime"].startswith("2026-10-29T01:00:00")  # 20:00 CDT on the 28th


async def test_completing_parent_checks_subtasks_and_undo_unchecks_them(planner) -> None:
    trip = await plan_by_name(planner, "Projects")
    parent = await planner.save_task({"plan_id": trip["id"], "summary": "Build shelf"})
    kids = [await planner.save_task({"parent_uid": parent["uid"], "summary": s}) for s in ("Cut", "Sand", "Stain")]
    await planner.set_completed(kids[0]["uid"], True, ALEX)
    await planner.set_completed(parent["uid"], True, ALEX)
    assert {(await planner.db.get_task(k["uid"]))["status"] for k in kids} == {"completed"}
    await planner.set_completed(parent["uid"], False, ALEX)
    statuses = [(await planner.db.get_task(k["uid"]))["status"] for k in kids]
    assert statuses == ["completed", "needs_action", "needs_action"]


async def test_invalid_rrule_is_rejected(planner) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    with pytest.raises(PlannerError):
        await planner.save_task(
            {"plan_id": chores["id"], "summary": "Bad", "recurrence_mode": "calendar", "recurrence_rule": "NOPE"}
        )
