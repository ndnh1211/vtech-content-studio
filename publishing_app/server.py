"""Starlette ASGI Web Application and REST API for the AI Chat-Based Content Publishing & Scheduling System."""
import base64
import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from publishing_app.chat_service import ChatOrchestrator
from publishing_app.database import Database, sanitize_dict
from publishing_app.models import ContentStatus, PublishingError
from publishing_app.nlp_agent import DEFAULT_TZ, extract_intent_and_entities, generate_content_draft
from publishing_app.publishers import PublisherRouter
from publishing_app.scheduler import SchedulingService
from publishing_app.validator import MEDIA_STORAGE_DIR, PLATFORM_REGISTRY, validate_content_item


STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    db_path: Optional[Path] = None,
    live_mode: Optional[bool] = None,
    start_worker: bool = True,
) -> Starlette:
    if db_path is None and os.environ.get("VTECH_DB_PATH"):
        db_path = Path(os.environ["VTECH_DB_PATH"])
    if live_mode is None:
        live_mode = os.environ.get("VTECH_LIVE_MODE", "").lower() in ("1", "true", "yes")

    db = Database(db_path=db_path)
    router = PublisherRouter(live_mode=live_mode)
    scheduler = SchedulingService(db=db, publisher_router=router)
    chat_orchestrator = ChatOrchestrator(db=db, scheduler=scheduler)

    def get_auth_user(request: Request) -> Optional[Dict[str, Any]]:
        auth_header = request.headers.get("Authorization", "")
        token = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        elif request.headers.get("X-Session-Token"):
            token = request.headers.get("X-Session-Token", "").strip()
        return db.authenticate_token(token)

    def json_resp(data: Any, status_code: int = 200) -> JSONResponse:
        return JSONResponse(sanitize_dict(data), status_code=status_code)

    async def index_page(request: Request) -> HTMLResponse:
        html_file = STATIC_DIR / "index.html"
        return HTMLResponse(html_file.read_text(encoding="utf-8"))

    async def serve_media(request: Request) -> Response:
        slug = request.path_params["slug"]
        filename = request.path_params["filename"]
        file_path = (MEDIA_STORAGE_DIR / slug / filename).resolve()
        if not str(file_path).startswith(str(MEDIA_STORAGE_DIR.resolve())) or not file_path.is_file():
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
        return FileResponse(str(file_path))

    async def get_platforms(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED", "message": "Vui lòng đăng nhập hợp lệ."}, 401)
        platforms = [cap.to_dict() for cap in PLATFORM_REGISTRY.values()]
        return json_resp({"platforms": platforms, "user": user, "live_mode": router.live_mode})

    async def set_publish_mode(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        body = await request.json()
        live = bool(body.get("live_mode"))
        router.set_live_mode(live)
        db.log_audit(user["workspace_id"], user["user_id"], "set_live_mode", None, {"live_mode": live})
        return json_resp({"live_mode": router.live_mode})

    async def post_chat(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED", "message": "Phiên đăng nhập không hợp lệ."}, 401)
        body = await request.json()
        message = (body.get("message") or "").strip()
        if not message:
            return json_resp({"error": "EMPTY_MESSAGE", "message": "Vui lòng nhập nội dung tin nhắn."}, 400)

        session_id = body.get("session_id") or f"sess_{user['workspace_id']}"
        tz_name = body.get("timezone") or DEFAULT_TZ
        active_cid = body.get("active_content_id")

        result = chat_orchestrator.handle_user_message(
            workspace_id=user["workspace_id"],
            user_id=user["user_id"],
            session_id=session_id,
            message=message,
            timezone_name=tz_name,
            active_content_id=active_cid,
        )
        return json_resp(result)

    async def upload_docx_chat(request: Request) -> JSONResponse:
        """Handles direct .docx file + optional image files uploaded from the Chat UI."""
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)

        body = await request.json()
        docx_name = body.get("docx_name") or "uploaded.docx"
        docx_b64 = body.get("docx_base64") or ""
        extra_images = body.get("images") or []
        message = (body.get("message") or "").strip() or f"Tải lên file {docx_name} cho website chính"
        tz_name = body.get("timezone") or DEFAULT_TZ
        session_id = body.get("session_id") or f"sess_{user['workspace_id']}"

        if not docx_b64:
            return json_resp({"error": "MISSING_FILE", "message": "Chưa có dữ liệu file .docx."}, 400)

        db.add_chat_message(session_id, user["workspace_id"], user["user_id"], "user", f"📎 [Đính kèm: {docx_name}] {message}")
        extracted = extract_intent_and_entities(message, tz_name=tz_name)
        platforms = extracted["platforms"] or ["vtechfilm"]

        with tempfile.TemporaryDirectory(prefix="vtech_upload_") as tmp_dir:
            tmp_p = Path(tmp_dir)
            docx_file = tmp_p / Path(docx_name).name
            docx_file.write_bytes(base64.b64decode(docx_b64))

            img_paths: List[Path] = []
            for idx, img_obj in enumerate(extra_images):
                img_name = Path(img_obj.get("name") or f"img_{idx + 1}.jpg").name
                img_b64 = img_obj.get("base64") or ""
                if img_b64:
                    ip = tmp_p / img_name
                    ip.write_bytes(base64.b64decode(img_b64))
                    img_paths.append(ip)

            result = chat_orchestrator._handle_import_docx(
                workspace_id=user["workspace_id"],
                user_id=user["user_id"],
                session_id=session_id,
                docx_path=str(docx_file),
                platforms=platforms,
                schedule_info=extracted.get("schedule"),
                timezone_name=tz_name,
                extra_image_paths=img_paths,
            )
            return json_resp(result)

    async def get_chat_history(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        session_id = request.query_params.get("session_id") or f"sess_{user['workspace_id']}"
        messages = db.list_chat_messages(session_id, user["workspace_id"])
        return json_resp({"session_id": session_id, "messages": messages})

    async def list_contents(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        status = request.query_params.get("status")
        platform = request.query_params.get("platform")
        items = db.list_content_items(user["workspace_id"], status=status, platform=platform)
        return json_resp({"items": items})

    async def get_content(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        cid = request.path_params["id"]
        item = db.get_content_item(cid, user["workspace_id"])
        if not item:
            return json_resp({"error": "NOT_FOUND", "message": "Không tìm thấy bài viết trong workspace của bạn."}, 404)
        return json_resp({"item": item})

    async def update_content(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        cid = request.path_params["id"]
        existing = db.get_content_item(cid, user["workspace_id"])
        if not existing:
            return json_resp({"error": "NOT_FOUND", "message": "Không tìm thấy bài viết."}, 404)

        body = await request.json()
        merged = dict(existing)
        for k in (
            "title", "body_html", "excerpt", "platform", "topic", "tone", "audience",
            "seo_title", "meta_description", "slug", "focus_keyphrase", "category",
            "featured_image", "featured_alt", "scheduled_at", "timezone", "recurrence_rule"
        ):
            if k in body:
                merged[k] = body[k]

        if "platform" in body and body["platform"] in PLATFORM_REGISTRY:
            merged["destination"] = PLATFORM_REGISTRY[body["platform"]].hostname

        if any(k in body for k in ("title", "body_html", "platform", "slug", "seo_title", "meta_description")):
            merged["reviewed"] = False
            merged["status"] = ContentStatus.PENDING_APPROVAL.value

        merged["validation"] = validate_content_item(merged, require_approval=False)
        updated = db.update_content_item(cid, user["workspace_id"], user["user_id"], merged)
        return json_resp({"item": updated})

    async def regenerate_content(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        cid = request.path_params["id"]
        existing = db.get_content_item(cid, user["workspace_id"])
        if not existing:
            return json_resp({"error": "NOT_FOUND"}, 404)

        body = await request.json()
        tone = body.get("tone") or existing.get("tone") or "thuyết phục, thu hút chuyển đổi"
        audience = body.get("audience") or existing.get("audience") or "khách hàng cá nhân & doanh nghiệp"
        regenerated = generate_content_draft(
            topic=existing.get("topic") or existing["title"],
            platform=existing["platform"],
            tone=tone,
            audience=audience,
            variant_index=1,
            scheduled_at_local=existing.get("scheduled_at"),
            timezone_name=existing.get("timezone", DEFAULT_TZ),
            recurrence_rule=existing.get("recurrence_rule"),
        )
        updated = db.update_content_item(cid, user["workspace_id"], user["user_id"], regenerated)
        return json_resp({"item": updated})

    async def approve_content_endpoint(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        cid = request.path_params["id"]
        try:
            approved = scheduler.approve_content(user["workspace_id"], user["user_id"], cid)
            return json_resp({"item": approved})
        except ValueError as exc:
            return json_resp({"error": "NOT_FOUND", "message": str(exc)}, 404)
        except PublishingError as exc:
            return json_resp({"error": exc.code, "message": exc.message}, 400)

    async def publish_content_endpoint(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        cid = request.path_params["id"]
        try:
            res = scheduler.publish_immediately(user["workspace_id"], user["user_id"], cid)
            return json_resp(res)
        except ValueError as exc:
            return json_resp({"error": "NOT_FOUND", "message": str(exc)}, 404)
        except PublishingError as exc:
            item = db.get_content_item(cid, user["workspace_id"])
            return json_resp({"error": exc.code, "message": exc.message, "item": item}, 400)

    async def schedule_content_endpoint(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        cid = request.path_params["id"]
        body = await request.json()
        scheduled_at = body.get("scheduled_at")
        tz_name = body.get("timezone") or DEFAULT_TZ
        recurrence = body.get("recurrence_rule")
        if not scheduled_at:
            return json_resp({"error": "MISSING_SCHEDULE_TIME", "message": "Vui lòng chọn ngày giờ đăng bài."}, 400)
        try:
            job = scheduler.schedule_content(
                workspace_id=user["workspace_id"],
                user_id=user["user_id"],
                content_id=cid,
                scheduled_at_str=scheduled_at,
                timezone_name=tz_name,
                recurrence_rule=recurrence,
            )
            item = db.get_content_item(cid, user["workspace_id"])
            return json_resp({"job": job, "item": item})
        except ValueError as exc:
            return json_resp({"error": "NOT_FOUND", "message": str(exc)}, 404)
        except PublishingError as exc:
            return json_resp({"error": exc.code, "message": exc.message}, 400)

    async def delete_content_endpoint(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        cid = request.path_params["id"]
        confirmed = request.query_params.get("confirmed", "").lower() == "true"
        if not confirmed:
            return json_resp(
                {"error": "CONFIRMATION_REQUIRED", "message": "Vui lòng xác nhận trước khi xóa bài viết (confirmed=true)."},
                400,
            )
        deleted = db.delete_content_item(cid, user["workspace_id"], user["user_id"])
        if not deleted:
            return json_resp({"error": "NOT_FOUND", "message": "Không tìm thấy bài viết cần xóa."}, 404)
        return json_resp({"deleted": True, "id": cid})

    async def list_jobs_endpoint(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        status = request.query_params.get("status")
        jobs = db.list_scheduled_jobs(user["workspace_id"], status=status)
        return json_resp({"jobs": jobs})

    async def reschedule_job_endpoint(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        job_id = request.path_params["job_id"]
        body = await request.json()
        new_time = body.get("scheduled_at")
        tz_name = body.get("timezone")
        if not new_time:
            return json_resp({"error": "MISSING_TIME", "message": "Thiếu thời gian lên lịch mới."}, 400)
        try:
            job = scheduler.reschedule_job(user["workspace_id"], user["user_id"], job_id, new_time, tz_name)
            return json_resp({"job": job})
        except ValueError as exc:
            return json_resp({"error": "NOT_FOUND", "message": str(exc)}, 404)
        except PublishingError as exc:
            return json_resp({"error": exc.code, "message": exc.message}, 400)

    async def cancel_job_endpoint(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        job_id = request.path_params["job_id"]
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        confirmed = bool(body.get("confirmed")) or request.query_params.get("confirmed", "").lower() == "true"
        try:
            job = scheduler.cancel_job(user["workspace_id"], user["user_id"], job_id, confirmed=confirmed)
            return json_resp({"job": job})
        except ValueError as exc:
            return json_resp({"error": "NOT_FOUND", "message": str(exc)}, 404)
        except PublishingError as exc:
            return json_resp({"error": exc.code, "message": exc.message}, 400)

    async def retry_job_endpoint(request: Request) -> JSONResponse:
        user = get_auth_user(request)
        if not user:
            return json_resp({"error": "UNAUTHORIZED"}, 401)
        job_id = request.path_params["job_id"]
        try:
            job = scheduler.retry_failed_job(user["workspace_id"], user["user_id"], job_id, execute_now=True)
            return json_resp({"job": job})
        except ValueError as exc:
            return json_resp({"error": "NOT_FOUND", "message": str(exc)}, 404)

    routes = [
        Route("/", index_page, methods=["GET"]),
        Route("/media/{slug}/{filename}", serve_media, methods=["GET"]),
        Route("/api/platforms", get_platforms, methods=["GET"]),
        Route("/api/mode", set_publish_mode, methods=["POST"]),
        Route("/api/chat", post_chat, methods=["POST"]),
        Route("/api/upload-docx", upload_docx_chat, methods=["POST"]),
        Route("/api/chat/history", get_chat_history, methods=["GET"]),
        Route("/api/content", list_contents, methods=["GET"]),
        Route("/api/content/{id}", get_content, methods=["GET"]),
        Route("/api/content/{id}", update_content, methods=["PUT"]),
        Route("/api/content/{id}", delete_content_endpoint, methods=["DELETE"]),
        Route("/api/content/{id}/regenerate", regenerate_content, methods=["POST"]),
        Route("/api/content/{id}/approve", approve_content_endpoint, methods=["POST"]),
        Route("/api/content/{id}/publish", publish_content_endpoint, methods=["POST"]),
        Route("/api/content/{id}/schedule", schedule_content_endpoint, methods=["POST"]),
        Route("/api/jobs", list_jobs_endpoint, methods=["GET"]),
        Route("/api/jobs/{job_id}/reschedule", reschedule_job_endpoint, methods=["POST"]),
        Route("/api/jobs/{job_id}/cancel", cancel_job_endpoint, methods=["POST"]),
        Route("/api/jobs/{job_id}/retry", retry_job_endpoint, methods=["POST"]),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app_instance: Starlette) -> AsyncIterator[None]:
        if start_worker:
            scheduler.start_background_worker()
        try:
            yield
        finally:
            if start_worker:
                scheduler.stop_background_worker()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.db = db
    app.state.scheduler = scheduler
    app.state.router = router
    app.state.chat_orchestrator = chat_orchestrator
    return app


app = create_app(start_worker=True)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8088"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
