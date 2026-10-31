"""TaskDragon: local-first household planner for Home Assistant.

Runs entirely inside Home Assistant Core: data lives in
``<config>/.storage/taskdragon.db`` (SQLite via aiosqlite), plans are exposed as
native ``todo.*`` entities, and the bundled ``taskdragon-card`` Lovelace card /
sidebar panel talks to the custom WebSocket API.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from . import websocket_api
from .config_flow import split_legacy_mealie
from .const import (
    CARD_URL,
    CONF_SHOW_PANEL,
    DOMAIN,
    FRONTEND_URL_BASE,
    PANEL_ELEMENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
    VERSION,
)
from .coordinator import TaskDragonCoordinator
from .migration import LEGACY_DOMAIN, LEGACY_PANEL_URL_PATH, MIGRATED_META, migrate_legacy_storage
from .http_views import async_register_views
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.TODO]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
FRONTEND_REGISTERED = f"{DOMAIN}_frontend_registered"

type TaskDragonConfigEntry = ConfigEntry[TaskDragonCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the WebSocket API, services and frontend module once."""
    websocket_api.async_register(hass)
    async_register_services(hass)
    async_register_views(hass)
    await _async_register_frontend(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1.1 → 1.2: the Mealie URL and token move from options into entry data."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        data, options = split_legacy_mealie(dict(entry.data), dict(entry.options))
        hass.config_entries.async_update_entry(entry, data=data, options=options, minor_version=2)
        _LOGGER.info("Moved TaskDragon's Mealie connection settings into protected entry data")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TaskDragonConfigEntry) -> bool:
    try:
        await hass.async_add_executor_job(migrate_legacy_storage, hass.config.config_dir)
    except Exception as err:  # noqa: BLE001 - a failed copy must never block setup; start fresh instead
        _LOGGER.error("Could not copy the previous installation's data into TaskDragon: %s", err)
    coordinator = TaskDragonCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except Exception as err:
        await coordinator.async_shutdown()
        raise ConfigEntryNotReady(f"TaskDragon could not open its database: {err}") from err
    entry.runtime_data = coordinator
    hass.data[DOMAIN] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if coordinator.options.get(CONF_SHOW_PANEL, True):
        await _async_register_panel(hass)
    if await coordinator.db.get_meta(MIGRATED_META) and LEGACY_DOMAIN not in hass.config.components:
        await _async_register_panel(hass, LEGACY_PANEL_URL_PATH)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TaskDragonConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
        hass.data.pop(DOMAIN, None)
        for url_path in (PANEL_URL_PATH, LEGACY_PANEL_URL_PATH):
            if url_path in hass.data.get(frontend.DATA_PANELS, {}) and (url_path == PANEL_URL_PATH or LEGACY_DOMAIN not in hass.config.components):
                frontend.async_remove_panel(hass, url_path)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: TaskDragonConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the card bundle and load it on every dashboard."""
    if hass.data.get(FRONTEND_REGISTERED) or hass.http is None:
        return
    directory = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths([StaticPathConfig(FRONTEND_URL_BASE, str(directory), False)])
    frontend.add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}")
    hass.data[FRONTEND_REGISTERED] = True


async def _async_register_panel(hass: HomeAssistant, url_path: str = PANEL_URL_PATH) -> None:
    """The sidebar panel; ``url_path`` = the pre-rename path registers a hidden copy so old QR labels still open."""
    if url_path in hass.data.get(frontend.DATA_PANELS, {}):
        return
    hidden = url_path != PANEL_URL_PATH
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name=PANEL_ELEMENT,
        frontend_url_path=url_path,
        module_url=f"{CARD_URL}?v={VERSION}",
        sidebar_title=None if hidden else PANEL_TITLE,
        sidebar_icon=None if hidden else PANEL_ICON,
        require_admin=False,
        config={"view_mode": "quick"},
    )
