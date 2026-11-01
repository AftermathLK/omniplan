"""Regression tests for the 1.4.1 security audit fixes."""

from __future__ import annotations

import asyncio
import ipaddress
from datetime import datetime

import pytest

from conftest import plan_by_name

from taskdragon.backup_manager import BackupManager
from taskdragon.database import HITEMS, HSECTIONS, MTASKS
from taskdragon.hoard import HoardManager, SecretBox
from taskdragon.maintenance_manager import ACTION_CONFIGURED_BY, ACTIONS_STAMPED_META, MaintenanceManager
from taskdragon.planner import Actor, PlannerError
from taskdragon.price_tracker import GuardedFetcher, Page, UnsafeUrl, _codec, _public_host, check_url
from taskdragon.ratelimit import RateLimiter

ALEX = Actor(user_id="u-alex", user_name="Alex", person="person.alex", is_admin=True)
SAM = Actor(user_id="u-sam", user_name="Sam", person="person.sam")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ---------------------------------------------------------------------------
# M3: IPv6 forms that embed an IPv4 address
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b:1::c0a8:101", "2002:c0a8:101::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2", "::ffff:10.0.0.1"])
def test_embedded_private_ipv4_is_not_public(host: str) -> None:
    assert not _public_host(host)
    with pytest.raises(UnsafeUrl):
        check_url(f"http://[{host}]/")


def test_embedded_public_ipv4_stays_public() -> None:
    assert _public_host("64:ff9b::5db8:d822")  # 93.184.216.34 through NAT64
    assert _public_host(str(ipaddress.IPv6Address("2606:4700::1111")))


def test_unknown_charsets_fall_back_to_utf8() -> None:
    assert _codec("utf-8") == "utf-8" and _codec("ISO-8859-1") == "iso8859-1"
    assert _codec("x-made-up") == "utf-8" and _codec(None) == "utf-8"


async def test_host_bookkeeping_is_bounded(monkeypatch) -> None:
    import taskdragon.price_tracker as pt

    monkeypatch.setattr(pt, "MAX_TRACKED_HOSTS", 3)
    fetcher = GuardedFetcher()
    held = fetcher._host_lock("busy.example")
    await held.acquire()
    for index in range(10):
        fetcher._host_lock(f"h{index}.example")
    assert len(fetcher._host_locks) <= 4 and "busy.example" in fetcher._host_locks, "a lock in use is never dropped"
    held.release()


# ---------------------------------------------------------------------------
# Rate limiter (L6, L9)
# ---------------------------------------------------------------------------
def test_rate_limiter_windows_per_user() -> None:
    now = [0.0]
    limiter = RateLimiter(3, 60, monotonic=lambda: now[0])
    assert limiter.allow("a") and limiter.allow("a", 2) and not limiter.allow("a")
    assert limiter.allow("b") and limiter.allow(None, 100), "other users and automations are separate"
    assert limiter.retry_after("a") == 61
    now[0] = 61
    assert limiter.allow("a") and limiter.retry_after("a") == 0


# ---------------------------------------------------------------------------
# M1: on-complete actions need an administrator who still is one
# ---------------------------------------------------------------------------
@pytest.fixture
async def maint(planner):
    manager = MaintenanceManager(planner.db, planner, state_reader={}.get)
    await manager.async_load()
    ran: list[dict] = []
    manager.run_action = lambda action, actor: ran.append(action)
    manager.test_ran = ran  # type: ignore[attr-defined]
    return manager


async def _task_with_action(planner, maint, actor=ALEX):
    plan = await plan_by_name(planner, "Home Maintenance")
    obj = await maint.save_object({"plan_id": plan["id"], "name": "Fridge"}, ALEX)
    return await maint.save_task(
        {"object_id": obj["id"], "name": "Filter", "interval_value": 6, "interval_unit": "months", "on_complete_action": {"service": "button.press", "entity_id": "button.fridge_filter_reset"}},
        actor,
    )


