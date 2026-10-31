"""Carrying an installation from the integration's former name over to TaskDragon."""

from __future__ import annotations

import json
import os
from datetime import datetime

from conftest import TZ

from taskdragon.backup_manager import BackupManager, parse_name
from taskdragon.database import TaskDragonDB
from taskdragon.kitchen import discover_rules
from taskdragon.migration import LEGACY_DB_FILENAME, LEGACY_DOCS_DIRNAME, MIGRATED_META, migrate_legacy_storage
from taskdragon.planner import Planner


async def _legacy_install(config_dir) -> str:
    storage = config_dir / ".storage"
    storage.mkdir(parents=True)
    db = TaskDragonDB(str(storage / LEGACY_DB_FILENAME))
    await db.async_open()
    planner = Planner(db, tz=TZ)
    await planner.seed_defaults()
    plan = await planner.create_plan({"name": "Garage sale", "archetype": "quick_list"})
    await planner.save_task({"plan_id": plan["id"], "summary": "Price the bikes"})
    await db.async_close()
    docs = storage / LEGACY_DOCS_DIRNAME
    docs.mkdir()
    (docs / ("a" * 64)).write_bytes(b"%PDF-1.4 manual")
    (docs / "half-written.tmp").write_bytes(b"x")
    return plan["id"]


async def test_first_start_copies_database_and_documents(tmp_path) -> None:
    plan_id = await _legacy_install(tmp_path)
    report = migrate_legacy_storage(str(tmp_path))
    assert report == {"database": True, "documents": 1}
    db = TaskDragonDB(str(tmp_path / ".storage" / "taskdragon.db"))
    await db.async_open()
    try:
        assert (await db.get_plan(plan_id))["name"] == "Garage sale"
        assert [t["summary"] for t in await db.list_tasks(plan_id)] == ["Price the bikes"]
        assert await db.get_meta(MIGRATED_META) == "omniplan"
    finally:
        await db.async_close()
    new_docs = tmp_path / ".storage" / "taskdragon_docs"
    assert (new_docs / ("a" * 64)).read_bytes() == b"%PDF-1.4 manual" and not (new_docs / "half-written.tmp").exists()
    # The old installation is untouched, and a second start changes nothing.
    assert (tmp_path / ".storage" / LEGACY_DB_FILENAME).exists()
    assert migrate_legacy_storage(str(tmp_path)) == {"database": False, "documents": 0}


def test_nothing_to_migrate(tmp_path) -> None:
    assert migrate_legacy_storage(str(tmp_path)) == {"database": False, "documents": 0}
    assert not (tmp_path / ".storage" / "taskdragon.db").exists()


async def test_old_backups_and_prep_markers_still_work(tmp_path) -> None:
    db = TaskDragonDB(str(tmp_path / "taskdragon.db"))
    await db.async_open()
    try:
        planner = Planner(db, tz=TZ)
        await planner.seed_defaults()
        dump = await db.export_portable()
        legacy = {"format": "omniplan-database-dump", "db_schema_version": dump["db_schema_version"], "tables": dump["tables"]}
        path = tmp_path / "database_dump.json"
        path.write_text(json.dumps(legacy))

        async def users():
            return []

        backups = BackupManager(
            db=db, docs_dir=str(tmp_path / "docs"), backup_dir=str(tmp_path / "backups"), app_version="test",
            users=users, people=lambda: [], options=lambda: {}, local_now=lambda: datetime(2026, 10, 31, 3, 15),
        )
        report = await backups.restore_file(str(path), "merge")
        assert report["format"] == "json"
        assert parse_name("omniplan_portable_backup_20261028_031500.zip")["kind"] == "snapshot"
        assert os.path.basename(report["safety_snapshot"]).startswith("taskdragon_portable_backup_")
    finally:
        await db.async_close()
    recipe = {"name": "Brisket", "tags": [], "notes": [], "extras": {"omniplan": "thaw 48h"}}
    assert {r.key: r.hours for r in discover_rules(recipe, [])}["thaw"] == 48.0
