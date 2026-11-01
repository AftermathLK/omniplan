"""Authenticated HTTP endpoints: maintenance documents, service records, Mealie recipe images and backups.

The card uploads with ``hass.fetchWithAuth`` and opens files through
short-lived signed paths (``auth/sign_path``), so every request carries a
Home Assistant user. Each endpoint checks that user against the plan the
object belongs to, exactly like the WebSocket API does.

Files are sniffed from their bytes (PDF, PNG, JPEG, GIF, WebP, HEIC or plain
text only), stored by SHA-256, and served with ``nosniff``, a locked-down
Content-Security-Policy and a sanitized file name.

Recipe images are fetched by Home Assistant from Mealie with the stored token
(so they work away from home and the token never reaches the browser) and are
only passed through when Mealie answers with a real image type.

Backup download and restore are administrator-only. Downloads stream from disk
(signed paths let the browser and the Companion apps save them natively);
uploads stream to a temporary file, capped at 1 GB, and are fully verified by
:class:`.backup_manager.BackupManager` before anything is changed.
"""

from __future__ import annotations

import os
import tempfile
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

from aiohttp import web

from homeassistant.components.http import KEY_HASS, KEY_HASS_USER, HomeAssistantView
from homeassistant.core import HomeAssistant

from .backup_manager import BackupError
from .const import DOC_CATEGORIES, DOC_CONTENT_TYPES, DOMAIN, MAX_UPLOAD_BYTES
from .database import MDOCS
from .maintenance import safe_filename, sniff_document_type
from .mealie_client import MealieError, validate_recipe_id
from .planner import PlannerError

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox",
    "Cache-Control": "private, max-age=300",
    "Referrer-Policy": "no-referrer",
}
INLINE_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp")
MAX_FIELD_BYTES = 1024
MAX_PARTS = 12
MAX_RESTORE_BYTES = 1024 * 1024 * 1024
STREAM_CHUNK = 1024 * 1024


def async_register_views(hass: HomeAssistant) -> None:
    if hass.http is None:
        return
    hass.http.register_view(DocumentUploadView())
    hass.http.register_view(DocumentView())
    hass.http.register_view(ServiceRecordView())
    hass.http.register_view(MealieImageView())
    hass.http.register_view(HoardImageView())
    hass.http.register_view(BackupDownloadView())
    hass.http.register_view(BackupRestoreView())


async def _context(request: web.Request) -> tuple[Any, Any]:
    hass: HomeAssistant = request.app[KEY_HASS]
    coordinator = hass.data.get(DOMAIN)
    if coordinator is None:
        raise web.HTTPServiceUnavailable(text="TaskDragon is not set up")
    user = request.get(KEY_HASS_USER)
    if user is None:
        raise web.HTTPUnauthorized()
    actor = await coordinator.actor_for_user_id(user.id)
    return coordinator, actor


async def _read_field(part: Any, limit: int = MAX_FIELD_BYTES) -> str:
    """A small multipart text field, read in chunks and refused once it passes ``limit`` bytes.

    ``part.text()`` would buffer the whole field first, however large the client makes it.
    """
    data = bytearray()
    while chunk := await part.read_chunk(1024):
        data.extend(chunk)
        if len(data) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(data))
    return data.decode("utf-8", "replace")


async def _drain(part: Any) -> None:
    """Skip a part nobody asked for without keeping its bytes."""
    while await part.read_chunk(65536):
        pass


def _error(err: PlannerError) -> web.Response:
    status = (
        HTTPStatus.NOT_FOUND
        if err.code == "not_found"
        else HTTPStatus.FORBIDDEN
        if err.code == "unauthorized"
        else HTTPStatus.TOO_MANY_REQUESTS
        if err.code == "rate_limited"
        else HTTPStatus.BAD_REQUEST
    )
    return web.json_response({"code": err.code, "message": err.message}, status=status)


