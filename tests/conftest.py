"""Test bootstrap.

The pure-logic modules of OmniPlan (units, dependencies, recurrence, shift
balancing, snooze rules, kitchen, brain dump parsing, database and planner)
import nothing from Home Assistant. To test them without installing Home
Assistant we register the integration directory as a bare ``omniplan`` package
so its ``__init__.py`` (which does import Home Assistant) is never executed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "omniplan"

if "omniplan" not in sys.modules:
    package = types.ModuleType("omniplan")
    package.__path__ = [str(PACKAGE_DIR)]  # type: ignore[attr-defined]
    sys.modules["omniplan"] = package


# ---------------------------------------------------------------------------
# Shared fixtures (imported after the package shim above is in place)
# ---------------------------------------------------------------------------
from datetime import datetime, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from omniplan.database import OmniPlanDB  # noqa: E402
from omniplan.planner import Planner  # noqa: E402

TZ = ZoneInfo("America/Chicago")


class Clock:
    """A controllable clock for the planner (aware UTC datetimes)."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def set_local(self, *args: int) -> None:
        self.now = datetime(*args, tzinfo=TZ).astimezone(timezone.utc)


@pytest.fixture
def clock() -> Clock:
    # Tuesday 2026-10-27, 09:00 local
    return Clock(datetime(2026, 10, 27, 9, 0, tzinfo=TZ).astimezone(timezone.utc))


@pytest.fixture
async def db(tmp_path):
    database = OmniPlanDB(str(tmp_path / "omniplan.db"))
    await database.async_open()
    yield database
    await database.async_close()


@pytest.fixture
async def planner(db, clock):
    events: list[tuple[str, dict]] = []
    notified: list[set[str]] = []
    states: dict[str, str] = {}
    instance = Planner(
        db,
        tz=TZ,
        rollover_hour=3,
        notify=notified.append,
        fire_event=lambda event, data: events.append((event, data)),
        state_reader=states.get,
        recipe_url=lambda slug: f"http://mealie.local/g/home/r/{slug}",
        household=lambda: ["person.alex", "person.sam"],
        weather_entity=lambda: "weather.home",
        clock=clock,
    )
    instance.test_events = events  # type: ignore[attr-defined]
    instance.test_notified = notified  # type: ignore[attr-defined]
    instance.test_states = states  # type: ignore[attr-defined]
    await instance.seed_defaults()
    return instance


async def plan_by_name(planner: Planner, name: str) -> dict:
    for plan in await planner.db.list_plans(include_archived=True):
        if plan["name"] == name:
            return plan
    raise AssertionError(f"no plan {name}")
