"""Conversational AI Orchestrator connecting natural-language chat with content generation,
validation, preview, approval, immediate publishing, and persistent scheduling."""
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from publishing_app.database import Database
from publishing_app.models import ContentStatus, PublishingError
from publishing_app.nlp_agent import (
    DEFAULT_TZ,
    extract_intent_and_entities,
    generate_content_draft,
    parse_natural_schedule,
)
from publishing_app.scheduler import SchedulingService
from publishing_app.validator import PLATFORM_REGISTRY, import_from_docx_pipeline, slugify_vn, validate_content_item


class ChatOrchestrator:
    """Handles multi-turn natural language conversations in Vietnamese & English."""

    def __init__(self, db: Database, scheduler: SchedulingService):
        self.db = db
        self.scheduler = scheduler

    def handle_user_message(
        self,
        workspace_id: str,
        user_id: str,
        session_id: str,
        message: str,
        timezone_name: str = DEFAULT_TZ,
        active_content_id: Optional[str] = None,
        now_local: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        session = self.db.get_or_create_chat_session(session_id, workspace_id, user_id)
        if active_content_id:
            self.db.update_chat_session(session_id, workspace_id, active_content_id=active_content_id)
            session["active_content_id"] = active_content_id

        self.db.add_chat_message(session_id, workspace_id, user_id, "user", message)

        pending = session.get("pending_context") or {}
        extracted = extract_intent_and_entities(message, tz_name=timezone_name, now_local=now_local)

        # Merge with pending follow-up state if we were waiting for missing info
        if pending.get("awaiting_followup"):
            extracted = self._merge_pending_context(pending, extracted, message, timezone_name, now_local)

        intent = extracted["intent"]
        current_cid = active_content_id or session.get("active_content_id")

        # ── 1. Handle DOCX Import ──
        if intent == "import_docx" and extracted.get("docx_path"):
            return self._handle_import_docx(
                workspace_id,
                user_id,
                session_id,
                extracted["docx_path"],
                extracted["platforms"] or ["vtechfilm"],
                schedule_info=extracted.get("schedule"),
                timezone_name=timezone_name,
            )

        # ── 2. Handle Approval Request via Chat ──
        if intent == "approve_post":
            if not current_cid:
                return self._reply(
                    session_id, workspace_id, user_id,
                    "Hiện chưa có bài viết nào đang chọn để phê duyệt. Bạn muốn tạo bài viết mới về chủ đề gì?",
                    {"intent": "approve_post", "needs_followup": True},
                )
            try:
                approved = self.scheduler.approve_content(workspace_id, user_id, current_cid)
                # If the post already had a requested schedule, schedule it upon approval
                if approved.get("scheduled_at"):
                    job = self.scheduler.schedule_content(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        content_id=current_cid,
                        scheduled_at_str=approved["scheduled_at"],
                        timezone_name=approved.get("timezone", timezone_name),
                        recurrence_rule=approved.get("recurrence_rule"),
                    )
                    updated_content = self.db.get_content_item(current_cid, workspace_id)
                    return self._reply(
                        session_id, workspace_id, user_id,
                        f"✅ Đã phê duyệt và lên lịch đăng bài **{approved['title']}** vào lúc `{approved['scheduled_at']}` "
                        f"trên nền tảng **{approved['platform']}** (Mã lịch: `{job['id']}`).",
                        {"intent": "approve_and_schedule", "content": updated_content, "job": job},
                    )
                return self._reply(
                    session_id, workspace_id, user_id,
                    f"✅ Đã phê duyệt bài viết **{approved['title']}**. Bạn có thể bấm **Đăng ngay** hoặc chọn giờ **Lên lịch**.",
                    {"intent": "approve_post", "content": approved},
                )
            except PublishingError as exc:
                return self._reply(
                    session_id, workspace_id, user_id,
                    f"⚠️ {exc.message}",
                    {"intent": "approve_post", "error": exc.code},
                )

        # ── 3. Handle Immediate Publish via Chat ──
        if intent == "publish_now":
            if not current_cid:
                return self._reply(
                    session_id, workspace_id, user_id,
                    "Chưa có bài viết nào được chọn để đăng. Hãy tạo hoặc chọn một bài viết trước nhé.",
                    {"intent": "publish_now", "needs_followup": True},
                )
            try:
                res = self.scheduler.publish_immediately(workspace_id, user_id, current_cid)
                c = res["content"]
                return self._reply(
                    session_id, workspace_id, user_id,
                    f"🚀 Xuất bản thành công bài viết **{c['title']}**!\n- Link bài viết: {c['external_url']}",
                    {"intent": "publish_now", "content": c, "publish_result": res.get("publish_result")},
                )
            except PublishingError as exc:
                return self._reply(
                    session_id, workspace_id, user_id,
                    f"⚠️ Không thể đăng ngay: {exc.message}",
                    {"intent": "publish_now", "error": exc.code},
                )

        # ── 4. Handle Edit / Regenerate Existing Active Post ──
        if intent in ("edit_post", "regenerate_post") and current_cid:
            return self._handle_edit_or_regenerate(
                workspace_id, user_id, session_id, current_cid, intent, message, extracted
            )

        # ── 5. Handle Scheduling an Existing Active Post ──
        if intent == "schedule_post" and current_cid and not extracted.get("topic"):
            sched = extracted["schedule"]
            if sched.get("needs_time_clarification") or not sched.get("scheduled_at_local"):
                self.db.update_chat_session(
                    session_id,
                    workspace_id,
                    pending_context={
                        "awaiting_followup": True,
                        "intent": "schedule_post",
                        "missing_fields": ["schedule_time"],
                        "platforms": extracted["platforms"],
                    },
                )
                return self._reply(
                    session_id, workspace_id, user_id,
                    "Bạn muốn lên lịch đăng bài này vào ngày giờ nào? (Ví dụ: *9 giờ sáng mai*, hoặc *25/10/2026 14:00*)",
                    {"intent": "schedule_post", "needs_followup": True, "missing_fields": ["schedule_time"]},
                )

            content = self.db.get_content_item(current_cid, workspace_id)
            if not content:
                return self._reply(session_id, workspace_id, user_id, "Không tìm thấy bài viết hiện tại.", {})

            updates: Dict[str, Any] = {
                "scheduled_at": sched["scheduled_at_local"],
                "timezone": timezone_name,
                "recurrence_rule": sched.get("recurrence_rule"),
            }
            if extracted["primary_platform"]:
                updates["platform"] = extracted["primary_platform"]
                updates["destination"] = PLATFORM_REGISTRY[extracted["primary_platform"]].hostname

            updated = self.db.update_content_item(current_cid, workspace_id, user_id, updates)
            self.db.update_chat_session(session_id, workspace_id, pending_context={})

            if updated and updated.get("reviewed"):
                job = self.scheduler.schedule_content(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    content_id=current_cid,
                    scheduled_at_str=sched["scheduled_at_local"],
                    timezone_name=timezone_name,
                    recurrence_rule=sched.get("recurrence_rule"),
                )
                updated = self.db.get_content_item(current_cid, workspace_id)
                return self._reply(
                    session_id, workspace_id, user_id,
                    f"📅 Đã lên lịch đăng bài **{updated['title']}** vào lúc `{sched['human_summary']}` ({timezone_name}).",
                    {"intent": "schedule_post", "content": updated, "job": job},
                )

            return self._reply(
                session_id, workspace_id, user_id,
                f"📅 Đã ghi nhận thời gian dự kiến đăng: **{sched['human_summary']}** ({timezone_name}).\n"
                f"Vui lòng kiểm tra bản xem trước và bấm **Phê duyệt & Lên lịch** để xác nhận.",
                {"intent": "schedule_post", "content": updated, "requires_approval": True},
            )

        # ── 6. Handle Create Post / Create Multiple Posts / Recurring Campaign ──
        if extracted.get("unknown_spec_question"):
            return self._reply(
                session_id,
                workspace_id,
                user_id,
                f"📋 **Theo quy tắc dữ liệu VTECH FILM (`vtechfilm-seo`):**\n{extracted['unknown_spec_question']}",
                {
                    "intent": intent,
                    "needs_followup": True,
                    "missing_fields": ["product_spec"],
                },
            )

        topic = extracted.get("topic") or ""
        platforms = extracted.get("platforms") or []
        sched = extracted["schedule"]

        # If user said "Lên lịch đăng bài Facebook mỗi ngày trong tuần tới" without explicit topic,
        # infer a sensible default topic or ask if completely empty
        if not topic and sched.get("recurrence_rule") and platforms:
            topic = "chuỗi bài chia sẻ giải pháp chống nóng & bảo vệ kính VTECH FILM"
            extracted["topic"] = topic

        missing_fields: List[str] = []
        if not topic:
            missing_fields.append("topic")
        if not platforms:
            missing_fields.append("platform")

        if missing_fields:
            self.db.update_chat_session(
                session_id,
                workspace_id,
                pending_context={
                    "awaiting_followup": True,
                    "intent": intent,
                    "missing_fields": missing_fields,
                    "topic": topic,
                    "platforms": platforms,
                    "count": extracted["count"],
                    "tone": extracted["tone"],
                    "audience": extracted["audience"],
                    "schedule": sched,
                },
            )
            question = self._build_followup_question(missing_fields, topic, platforms)
            return self._reply(
                session_id,
                workspace_id,
                user_id,
                question,
                {
                    "intent": intent,
                    "needs_followup": True,
                    "missing_fields": missing_fields,
                    "partial_entities": extracted,
                },
            )

        # All required fields present -> Clear pending context and generate content(s)
        self.db.update_chat_session(session_id, workspace_id, pending_context={})
        created_items: List[Dict[str, Any]] = []
        count = extracted.get("count", 1)

        if len(platforms) > 1:
            # Multi-platform generation (e.g. both vtechfilm and onex with anti-duplication)
            for idx, plat in enumerate(platforms):
                draft_data = generate_content_draft(
                    topic=topic,
                    platform=plat,
                    tone=extracted["tone"],
                    audience=extracted["audience"],
                    variant_index=idx,
                    scheduled_at_local=sched.get("scheduled_at_local"),
                    timezone_name=timezone_name,
                    recurrence_rule=sched.get("recurrence_rule"),
                )
                saved = self.db.create_content_item(workspace_id, user_id, draft_data)
                created_items.append(saved)
        else:
            target_plat = platforms[0]
            for idx in range(count):
                draft_data = generate_content_draft(
                    topic=topic,
                    platform=target_plat,
                    tone=extracted["tone"],
                    audience=extracted["audience"],
                    variant_index=idx,
                    scheduled_at_local=sched.get("scheduled_at_local"),
                    timezone_name=timezone_name,
                    recurrence_rule=sched.get("recurrence_rule"),
                )
                saved = self.db.create_content_item(workspace_id, user_id, draft_data)
                created_items.append(saved)

        primary_item = created_items[0]
        self.db.update_chat_session(session_id, workspace_id, active_content_id=primary_item["id"])

        cap = PLATFORM_REGISTRY.get(primary_item["platform"])
        cap_warning = f"\n⚠️ *Lưu ý nền tảng:* {cap.limitations_note}" if cap and not cap.is_configured else ""
        sched_note = (
            f"\n📅 **Lịch dự kiến:** `{sched['human_summary']}` ({timezone_name})"
            if sched.get("scheduled_at_local")
            else ""
        )

        if len(created_items) == 1:
            reply_text = (
                f"✨ Đã tạo bản nháp bài viết cho **{cap.name if cap else primary_item['platform']}**:\n"
                f"- **Tiêu đề:** {primary_item['title']}\n"
                f"- **Slug:** `{primary_item['slug']}`\n"
                f"- **Điểm kiểm định Preflight:** `{primary_item['validation']['score']}/100` "
                f"({'Đạt chuẩn' if primary_item['validation']['passed'] else 'Cần chỉnh sửa'})"
                f"{sched_note}{cap_warning}\n\n"
                f"👉 Vui lòng xem bản **Preview** bên cạnh. Bạn có thể chỉnh sửa trực tiếp, yêu cầu viết lại, "
                f"hoặc bấm **Phê duyệt (Approve)** để đăng ngay / lên lịch."
            )
        else:
            titles_bullet = "\n".join(
                f"  {i + 1}. **{it['title']}** (`{it['platform']}` - `{it['slug']}`)"
                for i, it in enumerate(created_items)
            )
            reply_text = (
                f"✨ Đã tạo **{len(created_items)} bài viết chuẩn SEO** theo yêu cầu:\n"
                f"{titles_bullet}{sched_note}{cap_warning}\n\n"
                f"👉 Bạn có thể chọn từng bài ở danh sách bên phải để xem Preview, chỉnh sửa và **Phê duyệt** trước khi xuất bản."
            )

        return self._reply(
            session_id,
            workspace_id,
            user_id,
            reply_text,
            {
                "intent": intent,
                "needs_followup": False,
                "content": primary_item,
                "created_items": created_items,
                "extracted": extracted,
            },
        )

    def _merge_pending_context(
        self,
        pending: Dict[str, Any],
        current: Dict[str, Any],
        raw_message: str,
        tz_name: str,
        now_local: Optional[datetime],
    ) -> Dict[str, Any]:
        merged = dict(current)
        merged["intent"] = pending.get("intent", current["intent"])
        merged["count"] = pending.get("count", current["count"])
        merged["tone"] = pending.get("tone", current["tone"])
        merged["audience"] = pending.get("audience", current["audience"])

        if pending.get("topic") and not current.get("topic"):
            merged["topic"] = pending["topic"]
        elif "topic" in (pending.get("missing_fields") or []) and not current.get("topic"):
            # User replied directly with the topic name (e.g., "Camera an ninh gia đình")
            cleaned = re.sub(
                r"\b(đăng lên|trên|cho|website|vtechfilm|onex|facebook|fanpage)\b",
                "",
                raw_message,
                flags=re.IGNORECASE,
            ).strip(" .,?!")
            if cleaned:
                merged["topic"] = cleaned

        if pending.get("platforms") and not current.get("platforms"):
            merged["platforms"] = pending["platforms"]
            merged["primary_platform"] = pending["platforms"][0]

        prev_sched = pending.get("schedule") or {}
        if prev_sched.get("scheduled_at_local") and not current["schedule"].get("scheduled_at_local"):
            merged["schedule"] = prev_sched

        return merged

    @staticmethod
    def _build_followup_question(missing_fields: List[str], topic: str, platforms: List[str]) -> str:
        if "topic" in missing_fields and "platform" in missing_fields:
            return (
                "Để mình soạn bài chuẩn xác nhất, bạn cho mình biết thêm 2 thông tin ngắn gọn nhé:\n"
                "1. **Chủ đề bài viết** là gì? (VD: *Phim cách nhiệt văn phòng*, *Camera an ninh*...)\n"
                "2. **Nền tảng đăng** ở đâu? (*Website chính vtechfilm.com*, *Web vệ tinh OneX*, hay *Facebook*?)"
            )
        if "topic" in missing_fields:
            plat_str = ", ".join(platforms)
            return f"Bạn muốn viết bài cho **{plat_str}** về **chủ đề hoặc sản phẩm** nào?"
        return (
            f"Bạn muốn đăng bài viết về **\"{topic}\"** lên nền tảng nào?\n"
            f"- **Website chính** (`vtechfilm.com`)\n"
            f"- **Website vệ tinh** (`phimcachnhietdankinh.vn`)\n"
            f"- **Facebook Fanpage**"
        )

    def _handle_import_docx(
        self,
        workspace_id: str,
        user_id: str,
        session_id: str,
        docx_path: str,
        platforms: List[str],
        schedule_info: Optional[Dict[str, Any]] = None,
        timezone_name: str = DEFAULT_TZ,
        extra_image_paths: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        created_items: List[Dict[str, Any]] = []
        sched = schedule_info or {}
        try:
            for idx, plat in enumerate(platforms):
                draft = import_from_docx_pipeline(docx_path, platform=plat, extra_image_paths=extra_image_paths)
                if idx > 0 or plat == "onex":
                    # Differentiate title/slug for second website to pass preflight cross-site check
                    draft["title"] = f"{draft['title']} – Giải Pháp Thi Công Chuẩn Kỹ Thuật"
                    draft["slug"] = f"{draft['slug']}-chuyen-nghiep"
                    draft["seo_title"] = f"{draft['title'][:45]} | OneX"
                    draft["meta_description"] = (
                        f"Dịch vụ {draft['focus_keyphrase']} thi công thực tế, cản nhiệt hiệu quả. "
                        f"Gọi ngay 0967 725 714 để nhận báo giá chi tiết."
                    )[:158]
                draft["status"] = ContentStatus.PENDING_APPROVAL.value
                draft["reviewed"] = False
                if sched.get("scheduled_at_local"):
                    draft["scheduled_at"] = sched["scheduled_at_local"]
                    draft["timezone"] = timezone_name
                    draft["recurrence_rule"] = sched.get("recurrence_rule")
                draft["validation"] = validate_content_item(draft, require_approval=False, other_site_items=created_items)
                saved = self.db.create_content_item(workspace_id, user_id, draft)
                created_items.append(saved)

            primary = created_items[0]
            self.db.update_chat_session(session_id, workspace_id, active_content_id=primary["id"])
            sched_msg = (
                f"\n- ⏰ **Giờ hẹn đăng đã cài sẵn:** `{sched['human_summary']}` ({timezone_name})"
                if sched.get("scheduled_at_local")
                else ""
            )
            return self._reply(
                session_id,
                workspace_id,
                user_id,
                f"📂 Đã tự động xử lý xong file Word `.docx` sang **{len(created_items)} bản nháp chuẩn SEO** "
                f"({', '.join(platforms)}):\n"
                f"- **Tiêu đề H1:** {primary['title']}\n"
                f"- **Slug URL:** `{primary['slug']}`\n"
                f"- **Ảnh đã nén WebP (`<=1200px`):** `{len(primary.get('media', []))} ảnh`"
                f"{sched_msg}\n\n"
                f"👉 Bạn chỉ cần bấm nút **✅ Duyệt & Lên lịch / Đăng ngay** là hệ thống tự động hoàn tất toàn bộ!",
                {"intent": "import_docx", "content": primary, "created_items": created_items},
            )
        except Exception as exc:
            return self._reply(
                session_id,
                workspace_id,
                user_id,
                f"⚠️ Không thể đọc file DOCX `{docx_path}`: {exc}",
                {"intent": "import_docx", "error": "DOCX_IMPORT_FAILED"},
            )

    def _handle_edit_or_regenerate(
        self,
        workspace_id: str,
        user_id: str,
        session_id: str,
        content_id: str,
        intent: str,
        message: str,
        extracted: Dict[str, Any],
    ) -> Dict[str, Any]:
        existing = self.db.get_content_item(content_id, workspace_id)
        if not existing:
            return self._reply(session_id, workspace_id, user_id, "Không tìm thấy bài viết cần chỉnh sửa.", {})

        if intent == "regenerate_post":
            new_tone = extracted["tone"]
            regenerated = generate_content_draft(
                topic=existing["topic"] or existing["title"],
                platform=existing["platform"],
                tone=new_tone,
                audience=existing["audience"],
                variant_index=1,
                scheduled_at_local=existing.get("scheduled_at"),
                timezone_name=existing.get("timezone", DEFAULT_TZ),
                recurrence_rule=existing.get("recurrence_rule"),
            )
            regenerated["status"] = ContentStatus.PENDING_APPROVAL.value
            regenerated["reviewed"] = False
            updated = self.db.update_content_item(content_id, workspace_id, user_id, regenerated)
            return self._reply(
                session_id,
                workspace_id,
                user_id,
                f"🔄 Đã viết lại bài viết với giọng văn **{new_tone}**: **{updated['title']}**. Mời bạn xem lại Preview!",
                {"intent": "regenerate_post", "content": updated},
            )

        # Edit title or content via chat command
        m_new_title = re.search(r"(?:thành|là|to|:)\s*:?\s*[\"']?(.+?)[\"']?$", message, re.IGNORECASE)
        new_title = m_new_title.group(1).strip(" :\"'") if m_new_title else f"{existing['title']} (Đã cập nhật)"
        new_slug = slugify_vn(new_title)
        temp = dict(existing)
        temp["title"] = new_title
        temp["slug"] = new_slug
        temp["reviewed"] = False
        temp["status"] = ContentStatus.PENDING_APPROVAL.value
        temp["validation"] = validate_content_item(temp, require_approval=False)

        updated = self.db.update_content_item(
            content_id,
            workspace_id,
            user_id,
            {
                "title": new_title,
                "slug": new_slug,
                "reviewed": False,
                "status": ContentStatus.PENDING_APPROVAL.value,
                "validation": temp["validation"],
            },
        )
        return self._reply(
            session_id,
            workspace_id,
            user_id,
            f"✏️ Đã cập nhật tiêu đề bài viết thành: **{new_title}** (`{new_slug}`). Vui lòng duyệt lại trước khi đăng.",
            {"intent": "edit_post", "content": updated},
        )

    def _reply(
        self,
        session_id: str,
        workspace_id: str,
        user_id: str,
        text: str,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        msg = self.db.add_chat_message(session_id, workspace_id, user_id, "assistant", text, metadata)
        return {
            "reply": msg["message"],
            "message_id": msg["id"],
            "session_id": session_id,
            **metadata,
        }
