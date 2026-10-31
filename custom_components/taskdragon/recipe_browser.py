"""Mealie recipe browser: cached recipe list, image proxy cache, and recipe → groceries import.

No Home Assistant imports: the coordinator injects the Mealie client getter, the
planner, the database and a clock, so this is unit-tested directly.

Privacy: the pantry cross-check only reads pantry plans the signed-in user can
see, and items only go to a shopping plan the user can see (the WebSocket guard
checks an explicit ``plan_id`` too). Recipe images are fetched by Home Assistant
with the stored token and handed to the card through an authenticated view, so
the token never leaves the server and images work away from home.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from .const import ARCHETYPE_PANTRY, ARCHETYPE_SHOPPING
from .mealie_client import IMAGE_SIZES, MEAL_TYPES, MealieError
from .planner import Actor, PlannerError
from .recipes import (
    DEFAULT_SEASONINGS,
    SKIP_PANTRY,
    SKIP_SEASONING,
    catalog,
    classify_ingredients,
    clean_seasonings,
    ingredient_rows,
    recipe_detail,
    recipe_summary,
    times_cooked,
)

SETTINGS_META = "mealie_import_settings"
SOURCE_MEALIE_RECIPE = "mealie_recipe"
RECIPE_CACHE_SECONDS = 15 * 60
HISTORY_DAYS = 365
IMAGE_CACHE_ENTRIES = 150
IMAGE_CACHE_BYTES = 24 * 1024 * 1024
IMAGE_CACHE_SECONDS = 6 * 3600
MAX_PLAN_AHEAD_DAYS = 366
MIN_SCALE = 0.125
MAX_SCALE = 20.0
# Card size names → Mealie's image files (cards use the small one, the cooking view the full one).
IMAGE_VARIANTS = {"min": IMAGE_SIZES[0], "original": IMAGE_SIZES[2]}


class RecipeBrowser:
    """Everything the card's "Browse Mealie recipes" view needs."""

    def __init__(
        self,
        *,
        mealie: Callable[[], Any],
        planner: Any,
        db: Any,
        today: Callable[[], date],
        on_mealplan_changed: Callable[[], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mealie = mealie
        self.planner = planner
        self.db = db
        self._today = today
        self._on_mealplan_changed = on_mealplan_changed
        self._monotonic = monotonic
        self._cache: dict[str, Any] | None = None
        self._cache_at = 0.0
        self._lock = asyncio.Lock()
        self._images: OrderedDict[str, tuple[bytes, str, float]] = OrderedDict()

    def _client(self) -> Any:
        client = self._mealie()
        if client is None:
            raise PlannerError("mealie_disabled", "Connect Mealie in TaskDragon's options to browse recipes")
        return client

    # ------------------------------------------------------------------
    # Recipe list & images
    # ------------------------------------------------------------------
    async def recipes(self, *, refresh: bool = False) -> dict[str, Any]:
        client = self._client()
        async with self._lock:
            fresh = self._cache is not None and self._monotonic() - self._cache_at < RECIPE_CACHE_SECONDS
            if refresh or not fresh:
                raw = await client.async_list_recipes()
                today = self._today()
                try:
                    cooked = times_cooked(await client.async_get_mealplans(today - timedelta(days=HISTORY_DAYS), today))
                except MealieError:
                    cooked = {}  # "Most cooked" just sorts by name then
                summaries = [s for s in (recipe_summary(r, cooked, client.recipe_url(r.get("slug"))) for r in raw) if s]
                self._cache = {"recipes": summaries, **catalog(summaries), "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds")}
                self._cache_at = self._monotonic()
            assert self._cache is not None
            return {**self._cache, "settings": await self.settings()}

    async def image(self, recipe_id: str, size: str = "min") -> tuple[bytes, str]:
        if size not in IMAGE_VARIANTS:
            raise PlannerError("invalid", "Unknown image size")
        key = f"{recipe_id}:{size}"
        cached = self._images.get(key)
        if cached and self._monotonic() - cached[2] < IMAGE_CACHE_SECONDS:
            self._images.move_to_end(key)
            return cached[0], cached[1]
        data, content_type = await self._client().async_get_recipe_image(recipe_id, IMAGE_VARIANTS[size])
        self._images[key] = (data, content_type, self._monotonic())
        while len(self._images) > IMAGE_CACHE_ENTRIES or sum(len(v[0]) for v in self._images.values()) > IMAGE_CACHE_BYTES:
            self._images.popitem(last=False)
        return data, content_type

    # ------------------------------------------------------------------
    # Import settings (household-wide)
    # ------------------------------------------------------------------
    async def settings(self) -> dict[str, Any]:
        stored = await self.db.get_meta(SETTINGS_META, {}) or {}
        seasonings = stored.get("seasonings")
        return {
            "skip_seasonings": bool(stored.get("skip_seasonings", True)),
            "seasonings": list(seasonings) if isinstance(seasonings, list) else list(DEFAULT_SEASONINGS),
            "defaults": list(DEFAULT_SEASONINGS),
        }

    async def save_settings(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        current = await self.settings()
        stored = {"skip_seasonings": current["skip_seasonings"], "seasonings": current["seasonings"]}
        if "skip_seasonings" in changes:
            stored["skip_seasonings"] = bool(changes["skip_seasonings"])
        if "seasonings" in changes:
            stored["seasonings"] = clean_seasonings(changes["seasonings"] or [])
        await self.db.set_meta(SETTINGS_META, stored)
        return await self.settings()

    # ------------------------------------------------------------------
    # Recipe → groceries
    # ------------------------------------------------------------------
    async def _pantry(self, actor: Actor) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for plan in await self.planner.accessible_plans(actor):
            if plan["archetype"] == ARCHETYPE_PANTRY:
                items.extend(await self.db.list_pantry(plan["id"]))
        return items

    async def detail(self, slug: str, actor: Actor, *, skip_seasonings: bool | None = None) -> dict[str, Any]:
        """The full recipe for the cooking view, ingredients flagged against the user's pantry."""
        client = self._client()
        recipe = await client.async_get_recipe(slug)
        settings = await self.settings()
        skip = settings["skip_seasonings"] if skip_seasonings is None else bool(skip_seasonings)
        detail = recipe_detail(
            recipe,
            seasonings=settings["seasonings"],
            skip_seasonings=skip,
            pantry=await self._pantry(actor),
            url=client.recipe_url(recipe.get("slug") or slug),
        )
        detail["slug"] = detail["slug"] or slug
        if detail["has_image"]:
            detail["image_url"] = f"/api/taskdragon/mealie_image/{detail['id']}?size=original"
        return detail

    async def preview(self, slug: str, actor: Actor, *, skip_seasonings: bool | None = None, scale: float = 1.0) -> dict[str, Any]:
        recipe = await self._client().async_get_recipe(slug)
        settings = await self.settings()
        skip = settings["skip_seasonings"] if skip_seasonings is None else bool(skip_seasonings)
        rows = classify_ingredients(
            ingredient_rows(recipe, self._scale(scale)),
            seasonings=settings["seasonings"],
            skip_seasonings=skip,
            pantry=await self._pantry(actor),
        )
        return {
            "recipe": {"id": recipe.get("id"), "slug": recipe.get("slug") or slug, "name": recipe.get("name") or slug},
            "skip_seasonings": skip,
            "ingredients": rows,
        }

    async def _target_plan(self, plan_id: str | None, actor: Actor) -> dict[str, Any]:
        plan = await self.db.get_plan(plan_id) if plan_id else await self.planner.grocery_plan()
        if plan is None or not self.planner.can_access(plan, actor):
            raise PlannerError("not_found", "That grocery list was not found")
        if plan["archetype"] != ARCHETYPE_SHOPPING:
            raise PlannerError("invalid", "Recipe ingredients go to a shopping list")
        return plan

    @staticmethod
    def _scale(value: Any) -> float:
        try:
            scale = float(value if value is not None else 1.0)
        except (TypeError, ValueError) as err:
            raise PlannerError("invalid", "The batch size must be a number") from err
        if not MIN_SCALE <= scale <= MAX_SCALE:
            raise PlannerError("invalid", "Pick a batch size between ⅛× and 20×")
        return scale

    def _meal_day(self, value: Any) -> date:
        try:
            day = date.fromisoformat(str(value)[:10])
        except ValueError as err:
            raise PlannerError("invalid", "Pick a day for the meal") from err
        today = self._today()
        if not today - timedelta(days=31) <= day <= today + timedelta(days=MAX_PLAN_AHEAD_DAYS):
            raise PlannerError("invalid", "Pick a day within the next year")
        return day

    async def add_to_groceries(
        self,
        slug: str,
        actor: Actor,
        *,
        plan_id: str | None = None,
        indices: Iterable[int] | None = None,
        skip_seasonings: bool | None = None,
        day: Any = None,
        entry_type: str = "dinner",
        scale: float = 1.0,
    ) -> dict[str, Any]:
        """Add a recipe's ingredients (all wanted ones, or exactly ``indices``) to a shopping list.

        ``scale`` multiplies parsed amounts (a double batch puts "2 cups" on the list for a 1-cup line).
        """
        plan = await self._target_plan(plan_id, actor)
        preview = await self.preview(slug, actor, skip_seasonings=skip_seasonings, scale=scale)
        rows = preview["ingredients"]
        if indices is None:
            chosen = [row for row in rows if not row["skip"]]
        else:
            wanted = {int(i) for i in indices}
            chosen = [row for row in rows if row["index"] in wanted]
        chosen_ids = {row["index"] for row in chosen}
        added, merged = [], 0
        for row in chosen:
            result = await self.planner.add_shopping_item(plan["id"], row["text"], actor, source=SOURCE_MEALIE_RECIPE, source_ref=preview["recipe"]["slug"])
            added.append(result["task"]["summary"])
            merged += 1 if result["merged"] else 0
        skipped = [
            {"text": row["text"], "reason": row["skip"] or "unchecked", "pantry_item": row["pantry_item"]}
            for row in rows
            if row["index"] not in chosen_ids
        ]
        meal = await self._plan(preview["recipe"], day, entry_type) if day else None
        return {
            "recipe": preview["recipe"]["name"],
            "plan_id": plan["id"],
            "plan_name": plan["name"],
            "added": added,
            "merged": merged,
            "skipped": skipped,
            "skipped_seasonings": sum(1 for s in skipped if s["reason"] == SKIP_SEASONING),
            "skipped_pantry": sum(1 for s in skipped if s["reason"] == SKIP_PANTRY),
            "meal_plan": meal,
        }

    async def _plan(self, recipe: Mapping[str, Any], day: Any, entry_type: str) -> dict[str, Any]:
        if entry_type not in MEAL_TYPES:
            raise PlannerError("invalid", "Unknown meal type")
        if not recipe.get("id"):
            raise PlannerError("invalid", "Mealie did not return this recipe's id")
        meal_day = self._meal_day(day)
        await self._client().async_create_mealplan(meal_day, str(recipe["id"]), entry_type)
        if self._on_mealplan_changed is not None:
            self._on_mealplan_changed()
        return {"date": meal_day.isoformat(), "entry_type": entry_type, "recipe": recipe.get("name")}

    async def plan_meal(
        self,
        slug: str,
        day: Any,
        actor: Actor,
        *,
        entry_type: str = "dinner",
        add_ingredients: bool = False,
        plan_id: str | None = None,
        skip_seasonings: bool | None = None,
    ) -> dict[str, Any]:
        """Put a recipe on the Mealie meal plan, optionally adding its ingredients to groceries."""
        self._meal_day(day)  # validate before anything is written
        if add_ingredients:
            return await self.add_to_groceries(slug, actor, plan_id=plan_id, skip_seasonings=skip_seasonings, day=day, entry_type=entry_type)
        recipe = await self._client().async_get_recipe(slug)
        meal = await self._plan({"id": recipe.get("id"), "name": recipe.get("name") or slug}, day, entry_type)
        return {"recipe": recipe.get("name") or slug, "added": [], "merged": 0, "skipped": [], "skipped_seasonings": 0, "skipped_pantry": 0, "meal_plan": meal}
