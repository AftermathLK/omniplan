"""OmniPlan: local-first household planner for Home Assistant.

Runs entirely inside Home Assistant Core: data lives in
``<config>/.storage/omniplan.db`` (SQLite via aiosqlite), plans are exposed as
native ``todo.*`` entities, and the bundled ``omniplan-card`` Lovelace card /
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
from .coordinator import OmniPlanCoordinator
from .http_views import async_register_views
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.TODO]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
FRONTEND_REGISTERED = f"{DOMAIN}_frontend_registered"

type OmniPlanConfigEntry = ConfigEntry[OmniPlanCoordinator]


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
        _LOGGER.info("Moved OmniPlan's Mealie connection settings into protected entry data")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OmniPlanConfigEntry) -> bool:
    coordinator = OmniPlanCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except Exception as err:
        await coordinator.async_shutdown()
        raise ConfigEntryNotReady(f"OmniPlan could not open its database: {err}") from err
    entry.runtime_data = coordinator
    hass.data[DOMAIN] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if coordinator.options.get(CONF_SHOW_PANEL, True):
        await _async_register_panel(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OmniPlanConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
        hass.data.pop(DOMAIN, None)
        if PANEL_URL_PATH in hass.data.get(frontend.DATA_PANELS, {}):
            frontend.async_remove_panel(hass, PANEL_URL_PATH)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: OmniPlanConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the card bundle and load it on every dashboard."""
    if hass.data.get(FRONTEND_REGISTERED) or hass.http is None:
        return
    directory = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths([StaticPathConfig(FRONTEND_URL_BASE, str(directory), False)])
    frontend.add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}")
    hass.data[FRONTEND_REGISTERED] = True


async def _async_register_panel(hass: HomeAssistant) -> None:
    if PANEL_URL_PATH in hass.data.get(frontend.DATA_PANELS, {}):
        return
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name=PANEL_ELEMENT,
        frontend_url_path=PANEL_URL_PATH,
        module_url=f"{CARD_URL}?v={VERSION}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=False,
        config={"view_mode": "quick"},
    )