async def test_actions_are_stamped_and_checked_at_run_time(planner, maint) -> None:
    task = await _task_with_action(planner, maint)
    assert ACTION_CONFIGURED_BY not in task["on_complete_action"] and not task["action_paused"]
    raw = await maint.db.row(MTASKS, task["id"])
    assert raw["on_complete_action"][ACTION_CONFIGURED_BY] == "u-alex"
    # A non-admin may save the task (other fields) without touching the action ...
    same = await maint.save_task({"id": task["id"], "name": "Water filter", "on_complete_action": task["on_complete_action"]}, SAM)
    assert (await maint.db.row(MTASKS, same["id"]))["on_complete_action"][ACTION_CONFIGURED_BY] == "u-alex"
    # ... but can't change or clear it.
    for action in ({"service": "shell_command.wipe"}, None):
        with pytest.raises(PlannerError):
            await maint.save_task({"id": task["id"], "on_complete_action": action}, SAM)
    admins = {"u-alex": True}

    async def trusted(action):
        return admins.get(action.get(ACTION_CONFIGURED_BY), False)

    maint.action_trusted = trusted
    await maint.complete(task["id"], SAM)
    assert maint.test_ran == [{"service": "button.press", "entity_id": ["button.fridge_filter_reset"], "data": {}}], "the stamp never reaches the service call"
    admins["u-alex"] = False  # Alex is no longer an administrator
    await maint.complete(task["id"], SAM)
    assert len(maint.test_ran) == 1


async def test_restored_actions_wait_for_an_administrator(planner, maint) -> None:
    task = await _task_with_action(planner, maint)
    assert await maint.pause_restored_actions() == 1
    view = (await maint.board((await plan_by_name(planner, "Home Maintenance"))["id"], ALEX))["tasks"][0]
    assert view["action_paused"] and ACTION_CONFIGURED_BY not in view["on_complete_action"]
    await maint.complete(task["id"], ALEX)
    assert maint.test_ran == [], "a paused action never runs"
    by_member = await maint.save_task({"id": task["id"], "name": "Filter (fridge)", "on_complete_action": task["on_complete_action"]}, SAM)
    assert by_member["action_paused"], "a non-admin can edit the task, but only an administrator turns the action back on"
    resaved = await maint.save_task({"id": task["id"], "on_complete_action": task["on_complete_action"]}, ALEX)
    assert not resaved["action_paused"]
    await maint.complete(task["id"], ALEX)
    assert len(maint.test_ran) == 1
    assert await maint.trust_existing_actions() == 0, "the upgrade step never runs after a restore"


async def test_upgrade_trusts_actions_saved_before_stamps(planner, maint) -> None:
    task = await _task_with_action(planner, maint)
    action = {k: v for k, v in (await maint.db.row(MTASKS, task["id"]))["on_complete_action"].items() if k != ACTION_CONFIGURED_BY}
    await maint.db.update_row(MTASKS, task["id"], {"on_complete_action": action})
    await maint.db.set_meta(ACTIONS_STAMPED_META, None)
    assert await maint.trust_existing_actions() == 1
    assert (await maint.db.row(MTASKS, task["id"]))["on_complete_action"][ACTION_CONFIGURED_BY] == "upgrade"
    assert await maint.trust_existing_actions() == 0


async def test_restore_reports_post_restore_warnings(db, tmp_path) -> None:
    async def users():
        return []

    async def after():
        return ["2 on-complete device actions are paused"]

    backups = BackupManager(
        db=db, docs_dir=str(tmp_path / "docs"), backup_dir=str(tmp_path / "b"), app_version="t", users=users, people=lambda: [], options=lambda: {},
        local_now=lambda: datetime(2026, 10, 31, 9, 0), on_restored=after,
    )
    snapshot = await backups.create_snapshot("snapshot")
    report = await backups.restore_snapshot(snapshot["name"], "merge")
    assert "2 on-complete device actions are paused" in report["warnings"]


