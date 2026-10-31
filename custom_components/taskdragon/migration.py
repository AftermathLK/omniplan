"""Bring an existing installation from the integration's former name ("omniplan") over to TaskDragon.

TaskDragon is the same integration under a new name, so a household that ran
it before keeps everything:

* On first start TaskDragon copies the old database with SQLite's online
  backup API (safe even while the old integration is still running) and links
  or copies the uploaded documents into its own folder.
* The config flow offers to import the old settings (people, schedule and the
  Mealie connection) in one step.
* QR labels printed before the rename keep opening the right item through a
  hidden legacy panel URL, and old ``.zip`` backups can still be restored.

Nothing belonging to the old installation is changed or deleted. No Home
Assistant imports, so this runs in plain unit tests.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from typing import Any

from .const import DB_FILENAME, DOCS_DIRNAME

_LOGGER = logging.getLogger(__name__)

LEGACY_DOMAIN = "omniplan"
LEGACY_DB_FILENAME = "omniplan.db"
LEGACY_DOCS_DIRNAME = "omniplan_docs"
LEGACY_PANEL_URL_PATH = "omniplan"
LEGACY_DUMP_FORMAT = "omniplan-database-dump"
LEGACY_PREP_MARKERS = ("omniplan", "omniplan_prep")
MIGRATED_META = "migrated_from"


def migrate_legacy_storage(config_dir: str) -> dict[str, Any]:
    """Copy the old database and documents once, if TaskDragon has no database of its own yet."""
    storage = os.path.join(config_dir, ".storage")
    new_db = os.path.join(storage, DB_FILENAME)
    old_db = os.path.join(storage, LEGACY_DB_FILENAME)
    report: dict[str, Any] = {"database": False, "documents": 0}
    if os.path.exists(new_db) or not os.path.isfile(old_db):
        return report
    temp = new_db + ".migrating"
    source = sqlite3.connect(old_db)
    try:
        target = sqlite3.connect(temp)
        try:
            source.backup(target)
            target.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (MIGRATED_META, '"' + LEGACY_DOMAIN + '"'),
            )
            target.commit()
        finally:
            target.close()
    except sqlite3.Error:
        if os.path.exists(temp):
            os.remove(temp)
        raise
    finally:
        source.close()
    os.replace(temp, new_db)
    report["database"] = True
    report["documents"] = _link_documents(os.path.join(storage, LEGACY_DOCS_DIRNAME), os.path.join(storage, DOCS_DIRNAME))
    _LOGGER.info("Copied the previous installation's database and %s documents into TaskDragon", report["documents"])
    return report


def _link_documents(old_dir: str, new_dir: str) -> int:
    """Hard-link (or copy) every stored document; files are named by their SHA-256, so existing ones are skipped."""
    if not os.path.isdir(old_dir):
        return 0
    os.makedirs(new_dir, exist_ok=True)
    moved = 0
    for name in os.listdir(old_dir):
        source = os.path.join(old_dir, name)
        target = os.path.join(new_dir, name)
        if name.endswith(".tmp") or not os.path.isfile(source) or os.path.exists(target):
            continue
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        moved += 1
    return moved
