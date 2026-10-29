"""Authenticated HTTP endpoints for the maintenance document vault and service records.

The card uploads with ``hass.fetchWithAuth`` and opens files through
short-lived signed paths (``auth/sign_path``), so every request carries a
Home Assistant user. Each endpoint checks that user against the plan the
object belongs to, exactly like the WebSocket API does.

Files are sniffed from their bytes (PDF, PNG, JPEG, GIF, WebP, HEIC or plain
text only), stored by SHA-256, and served with ``nosniff``, a locked-down
Content-Security-Policy and a sanitized file name.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

from aiohttp import web

from homeassistant.components.http import KEY_HASS, KEY_HASS_USER, HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOC_CATEGORIES, DOC_CONTENT_TYPES, DOMAIN, MAX_UPLOAD_BYTES
from .database import MDOCS
from .maintenance import safe_filename, sniff_document_type
from .planner import PlannerError

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox",
    "Cache-Control": "private, max-age=300",
    "Referrer-Policy": "no-referrer",
}
INLINE_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp")


def async_register_views(hass: HomeAssistant) -> None:
    if hass.http is None:
        return
    hass.http.register_view(DocumentUploadView())
    hass.http.register_view(DocumentView())
    hass.http.register_view(ServiceRecordView())


async def _context(request: web.Request) -> tuple[Any, Any]:
    hass: HomeAssistant = request.app[KEY_HASS]
    coordinator = hass.data.get(DOMAIN)
    if coordinator is None:
        raise web.HTTPServiceUnavailable(text="OmniPlan is not set up")
    user = request.get(KEY_HASS_USER)
    if user is None:
        raise web.HTTPUnauthorized()
    actor = await coordinator.actor_for_user_id(user.id)
    return coordinator, actor


def _error(err: PlannerError) -> web.Response:
    status = HTTPStatus.NOT_FOUND if err.code == "not_found" else HTTPStatus.FORBIDDEN if err.code == "unauthorized" else HTTPStatus.BAD_REQUEST
    return web.json_response({"code": err.code, "message": err.message}, status=status)


class DocumentUploadView(HomeAssistantView):
    url = "/api/omniplan/documents"
    name = "api:omniplan:documents"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        coordinator, actor = await _context(request)
        fields: dict[str, str] = {}
        payload = bytearray()
        filename: str | None = None
        reader = await request.multipart()
        while (part := await reader.next()) is not None:
            if part.name == "file":
                filename = unquote(part.filename or "")  # some clients percent-encode; it's sanitized below
                while chunk := await part.read_chunk(65536):
                    payload.extend(chunk)
                    if len(payload) > MAX_UPLOAD_BYTES:
                        return web.json_response({"code": "too_large", "message": "Files can be at most 15 MB"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            elif part.name in ("object_id", "task_id", "event_id", "category", "title"):
                fields[part.name] = (await part.text())[:200]
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
    url = "/api/omniplan/documents/{doc_id}"
    name = "api:omniplan:document"
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
    url = "/api/omniplan/service_record/{object_id}"
    name = "api:omniplan:service_record"
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
