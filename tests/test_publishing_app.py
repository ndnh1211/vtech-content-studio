"""Comprehensive automated test suite for the AI Chat-Based Content Publishing & Scheduling System.
Covers all 9 required test categories without modifying production data or publishing live posts.
Run with: python -m unittest discover -s tests -v
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.testclient import TestClient

from publishing_app.chat_service import ChatOrchestrator
from publishing_app.database import Database, redact_secrets
from publishing_app.models import ContentStatus, JobStatus, PublishingError
from publishing_app.nlp_agent import extract_intent_and_entities, get_zoneinfo, parse_natural_schedule
from publishing_app.publishers import PublisherRouter
from publishing_app.scheduler import SchedulingService
from publishing_app.server import create_app
from publishing_app.validator import validate_content_item


class TestPublishingApplication(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(prefix="vtech_test_")
        self.db_path = Path(self.tmp_dir.name) / "test_publishing.db"
        self.db = Database(db_path=self.db_path)
        self.router = PublisherRouter(live_mode=False)
        self.scheduler = SchedulingService(db=self.db, publisher_router=self.router)
        self.chat = ChatOrchestrator(db=self.db, scheduler=self.scheduler)
        self.ws_main = "ws_vtech_main"
        self.user_main = "user_vtech_admin"
        self.ws_other = "ws_agency_isolated"
        self.user_other = "user_agency_other"

    def tearDown(self) -> None:
        self.scheduler.stop_background_worker()
        self.tmp_dir.cleanup()

    # ── 1. Natural-Language Intent Extraction (VN & EN) ──

    def test_01_natural_language_intent_extraction(self) -> None:
        ref_now = datetime(2026, 10, 23, 10, 0, tzinfo=get_zoneinfo("Asia/Ho_Chi_Minh"))

        # Prompt 1: "Viết bài giới thiệu sản phẩm mới và đăng lên Facebook."
        res1 = extract_intent_and_entities(
            "Viết bài giới thiệu sản phẩm mới và đăng lên Facebook.",
            now_local=ref_now,
        )
        self.assertEqual(res1["intent"], "create_post")
        self.assertIn("facebook", res1["platforms"])
        self.assertIn("sản phẩm mới", res1["topic"].lower())

        # Prompt 2: "Tạo 3 bài SEO cho website về camera an ninh."
        res2 = extract_intent_and_entities(
            "Tạo 3 bài SEO cho website về camera an ninh.",
            now_local=ref_now,
        )
        self.assertEqual(res2["intent"], "create_multiple_posts")
        self.assertEqual(res2["count"], 3)
        self.assertIn("vtechfilm", res2["platforms"])
        self.assertIn("camera an ninh", res2["topic"].lower())

        # Prompt 3: "Đăng bài này lên website vào 9 giờ sáng mai."
        res3 = extract_intent_and_entities(
            "Đăng bài này lên website vào 9 giờ sáng mai.",
            now_local=ref_now,
        )
        self.assertEqual(res3["intent"], "schedule_post")
        self.assertTrue(res3["schedule"]["has_schedule"])
        self.assertIn("2026-10-24T09:00:00", res3["schedule"]["scheduled_at_local"])

        # Prompt 4: "Lên lịch đăng bài Facebook mỗi ngày trong tuần tới."
        res4 = extract_intent_and_entities(
            "Lên lịch đăng bài Facebook mỗi ngày trong tuần tới.",
            now_local=ref_now,
        )
        self.assertIn("facebook", res4["platforms"])
        self.assertTrue(res4["schedule"]["has_schedule"])
        self.assertEqual(res4["schedule"]["recurrence_rule"], "DAILY_7")

        # Prompt 5: English intent extraction
        res5 = extract_intent_and_entities(
            "Create a post about nano ceramic window film for website tomorrow at 2:30pm.",
            now_local=ref_now,
        )
        self.assertEqual(res5["intent"], "create_post")
        self.assertIn("vtechfilm", res5["platforms"])
        self.assertIn("nano ceramic window film", res5["topic"].lower())
        self.assertIn("2026-10-24T14:30:00", res5["schedule"]["scheduled_at_local"])

    # ── 2. Missing-Information Follow-Up (Multi-Turn State Machine) ──

    def test_02_missing_information_followup_flow(self) -> None:
        session_id = "sess_followup_test"

        # Step 1: User asks to write a post about a topic but omits the platform
        turn1 = self.chat.handle_user_message(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            session_id=session_id,
            message="Viết bài chuẩn SEO về phim cách nhiệt chung cư cao cấp.",
        )
        self.assertTrue(turn1["needs_followup"])
        self.assertIn("platform", turn1["missing_fields"])
        self.assertIsNone(turn1.get("content"))

        # Step 2: User replies with only the missing platform -> AI merges context and generates the post
        turn2 = self.chat.handle_user_message(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            session_id=session_id,
            message="Đăng lên website chính vtechfilm nhé",
        )
        self.assertFalse(turn2["needs_followup"])
        self.assertIsNotNone(turn2.get("content"))
        content = turn2["content"]
        self.assertEqual(content["platform"], "vtechfilm")
        self.assertIn("phim cách nhiệt chung cư", content["title"].lower())
        self.assertEqual(content["status"], ContentStatus.PENDING_APPROVAL.value)
        # Verify vtechfilm-seo skill data is present in generated body (Central Premium Quận 8 / VT0595)
        self.assertIn("Central Premium", content["body_html"])
        self.assertIn("VT0595", content["body_html"])

        # Step 3: Verify vtechfilm-seo '?' rule: asking about VT4586 asks user instead of fabricating specs
        turn_unknown = self.chat.handle_user_message(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            session_id="sess_vt4586",
            message="Viết bài chi tiết về mã phim VT4586 cho website chính.",
        )
        self.assertTrue(turn_unknown["needs_followup"])
        self.assertIn("product_spec", turn_unknown["missing_fields"])
        self.assertIn("VT4586", turn_unknown["reply"])

    # ── 3. Content Generation, Multi-Post SEO & Editing ──

    def test_03_content_generation_and_editing(self) -> None:
        session_id = "sess_gen_edit"

        # Generate 3 SEO posts for website about security cameras
        res = self.chat.handle_user_message(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            session_id=session_id,
            message="Tạo 3 bài SEO cho website về camera an ninh.",
        )
        created = res.get("created_items", [])
        self.assertEqual(len(created), 3)

        # Ensure all 3 generated posts have unique titles & slugs and pass preflight validation
        slugs = {it["slug"] for it in created}
        titles = {it["title"] for it in created}
        self.assertEqual(len(slugs), 3)
        self.assertEqual(len(titles), 3)
        for it in created:
            self.assertTrue(it["validation"]["passed"], f"Validation failed: {it['validation']}")

        # Edit title via chat and verify slug + validation update
        first_id = created[0]["id"]
        edit_res = self.chat.handle_user_message(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            session_id=session_id,
            active_content_id=first_id,
            message="Sửa tiêu đề thành: Lắp Đặt Camera An Ninh Gia Đình Chuẩn Kỹ Thuật 2026",
        )
        updated = edit_res["content"]
        self.assertEqual(updated["title"], "Lắp Đặt Camera An Ninh Gia Đình Chuẩn Kỹ Thuật 2026")
        self.assertEqual(updated["slug"], "lap-dat-camera-an-ninh-gia-dinh-chuan-ky-thuat-2026")
        self.assertFalse(updated["reviewed"])

        # Regenerate with new tone
        regen_res = self.chat.handle_user_message(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            session_id=session_id,
            active_content_id=first_id,
            message="Viết lại bài này với giọng văn thuyết phục bán hàng",
        )
        self.assertIn("thuyết phục", regen_res["content"]["tone"])

    # ── 4. Preview & Approval Flow + Preflight Enforcement ──

    def test_04_preview_and_approval_enforcement(self) -> None:
        res = self.chat.handle_user_message(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            session_id="sess_approval",
            message="Viết bài về phim cách nhiệt văn phòng cho website chính.",
        )
        cid = res["content"]["id"]

        # Attempting to publish or schedule BEFORE approval must raise APPROVAL_REQUIRED
        with self.assertRaises(PublishingError) as ctx_pub:
            self.scheduler.publish_immediately(self.ws_main, self.user_main, cid)
        self.assertEqual(ctx_pub.exception.code, "APPROVAL_REQUIRED")

        with self.assertRaises(PublishingError) as ctx_sched:
            self.scheduler.schedule_content(self.ws_main, self.user_main, cid, "2026-10-25T09:00:00")
        self.assertEqual(ctx_sched.exception.code, "APPROVAL_REQUIRED")

        # Inject invalid hotline number into HTML -> Approval must reject with VALIDATION_FAILED
        self.db.update_content_item(
            cid,
            self.ws_main,
            self.user_main,
            {"body_html": "<p>Liên hệ số lạ 0912 345 678 để mua hàng.</p>"},
        )
        with self.assertRaises(PublishingError) as ctx_val:
            self.scheduler.approve_content(self.ws_main, self.user_main, cid)
        self.assertEqual(ctx_val.exception.code, "VALIDATION_FAILED")

        # Restore valid hotline and approve successfully
        self.db.update_content_item(
            cid,
            self.ws_main,
            self.user_main,
            {"body_html": "<p>Giải pháp phim cách nhiệt văn phòng chính hãng. Liên hệ 0967 725 714.</p>"},
        )
        approved = self.scheduler.approve_content(self.ws_main, self.user_main, cid)
        self.assertTrue(approved["reviewed"])
        self.assertEqual(approved["status"], ContentStatus.APPROVED.value)

    # ── 5. Immediate Publishing & Anti-Fabrication Guarantee ──

    def test_05_immediate_publishing_and_truthfulness(self) -> None:
        res = self.chat.handle_user_message(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            session_id="sess_imm",
            message="Viết bài về dán kính chống nắng nhà ở cho website chính.",
        )
        cid = res["content"]["id"]
        self.scheduler.approve_content(self.ws_main, self.user_main, cid)

        pub_out = self.scheduler.publish_immediately(self.ws_main, self.user_main, cid)
        self.assertFalse(pub_out["already_published"])
        self.assertEqual(pub_out["content"]["status"], ContentStatus.PUBLISHED.value)
        self.assertTrue(pub_out["content"]["external_url"].startswith("https://vtechfilm.com/"))

        # Calling publish_immediately a second time is idempotent and does NOT call the API twice
        call_count_before = len(self.scheduler.publish_call_log)
        pub_out_again = self.scheduler.publish_immediately(self.ws_main, self.user_main, cid)
        self.assertTrue(pub_out_again["already_published"])
        self.assertEqual(len(self.scheduler.publish_call_log), call_count_before)

        # Verify unconfigured Facebook in live_mode NEVER fabricates a published result
        live_router = PublisherRouter(live_mode=True)
        live_sched = SchedulingService(db=self.db, publisher_router=live_router)
        fb_res = self.chat.handle_user_message(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            session_id="sess_fb_live",
            message="Viết bài giới thiệu sản phẩm mới và đăng lên Facebook.",
        )
        fb_cid = fb_res["content"]["id"]
        live_sched.approve_content(self.ws_main, self.user_main, fb_cid)
        with self.assertRaises(PublishingError) as fb_err:
            live_sched.publish_immediately(self.ws_main, self.user_main, fb_cid)
        self.assertEqual(fb_err.exception.code, "UNSUPPORTED_CAPABILITY")
        failed_fb = self.db.get_content_item(fb_cid, self.ws_main)
        self.assertEqual(failed_fb["status"], ContentStatus.FAILED.value)
        self.assertIsNone(failed_fb["external_url"])

    # ── 6. Scheduled Publishing, Timezone Handling & Recurring Schedules ──

    def test_06_scheduled_publishing_timezones_and_recurrence(self) -> None:
        res = self.chat.handle_user_message(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            session_id="sess_sched",
            message="Viết bài về phim cách nhiệt ô tô cho website chính.",
        )
        cid = res["content"]["id"]
        self.scheduler.approve_content(self.ws_main, self.user_main, cid)

        # Schedule for 2026-10-24 09:00:00 in Asia/Ho_Chi_Minh (which is 2026-10-24 02:00:00 UTC)
        job = self.scheduler.schedule_content(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            content_id=cid,
            scheduled_at_str="2026-10-24T09:00:00",
            timezone_name="Asia/Ho_Chi_Minh",
            recurrence_rule="DAILY_7",
        )
        self.assertEqual(job["scheduled_at_utc"], "2026-10-24T02:00:00+00:00")
        self.assertEqual(job["status"], JobStatus.QUEUED.value)

        # Before due time (01:30 UTC): job must NOT run
        before_due = datetime(2026, 10, 24, 1, 30, tzinfo=timezone.utc)
        processed_early = self.scheduler.process_due_jobs(now_utc=before_due)
        self.assertEqual(len(processed_early), 0)

        # At due time (02:00 UTC): job executes, publishes content, and spawns next recurring job (DAILY_6)
        at_due = datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc)
        processed_due = self.scheduler.process_due_jobs(now_utc=at_due)
        self.assertEqual(len(processed_due), 1)
        self.assertEqual(processed_due[0]["status"], JobStatus.SUCCEEDED.value)

        all_jobs = self.db.list_scheduled_jobs(self.ws_main)
        self.assertEqual(len(all_jobs), 2)
        next_job = [j for j in all_jobs if j["id"] != job["id"]][0]
        self.assertEqual(next_job["status"], JobStatus.QUEUED.value)
        self.assertEqual(next_job["recurrence_rule"], "DAILY_6")
        self.assertEqual(next_job["scheduled_at_utc"], "2026-10-25T02:00:00+00:00")

    # ── 7. Restart Recovery & Duplicate Prevention (Idempotency) ──

    def test_07_restart_recovery_and_duplicate_prevention(self) -> None:
        res = self.chat.handle_user_message(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            session_id="sess_restart",
            message="Viết bài về phim cách nhiệt nhà kính cho website chính.",
        )
        cid = res["content"]["id"]
        self.scheduler.approve_content(self.ws_main, self.user_main, cid)

        job1 = self.scheduler.schedule_content(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            content_id=cid,
            scheduled_at_str="2026-10-26T09:00:00",
            timezone_name="Asia/Ho_Chi_Minh",
        )
        # Scheduling the exact same content/platform/time again returns the existing job (no duplicate)
        job_dup = self.scheduler.schedule_content(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            content_id=cid,
            scheduled_at_str="2026-10-26T09:00:00",
            timezone_name="Asia/Ho_Chi_Minh",
        )
        self.assertEqual(job1["id"], job_dup["id"])

        # Simulate worker crash while job is in 'running' state BEFORE publishing finished
        self.db.update_job_status(job1["id"], self.ws_main, {"status": JobStatus.RUNNING.value})

        # Simulate application restart by instantiating a brand new Database & SchedulingService on the same SQLite file
        restarted_db = Database(db_path=self.db_path)
        restarted_scheduler = SchedulingService(db=restarted_db, publisher_router=PublisherRouter(live_mode=False))
        recovered = restarted_scheduler.recover_on_startup()
        self.assertEqual(recovered, 1)

        requeued_job = restarted_db.get_scheduled_job(job1["id"], self.ws_main)
        self.assertEqual(requeued_job["status"], JobStatus.QUEUED.value)

        # Now execute the recovered job at due time
        due_dt = datetime(2026, 10, 26, 2, 5, tzinfo=timezone.utc)
        restarted_scheduler.process_due_jobs(now_utc=due_dt)
        self.assertEqual(len(restarted_scheduler.publish_call_log), 1)

        # Simulate a second crash where content WAS already published, and job was stuck in 'running'
        self.db.update_job_status(job1["id"], self.ws_main, {"status": JobStatus.RUNNING.value})
        restarted_scheduler.recover_on_startup()
        restarted_scheduler.process_due_jobs(now_utc=due_dt)
        # Must NOT have published a second time!
        self.assertEqual(len(restarted_scheduler.publish_call_log), 1)

    # ── 8. Auth Expiry, Rate Limits, Network Errors & Retries ──

    def test_08_auth_expiry_api_errors_and_retries(self) -> None:
        res = self.chat.handle_user_message(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            session_id="sess_errors",
            message="Viết bài về phim cách nhiệt chống tia UV cho website chính.",
        )
        cid = res["content"]["id"]
        self.scheduler.approve_content(self.ws_main, self.user_main, cid)

        job = self.scheduler.schedule_content(
            workspace_id=self.ws_main,
            user_id=self.user_main,
            content_id=cid,
            scheduled_at_str="2026-10-27T09:00:00",
            timezone_name="Asia/Ho_Chi_Minh",
            max_retries=3,
        )
        due_time = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)

        # Case A: Inject retryable RateLimitError -> job stays queued with retry_count=1 and next_retry_at_utc
        self.router.inject_fault(cid, "rate_limit")
        self.scheduler.process_due_jobs(now_utc=due_time)
        j_after_rl = self.db.get_scheduled_job(job["id"], self.ws_main)
        self.assertEqual(j_after_rl["status"], JobStatus.QUEUED.value)
        self.assertEqual(j_after_rl["retry_count"], 1)
        self.assertEqual(j_after_rl["last_error_code"], "RATE_LIMIT")
        self.assertIsNotNone(j_after_rl["next_retry_at_utc"])

        # Case B: Inject non-retryable AuthExpiredError -> job transitions immediately to FAILED
        self.router.inject_fault(cid, "auth_expired")
        retry_due = due_time + timedelta(seconds=60)
        self.scheduler.process_due_jobs(now_utc=retry_due)
        j_after_auth = self.db.get_scheduled_job(job["id"], self.ws_main)
        self.assertEqual(j_after_auth["status"], JobStatus.FAILED.value)
        self.assertEqual(j_after_auth["last_error_code"], "AUTH_EXPIRED")

        # Case C: Clear fault and trigger manual retry -> succeeds cleanly
        self.router.inject_fault(cid, None)
        retried_job = self.scheduler.retry_failed_job(self.ws_main, self.user_main, job["id"], execute_now=True)
        self.assertEqual(retried_job["status"], JobStatus.SUCCEEDED.value)
        self.assertTrue(retried_job["external_url"].startswith("https://vtechfilm.com/"))

    # ── 9. User/Workspace Isolation, Destructive Confirmations & Secret Redaction ──

    def test_09_workspace_authorization_and_security(self) -> None:
        app = create_app(db_path=self.db_path, live_mode=False, start_worker=False)
        client = TestClient(app)

        main_headers = {"Authorization": "Bearer vtech-session-main-2026"}
        other_headers = {"Authorization": "Bearer agency-session-isolated-2026"}

        # 1. Unauthenticated access must be rejected with 401
        unauth_resp = client.get("/api/content")
        self.assertEqual(unauth_resp.status_code, 401)

        # 2. Create content in VTECH Main Workspace
        chat_resp = client.post(
            "/api/chat",
            headers=main_headers,
            json={"message": "Viết bài về phim cách nhiệt văn phòng cho website chính."},
        )
        self.assertEqual(chat_resp.status_code, 200)
        cid = chat_resp.json()["content"]["id"]

        # 3. Other Workspace user cannot view, edit, approve, or delete VTECH Main Workspace's content
        other_list = client.get("/api/content", headers=other_headers).json()["items"]
        self.assertEqual(len(other_list), 0)

        other_get = client.get(f"/api/content/{cid}", headers=other_headers)
        self.assertEqual(other_get.status_code, 404)

        other_approve = client.post(f"/api/content/{cid}/approve", headers=other_headers)
        self.assertEqual(other_approve.status_code, 404)

        # 4. Destructive delete without confirmation must be rejected with 400 CONFIRMATION_REQUIRED
        del_unconfirmed = client.delete(f"/api/content/{cid}", headers=main_headers)
        self.assertEqual(del_unconfirmed.status_code, 400)
        self.assertEqual(del_unconfirmed.json()["error"], "CONFIRMATION_REQUIRED")

        # 5. Secret redaction check: tokens, cookies, and API keys are never leaked in text/chat responses
        leaked_str = (
            "Lỗi kết nối với Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 "
            "và wordpress_logged_in_12345abc=secret_cookie_value_999 "
            "cùng access_token=EAABsbCS1iHgBO1234567890"
        )
        sanitized = redact_secrets(leaked_str)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", sanitized)
        self.assertNotIn("secret_cookie_value_999", sanitized)
        self.assertNotIn("EAABsbCS1iHgBO1234567890", sanitized)
        self.assertIn("[REDACTED]", sanitized)


if __name__ == "__main__":
    unittest.main()
