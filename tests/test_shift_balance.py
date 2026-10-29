"""PRN / shift-aware chore assignment."""

from __future__ import annotations

from datetime import date, datetime

from conftest import TZ, plan_by_name

from omniplan.shift_balance import (
    Chore,
    PersonAvailability,
    ShiftWindow,
    balance_chores,
    choose_assignee,
    event_is_shift,
    parse_keywords,
    shifts_for_day,
)

DAY = date(2026, 10, 28)


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def shift(start: datetime, end: datetime) -> ShiftWindow:
    return ShiftWindow(start=start, end=end)


def test_parse_keywords() -> None:
    assert parse_keywords("Shift, Work , PRN,,") == ["shift", "work", "prn"]
    assert parse_keywords(None) == []


def test_event_is_shift_keywords() -> None:
    keywords = ["shift", "prn"]
    assert event_is_shift("PRN - ICU", None, keywords)
    assert event_is_shift("Day", "Night shift coverage", keywords)
    assert not event_is_shift("Dentist", None, keywords)
    assert event_is_shift("Anything at all", None, [])  # no keywords: every event counts


def test_shifts_for_day_handles_all_day_timed_and_overnight_events() -> None:
    events = [
        {"summary": "PRN shift", "start": "2026-10-28T07:00:00-05:00", "end": "2026-10-28T19:30:00-05:00"},
        {"summary": "Work", "start": "2026-10-28", "end": "2026-10-29"},
        {"summary": "Night shift", "start": "2026-10-27T19:00:00-05:00", "end": "2026-10-28T07:00:00-05:00"},
        {"summary": "Shift", "start": "2026-10-29T07:00:00-05:00", "end": "2026-10-29T19:00:00-05:00"},
        {"summary": "Dentist", "start": "2026-10-28T10:00:00-05:00", "end": "2026-10-28T11:00:00-05:00"},
    ]
    windows = shifts_for_day(events, ["shift", "work", "prn"], DAY, TZ)
    summaries = sorted(w.summary for w in windows)
    assert summaries == ["Night shift", "PRN shift", "Work"]
    all_day = next(w for w in windows if w.summary == "Work")
    assert all_day.all_day and all_day.end == at(0, day=date(2026, 10, 29))


def test_off_duty_partner_gets_the_chore() -> None:
    people = [
        PersonAvailability("person.alex", [shift(at(7), at(19))]),
        PersonAvailability("person.sam", []),
    ]
    assert choose_assignee(people, {}) == "person.sam"


def test_when_both_work_the_earlier_shift_end_wins() -> None:
    people = [
        PersonAvailability("person.alex", [shift(at(7), at(19, 30))]),
        PersonAvailability("person.sam", [shift(at(9), at(15))]),
    ]
    assert choose_assignee(people, {}) == "person.sam"


def test_overnight_shift_ending_this_morning_counts_as_ending_early() -> None:
    people = [
        PersonAvailability("person.alex", [shift(at(19, day=date(2026, 10, 27)), at(7))]),
        PersonAvailability("person.sam", [shift(at(8), at(17))]),
    ]
    assert choose_assignee(people, {}) == "person.alex"


def test_all_day_shift_loses_to_timed_shift() -> None:
    people = [
        PersonAvailability("person.alex", [ShiftWindow(at(0), at(0, day=date(2026, 10, 29)), all_day=True)]),
        PersonAvailability("person.sam", [shift(at(7), at(19))]),
    ]
    assert choose_assignee(people, {}) == "person.sam"


def test_load_balancing_between_two_off_duty_people() -> None:
    people = [PersonAvailability("person.alex"), PersonAvailability("person.sam")]
    chores = [Chore("dishes", 15), Chore("trash", 5), Chore("laundry", 30), Chore("cat", 5)]
    assignments = balance_chores(chores, people)
    load = {"person.alex": 0, "person.sam": 0}
    minutes = {c.uid: c.weight for c in chores}
    for uid, person in assignments.items():
        load[person] += minutes[uid]
    assert set(assignments) == {"dishes", "trash", "laundry", "cat"}
    assert abs(load["person.alex"] - load["person.sam"]) <= 15


def test_existing_load_is_respected() -> None:
    people = [PersonAvailability("person.alex"), PersonAvailability("person.sam")]
    assignments = balance_chores([Chore("dishes", 15)], people, initial_load={"person.alex": 60})
    assert assignments == {"dishes": "person.sam"}


def test_one_working_one_off_sends_everything_to_the_off_duty_person() -> None:
    people = [
        PersonAvailability("person.alex", [shift(at(7), at(19))]),
        PersonAvailability("person.sam", []),
    ]
    assignments = balance_chores([Chore("a"), Chore("b"), Chore("c")], people)
    assert set(assignments.values()) == {"person.sam"}


def test_no_people_assigns_nothing() -> None:
    assert balance_chores([Chore("a")], []) == {}


async def test_planner_candidates_and_apply(planner) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    dishes = await planner.save_task(
        {"plan_id": chores["id"], "summary": "Dishes", "shift_aware": True, "estimated_minutes": 15, "due_date": "2026-10-27"}
    )
    await planner.save_task({"plan_id": chores["id"], "summary": "Future", "shift_aware": True, "due_date": "2026-11-05"})
    await planner.save_task({"plan_id": chores["id"], "summary": "Normal", "due_date": "2026-10-27"})
    candidates = await planner.shift_aware_candidates()
    assert [c["summary"] for c in candidates] == ["Dishes"]

    people = [PersonAvailability("person.alex", [shift(at(7), at(19))]), PersonAvailability("person.sam")]
    assignments = balance_chores([Chore(c["uid"], c["estimated_minutes"]) for c in candidates], people)
    assert await planner.apply_assignments(assignments) == 1
    updated = await planner.db.get_task(dishes["uid"])
    assert updated["assigned_to"] == ["person.sam"]
    assert updated["extra"]["auto_assigned"]["person"] == "person.sam"
    # Re-applying the same assignment is a no-op.
    assert await planner.apply_assignments(assignments) == 0
