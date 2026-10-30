"""Mealie integration through the coordinator, against an in-memory fake Mealie."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.omniplan.const import DOMAIN
from custom_components.omniplan.mealie_client import MealieError

ORZO_ID = "0d2b6f1a-1111-4222-8333-444455556666"


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
        self.planned: list[tuple] = []
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
        if slug not in self.recipes:
            raise MealieError("Recipe not found in Mealie")
        return self.recipes[slug]

    async def async_list_recipes(self):
        return [{k: r.get(k) for k in ("id", "slug", "name", "recipeCategory", "tags", "image", "totalTime")} for r in self.recipes.values()]

    async def async_create_mealplan(self, day, recipe_id, entry_type="dinner"):
        self.planned.append((day.isoformat(), recipe_id, entry_type))
        return {"id": len(self.planned)}

    async def async_get_recipe_image(self, recipe_id, size="min-original.webp"):
        if recipe_id != ORZO_ID:
            raise MealieError("This recipe has no image")
        return b"RIFF....WEBPVP8 ", "image/webp"


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


async def test_recipe_browser_websocket_and_image_proxy(hass: HomeAssistant, mealie_entry: MockConfigEntry, hass_ws_client, hass_client, hass_client_no_auth) -> None:
    coordinator = mealie_entry.runtime_data
    fake = FakeMealie.instance
    fake.recipes["chicken-sausage-orzo"] = {
        "id": ORZO_ID,
        "slug": "chicken-sausage-orzo",
        "name": "Chicken Sausage Orzo <img src=x onerror=alert(1)>",
        "recipeCategory": [{"name": "Chicken"}, {"name": "Pasta"}],
        "tags": [{"name": "Weeknight"}],
        "image": "k1",
        "totalTime": "35 minutes",
        "recipeIngredient": [
            {"quantity": 12, "unit": {"abbreviation": "oz"}, "food": {"name": "chicken sausage"}},
            {"quantity": 1.5, "unit": {"name": "cup"}, "food": {"name": "orzo"}},
            {"quantity": 1, "unit": {"abbreviation": "tsp"}, "food": {"name": "kosher salt"}},
            {"note": "1 tsp smoked paprika"},
        ],
    }
    ws = await hass_ws_client(hass)
    seq = iter(range(1, 1000))

    async def call(message: dict) -> dict:
        msg_id = next(seq)
        await ws.send_json({"id": msg_id, **message})
        while True:
            reply = await ws.receive_json()
            if reply.get("id") == msg_id and reply.get("type") == "result":
                return reply

    listing = (await call({"type": "omniplan/mealie_get_recipes"}))["result"]
    [orzo] = listing["recipes"]
    assert orzo["slug"] == "chicken-sausage-orzo" and orzo["has_image"] and listing["categories"] == ["Chicken", "Pasta"]
    assert "token" not in json.dumps(listing)  # the API token never reaches the card

    preview = (await call({"type": "omniplan/mealie_recipe_preview", "recipe_slug": "chicken-sausage-orzo"}))["result"]
    assert [row["skip"] for row in preview["ingredients"]] == [None, None, "seasoning", "seasoning"]

    added = (await call({"type": "omniplan/mealie_add_recipe_to_groceries", "recipe_slug": "chicken-sausage-orzo"}))["result"]
    assert sorted(added["added"]) == ["Chicken sausage", "Orzo"] and added["skipped_seasonings"] == 2
    groceries = await coordinator.planner.grocery_plan()
    assert {t["summary"] for t in await coordinator.db.list_tasks(groceries["id"], top_level_only=True)} >= {"Chicken sausage", "Orzo"}

    day = (dt_util.now().date() + timedelta(days=2)).isoformat()
    planned = (await call({"type": "omniplan/mealie_plan_meal", "recipe_slug": "chicken-sausage-orzo", "date": day}))["result"]
    assert planned["meal_plan"]["date"] == day and fake.planned == [(day, ORZO_ID, "dinner")]

    settings = (await call({"type": "omniplan/mealie_import_settings", "settings": {"skip_seasonings": False}}))["result"]
    assert settings["skip_seasonings"] is False
    preview = (await call({"type": "omniplan/mealie_recipe_preview", "recipe_slug": "chicken-sausage-orzo"}))["result"]
    assert all(row["skip"] is None for row in preview["ingredients"])

    bad = await call({"type": "omniplan/mealie_recipe_preview", "recipe_slug": "missing"})
    assert not bad["success"] and bad["error"]["code"] == "mealie_error"
    chores = await coordinator.planner.chores_plan()
    wrong = await call({"type": "omniplan/mealie_add_recipe_to_groceries", "recipe_slug": "chicken-sausage-orzo", "plan_id": chores["id"]})
    assert not wrong["success"] and wrong["error"]["code"] == "invalid"

    client = await hass_client()
    image = await client.get(f"/api/omniplan/mealie_image/{ORZO_ID}")
    assert image.status == 200 and image.content_type == "image/webp" and image.headers["X-Content-Type-Options"] == "nosniff"
    assert (await client.get("/api/omniplan/mealie_image/aaaabbbb-0000")).status == 404  # no image in Mealie
    assert (await client.get("/api/omniplan/mealie_image/not%20an%20id")).status == 400
    assert (await (await hass_client_no_auth()).get(f"/api/omniplan/mealie_image/{ORZO_ID}")).status == 401

    # The cooking view: the full recipe, its full-size image through the same proxy.
    detail = (await call({"type": "omniplan/mealie_get_recipe_detail", "recipe_slug": "chicken-sausage-orzo"}))["result"]
    assert detail["image_url"] == f"/api/omniplan/mealie_image/{ORZO_ID}?size=original" and "token" not in json.dumps(detail)
    assert [row["food"] for row in detail["ingredients"]][:2] == ["chicken sausage", "orzo"]
    assert (await client.get(detail["image_url"])).status == 200
    assert (await client.get(f"/api/omniplan/mealie_image/{ORZO_ID}?size=huge")).status == 400

