"""Portable backup, restore and standalone-migration bridge (no Home Assistant imports).

A backup is one ``taskdragon_portable_backup_YYYYMMDD_HHMMSS.zip`` holding:

``manifest_bridge.json``
    Format version, export time, SHA-256 of every other file, plus two bridge
    tables a future standalone TaskDragon can use to stand up without Home
    Assistant: ``users_bridge_map`` (each Home Assistant user / ``person.*``
    entity as a platform-independent profile with a stable UUID) and
    ``hardware_bindings_bridge`` (every entity, NFC tag, area, notify service
    and other Home Assistant reference the data points at, each with a stable
    binding id, a suggested MQTT topic and a webhook id for re-binding).
``database_dump.json``
    Every SQLite table as plain JSON rows (the canonical, version-independent copy).
``taskdragon.db``
    A hot, consistent copy of the database from SQLite's online backup API.
``documents/<sha256>``
    Every uploaded manual, receipt and photo still referenced by the data.
``secrets/<name>``
    Small keys the data depends on (the wishlist's hiding-spot encryption key),
    so a restore on another installation can still read what they protect.

Restores accept such a zip (or a bare ``database_dump.json``), verify every
checksum and the schema before touching anything, write a safety snapshot of
the current state, and then either replace everything or merge in only what
is missing. Zip members are read by exact name and size-capped; nothing is
ever extracted to a path taken from the archive.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import uuid
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import date, datetime, timezone
from typing import Any

from .const import BACKUP_DEFAULT_RETENTION, BACKUP_DEFAULT_TIME
from .database import PORTABLE_MIN_SCHEMA
from .migration import LEGACY_DUMP_FORMAT

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0.0-portable"
SOURCE_PLATFORM = "home_assistant_custom_component"
DUMP_FORMAT = "taskdragon-database-dump"
MANIFEST = "manifest_bridge.json"
DUMP = "database_dump.json"
DB_COPY = "taskdragon.db"
PREFIX = "taskdragon_portable_backup_"
SETTINGS_META = "backup_settings"
BINDINGS_META = "bridge_bindings"
KIND_SUFFIX = {"pre_restore": "_pre-restore"}
# Snapshots written before the rename (omniplan_portable_backup_*.zip) still list and restore.
NAME_RE = re.compile(r"^(?:taskdragon|omniplan)_portable_backup_(\d{8})_(\d{6})(?:_(\d{1,3}))?(_pre-restore)?\.zip$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# A fixed namespace so the same Home Assistant user always gets the same portable UUID.
USER_NAMESPACE = uuid.UUID("6f0b8e5e-6c1f-4d8e-9a57-3c1b0f9a4d21")
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_DUMP_BYTES = 1024 * 1024 * 1024
MAX_DB_BYTES = 2 * 1024 * 1024 * 1024
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_RETENTION = 365
CHUNK = 1024 * 1024
STORED_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif", "image/heic")
PERSON_FIELDS = (
    ("tasks", "assigned_to"),
    ("completion_logs", "person_entity_id"),
    ("snooze_rules", "person_entity_id"),
    ("maint_tasks", "assigned_to"),
    ("maint_tasks", "rotation"),
    ("maint_events", "person_entity_id"),
    ("hoard_sections", "gift_for_person_id"),
    ("hoard_items", "gift_for_person_id"),
)
SECRET_NAME_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
MAX_SECRET_BYTES = 4096


class BackupError(Exception):
    """A backup could not be written, or an upload is not a valid TaskDragon backup."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _json_cell(value: Any, default: Any) -> Any:
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def _initials(name: str) -> str:
    words = [word for word in re.split(r"[\s._-]+", name or "") if word]
    if not words:
        return "?"
    return (words[0][0] + (words[1][0] if len(words) > 1 else words[0][1:2])).upper()


def snapshot_name(moment: datetime, kind: str, taken: Iterable[str] = ()) -> str:
    stem = PREFIX + moment.strftime("%Y%m%d_%H%M%S")
    suffix = KIND_SUFFIX.get(kind, "") + ".zip"
    name = stem + suffix
    existing = set(taken)
    counter = 2
    while name in existing:
        name = f"{stem}_{counter}{suffix}"
        counter += 1
    return name


