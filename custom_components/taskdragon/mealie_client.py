"""Async Mealie REST client plus the shopping-list text helpers used by the sync engine.

Supports Mealie v2 (``/api/households/...``) and falls back to the v1 group
routes (``/api/groups/...``) when the household routes return 404. Unit
normalization and aisle categorization live in :mod:`.units` and are re-exported
here because the sync engine uses them to merge Mealie items.

Security notes: the API token only ever travels in the ``Authorization`` header
of requests to the configured base URL. Redirects are refused (so the header is
never replayed to another host), every request has a hard timeout, ids from
Mealie or the UI are percent-encoded into URL paths, and error messages never
contain the URL, the token or response bodies, because they reach logs and the
dashboard.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
from datetime import date
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from .const import MEALIE_TIMEOUT_SECONDS, SLUG_PATTERN
from .units import categorize, categorize_parsed, combine_quantity_text, merge_items, name_key, parse_item

__all__ = [
    "InvalidMealieUrl",
    "MealieAuthError",
    "MealieClient",
    "MealieError",
    "categorize",
    "categorize_parsed",
    "combine_quantity_text",
    "item_text",
    "item_hash",
    "merge_items",
    "name_key",
    "parse_item",
    "validate_base_url",
    "validate_recipe_id",
    "validate_slug",
]

_LOGGER = logging.getLogger(__name__)
TIMEOUT = aiohttp.ClientTimeout(total=MEALIE_TIMEOUT_SECONDS)
_SLUG_RE = re.compile(SLUG_PATTERN)
_RECIPE_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
RECIPE_PAGE_SIZE = 1000
MAX_RECIPES = 3000
IMAGE_SIZES = ("min-original.webp", "tiny-original.webp", "original.webp")
IMAGE_TYPES = ("image/webp", "image/jpeg", "image/png", "image/gif")
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MEAL_TYPES = ("breakfast", "lunch", "dinner", "side", "snack", "dessert", "drink")


class MealieError(Exception):
    """Mealie could not be reached or returned an error."""


class MealieAuthError(MealieError):
    """The API token was rejected."""


class InvalidMealieUrl(ValueError):
    """The configured base URL is not a plain http(s) URL."""


def validate_base_url(raw: str) -> str:
    """Normalize a Mealie base URL or raise :class:`InvalidMealieUrl`.

    Only ``http``/``https`` URLs with a host are accepted. Credentials, query
    strings and fragments are refused so the token can't be smuggled to a
    different place and nothing but the base path is ever requested. LAN
    addresses are allowed on purpose: Mealie normally runs next to Home
    Assistant. Link-local, unspecified and multicast addresses (cloud metadata
    endpoints, 0.0.0.0) are refused.
    """
    try:
        parts = urlsplit((raw or "").strip())
        port = parts.port  # raises ValueError for junk ports
    except ValueError as err:
        raise InvalidMealieUrl("invalid port") from err
    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidMealieUrl("scheme must be http or https")
    if not parts.hostname:
        raise InvalidMealieUrl("missing host")
    if parts.username or parts.password:
        raise InvalidMealieUrl("credentials in the URL are not allowed")
    if parts.query or parts.fragment:
        raise InvalidMealieUrl("query strings and fragments are not allowed")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_link_local or address.is_unspecified or address.is_multicast):
        raise InvalidMealieUrl("that address cannot be a Mealie server")
    netloc = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", ""))


def validate_slug(slug: str) -> str:
    """Recipe slugs go into URL paths; accept only slug characters."""
    value = (slug or "").strip()
    if not _SLUG_RE.match(value):
        raise MealieError("That does not look like a Mealie recipe slug")
    return value


def validate_recipe_id(recipe_id: str) -> str:
    """Recipe ids (UUIDs) go into the image URL path."""
    value = (recipe_id or "").strip()
    if not _RECIPE_ID_RE.match(value):
        raise MealieError("That does not look like a Mealie recipe id")
    return value


def _seg(value: Any) -> str:
    """Percent-encode one URL path segment (``/`` included)."""
    return quote(str(value), safe="")


class MealieClient:
    """Thin async wrapper around the Mealie REST API."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str) -> None:
        self._session = session
        self.base_url = validate_base_url(base_url)
        self._token = token
        self._prefix: str | None = None  # "households" (v2) or "groups" (v1)
        self._group_slug: str | None = None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                timeout=TIMEOUT,
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise MealieAuthError(f"Mealie rejected the API token (HTTP {response.status})")
                if 300 <= response.status < 400:
                    raise MealieError(f"Mealie answered with a redirect (HTTP {response.status}); enter the final URL")
                if response.status == 404:
                    raise _NotFound("Mealie returned 404")
                if response.status >= 400:
                    raise MealieError(f"Mealie returned HTTP {response.status}")
                if response.status == 204 or _length(response) == 0:
                    return None
                return await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            # aiohttp messages include host and port; keep them out of logs and the UI.
            raise MealieError(f"Cannot reach Mealie ({type(err).__name__})") from err

    async def _request_image(self, path: str) -> tuple[bytes, str]:
        """GET an image: same rules as :meth:`_request`, plus a type check and a size cap."""
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self._token}", "Accept": ", ".join(IMAGE_TYPES)}
        try:
            async with self._session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise MealieAuthError(f"Mealie rejected the API token (HTTP {response.status})")
                if response.status == 404:
                    raise _NotFound("Mealie returned 404")
                if response.status != 200:
                    raise MealieError(f"Mealie returned HTTP {response.status}")
                content_type = (response.content_type or "").lower()
                if content_type not in IMAGE_TYPES:
                    raise MealieError("Mealie did not return an image")
                if (_length(response) or 0) > MAX_IMAGE_BYTES:
                    raise MealieError("The recipe image is too large")
                data = await response.content.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    raise MealieError("The recipe image is too large")
                return data, content_type
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise MealieError(f"Cannot reach Mealie ({type(err).__name__})") from err

    async def _household_request(self, method: str, suffix: str, **kwargs: Any) -> Any:
        """Call a household-scoped route, detecting v1/v2 on first use."""
        prefixes = [self._prefix] if self._prefix else ["households", "groups"]
        last_error: Exception | None = None
        for prefix in prefixes:
            try:
                result = await self._request(method, f"/api/{prefix}{suffix}", **kwargs)
                self._prefix = prefix
                return result
            except _NotFound as err:
                last_error = err
                continue
        raise MealieError("This Mealie version does not support that request") from last_error

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    async def async_get_self(self) -> dict[str, Any]:
        """Return the current user; used to validate URL + token."""
        try:
            data = await self._request("GET", "/api/users/self")
        except _NotFound as err:
            raise MealieError("This does not look like a Mealie server") from err
        if isinstance(data, dict):
            self._group_slug = data.get("groupSlug") or data.get("group_slug") or self._group_slug
        return data or {}

    def recipe_url(self, slug: str | None) -> str | None:
        if not slug:
            return None
        if self._group_slug:
            return f"{self.base_url}/g/{_seg(self._group_slug)}/r/{_seg(slug)}"
        return f"{self.base_url}/recipe/{_seg(slug)}"

    # ------------------------------------------------------------------
    # Meal plans & recipes
    # ------------------------------------------------------------------
    async def async_get_mealplans(self, start: date, end: date) -> list[dict[str, Any]]:
        data = await self._household_request(
            "GET",
            "/mealplans",
            params={
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "perPage": -1,
                "orderBy": "date",
                "orderDirection": "asc",
            },
        )
        return _items(data)

    async def async_get_recipe(self, slug: str) -> dict[str, Any]:
        try:
            data = await self._request("GET", f"/api/recipes/{_seg(validate_slug(slug))}")
        except _NotFound as err:
            raise MealieError("Recipe not found in Mealie") from err
        return data or {}

    async def async_list_recipes(self) -> list[dict[str, Any]]:
        """Every recipe summary (name, slug, image key, categories, tags, times), page by page."""
        recipes: list[dict[str, Any]] = []
        page = 1
        while len(recipes) < MAX_RECIPES:
            data = await self._request(
                "GET",
                "/api/recipes",
                params={"page": page, "perPage": RECIPE_PAGE_SIZE, "orderBy": "name", "orderDirection": "asc"},
            )
            items = [item for item in _items(data) if isinstance(item, dict)]
            recipes.extend(items)
            total_pages = data.get("total_pages") if isinstance(data, dict) else None
            if not items or not isinstance(total_pages, int) or page >= total_pages:
                break
            page += 1
        return recipes[:MAX_RECIPES]

    async def async_create_mealplan(self, day: date, recipe_id: str, entry_type: str = "dinner") -> dict[str, Any]:
        """Put a recipe on the household meal plan."""
        if entry_type not in MEAL_TYPES:
            raise MealieError("Unknown meal type")
        payload = {"date": day.isoformat(), "entryType": entry_type, "recipeId": validate_recipe_id(recipe_id), "title": "", "text": ""}
        return await self._household_request("POST", "/mealplans", json=payload) or {}

    async def async_get_recipe_image(self, recipe_id: str, size: str = IMAGE_SIZES[0]) -> tuple[bytes, str]:
        """A recipe's image bytes and content type (for the authenticated Home Assistant proxy)."""
        if size not in IMAGE_SIZES:
            raise MealieError("Unknown image size")
        path = f"/api/media/recipes/{_seg(validate_recipe_id(recipe_id))}/images/{size}"
        try:
            return await self._request_image(path)
        except _NotFound as err:
            raise MealieError("This recipe has no image") from err

    # ------------------------------------------------------------------
    # Shopping lists
    # ------------------------------------------------------------------
    async def async_get_shopping_lists(self) -> list[dict[str, Any]]:
        return _items(await self._household_request("GET", "/shopping/lists", params={"perPage": -1}))

    async def async_get_shopping_list(self, list_id: str) -> dict[str, Any]:
        return await self._household_request("GET", f"/shopping/lists/{_seg(list_id)}") or {}

    async def async_create_item(self, list_id: str, text: str, *, checked: bool = False, position: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "shoppingListId": list_id,
            "note": text,
            "display": text,
            "quantity": 0,
            "checked": checked,
            "isFood": False,
        }
        if position is not None:
            payload["position"] = position
        data = await self._household_request("POST", "/shopping/items", json=payload)
        return _created_item(data, list_id, text)

    async def async_update_item(self, raw_item: dict[str, Any], *, text: str | None = None, checked: bool | None = None) -> dict[str, Any]:
        """Update an item by sending back the full object Mealie gave us (keeps food/unit links)."""
        payload = {
            key: value
            for key, value in raw_item.items()
            if key not in ("createdAt", "updateAt", "updatedAt", "recipeReferences", "label", "food", "unit")
        }
        if checked is not None:
            payload["checked"] = checked
        if text is not None and text != item_text(raw_item):
            # Free-text edit: detach from the structured food so the note is what shows.
            payload.update({"note": text, "display": text, "isFood": False, "foodId": None, "unitId": None, "quantity": 0})
        data = await self._household_request("PUT", f"/shopping/items/{_seg(raw_item['id'])}", json=payload)
        updated = _created_item(data, raw_item.get("shoppingListId", ""), text or item_text(raw_item))
        return updated if updated.get("id") else {**raw_item, **payload}

    async def async_delete_item(self, item_id: str) -> None:
        try:
            await self._household_request("DELETE", f"/shopping/items/{_seg(item_id)}")
        except MealieError as err:
            # Already gone in Mealie is fine; anything else is a real failure.
            if not isinstance(err.__cause__, _NotFound):
                raise


