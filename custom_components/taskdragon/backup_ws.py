"""WebSocket commands for portable backups (``taskdragon/backup_*``), administrators only.

Downloads and uploads go through the authenticated HTTP views in
:mod:`.http_views`; these commands list, create, delete and restore the
rolling snapshots kept in ``<config>/backups/taskdragon/`` and edit the schedule.
"""

from __future__ import annotations

import voluptuous as vol

from .const import BACKUP_DIRNAME
from .websocket_api import _command, _text

NAME = vol.All(str, vol.Length(min=1, max=120))
MODE = vol.In(("replace", "merge"))


@_command({vol.Required("type"): "taskdragon/backup_list"}, admin=True)
async def ws_backup_list(hass, connection, msg, coordinator, actor):
    backups = coordinator.backups
    return {
        "snapshots": await backups.list_snapshots(),
        "settings": await backups.settings(),
        "directory": f"backups/{BACKUP_DIRNAME}",
        "restoring": backups.restoring,
    }


@_command({vol.Required("type"): "taskdragon/backup_create"}, admin=True)
async def ws_backup_create(hass, connection, msg, coordinator, actor):
    return await coordinator.backups.create_snapshot("snapshot")


@_command({vol.Required("type"): "taskdragon/backup_restore_snapshot", vol.Required("name"): NAME, vol.Required("mode"): MODE}, admin=True)
async def ws_backup_restore_snapshot(hass, connection, msg, coordinator, actor):
    return await coordinator.backups.restore_snapshot(msg["name"], msg["mode"])


@_command({vol.Required("type"): "taskdragon/backup_delete", vol.Required("name"): NAME}, admin=True)
async def ws_backup_delete(hass, connection, msg, coordinator, actor):
    await coordinator.backups.delete_snapshot(msg["name"])
    return {"snapshots": await coordinator.backups.list_snapshots()}


@_command(
    {
        vol.Required("type"): "taskdragon/backup_settings",
        vol.Optional("settings"): vol.Schema(
            {
                vol.Optional("enabled"): bool,
                vol.Optional("retention"): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                vol.Optional("time"): _text(5),
            }
        ),
    },
    admin=True,
)
async def ws_backup_settings(hass, connection, msg, coordinator, actor):
    if "settings" in msg:
        return await coordinator.backups.save_settings(msg["settings"])
    return await coordinator.backups.settings()


COMMANDS = (ws_backup_list, ws_backup_create, ws_backup_restore_snapshot, ws_backup_delete, ws_backup_settings)
__all__ = ["COMMANDS"]