class DocumentUploadView(HomeAssistantView):
    url = "/api/taskdragon/documents"
    name = "api:taskdragon:documents"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        coordinator, actor = await _context(request)
        fields: dict[str, str] = {}
        payload = bytearray()
        filename: str | None = None
        reader = await request.multipart()
        parts = 0
        while (part := await reader.next()) is not None:
            parts += 1
            if parts > MAX_PARTS:
                return web.json_response({"code": "invalid", "message": "Too many form fields"}, status=HTTPStatus.BAD_REQUEST)
            if part.name == "file":
                filename = unquote(part.filename or "")[:255]  # some clients percent-encode; it's sanitized below
                while chunk := await part.read_chunk(65536):
                    payload.extend(chunk)
                    if len(payload) > MAX_UPLOAD_BYTES:
                        return web.json_response({"code": "too_large", "message": "Files can be at most 15 MB"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            elif part.name in ("object_id", "task_id", "event_id", "category", "title"):
                fields[part.name] = (await _read_field(part))[:200]
            else:
                await _drain(part)
        if not payload:
            return web.json_response({"code": "invalid", "message": "No file was uploaded"}, status=HTTPStatus.BAD_REQUEST)
        content_type = sniff_document_type(bytes(payload), filename)
        if content_type is None:
            return web.json_response(
                {"code": "unsupported", "message": "Upload a PDF, an image (PNG, JPEG, GIF, WebP, HEIC) or a .txt file"},
                status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
        try:
            await coordinator.planner.authorize(actor, mobjects=[fields.get("object_id")], mtasks=[fields.get("task_id")])
            if not fields.get("object_id"):
                raise PlannerError("invalid", "Pick an object for the document")
            runtime = coordinator.maintenance
            sha = await runtime.store_file(bytes(payload))
            document = await runtime.manager.add_document(
                {
                    "object_id": fields["object_id"],
                    "task_id": fields.get("task_id") or None,
                    "event_id": fields.get("event_id") or None,
                    "category": fields.get("category") if fields.get("category") in DOC_CATEGORIES else "other",
                    "title": fields.get("title") or None,
                    "kind": "file",
                    "sha256": sha,
                    "filename": safe_filename(filename, DOC_CONTENT_TYPES[content_type]),
                    "content_type": content_type,
                    "size": len(payload),
                },
                actor,
            )
        except PlannerError as err:
            return _error(err)
        return self.json({k: document.get(k) for k in ("id", "object_id", "task_id", "event_id", "category", "title", "kind", "filename", "content_type", "size", "created_at")})


class DocumentView(HomeAssistantView):
    url = "/api/taskdragon/documents/{doc_id}"
    name = "api:taskdragon:document"
    requires_auth = True

    async def get(self, request: web.Request, doc_id: str) -> web.StreamResponse:
        coordinator, actor = await _context(request)
        try:
            await coordinator.planner.authorize(actor, mdocs=[doc_id])
        except PlannerError as err:
            return _error(err)
        document = await coordinator.db.row(MDOCS, doc_id)
        if document is None or document["kind"] != "file" or document["content_type"] not in DOC_CONTENT_TYPES:
            raise web.HTTPNotFound()
        runtime = coordinator.maintenance
        try:
            data = await request.app[KEY_HASS].async_add_executor_job(runtime.read_file, document["sha256"])
        except (OSError, ValueError) as err:
            raise web.HTTPNotFound() from err
        disposition = "inline" if document["content_type"] in INLINE_TYPES else "attachment"
        headers = {**SECURITY_HEADERS, "Content-Disposition": f'{disposition}; filename="{document["filename"]}"'}
        return web.Response(body=data, content_type=document["content_type"], headers=headers)


class ServiceRecordView(HomeAssistantView):
    url = "/api/taskdragon/service_record/{object_id}"
    name = "api:taskdragon:service_record"
    requires_auth = True

    async def get(self, request: web.Request, object_id: str) -> web.StreamResponse:
        coordinator, actor = await _context(request)
        task_id = request.query.get("task") or None
        try:
            await coordinator.planner.authorize(actor, mobjects=[object_id], mtasks=[task_id])
            people = {p["entity_id"]: p["name"] for p in coordinator.people_info()}
            pdf = await coordinator.maintenance.manager.service_record_pdf(
                object_id, start=request.query.get("from"), end=request.query.get("to"), task_id=task_id, people=people
            )
        except PlannerError as err:
            return _error(err)
        headers = {**SECURITY_HEADERS, "Content-Disposition": 'inline; filename="service-record.pdf"'}
        return web.Response(body=pdf, content_type="application/pdf", headers=headers)


class MealieImageView(HomeAssistantView):
    url = "/api/taskdragon/mealie_image/{recipe_id}"
    name = "api:taskdragon:mealie_image"
    requires_auth = True

    async def get(self, request: web.Request, recipe_id: str) -> web.StreamResponse:
        coordinator, _actor = await _context(request)
        try:
            validate_recipe_id(recipe_id)
        except MealieError as err:
            raise web.HTTPBadRequest() from err
        size = request.query.get("size", "min")
        if size not in ("min", "original"):
            raise web.HTTPBadRequest()
        try:
            data, content_type = await coordinator.recipes.image(recipe_id, size)
        except (MealieError, PlannerError) as err:
            raise web.HTTPNotFound() from err
        headers = {**SECURITY_HEADERS, "Cache-Control": "private, max-age=86400"}
        return web.Response(body=data, content_type=content_type, headers=headers)


class HoardImageView(HomeAssistantView):
    """A wishlist item's product photo, proxied so browsers never contact the store (and gifts stay hidden)."""

    url = "/api/taskdragon/hoard_image/{item_id}"
    name = "api:taskdragon:hoard_image"
    requires_auth = True

    async def get(self, request: web.Request, item_id: str) -> web.StreamResponse:
        coordinator, actor = await _context(request)
        if not 0 < len(item_id) <= 64:
            raise web.HTTPNotFound()
        try:
            data, content_type = await coordinator.hoard.image(item_id, actor)
        except PlannerError as err:
            if err.code == "rate_limited":
                return _error(err)
            raise web.HTTPNotFound() from err
        headers = {**SECURITY_HEADERS, "Cache-Control": "private, max-age=86400"}
        return web.Response(body=data, content_type=content_type, headers=headers)


def _admin_only(actor: Any) -> web.Response | None:
    if actor.is_admin:
        return None
    return web.json_response({"code": "unauthorized", "message": "Only an administrator can download or restore backups"}, status=HTTPStatus.FORBIDDEN)


class BackupDownloadView(HomeAssistantView):
    """A fresh portable backup (or, with ``?snapshot=<name>``, one of the rolling snapshots)."""

    url = "/api/taskdragon/backup/download"
    name = "api:taskdragon:backup_download"
    requires_auth = True

    async def get(self, request: web.Request) -> web.StreamResponse:
        coordinator, actor = await _context(request)
        if (denied := _admin_only(actor)) is not None:
            return denied
        hass: HomeAssistant = request.app[KEY_HASS]
        backups = coordinator.backups
        temporary = False
        try:
            if request.query.get("snapshot"):
                filename = request.query["snapshot"]
                path = await hass.async_add_executor_job(backups.snapshot_path, filename)
            else:
                path, filename = await backups.export_download()
                temporary = True
        except BackupError as err:
            status = HTTPStatus.NOT_FOUND if request.query.get("snapshot") else HTTPStatus.INTERNAL_SERVER_ERROR
            return web.json_response({"code": "backup_error", "message": str(err)}, status=status)
        try:
            size = (await hass.async_add_executor_job(os.stat, path)).st_size
            response = web.StreamResponse(
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                }
            )
            response.content_type = "application/zip"
            response.content_length = size
            await response.prepare(request)
            handle = await hass.async_add_executor_job(open, path, "rb")
            try:
                while chunk := await hass.async_add_executor_job(handle.read, STREAM_CHUNK):
                    await response.write(chunk)
            finally:
                await hass.async_add_executor_job(handle.close)
                if temporary:
                    # Gone before the browser sees the end of the download.
                    await hass.async_add_executor_job(_remove, path)
                    temporary = False
            await response.write_eof()
            return response
        finally:
            if temporary:
                await hass.async_add_executor_job(_remove, path)


class BackupRestoreView(HomeAssistantView):
    """Upload a portable backup (.zip) or its database_dump.json and restore it (``mode`` = replace | merge)."""

    url = "/api/taskdragon/backup/restore"
    name = "api:taskdragon:backup_restore"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        coordinator, actor = await _context(request)
        if (denied := _admin_only(actor)) is not None:
            return denied
        hass: HomeAssistant = request.app[KEY_HASS]
        backups = coordinator.backups
        await hass.async_add_executor_job(lambda: os.makedirs(backups.backup_dir, exist_ok=True))
        handle_fd, path = await hass.async_add_executor_job(tempfile.mkstemp, ".tmp", ".upload-", backups.backup_dir)
        mode = "merge"
        received = 0
        try:
            handle = await hass.async_add_executor_job(os.fdopen, handle_fd, "wb")
            try:
                reader = await request.multipart()
                while (part := await reader.next()) is not None:
                    if part.name == "file":
                        while chunk := await part.read_chunk(STREAM_CHUNK):
                            received += len(chunk)
                            if received > MAX_RESTORE_BYTES:
                                return web.json_response(
                                    {"code": "too_large", "message": "Backups can be at most 1 GB"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                                )
                            await hass.async_add_executor_job(handle.write, chunk)
                    elif part.name == "mode":
                        mode = (await _read_field(part, 64)).strip()[:20]
                    else:
                        await _drain(part)
            finally:
                await hass.async_add_executor_job(handle.close)
            if not received:
                return web.json_response({"code": "invalid", "message": "No backup file was uploaded"}, status=HTTPStatus.BAD_REQUEST)
            if mode not in ("replace", "merge"):
                return web.json_response({"code": "invalid", "message": "Pick Full replace or Merge"}, status=HTTPStatus.BAD_REQUEST)
            try:
                report = await backups.restore_file(path, mode)
            except BackupError as err:
                return web.json_response({"code": "backup_error", "message": str(err)}, status=HTTPStatus.BAD_REQUEST)
            return self.json(report)
        finally:
            await hass.async_add_executor_job(_remove, path)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
