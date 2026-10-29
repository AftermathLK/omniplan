"""Diagnostics for OmniPlan (Settings → Devices & services → OmniPlan → Download diagnostics).

The download is meant to be attached to bug reports, so it carries settings and
counts only: the Mealie token and URL, user ids, calendars and every other
identifying value are replaced with ``**REDACTED**``, and no task, pantry or
private plan text is included.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONVERSATION_AGENT,
    CONF_MEALIE_TOKEN,
    CONF_MEALIE_URL,
    CONF_WEATHER_ENTITY,
    CONF_WORK_CALENDAR,
    LEGACY_CONF_MEALIE_TOKEN,
    VERSION,
)

TO_REDACT = {
    CONF_MEALIE_TOKEN,
    LEGACY_CONF_MEALIE_TOKEN,
    CONF_MEALIE_URL,
    CONF_WORK_CALENDAR,
    CONF_WEATHER_ENTITY,
    CONF_CONVERSATION_AGENT,
    "token",
    "api_key",
    "access_token",
    "webhook_id",
    "url",
    "internal_url",
    "external_url",
    "link_url",
    "user_id",
    "owner_user_id",
    "shared_with",
    "entity_id",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    plans = await coordinator.db.list_plans(include_archived=True)
    tasks = await coordinator.db.list_tasks()
    per_plan: dict[str, int] = {}
    for task in tasks:
        per_plan[task["plan_id"]] = per_plan.get(task["plan_id"], 0) + 1
    return {
        "version": VERSION,
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "database": {
            "schema_version": await coordinator.db.schema_version(),
            "counts": await coordinator.db.counts(),
        },
        "plans": [
            {
                "id": plan["id"],
                # Names of private lists can be personal; household list names help debugging.
                "name": "**REDACTED**" if plan["is_private"] else plan["name"],
                "archetype": plan["archetype"],
                "is_private": plan["is_private"],
                "archived": plan["archived"],
                "role": (plan.get("settings") or {}).get("role"),
                "mealie_linked": bool((plan.get("settings") or {}).get("mealie_list_id")),
                "tasks": per_plan.get(plan["id"], 0),
            }
            for plan in plans
        ],
        "mealie": async_redact_data(dict(coordinator.mealie_status), TO_REDACT),
        "household_size": len(coordinator.household()),
    }