def _length(response: Any) -> int | None:
    """The declared body size, if the server sent one."""
    value = response.headers.get("Content-Length")
    return int(value) if value and value.isdigit() else None


class _NotFound(MealieError):
    """Internal 404 marker used for route detection."""


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        items = data.get("items")
        return list(items) if isinstance(items, list) else []
    if isinstance(data, list):
        return data
    return []


def _created_item(data: Any, list_id: str, text: str) -> dict[str, Any]:
    """POST/PUT responses differ across Mealie versions; dig the item out."""
    if isinstance(data, dict):
        if data.get("id"):
            return data
        for key in ("createdItems", "updatedItems"):
            items = data.get(key)
            if isinstance(items, list) and items:
                for item in items:
                    if item_text(item) == text:
                        return item
                return items[0]
    return {}


def item_text(item: dict[str, Any]) -> str:
    """Human text of a Mealie shopping item."""
    display = str(item.get("display") or "").strip()
    if display:
        return display
    food = item.get("food") if isinstance(item.get("food"), dict) else None
    unit = item.get("unit") if isinstance(item.get("unit"), dict) else None
    quantity = item.get("quantity") or 0
    parts = []
    if quantity:
        parts.append(f"{float(quantity):g}")
    if unit:
        parts.append(str(unit.get("abbreviation") or unit.get("name") or ""))
    if food:
        parts.append(str(food.get("name") or ""))
    note = str(item.get("note") or "").strip()
    if note:
        parts.append(note if not food else f"({note})")
    return " ".join(part for part in parts if part).strip()


def item_hash(text: str, checked: bool) -> str:
    """Change-detection fingerprint of an item's user-visible state."""
    return hashlib.sha1(f"{text.strip().lower()}|{int(bool(checked))}".encode()).hexdigest()
