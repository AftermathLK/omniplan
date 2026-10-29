"""Mealie integration through the coordinator, against an in-memory fake Mealie."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.omniplan.const import DOMAIN


class FakeMealie:
    """Just enough of the Mealie API for the sync engine."""

    instance: FakeMealie | None = None

    def __init__(self, session, base_url: str, token: str) -> None:
        FakeMealie.instance = self
        self.base_url = base_url
        self.items: dict[str, dict] = {}
        self.mealplans: list[dict] = []
        self.recipes: dict[str, dict] = {}
        self.created: list[str] = []
        self.updated: list[dict] = []
        self.deleted: list[str] = []
        self._seq = 0

    async def async_get_self(self):
        return {"groupSlug": "home"}

    def recipe_url(self, slug):
        return f"{self.base_url}/g/home/r/{slug}" if slug else None

    async def async_get_shopping_lists(self):
        return [{"id": "L1", "name": "Groceries"}]

    async def async_get_shopping_list(self, list_id):
        return {"id": list_id, "listItems": [dict(item) for item in self.items.values()]}

    async def async_create_item(self, list_id, text, *, checked=False, position=None):
        self._seq += 1
        item = {"id": f"new{self._seq}", "shoppingListId": list_id, "display": text, "note": text, "checked": checked}
        self.items[item["id"]] = item
        self.created.append(text)
        return dict(item)

    async def async_update_item(self, raw_item, *, text=None, checked=None):
        item = self.items[raw_item["id"]]
        if text is not None:
            item["display"] = item["note"] = text
        if checked is not None:
            item["checked"] = checked
        self.updated.append({"id": item["id"], "text": text, "checked": checked})
        return dict(item)

    async def async_delete_item(self, item_id):
        self.items.pop(item_id, None)
        self.deleted.append(item_id)

    async def async_get_mealplans(self, start, end):
        return self.mealplans

    async def async_get_recipe(self, slug):
        return self.recipes[slug]


@pytest.fixture
async def mealie_entry(hass: HomeAssistant):
    hass.states.async_set("person.alex", "home", {"friendly_name": "Alex"})
    with (
        patch("custom_components.omniplan.coordinator.MealieClient", FakeMealie),
        patch("custom_components.omniplan.coordinator.async_get_clientsession"),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN,
            version=1,
            minor_version=2,
            data={"mealie_url": "http://mealie.local:9000", "mealie_api_token": "token", "mealie_verify_ssl": True},
            options={"people": [{"entity_id": "person.alex"}], "dinner_time": "18:00:00"},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        yield entry
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_two_way_shopping_sync(hass: HomeAssistant, mealie_entry: MockConfigEntry) -> None:
    coordinator = mealie_entry.runtime_data
    planner = coordinator.planner
    fake = FakeMealie.instance
    groceries = await planner.grocery_plan()
    await planner.update_plan(groceries["id"], {"settings": {"mealie_list_id": "L1"}})
    fake.items = {
        "r1": {"id": "r1", "shoppingListId": "L1", "display": "2 lb ground beef", "checked": False},
        "r2": {"id": "r2", "shoppingListId": "L1", "display": "Milk", "checked": False},
    }
    await planner.add_shopping_item(groceries["id"], "garlic powder")
    milk_local = (await planner.add_shopping_item(groceries["id"], "milk"))["task"]

    status = await coordinator.async_sync_mealie(meal_plans=False)
    assert status["ok"] is True
    tasks = {t["summary"]: t for t in await coordinator.db.list_tasks(groceries["id"])}
    assert set(tasks) == {"Garlic powder", "Milk", "Ground beef"}
    assert tasks["Ground beef"]["quantity"] == "2 lb" and tasks["Ground beef"]["category"] == "Meat"
    assert tasks["Milk"]["uid"] == milk_local["uid"] and tasks["Milk"]["external_id"] == "r2"  # linked, not duplicated
    assert fake.created == ["Garlic powder"]

    # Checked off in Mealie -> completed here.
    fake.items["r1"]["checked"] = True
    fake.items["r1"]["updateAt"] = (dt_util.utcnow() + timedelta(seconds=5)).isoformat()
    await coordinator.async_sync_mealie(meal_plans=False)
    assert (await coordinator.db.get_task(tasks["Ground beef"]["uid"]))["status"] == "completed"

    # Checked off here -> checked in Mealie.
    await planner.set_completed(tasks["Garlic powder"]["uid"], True)
    await coordinator.async_sync_mealie(meal_plans=False)
    garlic_remote = next(i for i in fake.items.values() if i["display"] == "Garlic powder")
    assert garlic_remote["checked"] is True

    # Deleted here -> deleted in Mealie.
    await planner.delete_tasks([milk_local["uid"]])
    await coordinator.async_sync_mealie(meal_plans=False)
    assert "r2" in fake.deleted

    # Deleted in Mealie -> deleted here.
    fake.items.pop("r1")
    await coordinator.async_sync_mealie(meal_plans=False)
    assert await coordinator.db.get_task(tasks["Ground beef"]["uid"]) is None


async def test_meal_plan_prep_cook_and_leftovers(hass: HomeAssistant, mealie_entry: MockConfigEntry) -> None:
    coordinator = mealie_entry.runtime_data
    planner = coordinator.planner
    fake = FakeMealie.instance
    tomorrow = planner.today() + timedelta(days=2)
    fake.recipes["smoked-brisket"] = {
        "name": "Smoked Brisket",
        "slug": "smoked-brisket",
        "tags": [{"name": "thaw-36h", "slug": "thaw-36h"}, {"name": "smoker-4h", "slug": "smoker-4h"}],
        "recipeIngredient": [{"quantity": 12, "unit": {"abbreviation": "lb"}, "food": {"name": "brisket"}}],
        "totalTime": "10 hours",
    }
    fake.mealplans = [
        {"id": 42, "date": tomorrow.isoformat(), "entryType": "dinner", "recipe": {"slug": "smoked-brisket", "name": "Smoked Brisket"}}
    ]
    await coordinator.async_sync_mealie()
    prep = {t["source_ref"]: t for t in await coordinator.db.list_tasks(source="mealie_prep")}
    assert set(prep) == {"42:thaw", "42:smoker"}
    assert prep["42:thaw"]["summary"] == "Move brisket from freezer to fridge to thaw for Smoked Brisket"
    assert prep["42:smoker"]["summary"] == "Prep rub and fire up smoker for Smoked Brisket"
    assert prep["42:thaw"]["link_url"] == "http://mealie.local:9000/g/home/r/smoked-brisket"
    [cook] = await coordinator.db.list_tasks(source="mealie_cook")
    assert cook["summary"] == "Cook Smoked Brisket" and cook["estimated_minutes"] == 600

    # Re-syncing doesn't duplicate; removing the meal removes open generated tasks.
    await coordinator.async_sync_mealie()
    assert len(await coordinator.db.list_tasks(source="mealie_prep")) == 2

    # Finishing the cook task creates the leftover tracker.
    await planner.set_completed(cook["uid"], True)
    await hass.async_block_till_done()
    [leftover] = await coordinator.db.list_tasks(source="leftover")
    assert leftover["summary"].startswith("Eat or freeze leftover Smoked Brisket (Cooked ")
    assert leftover["expires_at"] is not None

    fake.mealplans = []
    await coordinator.async_sync_mealie()
    assert await coordinator.db.list_tasks(source="mealie_prep") == []
