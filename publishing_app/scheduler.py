"""Persistent job scheduler, background worker, retry manager, and idempotency coordinator."""
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from publishing_app.database import Database, utc_now_iso
from publishing_app.models import (
    ContentStatus,
    JobStatus,
    PublishingError,
    RateLimitError,
)
from publishing_app.nlp_agent import DEFAULT_TZ, get_zoneinfo
from publishing_app.publishers import PublisherRouter
from publishing_app.validator import validate_content_item


class SchedulingService:
    """Manages immediate publishing, scheduled jobs, recurring schedules, retries, and crash recovery."""

    def __init__(
        self,
        db: Database,
        publisher_router: Optional[PublisherRouter] = None,
        poll_interval_seconds: float = 2.0,
    ):
        self.db = db
        self.router = publisher_router or PublisherRouter(live_mode=False)
        self.poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self.publish_call_log: List[Dict[str, Any]] = []  # Tracks exact publish calls for test verification

    # ── Startup & Background Worker ──

    def recover_on_startup(self, now_utc: Optional[datetime] = None) -> int:
        now_iso = (now_utc or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
        return self.db.recover_stalled_jobs(now_iso)

    def start_background_worker(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self.recover_on_startup()
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, name="PublishingSchedulerWorker", daemon=True)
        self._worker_thread.start()

    def stop_background_worker(self) -> None:
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.process_due_jobs()
            except Exception:
                pass
            self._stop_event.wait(self.poll_interval_seconds)

    # ── Approval & Immediate Publishing ──

    def approve_content(
        self,
        workspace_id: str,
        user_id: str,
        content_id: str,
    ) -> Dict[str, Any]:
        content = self.db.get_content_item(content_id, workspace_id)
        if not content:
            raise ValueError("Không tìm thấy bài viết hoặc bạn không có quyền truy cập.")

        # Validate content fields first
        temp_item = dict(content)
        temp_item["reviewed"] = True
        report = validate_content_item(temp_item, require_approval=True)
        if not report["passed"]:
            self.db.update_content_item(
                content_id,
                workspace_id,
                user_id,
                {"validation": report},
            )
            raise PublishingError(
                "Chưa thể phê duyệt vì bài viết còn lỗi kiểm định: " + "; ".join(report["fails"]),
                code="VALIDATION_FAILED",
            )

        updated = self.db.update_content_item(
            content_id,
            workspace_id,
            user_id,
            {
                "reviewed": True,
                "approved_by": user_id,
                "approved_at": utc_now_iso(),
                "status": ContentStatus.APPROVED.value,
                "validation": report,
                "error_code": None,
                "error_message": None,
            },
        )
        return updated  # type: ignore

    def publish_immediately(
        self,
        workspace_id: str,
        user_id: str,
        content_id: str,
    ) -> Dict[str, Any]:
        content = self.db.get_content_item(content_id, workspace_id)
        if not content:
            raise ValueError("Không tìm thấy bài viết trong workspace hiện tại.")

        # Idempotency check: if already published with external_url, return existing result
        if content["status"] == ContentStatus.PUBLISHED.value and content.get("external_url"):
            return {
                "already_published": True,
                "content": content,
                "external_url": content["external_url"],
            }

        if not content.get("reviewed"):
            raise PublishingError(
                "Cần phê duyệt bài viết (Approve) trước khi đăng ngay.",
                code="APPROVAL_REQUIRED",
            )

        self.db.update_content_item(
            content_id,
            workspace_id,
            user_id,
            {"status": ContentStatus.PUBLISHING.value, "error_code": None, "error_message": None},
        )

        try:
            pub_res = self.router.publish_content(content)
            self.publish_call_log.append({
                "content_id": content_id,
                "platform": content["platform"],
                "timestamp": utc_now_iso(),
            })
            updated = self.db.update_content_item(
                content_id,
                workspace_id,
                user_id,
                {
                    "status": ContentStatus.PUBLISHED.value,
                    "external_post_id": pub_res["external_post_id"],
                    "external_url": pub_res["external_url"],
                    "error_code": None,
                    "error_message": None,
                },
            )
            self.db.log_audit(
                workspace_id,
                user_id,
                "publish_immediately_success",
                content_id,
                {"external_url": pub_res["external_url"]},
            )
            return {
                "already_published": False,
                "content": updated,
                "publish_result": pub_res,
            }
        except PublishingError as exc:
            updated = self.db.update_content_item(
                content_id,
                workspace_id,
                user_id,
                {
                    "status": ContentStatus.FAILED.value,
                    "error_code": exc.code,
                    "error_message": exc.message,
                },
            )
            self.db.log_audit(
                workspace_id,
                user_id,
                "publish_immediately_failed",
                content_id,
                {"error_code": exc.code, "error_message": exc.message},
            )
            raise

    # ── Scheduling & Recurring Jobs ──

    @staticmethod
    def normalize_schedule_times(
        scheduled_at_str: str,
        timezone_name: str = DEFAULT_TZ,
    ) -> Dict[str, str]:
        tz = get_zoneinfo(timezone_name)
        dt = datetime.fromisoformat(scheduled_at_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            local_dt = dt.replace(tzinfo=tz)
        else:
            local_dt = dt.astimezone(tz)
        utc_dt = local_dt.astimezone(timezone.utc).replace(microsecond=0)
        return {
            "scheduled_at_local": local_dt.replace(microsecond=0).isoformat(),
            "scheduled_at_utc": utc_dt.isoformat(),
            "timezone": timezone_name,
        }

    def schedule_content(
        self,
        workspace_id: str,
        user_id: str,
        content_id: str,
        scheduled_at_str: str,
        timezone_name: str = DEFAULT_TZ,
        recurrence_rule: Optional[str] = None,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        content = self.db.get_content_item(content_id, workspace_id)
        if not content:
            raise ValueError("Không tìm thấy bài viết trong workspace hiện tại.")

        if not content.get("reviewed"):
            raise PublishingError(
                "Vui lòng phê duyệt bài viết (Approve) trước khi lên lịch đăng.",
                code="APPROVAL_REQUIRED",
            )

        norm = self.normalize_schedule_times(scheduled_at_str, timezone_name)
        job = self.db.create_scheduled_job(
            workspace_id=workspace_id,
            user_id=user_id,
            content_id=content_id,
            platform=content["platform"],
            scheduled_at_utc=norm["scheduled_at_utc"],
            scheduled_at_local=norm["scheduled_at_local"],
            timezone_name=timezone_name,
            recurrence_rule=recurrence_rule,
            max_retries=max_retries,
        )

        self.db.update_content_item(
            content_id,
            workspace_id,
            user_id,
            {
                "status": ContentStatus.SCHEDULED.value,
                "scheduled_at": norm["scheduled_at_local"],
                "timezone": timezone_name,
                "recurrence_rule": recurrence_rule,
                "error_code": None,
                "error_message": None,
            },
        )
        return job

    def reschedule_job(
        self,
        workspace_id: str,
        user_id: str,
        job_id: str,
        new_scheduled_at_str: str,
        timezone_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        job = self.db.get_scheduled_job(job_id, workspace_id)
        if not job:
            raise ValueError("Không tìm thấy lịch đăng cần đổi giờ.")
        if job["status"] == JobStatus.SUCCEEDED.value:
            raise PublishingError("Lịch đăng này đã hoàn tất, không thể đổi giờ.", code="ALREADY_PUBLISHED")

        tz_name = timezone_name or job.get("timezone", DEFAULT_TZ)
        norm = self.normalize_schedule_times(new_scheduled_at_str, tz_name)
        new_idem = self.db.build_idempotency_key(
            workspace_id, job["content_id"], job["platform"], norm["scheduled_at_utc"]
        )

        updated_job = self.db.update_job_status(
            job_id,
            workspace_id,
            {
                "scheduled_at_utc": norm["scheduled_at_utc"],
                "scheduled_at_local": norm["scheduled_at_local"],
                "timezone": tz_name,
                "idempotency_key": new_idem,
                "status": JobStatus.QUEUED.value,
                "next_retry_at_utc": None,
                "last_error_code": None,
                "last_error_message": None,
            },
        )
        self.db.update_content_item(
            job["content_id"],
            workspace_id,
            user_id,
            {
                "status": ContentStatus.SCHEDULED.value,
                "scheduled_at": norm["scheduled_at_local"],
                "timezone": tz_name,
            },
        )
        self.db.log_audit(workspace_id, user_id, "reschedule_job", job_id, norm)
        return updated_job  # type: ignore

    def cancel_job(
        self,
        workspace_id: str,
        user_id: str,
        job_id: str,
        confirmed: bool = False,
    ) -> Dict[str, Any]:
        if not confirmed:
            raise PublishingError(
                "Hành động hủy lịch đăng cần xác nhận tường minh (confirmed=true).",
                code="CONFIRMATION_REQUIRED",
            )
        job = self.db.get_scheduled_job(job_id, workspace_id)
        if not job:
            raise ValueError("Không tìm thấy lịch đăng trong workspace hiện tại.")

        updated_job = self.db.update_job_status(
            job_id,
            workspace_id,
            {"status": JobStatus.CANCELLED.value},
        )
        self.db.update_content_item(
            job["content_id"],
            workspace_id,
            user_id,
            {"status": ContentStatus.APPROVED.value, "scheduled_at": None},
        )
        self.db.log_audit(workspace_id, user_id, "cancel_job", job_id, {"confirmed": True})
        return updated_job  # type: ignore

    def retry_failed_job(
        self,
        workspace_id: str,
        user_id: str,
        job_id: str,
        execute_now: bool = True,
    ) -> Dict[str, Any]:
        job = self.db.get_scheduled_job(job_id, workspace_id)
        if not job:
            raise ValueError("Không tìm thấy lịch đăng cần thử lại.")
        if job["status"] == JobStatus.SUCCEEDED.value:
            return job

        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now_iso = now_dt.isoformat()
        job_updates: Dict[str, Any] = {
            "status": JobStatus.QUEUED.value,
            "retry_count": 0,
            "next_retry_at_utc": now_iso,
            "last_error_code": None,
            "last_error_message": None,
        }
        if execute_now:
            job_updates["scheduled_at_utc"] = now_iso

        self.db.update_job_status(job_id, workspace_id, job_updates)
        self.db.update_content_item(
            job["content_id"],
            workspace_id,
            user_id,
            {
                "status": ContentStatus.SCHEDULED.value,
                "error_code": None,
                "error_message": None,
            },
        )
        if execute_now:
            self.process_due_jobs(now_dt)
        return self.db.get_scheduled_job(job_id, workspace_id)  # type: ignore

    # ── Job Processing Engine ──

    def process_due_jobs(self, now_utc: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Processes all due jobs atomically, handling idempotency, retries, and recurring schedules."""
        current_utc = (now_utc or datetime.now(timezone.utc)).replace(microsecond=0)
        now_iso = current_utc.isoformat()

        claimed_jobs = self.db.claim_due_jobs(now_iso)
        processed_results: List[Dict[str, Any]] = []

        for job in claimed_jobs:
            ws_id = job["workspace_id"]
            u_id = job["user_id"]
            c_id = job["content_id"]
            job_id = job["id"]

            content = self.db.get_content_item(c_id, ws_id)
            if not content:
                self.db.update_job_status(
                    job_id,
                    ws_id,
                    {
                        "status": JobStatus.FAILED.value,
                        "last_error_code": "CONTENT_MISSING",
                        "last_error_message": "Bài viết gốc đã bị xóa.",
                    },
                )
                continue

            # Idempotency safeguard: if content was already published (for non-recurring job), mark succeeded without calling API again
            if (
                not job.get("recurrence_rule")
                and content["status"] == ContentStatus.PUBLISHED.value
                and content.get("external_url")
            ):
                done_job = self.db.update_job_status(
                    job_id,
                    ws_id,
                    {
                        "status": JobStatus.SUCCEEDED.value,
                        "external_post_id": content.get("external_post_id"),
                        "external_url": content.get("external_url"),
                        "completed_at_utc": now_iso,
                    },
                )
                processed_results.append(done_job)  # type: ignore
                continue

            self.db.update_content_item(
                c_id,
                ws_id,
                u_id,
                {"status": ContentStatus.PUBLISHING.value},
            )

            try:
                pub_res = self.router.publish_content(content)
                self.publish_call_log.append({
                    "job_id": job_id,
                    "content_id": c_id,
                    "platform": content["platform"],
                    "timestamp": now_iso,
                })

                done_job = self.db.update_job_status(
                    job_id,
                    ws_id,
                    {
                        "status": JobStatus.SUCCEEDED.value,
                        "external_post_id": pub_res["external_post_id"],
                        "external_url": pub_res["external_url"],
                        "completed_at_utc": now_iso,
                        "last_error_code": None,
                        "last_error_message": None,
                    },
                )
                self.db.update_content_item(
                    c_id,
                    ws_id,
                    u_id,
                    {
                        "status": ContentStatus.PUBLISHED.value,
                        "external_post_id": pub_res["external_post_id"],
                        "external_url": pub_res["external_url"],
                        "error_code": None,
                        "error_message": None,
                    },
                )

                # Spawn next recurring job if recurrence_rule is set
                rec_rule = job.get("recurrence_rule")
                if rec_rule:
                    self._spawn_next_recurring_job(job, content)

                processed_results.append(done_job)  # type: ignore

            except PublishingError as exc:
                retry_count = int(job.get("retry_count") or 0) + 1
                max_retries = int(job.get("max_retries") or 3)

                if exc.retryable and retry_count <= max_retries:
                    backoff_sec = (
                        exc.retry_after_seconds
                        if isinstance(exc, RateLimitError)
                        else (30 * (2 ** (retry_count - 1)))
                    )
                    next_retry_dt = current_utc + timedelta(seconds=backoff_sec)
                    updated_job = self.db.update_job_status(
                        job_id,
                        ws_id,
                        {
                            "status": JobStatus.QUEUED.value,
                            "retry_count": retry_count,
                            "next_retry_at_utc": next_retry_dt.isoformat(),
                            "last_error_code": exc.code,
                            "last_error_message": f"{exc.message} (Lần thử {retry_count}/{max_retries})",
                        },
                    )
                    self.db.update_content_item(
                        c_id,
                        ws_id,
                        u_id,
                        {
                            "status": ContentStatus.SCHEDULED.value,
                            "error_code": exc.code,
                            "error_message": f"{exc.message} (Đang chờ thử lại lần {retry_count}/{max_retries})",
                        },
                    )
                else:
                    updated_job = self.db.update_job_status(
                        job_id,
                        ws_id,
                        {
                            "status": JobStatus.FAILED.value,
                            "retry_count": retry_count,
                            "last_error_code": exc.code,
                            "last_error_message": exc.message,
                        },
                    )
                    self.db.update_content_item(
                        c_id,
                        ws_id,
                        u_id,
                        {
                            "status": ContentStatus.FAILED.value,
                            "error_code": exc.code,
                            "error_message": exc.message,
                        },
                    )
                processed_results.append(updated_job)  # type: ignore

        return processed_results

    def _spawn_next_recurring_job(self, completed_job: Dict[str, Any], content: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        rule = (completed_job.get("recurrence_rule") or "").upper()
        tz_name = completed_job.get("timezone") or DEFAULT_TZ
        tz = get_zoneinfo(tz_name)

        prev_local = datetime.fromisoformat(completed_job["scheduled_at_local"])
        if prev_local.tzinfo is None:
            prev_local = prev_local.replace(tzinfo=tz)

        next_rule: Optional[str] = rule
        if rule == "DAILY":
            next_local = prev_local + timedelta(days=1)
        elif rule.startswith("DAILY_"):
            remaining = int(rule.split("_")[1]) - 1
            if remaining <= 0:
                return None
            next_local = prev_local + timedelta(days=1)
            next_rule = f"DAILY_{remaining}" if remaining > 1 else None
        elif rule == "WEEKLY":
            next_local = prev_local + timedelta(days=7)
        else:
            return None

        next_utc = next_local.astimezone(timezone.utc).replace(microsecond=0)
        return self.db.create_scheduled_job(
            workspace_id=completed_job["workspace_id"],
            user_id=completed_job["user_id"],
            content_id=content["id"],
            platform=completed_job["platform"],
            scheduled_at_utc=next_utc.isoformat(),
            scheduled_at_local=next_local.replace(microsecond=0).isoformat(),
            timezone_name=tz_name,
            recurrence_rule=next_rule,
            max_retries=completed_job.get("max_retries", 3),
        )
