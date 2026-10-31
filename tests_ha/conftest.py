"""Fixtures for integration tests against a real Home Assistant core.

Run with the ``pytest-homeassistant-custom-component`` plugin installed::

    pytest tests_ha
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest_plugins = "pytest_homeassistant_custom_component"

if sys.platform == "win32":
    # Windows' proactor event loop needs a TCP socketpair for its self-pipe, which the
    # socket guard installed by the HA test plugin rejects. (CI on Linux keeps the guard.)
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None
    pytest_socket.socket_allow_hosts = lambda *args, **kwargs: None

    # aiodns (aiohttp's default resolver when installed) refuses the proactor loop.
    import aiohttp.connector
    import aiohttp.resolver

    aiohttp.resolver.DefaultResolver = aiohttp.resolver.ThreadedResolver
    aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/taskdragon."""
    yield


@pytest.fixture(autouse=True)
def isolated_config_dir(hass, tmp_path):
    """Keep taskdragon.db out of the shared test config directory."""
    hass.config.config_dir = str(tmp_path)
    yield


@pytest.fixture(autouse=True)
def stub_frontend(hass):
    """The frontend/panel/conversation components need packages tests don't ship."""
    hass.config.components.update({"frontend", "panel_custom", "conversation"})
    with (
        patch("custom_components.taskdragon._async_register_frontend", AsyncMock()),
        patch("custom_components.taskdragon._async_register_panel", AsyncMock()),
        # Never bind a real port; the WebSocket test client serves hass.http.app itself.
        patch("homeassistant.components.http.HomeAssistantHTTP.start", AsyncMock()),
    ):
        yield
