"""Blocking dependencies: pure resolution plus the planner's unblock pipeline."""

from __future__ import annotations

import pytest
from conftest import plan_by_name

from taskdragon.dependencies import dependency_depths, open_blockers, resolve_blocking, would_create_cycle
from taskdragon.planner import Actor, PlannerError

ALEX = Actor(user_id="u1", user_name="Alex", person="person.alex")


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------
def test_open_blocker_blocks_task() -> None:
    tasks = {"a": {"status": "needs_action"}, "b": {"status": "needs_action", "kanban_column": "ready"}}
    [change] = resolve_blocking(tasks, [("b", "a")])
    assert change.uid == "b"
    assert change.status == "blocked"
    assert change.status_before_block == "needs_action"
    assert change.kanban_column == "backlog"


def test_completing_blocker_unblocks_and_restores_previous_status() -> None:
    tasks = {
        "a": {"status": "completed"},
        "b": {"status": "blocked", "status_before_block": "in_process", "kanban_column": "backlog"},
    }
    [change] = resolve_blocking(tasks, [("b", "a")])
    assert change.unblocked
    assert change.status == "in_process"
    assert change.kanban_column == "ready"


def test_task_stays_blocked_until_every_blocker_is_done() -> None:
    tasks = {
        "a": {"status": "completed"},
        "c": {"status": "needs_action"},
        "b": {"status": "blocked", "status_before_block": "needs_action"},
    }
    assert resolve_blocking(tasks, [("b", "a"), ("b", "c")]) == []
    assert open_blockers("b", [("b", "a"), ("b", "c")], {k: v["status"] for k, v in tasks.items()}) == ["c"]


def test_completed_and_snoozed_tasks_are_left_alone() -> None:
    tasks = {"a": {"status": "needs_action"}, "b": {"status": "completed"}, "c": {"status": "snoozed"}}
    assert resolve_blocking(tasks, [("b", "a"), ("c", "a")]) == []


def test_manually_blocked_task_without_dependencies_stays_blocked() -> None:
    assert resolve_blocking({"a": {"status": "blocked"}}, []) == []


def test_dependency_blocked_task_is_released_when_its_blocker_disappears() -> None:
    [change] = resolve_blocking({"b": {"status": "blocked", "status_before_block": "needs_action"}}, [])
    assert change.unblocked and change.status == "needs_action"


def test_cycle_detection() -> None:
    edges = [("b", "a"), ("c", "b")]
    assert would_create_cycle(edges, "a", "c")  # a blocked by c -> a->c->b->a
    assert would_create_cycle(edges, "a", "a")
    assert not would_create_cycle(edges, "d", "c")


def test_dependency_depths() -> None:
    depths = dependency_depths(["a", "b", "c", "d"], [("b", "a"), ("c", "b"), ("d", "a")])
    assert depths == {"a": 0, "b": 1, "c": 2, "d": 1}


# ---------------------------------------------------------------------------
# Planner integration (real SQLite database)
# ---------------------------------------------------------------------------
async def test_unblocking_pipeline(planner) -> None:
    projects = await plan_by_name(planner, "Projects")
    frame = await planner.save_task({"plan_id": projects["id"], "summary": "Frame wall"})
    drywall = await planner.save_task(
        {"plan_id": projects["id"], "summary": "Hang drywall", "blocked_by": [frame["uid"]], "hide_when_blocked": True}
    )
    paint = await planner.save_task({"plan_id": projects["id"], "summary": "Paint", "blocked_by": [drywall["uid"]]})
    assert drywall["status"] == "blocked"
    assert paint["status"] == "blocked"
    assert drywall["kanban_column"] == "backlog"

    result = await planner.set_completed(frame["uid"], True, ALEX)
    assert result["unblocked"] == [drywall["uid"]]
    drywall = await planner.db.get_task(drywall["uid"])
    assert drywall["status"] == "needs_action"
    assert drywall["kanban_column"] == "ready"
    # Paint is still waiting on drywall.
    assert (await planner.db.get_task(paint["uid"]))["status"] == "blocked"

    # Undoing the completion re-blocks the dependent.
    await planner.set_completed(frame["uid"], False, ALEX)
    assert (await planner.db.get_task(drywall["uid"]))["status"] == "blocked"


async def test_board_reports_open_blockers(planner) -> None:
    projects = await plan_by_name(planner, "Projects")
    frame = await planner.save_task({"plan_id": projects["id"], "summary": "Frame wall"})
    drywall = await planner.save_task({"plan_id": projects["id"], "summary": "Hang drywall", "blocked_by": [frame["uid"]]})
    board = await planner.get_board()
    row = next(t for t in board["tasks"] if t["uid"] == drywall["uid"])
    assert row["blocked_by"] == [frame["uid"]]
    assert row["open_blockers"] == [{"uid": frame["uid"], "summary": "Frame wall"}]


async def test_deleting_a_blocker_releases_dependents_and_undo_restores_it(planner) -> None:
    projects = await plan_by_name(planner, "Projects")
    frame = await planner.save_task({"plan_id": projects["id"], "summary": "Frame wall"})
    drywall = await planner.save_task({"plan_id": projects["id"], "summary": "Hang drywall", "blocked_by": [frame["uid"]]})

    snapshot = await planner.delete_tasks([frame["uid"]])
    assert (await planner.db.get_task(drywall["uid"]))["status"] == "needs_action"

    await planner.restore(snapshot)
    assert await planner.db.blockers_of(drywall["uid"]) == [frame["uid"]]
    assert (await planner.db.get_task(drywall["uid"]))["status"] == "blocked"


async def test_cycles_are_refused(planner) -> None:
    projects = await plan_by_name(planner, "Projects")
    a = await planner.save_task({"plan_id": projects["id"], "summary": "A"})
    b = await planner.save_task({"plan_id": projects["id"], "summary": "B", "blocked_by": [a["uid"]]})
    with pytest.raises(PlannerError) as err:
        await planner.add_dependency(a["uid"], b["uid"])
    assert err.value.code == "dependency_cycle"


async def test_dragging_blocked_card_to_ready_keeps_it_blocked(planner) -> None:
    projects = await plan_by_name(planner, "Projects")
    a = await planner.save_task({"plan_id": projects["id"], "summary": "A"})
    b = await planner.save_task({"plan_id": projects["id"], "summary": "B", "blocked_by": [a["uid"]]})
    await planner.move_task(b["uid"], None, kanban_column="ready")
    assert (await planner.db.get_task(b["uid"]))["status"] == "blocked"
    # Moving the blocker to Done (kanban) completes it and unblocks B.
    await planner.move_task(a["uid"], None, kanban_column="done", actor=ALEX)
    assert (await planner.db.get_task(a["uid"]))["status"] == "completed"
    assert (await planner.db.get_task(b["uid"]))["status"] == "needs_action"