# ---------------------------------------------------------------------------
# L8, L7, L3 (planner and maintenance)
# ---------------------------------------------------------------------------
async def test_document_event_must_belong_to_the_object(planner, maint) -> None:
    plan = await plan_by_name(planner, "Home Maintenance")
    first = await maint.save_object({"plan_id": plan["id"], "name": "Furnace"}, ALEX)
    other = await maint.save_object({"plan_id": plan["id"], "name": "Car"}, ALEX)
    task = await maint.save_task({"object_id": other["id"], "name": "Oil", "interval_value": 1, "interval_unit": "years"}, ALEX)
    event = (await maint.complete(task["id"], ALEX))["event"]
    with pytest.raises(PlannerError):
        await maint.add_document({"object_id": first["id"], "event_id": event["id"], "kind": "link", "url": "https://example.com/r.pdf", "title": "x"}, ALEX)
    ok = await maint.add_document({"object_id": other["id"], "event_id": event["id"], "kind": "link", "url": "https://example.com/r.pdf", "title": "Receipt"}, ALEX)
    assert ok["event_id"] == event["id"]


async def test_bad_brain_dump_times_fail_alone(planner) -> None:
    chores = await plan_by_name(planner, "Daily Chores")
    results = await planner.apply_actions(
        [
            {"type": "task", "plan_id": chores["id"], "summary": "Call the plumber", "due_time": "ab:cd"},
            {"type": "task", "plan_id": chores["id"], "summary": "Feed the cat", "due_time": "25:00"},
            {"type": "task", "plan_id": chores["id"], "summary": "Water plants", "due_time": "7:30"},
        ],
        ALEX,
    )
    assert [r["ok"] for r in results] == [False, False, True]
    assert results[0]["error"] == "Times look like 14:30"


async def test_two_tabs_get_one_private_quick_list(planner) -> None:
    first, second = await asyncio.gather(planner.quick_lists(SAM), planner.quick_lists(SAM))
    assert first["private"] == second["private"]
    private = [p for p in await planner.db.list_plans(include_archived=True) if p["is_private"] and p["owner_user_id"] == "u-sam"]
    assert len(private) == 1


# ---------------------------------------------------------------------------
# Wishlist: L3, L5, L6, L7
# ---------------------------------------------------------------------------
class Fetcher:
    def __init__(self) -> None:
        self.pages: dict[str, object] = {}
        self.calls: list[str] = []

    async def get(self, url, *, headers, max_bytes, truncate=False):
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        body, kind = value if isinstance(value, tuple) else (value, "text/html")
        return Page(url, 200, body if isinstance(body, bytes) else str(body).encode(), "utf-8", kind)

    async def get_json(self, url, headers=None):
        raise AssertionError("not used")

    async def close(self) -> None:
        pass


class Products:
    calls = 0

    async def lookup(self, code):
        self.calls += 1
        return None


@pytest.fixture
async def hoard(planner, tmp_path):
    box = SecretBox(str(tmp_path / "k" / "hoard.key"))
    box.load()
    spawned: list = []
    manager = HoardManager(
        planner.db, planner, box=box, fetcher=Fetcher(), products=Products(),
        person_user={"person.sam": "u-sam", "person.alex": "u-alex"}.get, spawn=lambda coro, name: spawned.append(coro),
    )
    planner.hoard = manager
    manager.test_plan = await planner.create_plan({"name": "Wishlist & Gifts", "archetype": "wishlist_hoard"})  # type: ignore[attr-defined]
    yield manager
    for coro in spawned:
        coro.close()


async def test_concurrent_first_opens_seed_once(hoard) -> None:
    plan = hoard.test_plan
    await asyncio.gather(*(hoard.board(plan["id"], ALEX) for _ in range(4)))
    assert len(await hoard.db.rows(HSECTIONS, {"plan_id": plan["id"]})) == 3


async def test_overlapping_checks_alert_once(hoard) -> None:
    alerts: list[dict] = []

    async def on_alert(alert):
        await asyncio.sleep(0)
        alerts.append(alert)

    hoard._on_alert = on_alert
    item = await hoard.save_item({"plan_id": hoard.test_plan["id"], "title": "Drill", "target_price": 100}, ALEX)
    await hoard.add_url(item["id"], ALEX, url="https://shop.example/drill", check=False)
    url_id = (await hoard.item_view(item["id"], ALEX))["urls"][0]["id"]
    await hoard.db.update_row(hoard_urls(), url_id, {"last_price": 99.0, "lowest_price": 99.0, "highest_price": 120.0})
    await asyncio.gather(hoard.evaluate_alerts(item["id"]), hoard.evaluate_alerts(item["id"]))
    assert [a["kind"] for a in alerts] == ["target"]