def parse_name(name: str) -> dict[str, Any] | None:
    match = NAME_RE.match(name or "")
    if not match:
        return None
    try:
        moment = datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return {"taken_at": moment.isoformat(), "kind": "pre_restore" if match[4] else "snapshot"}


class _Limited:
    """Read at most ``limit`` bytes from a zip member, whatever its header claims."""

    def __init__(self, handle: Any, limit: int, label: str) -> None:
        self._handle = handle
        self._left = limit
        self._label = label

    def read_all(self) -> bytes:
        parts = []
        while chunk := self._handle.read(CHUNK):
            self._left -= len(chunk)
            if self._left < 0:
                raise BackupError(f"{self._label} is larger than allowed")
            parts.append(chunk)
        return b"".join(parts)

    def copy_to(self, path: str) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with open(path, "wb") as out:
            while chunk := self._handle.read(CHUNK):
                size += len(chunk)
                if size > self._left:
                    raise BackupError(f"{self._label} is larger than allowed")
                digest.update(chunk)
                out.write(chunk)
        return digest.hexdigest(), size


class BackupManager:
    """Exports, snapshots and restores; the coordinator schedules and exposes it."""

    def __init__(
        self,
        *,
        db: Any,
        docs_dir: str,
        backup_dir: str,
        app_version: str,
        users: Callable[[], Awaitable[list[dict[str, Any]]]],
        people: Callable[[], list[dict[str, Any]]],
        options: Callable[[], Mapping[str, Any]],
        local_now: Callable[[], datetime],
        run_blocking: Callable[..., Awaitable[Any]] | None = None,
        before_restore: Callable[[], Awaitable[None]] | None = None,
        on_restored: Callable[[], Awaitable[list[str] | None]] | None = None,
        secrets_provider: Callable[[], Awaitable[Mapping[str, bytes]]] | None = None,
        restore_secrets: Callable[[Mapping[str, bytes]], Awaitable[None]] | None = None,
    ) -> None:
        self.db = db
        self.docs_dir = docs_dir
        self.backup_dir = backup_dir
        self.app_version = app_version
        self._users = users
        self._people = people
        self._options = options
        self._local_now = local_now
        self._run = run_blocking or asyncio.to_thread
        self._before_restore = before_restore
        self._on_restored = on_restored
        self._secrets_provider = secrets_provider
        self._restore_secrets = restore_secrets
        self._lock = asyncio.Lock()
        # True while a restore runs; background writers (sensor counters) hold off meanwhile.
        self.restoring = False
        self._last_auto: date | None = None

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    async def settings(self) -> dict[str, Any]:
        stored = await self.db.get_meta(SETTINGS_META, {}) or {}
        retention = stored.get("retention")
        when = stored.get("time")
        return {
            "enabled": bool(stored.get("enabled", True)),
            "retention": int(retention) if isinstance(retention, int) and 1 <= retention <= MAX_RETENTION else BACKUP_DEFAULT_RETENTION,
            "time": when if isinstance(when, str) and TIME_RE.match(when) else BACKUP_DEFAULT_TIME,
        }

    async def save_settings(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        current = await self.settings()
        if "enabled" in changes:
            current["enabled"] = bool(changes["enabled"])
        if "retention" in changes:
            retention = int(changes["retention"])
            if not 1 <= retention <= MAX_RETENTION:
                raise BackupError(f"Keep between 1 and {MAX_RETENTION} snapshots")
            current["retention"] = retention
        if "time" in changes:
            if not TIME_RE.match(str(changes["time"])):
                raise BackupError("Pick a time like 03:15")
            current["time"] = str(changes["time"])
        await self.db.set_meta(SETTINGS_META, current)
        await self._run(self._prune, current["retention"])
        return current

    # ------------------------------------------------------------------
    # Snapshots on disk
    # ------------------------------------------------------------------
    def _list(self) -> list[dict[str, Any]]:
        if not os.path.isdir(self.backup_dir):
            return []
        out = []
        for name in os.listdir(self.backup_dir):
            info = parse_name(name)
            path = os.path.join(self.backup_dir, name)
            if info is None or not os.path.isfile(path):
                continue
            stat = os.stat(path)
            out.append({"name": name, "size": stat.st_size, "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"), **info})
        out.sort(key=lambda item: (item["taken_at"], item["name"]), reverse=True)
        return out

    async def list_snapshots(self) -> list[dict[str, Any]]:
        return await self._run(self._list)

    def _prune(self, retention: int) -> list[str]:
        removed = []
        for item in self._list()[retention:]:
            try:
                os.remove(os.path.join(self.backup_dir, item["name"]))
                removed.append(item["name"])
            except OSError as err:
                _LOGGER.warning("Could not remove old TaskDragon backup %s: %s", item["name"], err)
        return removed

    def snapshot_path(self, name: str) -> str:
        """The on-disk path of a snapshot, refusing anything that is not one of ours."""
        if parse_name(name) is None:
            raise BackupError("Unknown backup file")
        path = os.path.join(self.backup_dir, name)
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(self.backup_dir) or not os.path.isfile(path):
            raise BackupError("That backup file does not exist")
        return path

    async def delete_snapshot(self, name: str) -> None:
        path = await self._run(self.snapshot_path, name)
        await self._run(os.remove, path)

    async def create_snapshot(self, kind: str = "snapshot") -> dict[str, Any]:
        """Write a snapshot into the backup folder and prune old ones."""
        async with self._lock:
            return await self._create_snapshot(kind)

    async def _create_snapshot(self, kind: str) -> dict[str, Any]:
        await self._run(_makedirs, self.backup_dir)
        taken = {item["name"] for item in await self.list_snapshots()}
        name = snapshot_name(self._local_now(), kind, taken)
        path = os.path.join(self.backup_dir, name)
        manifest = await self._export(path, kind)
        settings = await self.settings()
        await self._run(self._prune, settings["retention"])
        size = (await self._run(os.stat, path)).st_size
        return {
            "name": name,
            "size": size,
            "kind": kind,
            "exported_at": manifest["exported_at"],
            "documents": len(manifest["documents"]),
            "missing_documents": len(manifest["missing_documents"]),
            "rows": sum(manifest["counts"].values()),
        }

    async def export_download(self) -> tuple[str, str]:
        """A fresh backup in a temporary file for streaming; the caller deletes it. Returns (path, download name)."""
        await self._run(_makedirs, self.backup_dir)
        handle, path = await self._run(tempfile.mkstemp, ".zip", ".download-", self.backup_dir)
        await self._run(os.close, handle)
        try:
            async with self._lock:
                await self._export(path, "download")
        except BaseException:
            await self._run(_remove_quietly, path)
            raise
        return path, snapshot_name(self._local_now(), "download")

    async def daily_tick(self) -> dict[str, Any] | None:
        """Called every minute: take the scheduled snapshot at the configured time, once a day."""
        settings = await self.settings()
        now = self._local_now()
        if not settings["enabled"] or now.strftime("%H:%M") != settings["time"] or self._last_auto == now.date():
            return None
        self._last_auto = now.date()
        try:
            return await self.create_snapshot("snapshot")
        except (BackupError, OSError, sqlite3.Error) as err:
            _LOGGER.error("The scheduled TaskDragon backup failed: %s", err)
            return None

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    async def _export(self, path: str, kind: str) -> dict[str, Any]:
        copy_path = path + ".db.tmp"
        try:
            dump = await self.db.export_portable(copy_path)
            exported_at = _now_utc().isoformat(timespec="seconds").replace("+00:00", "Z")
            dump_doc = {
                "format": DUMP_FORMAT,
                "schema_version": SCHEMA_VERSION,
                "db_schema_version": dump["db_schema_version"],
                "exported_at": exported_at,
                "app_version": self.app_version,
                "tables": dump["tables"],
            }
            documents = self._documents(dump["tables"])
            manifest = {
                "format": "taskdragon-portable-backup",
                "schema_version": SCHEMA_VERSION,
                "exported_at": exported_at,
                "source_platform": SOURCE_PLATFORM,
                "app_version": self.app_version,
                "db_schema_version": dump["db_schema_version"],
                "kind": kind,
                "counts": {name: table["row_count"] for name, table in dump["tables"].items()},
                "users_bridge_map": await self._users_bridge(dump["tables"]),
                "hardware_bindings_bridge": await self._bindings_bridge(dump["tables"]),
            }
            keys = dict(await self._secrets_provider()) if self._secrets_provider is not None else {}
            await self._run(self._write_zip, path, manifest, dump_doc, copy_path, documents, keys)
            return manifest
        except (OSError, sqlite3.Error) as err:
            await self._run(_remove_quietly, path)
            raise BackupError(f"The backup could not be written: {err}") from err
        finally:
            await self._run(_remove_quietly, copy_path)

    @staticmethod
    def _documents(tables: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        docs: dict[str, dict[str, Any]] = {}
        for row in (tables.get("maint_documents") or {}).get("rows", []):
            sha = row.get("sha256")
            if row.get("kind") != "file" or not isinstance(sha, str) or not SHA_RE.match(sha):
                continue
            entry = docs.setdefault(sha, {"filenames": [], "content_types": []})
            for key, field in (("filenames", "filename"), ("content_types", "content_type")):
                if row.get(field) and row[field] not in entry[key]:
                    entry[key].append(str(row[field])[:200])
        return docs

    def _write_zip(
        self,
        path: str,
        manifest: dict[str, Any],
        dump_doc: dict[str, Any],
        copy_path: str,
        documents: Mapping[str, Mapping[str, Any]],
        keys: Mapping[str, bytes] | None = None,
    ) -> None:
        temp = path + ".partial"
        dump_bytes = json.dumps(dump_doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        db_sha, db_size = _sha256_file(copy_path)
        manifest["files"] = {
            DUMP: {"sha256": hashlib.sha256(dump_bytes).hexdigest(), "size": len(dump_bytes)},
            DB_COPY: {"sha256": db_sha, "size": db_size},
        }
        manifest["documents"] = []
        manifest["missing_documents"] = []
        try:
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
                archive.writestr(DUMP, dump_bytes)
                archive.write(copy_path, DB_COPY)
                for sha, info in sorted(documents.items()):
                    source = os.path.join(self.docs_dir, sha)
                    if not os.path.isfile(source):
                        manifest["missing_documents"].append(sha)
                        continue
                    actual, size = _sha256_file(source)
                    if actual != sha:
                        _LOGGER.warning("Skipping document %s from the backup: its contents no longer match its checksum", sha)
                        manifest["missing_documents"].append(sha)
                        continue
                    stored = all(t in STORED_TYPES for t in info["content_types"]) and bool(info["content_types"])
                    archive.write(source, "documents/" + sha, compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
                    manifest["documents"].append({"path": "documents/" + sha, "sha256": sha, "size": size, **info})
                for name, data in sorted((keys or {}).items()):
                    if not SECRET_NAME_RE.match(name) or len(data) > MAX_SECRET_BYTES:
                        continue
                    member = "secrets/" + name
                    archive.writestr(member, data)
                    manifest["files"][member] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            os.replace(temp, path)
        finally:
            _remove_quietly(temp)

    async def _users_bridge(self, tables: Mapping[str, Any]) -> list[dict[str, Any]]:
        def rows(table: str) -> list[dict[str, Any]]:
            return list((tables.get(table) or {}).get("rows", []))

        people = self._people()
        by_user = {p["user_id"]: p for p in people if p.get("user_id")}
        names: dict[str, str] = {}
        referenced: set[str] = set()
        for table in ("completion_logs", "maint_events"):
            for row in rows(table):
                if row.get("user_id"):
                    referenced.add(row["user_id"])
                    if row.get("user_name"):
                        names[row["user_id"]] = str(row["user_name"])
        for row in rows("plans"):
            if row.get("owner_user_id"):
                referenced.add(row["owner_user_id"])
            referenced.update(str(u) for u in _json_cell(row.get("shared_with"), []) or [] if u)
        referenced.update(row["user_id"] for row in rows("user_prefs") if row.get("user_id"))
        for table in ("hoard_sections", "hoard_items"):
            for row in rows(table):
                if row.get("created_by"):
                    referenced.add(row["created_by"])
                    if row.get("created_by_name"):
                        names.setdefault(row["created_by"], str(row["created_by_name"]))

        profiles: list[dict[str, Any]] = []
        seen_people: set[str] = set()
        users = {u["id"]: u for u in await self._users() if not u.get("system_generated")}
        for user_id in sorted(set(users) | referenced):
            user = users.get(user_id)
            person = by_user.get(user_id)
            name = (person or {}).get("name") or (user or {}).get("name") or names.get(user_id) or "Former user"
            if person:
                seen_people.add(person["entity_id"])
            profiles.append(
                {
                    "portable_user_uuid": str(uuid.uuid5(USER_NAMESPACE, "ha-user:" + user_id)),
                    "display_name": name,
                    "short_initials": _initials(name),
                    "role": "admin" if (user or {}).get("is_admin") else "member",
                    "work_calendar_ref": (person or {}).get("calendar"),
                    "ha_user_id": user_id,
                    "person_entity_id": (person or {}).get("entity_id"),
                    "status": "active" if user and user.get("is_active", True) else "inactive" if user else "removed",
                }
            )
        person_refs = {p["entity_id"]: p for p in people}
        for table, field in PERSON_FIELDS:
            for row in rows(table):
                value = _json_cell(row.get(field), row.get(field))
                for entity in value if isinstance(value, list) else [value]:
                    if isinstance(entity, str) and entity.startswith("person.") and ENTITY_RE.match(entity):
                        person_refs.setdefault(entity, {"entity_id": entity, "name": entity.split(".", 1)[1].replace("_", " ").title()})
        for entity_id, person in sorted(person_refs.items()):
            if entity_id in seen_people:
                continue
            name = person.get("name") or entity_id
            profiles.append(
                {
                    "portable_user_uuid": str(uuid.uuid5(USER_NAMESPACE, "ha-person:" + entity_id)),
                    "display_name": name,
                    "short_initials": _initials(name),
                    "role": "member",
                    "work_calendar_ref": person.get("calendar"),
                    "ha_user_id": None,
                    "person_entity_id": entity_id,
                    "status": "person_only",
                }
            )
        return profiles

    async def _bindings_bridge(self, tables: Mapping[str, Any]) -> list[dict[str, Any]]:
        found: dict[tuple[str, str], list[dict[str, Any]]] = {}

        def add(kind: str, ref: Any, table: str, row_id: Any, field: str) -> None:
            if isinstance(ref, str) and ref.strip():
                found.setdefault((kind, ref.strip()[:255]), []).append({"table": table, "id": row_id, "field": field})

        def rows(table: str) -> list[dict[str, Any]]:
            return list((tables.get(table) or {}).get("rows", []))

        for row in rows("tasks"):
            add("sensor", row.get("sensor_entity_id"), "tasks", row.get("uid"), "sensor_entity_id")
        for row in rows("snooze_rules"):
            config = _json_cell(row.get("config"), {})
            if isinstance(config, Mapping):
                add("entity_state", config.get("entity_id"), "snooze_rules", row.get("id"), "config.entity_id")
                add("weather", config.get("weather_entity"), "snooze_rules", row.get("id"), "config.weather_entity")
        for row in rows("maint_tasks"):
            for trigger in _json_cell(row.get("triggers"), []) or []:
                if isinstance(trigger, Mapping):
                    add("sensor", trigger.get("entity_id"), "maint_tasks", row.get("id"), f"triggers[{trigger.get('id')}].entity_id")
            add("nfc_tag", row.get("nfc_tag_id"), "maint_tasks", row.get("id"), "nfc_tag_id")
            action = _json_cell(row.get("on_complete_action"), None)
            if isinstance(action, Mapping):
                add("service", action.get("service"), "maint_tasks", row.get("id"), "on_complete_action.service")
                targets = action.get("entity_id")
                for entity in targets if isinstance(targets, list) else [targets]:
                    add("service_target", entity, "maint_tasks", row.get("id"), "on_complete_action.entity_id")
        for row in rows("maint_objects"):
            add("zone", row.get("zone_entity_id"), "maint_objects", row.get("id"), "zone_entity_id")
            add("area", row.get("area_id"), "maint_objects", row.get("id"), "area_id")
            config = _json_cell(row.get("config"), {})
            if isinstance(config, Mapping):
                for key, value in config.items():
                    if str(key).endswith("_entity"):
                        add("sensor", value, "maint_objects", row.get("id"), f"config.{key}")
        for row in rows("plans"):
            settings = _json_cell(row.get("settings"), {})
            if isinstance(settings, Mapping):
                add("notify_service", settings.get("notify_service"), "plans", row.get("id"), "settings.notify_service")
                add("mealie_shopping_list", settings.get("mealie_list_id"), "plans", row.get("id"), "settings.mealie_list_id")
        options = self._options()
        add("weather", options.get("weather_entity"), "options", None, "weather_entity")
        add("notify_service", options.get("maintenance_notify_service"), "options", None, "maintenance_notify_service")
        add("conversation_agent", options.get("conversation_agent"), "options", None, "conversation_agent")
        for person in self._people():
            add("calendar", person.get("calendar"), "options", person.get("entity_id"), "people.calendar")

        stored = await self.db.get_meta(BINDINGS_META, {}) or {}
        ids = dict(stored) if isinstance(stored, Mapping) else {}
        changed = False
        bridge = []
        for (kind, ref), used_by in sorted(found.items()):
            key = kind + "|" + ref
            entry = ids.get(key)
            if not isinstance(entry, Mapping) or not entry.get("binding_id") or not entry.get("webhook_id"):
                entry = {"binding_id": "bnd_" + secrets.token_hex(6), "webhook_id": "taskdragon_" + secrets.token_urlsafe(24)}
                ids[key] = entry
                changed = True
            is_entity = bool(ENTITY_RE.match(ref)) and kind not in ("nfc_tag", "area", "service", "notify_service", "mealie_shopping_list", "conversation_agent")
            bridge.append(
                {
                    "binding_id": entry["binding_id"],
                    "binding_type": kind,
                    "ha_entity_id": ref if is_entity else None,
                    "ha_ref": None if is_entity else ref,
                    "domain": ref.split(".", 1)[0] if is_entity else None,
                    "mqtt_topic_fallback": f"taskdragon/bindings/{entry['binding_id']}/state",
                    "webhook_id": entry["webhook_id"],
                    "used_by": used_by[:200],
                }
            )
        if changed:
            await self.db.set_meta(BINDINGS_META, ids)
        return bridge

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------
    async def restore_snapshot(self, name: str, mode: str) -> dict[str, Any]:
        path = await self._run(self.snapshot_path, name)
        report = await self.restore_file(path, mode)
        report["source"] = name
        return report

    async def restore_file(self, path: str, mode: str) -> dict[str, Any]:
        """Validate a backup file completely, take a safety snapshot, then restore it."""
        if mode not in ("replace", "merge"):
            raise BackupError("Pick Full replace or Merge")
        async with self._lock:
            current = await self.db.schema_version()
            parsed = await self._run(self._read_backup, path, current)
            safety = await self._create_snapshot("pre_restore")
            self.restoring = True
            try:
                if self._before_restore is not None:
                    await self._before_restore()
                # Files first: a file without a row is cleaned up later, a row without its file is not.
                docs = await self._run(self._restore_documents, path, parsed["documents"]) if parsed["documents"] else {"restored": 0, "present": 0}
                try:
                    db_report = await self.db.restore_portable(parsed["tables"], mode=mode)
                except ValueError as err:
                    raise BackupError(str(err)) from err
                if parsed.get("secrets") and self._restore_secrets is not None:
                    await self._restore_secrets(parsed["secrets"])
                after: list[str] = []
                if self._on_restored is not None:
                    after = list(await self._on_restored() or [])
            finally:
                self.restoring = False
        totals = {key: sum(t[key] for t in db_report["tables"].values()) for key in ("inserted", "skipped", "deleted", "removed")}
        return {
            "mode": mode,
            "format": parsed["format"],
            "exported_at": parsed["manifest"].get("exported_at"),
            "app_version": parsed["manifest"].get("app_version"),
            "tables": db_report["tables"],
            "totals": totals,
            "documents": {**docs, "missing": len(parsed["manifest"].get("missing_documents") or [])},
            "warnings": parsed["warnings"] + db_report["warnings"] + after,
            "safety_snapshot": safety["name"],
        }

    def _read_backup(self, path: str, current_schema: int) -> dict[str, Any]:
        with open(path, "rb") as handle:
            head = handle.read(4)
        if head.startswith(b"PK\x03\x04"):
            return self._read_zip(path, current_schema)
        if os.path.getsize(path) > MAX_DUMP_BYTES:
            raise BackupError("That file is too large to be an TaskDragon backup")
        try:
            with open(path, "rb") as handle:
                payload = json.loads(handle.read().decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as err:
            raise BackupError("That file is neither an TaskDragon backup .zip nor its database_dump.json") from err
        tables = self._check_dump(payload, current_schema)
        return {"format": "json", "manifest": {"exported_at": payload.get("exported_at"), "app_version": payload.get("app_version")}, "tables": tables, "documents": [], "warnings": ["Documents are not part of a .json backup; uploaded files were left as they are"]}

    def _read_zip(self, path: str, current_schema: int) -> dict[str, Any]:
        warnings: list[str] = []
        try:
            archive = zipfile.ZipFile(path)
        except zipfile.BadZipFile as err:
            raise BackupError("That .zip file is damaged") from err
        with archive:
            members = {info.filename: info for info in archive.infolist()}
            if MANIFEST not in members:
                raise BackupError("That .zip is not an TaskDragon portable backup (manifest_bridge.json is missing)")
            manifest = self._member_json(archive, members[MANIFEST], MAX_MANIFEST_BYTES)
            if not isinstance(manifest, Mapping) or not str(manifest.get("schema_version") or "").startswith("1.") or not str(manifest.get("schema_version")).endswith("-portable"):
                raise BackupError("This backup was written by an TaskDragon version this one cannot read")
            files = manifest.get("files") if isinstance(manifest.get("files"), Mapping) else {}
            temp_db = None
            if DUMP in members:
                raw = self._member_bytes(archive, members[DUMP], MAX_DUMP_BYTES)
                self._verify(DUMP, hashlib.sha256(raw).hexdigest(), len(raw), files)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as err:
                    raise BackupError("database_dump.json is not valid JSON") from err
                tables = self._check_dump(payload, current_schema)
            elif DB_COPY in members:
                temp_db = path + ".restore.db"
                try:
                    with archive.open(members[DB_COPY]) as handle:
                        sha, size = _Limited(handle, MAX_DB_BYTES, DB_COPY).copy_to(temp_db)
                    self._verify(DB_COPY, sha, size, files)
                    tables = self._tables_from_sqlite(temp_db, current_schema)
                finally:
                    _remove_quietly(temp_db)
                temp_db = None
                warnings.append("Restored from taskdragon.db because database_dump.json was missing")
            else:
                raise BackupError("The backup holds no database")
            documents = []
            for doc in manifest.get("documents") or []:
                sha = doc.get("sha256") if isinstance(doc, Mapping) else None
                name = "documents/" + str(sha)
                if not isinstance(sha, str) or not SHA_RE.match(sha) or name not in members:
                    raise BackupError("A document listed in the manifest is missing from the backup")
                if members[name].file_size > MAX_DOCUMENT_BYTES:
                    raise BackupError("A document in the backup is larger than allowed")
                with archive.open(members[name]) as handle:
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := handle.read(CHUNK):
                        size += len(chunk)
                        if size > MAX_DOCUMENT_BYTES:
                            raise BackupError("A document in the backup is larger than allowed")
                        digest.update(chunk)
                if digest.hexdigest() != sha:
                    raise BackupError("A document in the backup failed its SHA-256 check")
                documents.append(sha)
            keys: dict[str, bytes] = {}
            for member, info in members.items():
                if not member.startswith("secrets/"):
                    continue
                name = member[len("secrets/") :]
                if not SECRET_NAME_RE.match(name):
                    warnings.append("Skipped an unexpected file in the backup's secrets folder")
                    continue
                data = self._member_bytes(archive, info, MAX_SECRET_BYTES)
                self._verify(member, hashlib.sha256(data).hexdigest(), len(data), files)
                keys[name] = data
        return {"format": "zip", "manifest": manifest, "tables": tables, "documents": documents, "warnings": warnings, "temp_db": temp_db, "secrets": keys}

    @staticmethod
    def _verify(name: str, sha: str, size: int, files: Mapping[str, Any]) -> None:
        expected = files.get(name) if isinstance(files.get(name), Mapping) else None
        if expected is None:
            raise BackupError(f"The manifest has no checksum for {name}")
        if expected.get("sha256") != sha or int(expected.get("size") or -1) != size:
            raise BackupError(f"{name} failed its SHA-256 check; the backup is damaged")

    @staticmethod
    def _member_bytes(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
        if info.file_size > limit:
            raise BackupError(f"{info.filename} is larger than allowed")
        with archive.open(info) as handle:
            return _Limited(handle, limit, info.filename).read_all()

    def _member_json(self, archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> Any:
        try:
            return json.loads(self._member_bytes(archive, info, limit).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as err:
            raise BackupError(f"{info.filename} is not valid JSON") from err

    @staticmethod
    def _check_dump(payload: Any, current_schema: int) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping) or payload.get("format") not in (DUMP_FORMAT, LEGACY_DUMP_FORMAT) or not isinstance(payload.get("tables"), Mapping):
            raise BackupError("That file is not an TaskDragon database dump")
        version = payload.get("db_schema_version")
        if not isinstance(version, int) or version < PORTABLE_MIN_SCHEMA:
            raise BackupError("The dump has no usable database schema version")
        if version > current_schema:
            raise BackupError("This backup comes from a newer TaskDragon; update TaskDragon before restoring it")
        return payload["tables"]

    @staticmethod
    def _tables_from_sqlite(path: str, current_schema: int) -> dict[str, Any]:
        try:
            conn = sqlite3.connect("file:" + path.replace("?", "%3f").replace("#", "%23") + "?mode=ro", uri=True)
        except sqlite3.Error as err:
            raise BackupError("taskdragon.db in the backup cannot be opened") from err
        try:
            conn.row_factory = sqlite3.Row
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("taskdragon.db in the backup is damaged")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version < PORTABLE_MIN_SCHEMA or version > current_schema:
                raise BackupError("taskdragon.db in the backup has an incompatible schema version")
            tables: dict[str, Any] = {}
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", name):
                    continue
                cursor = conn.execute('SELECT * FROM "' + name + '"')
                tables[name] = {"rows": [{key: _blob(row[key]) for key in row.keys()} for row in cursor.fetchall()]}
            return tables
        except sqlite3.DatabaseError as err:
            raise BackupError("taskdragon.db in the backup is not a usable database") from err
        finally:
            conn.close()

    def _restore_documents(self, path: str, shas: list[str]) -> dict[str, int]:
        os.makedirs(self.docs_dir, exist_ok=True)
        restored = present = 0
        with zipfile.ZipFile(path) as archive:
            for sha in shas:
                target = os.path.join(self.docs_dir, sha)
                if os.path.isfile(target) and _sha256_file(target)[0] == sha:
                    present += 1
                    continue
                temp = target + ".restore.tmp"
                with archive.open("documents/" + sha) as handle:
                    actual, _size = _Limited(handle, MAX_DOCUMENT_BYTES, "A document").copy_to(temp)
                if actual != sha:
                    _remove_quietly(temp)
                    raise BackupError("A document changed while it was being restored")
                os.replace(temp, target)
                restored += 1
        return {"restored": restored, "present": present}


def _blob(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$base64": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as err:
        _LOGGER.debug("Could not remove %s: %s", path, err)


def _makedirs(path: str) -> None:
    os.makedirs(path, exist_ok=True)
