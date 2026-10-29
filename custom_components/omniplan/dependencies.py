"""Blocking-dependency resolution (pure logic).

A dependency edge ``(task_uid, blocked_by_uid)`` means *task* cannot start until
*blocked_by* is completed. While any blocker is open the task's status is
``blocked``; once every blocker is completed the task returns to the status it
had before it was blocked.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .const import (
    KANBAN_BACKLOG,
    KANBAN_READY,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_NEEDS_ACTION,
    STATUS_SNOOZED,
)

Edge = tuple[str, str]


@dataclass(frozen=True)
class StatusChange:
    """A status transition produced by :func:`resolve_blocking`."""

    uid: str
    status: str
    status_before_block: str | None
    kanban_column: str | None
    unblocked: bool


def blockers_by_task(edges: Iterable[Edge]) -> dict[str, set[str]]:
    """Map each task to the set of tasks blocking it."""
    result: dict[str, set[str]] = defaultdict(set)
    for task_uid, blocker_uid in edges:
        result[task_uid].add(blocker_uid)
    return result


def open_blockers(uid: str, edges: Iterable[Edge], statuses: Mapping[str, str]) -> list[str]:
    """Return the blockers of ``uid`` that are not completed (unknown tasks are ignored)."""
    return sorted(
        blocker
        for task_uid, blocker in edges
        if task_uid == uid and blocker in statuses and statuses[blocker] != STATUS_COMPLETED
    )


def resolve_blocking(
    tasks: Mapping[str, Mapping[str, object]],
    edges: Iterable[Edge],
) -> list[StatusChange]:
    """Compute the status transitions implied by the dependency graph.

    ``tasks`` maps uid to a mapping with at least ``status`` and optionally
    ``status_before_block`` and ``kanban_column``. Completed and snoozed tasks
    are never touched: a snoozed task is re-evaluated when it wakes up.
    """
    statuses = {uid: str(task.get("status")) for uid, task in tasks.items()}
    graph = blockers_by_task(edges)
    changes: list[StatusChange] = []
    for uid, task in tasks.items():
        status = statuses[uid]
        if status in (STATUS_COMPLETED, STATUS_SNOOZED):
            continue
        blockers = [b for b in graph.get(uid, ()) if b in statuses]
        is_blocked = any(statuses[b] != STATUS_COMPLETED for b in blockers)
        column = task.get("kanban_column")
        if is_blocked and status != STATUS_BLOCKED:
            changes.append(
                StatusChange(
                    uid=uid,
                    status=STATUS_BLOCKED,
                    status_before_block=status,
                    kanban_column=KANBAN_BACKLOG if column is not None else None,
                    unblocked=False,
                )
            )
        elif not is_blocked and status == STATUS_BLOCKED and (blockers or task.get("status_before_block")):
            # Only dependency-blocked tasks auto-unblock (they remember their previous status, or
            # still have edges); a task marked blocked by hand stays put.
            previous = task.get("status_before_block") or STATUS_NEEDS_ACTION
            if previous in (STATUS_BLOCKED, STATUS_COMPLETED, STATUS_SNOOZED):
                previous = STATUS_NEEDS_ACTION
            # A project card that was parked in Backlog only because of its blocker is now Ready.
            new_column = KANBAN_READY if column == KANBAN_BACKLOG else column
            changes.append(
                StatusChange(
                    uid=uid,
                    status=str(previous),
                    status_before_block=None,
                    kanban_column=new_column,
                    unblocked=True,
                )
            )
    return changes


def would_create_cycle(edges: Iterable[Edge], task_uid: str, blocker_uid: str) -> bool:
    """Return True if adding ``task_uid -> blocked_by blocker_uid`` creates a cycle."""
    if task_uid == blocker_uid:
        return True
    graph = blockers_by_task(edges)
    # A cycle exists if task_uid is (transitively) a blocker of blocker_uid.
    stack = [blocker_uid]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == task_uid:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(graph.get(current, ()))
    return False


def dependency_depths(uids: Iterable[str], edges: Iterable[Edge]) -> dict[str, int]:
    """Return each task's depth in the dependency tree (0 = no blockers).

    Used to indent the dependency list view. Cycles (which the API refuses to
    create) are cut off rather than recursing forever.
    """
    graph = blockers_by_task(edges)
    known = set(uids)
    depths: dict[str, int] = {}

    def depth(uid: str, trail: frozenset[str]) -> int:
        if uid in depths:
            return depths[uid]
        parents = [b for b in graph.get(uid, ()) if b in known and b not in trail]
        value = 0 if not parents else 1 + max(depth(p, trail | {uid}) for p in parents)
        depths[uid] = value
        return value

    for uid in known:
        depth(uid, frozenset())
    return depths