def hoard_urls():
    from taskdragon.database import HURLS

    return HURLS


async def test_photos_are_proxied_typed_cached_and_hidden_like_gifts(hoard) -> None:
    plan = hoard.test_plan
    board = await hoard.board(plan["id"], ALEX)
    xmas = next(s for s in board["sections"] if s["name"] == "Christmas")
    gift = await hoard.save_item({"plan_id": plan["id"], "title": "Bike", "section_id": xmas["id"], "gift_for_person_id": "person.sam", "image_url": "https://img.example/bike.png"}, ALEX)
    assert gift["image_path"].startswith(f"/api/taskdragon/hoard_image/{gift['id']}?v=")
    hoard.fetcher.pages = {"https://img.example/bike.png": (PNG, "image/png")}
    data, kind = await hoard.image(gift["id"], ALEX)
    assert data == PNG and kind == "image/png"
    await hoard.image(gift["id"], ALEX)
    assert hoard.fetcher.calls == ["https://img.example/bike.png"], "served from the cache the second time"
    with pytest.raises(PlannerError) as caught:
        await hoard.image(gift["id"], SAM)
    assert caught.value.code == "not_found", "the gift's recipient can't fetch its photo either"
    other = await hoard.save_item({"plan_id": plan["id"], "title": "Page", "image_url": "https://img.example/page"}, ALEX)
    hoard.fetcher.pages["https://img.example/page"] = (b"<html><script>alert(1)</script></html>", "image/png")
    with pytest.raises(PlannerError):
        await hoard.image(other["id"], ALEX)


async def test_scraped_photos_on_private_addresses_are_ignored(hoard) -> None:
    item = await hoard.save_item({"plan_id": hoard.test_plan["id"], "title": "Router"}, ALEX)
    await hoard.add_url(item["id"], ALEX, url="https://shop.example/router", check=False)
    page = '<html><head><meta property="og:image" content="https://192.168.1.1/cam.jpg"><meta property="product:price:amount" content="99"></head></html>'
    hoard.fetcher.pages = {"https://shop.example/router": page}
    await hoard.check_urls([(await hoard.item_view(item["id"], ALEX))["urls"][0]["id"]])
    assert (await hoard.db.row(HITEMS, item["id"]))["image_url"] is None


async def test_outbound_work_is_limited_per_user(hoard) -> None:
    from taskdragon import hoard as hoard_module

    hoard._fetch_limit = hoard_module.RateLimiter(3, 3600)
    item = await hoard.save_item({"plan_id": hoard.test_plan["id"], "title": "Lamp"}, SAM)
    for n in range(3):
        await hoard.add_url(item["id"], SAM, url=f"https://shop{n}.example/lamp")
    with pytest.raises(PlannerError) as caught:
        await hoard.add_url(item["id"], SAM, url="https://shop9.example/lamp")
    assert caught.value.code == "rate_limited"
    await hoard.add_url(item["id"], ALEX, url="https://shop9.example/lamp")  # another user has their own allowance
    hoard._lookup_limit = hoard_module.RateLimiter(1, 3600)
    first = await hoard.scan_price_check("885909950805", SAM)
    second = await hoard.scan_price_check("885909950805", SAM)
    assert first["lookup"] == "not_found" and second["lookup"] == "limit" and hoard.products.calls == 1


async def test_one_failing_deal_search_does_not_stop_the_round(hoard, monkeypatch) -> None:
    plan = hoard.test_plan
    a = await hoard.save_item({"plan_id": plan["id"], "title": "Kayak", "watch_keyword_deals": True}, ALEX)
    b = await hoard.save_item({"plan_id": plan["id"], "title": "Tent", "watch_keyword_deals": True}, ALEX)
    seen: list[str] = []

    async def search(query):
        seen.append(query)
        if query == "Kayak":
            raise RuntimeError("boom")
        return []

    monkeypatch.setattr(hoard, "_search_deals", search)
    await hoard.refresh_deals(force=True)
    assert set(seen) == {"Kayak", "Tent"}
    for item in (a, b):
        assert (await hoard.db.row(HITEMS, item["id"]))["deals_checked_at"]
