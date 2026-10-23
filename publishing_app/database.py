"""SQLite persistent storage for content, scheduled jobs, workspaces, and chat state."""
import contextlib
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from publishing_app.models import ContentStatus, JobStatus


SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"(EAAB[A-Za-z0-9]+)"),  # Facebook access tokens
    re.compile(r"(wordpress_logged_in_[a-z0-9]+=)[^;\s\"']+", re.IGNORECASE),
    re.compile(r"(api[_-]?key|access[_-]?token|secret|password|cookie)\s*[:=]\s*['\"]?[^\s'\",}]+", re.IGNORECASE),
]


def redact_secrets(text: str) -> str:
    """Redacts any credentials, tokens, or session cookies from strings before returning to client."""
    if not text or not isinstance(text, str):
        return text
    cleaned = text
    cleaned = re.sub(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", r"\1[REDACTED]", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"EAAB[A-Za-z0-9]{8,}", "[REDACTED_FB_TOKEN]", cleaned)
    cleaned = re.sub(r"(wordpress_logged_in_[a-z0-9]+=)[^;\s\"']+", r"\1[REDACTED]", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"((?:api[_-]?key|access[_-]?token|client[_-]?secret|password|auth[_-]?token)\s*[:=]\s*['\"]?)([^\s'\",}]+)",
        r"\1[REDACTED]",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def sanitize_dict(data: Any) -> Any:
    """Recursively strips sensitive keys and redacts secret patterns from dicts/lists."""
    sensitive_keys = {"api_key", "access_token", "password", "secret", "cookie", "cookies", "auth_state", "token"}
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if k.lower() in sensitive_keys:
                continue
            result[k] = sanitize_dict(v)
        return result
    if isinstance(data, list):
        return [sanitize_dict(item) for item in data]
    if isinstance(data, str):
        return redact_secrets(data)
    return data


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    """Thread-safe SQLite database manager with workspace isolation and idempotency safeguards."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path(__file__).resolve().parent / "data" / "publishing.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()
        self._seed_default_workspaces()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'editor',
                    session_token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS content_items (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    body_html TEXT NOT NULL DEFAULT '',
                    excerpt TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT '',
                    destination TEXT NOT NULL DEFAULT '',
                    topic TEXT NOT NULL DEFAULT '',
                    tone TEXT NOT NULL DEFAULT 'chuyên nghiệp, kỹ thuật dễ hiểu',
                    audience TEXT NOT NULL DEFAULT 'khách hàng cá nhân & doanh nghiệp',
                    seo_title TEXT NOT NULL DEFAULT '',
                    meta_description TEXT NOT NULL DEFAULT '',
                    slug TEXT NOT NULL DEFAULT '',
                    focus_keyphrase TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT 'Tin tức',
                    featured_image TEXT NOT NULL DEFAULT '',
                    featured_alt TEXT NOT NULL DEFAULT '',
                    media_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'draft',
                    reviewed INTEGER NOT NULL DEFAULT 0,
                    approved_by TEXT,
                    approved_at TEXT,
                    scheduled_at TEXT,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
                    recurrence_rule TEXT,
                    external_post_id TEXT,
                    external_url TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    validation_json TEXT NOT NULL DEFAULT '{}',
                    source_docx TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_content_workspace
                    ON content_items(workspace_id, status);

                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    scheduled_at_utc TEXT NOT NULL,
                    scheduled_at_local TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
                    recurrence_rule TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'queued',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    next_retry_at_utc TEXT,
                    last_error_code TEXT,
                    last_error_message TEXT,
                    external_post_id TEXT,
                    external_url TEXT,
                    worker_id TEXT,
                    locked_at_utc TEXT,
                    completed_at_utc TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (content_id) REFERENCES content_items(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_due
                    ON scheduled_jobs(status, scheduled_at_utc, next_retry_at_utc);
                CREATE INDEX IF NOT EXISTS idx_jobs_workspace
                    ON scheduled_jobs(workspace_id, status);

                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    pending_context_json TEXT NOT NULL DEFAULT '{}',
                    active_content_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    message TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_id TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _seed_default_workspaces(self) -> None:
        """Creates default authenticated workspace users for VTECH team and isolated test workspace."""
        default_users = [
            {
                "user_id": "user_vtech_admin",
                "username": "vtech_admin",
                "display_name": "VTECH Content Admin",
                "workspace_id": "ws_vtech_main",
                "role": "admin",
                "raw_token": "vtech-session-main-2026",
            },
            {
                "user_id": "user_vtech_editor",
                "username": "vtech_editor",
                "display_name": "VTECH Marketing Editor",
                "workspace_id": "ws_vtech_main",
                "role": "editor",
                "raw_token": "vtech-session-editor-2026",
            },
            {
                "user_id": "user_agency_other",
                "username": "agency_partner",
                "display_name": "External Agency Workspace",
                "workspace_id": "ws_agency_isolated",
                "role": "editor",
                "raw_token": "agency-session-isolated-2026",
            },
        ]
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            for u in default_users:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO users
                    (user_id, username, display_name, workspace_id, role, session_token_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        u["user_id"],
                        u["username"],
                        u["display_name"],
                        u["workspace_id"],
                        u["role"],
                        self.hash_token(u["raw_token"]),
                        now,
                    ),
                )

    def authenticate_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        token_hash = self.hash_token(token.strip())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, username, display_name, workspace_id, role FROM users WHERE session_token_hash = ?",
                (token_hash,),
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_id(self, user_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, username, display_name, workspace_id, role FROM users WHERE user_id = ? AND workspace_id = ?",
                (user_id, workspace_id),
            ).fetchone()
            return dict(row) if row else None

    def log_audit(
        self,
        workspace_id: str,
        user_id: str,
        action: str,
        target_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = utc_now_iso()
        safe_details = sanitize_dict(details or {})
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (id, workspace_id, user_id, action, target_id, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), workspace_id, user_id, action, target_id, json.dumps(safe_details, ensure_ascii=False), now),
            )

    # ── Content Item Operations (Workspace Isolated) ──

    def create_content_item(self, workspace_id: str, user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        content_id = data.get("id") or f"post_{uuid.uuid4().hex[:10]}"
        now = utc_now_iso()
        media_list = data.get("media", [])
        validation = data.get("validation", {})
        status = data.get("status", ContentStatus.DRAFT.value)
        if isinstance(status, ContentStatus):
            status = status.value

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO content_items (
                    id, workspace_id, user_id, title, body_html, excerpt, platform, destination,
                    topic, tone, audience, seo_title, meta_description, slug, focus_keyphrase,
                    category, featured_image, featured_alt, media_json, status, reviewed,
                    approved_by, approved_at, scheduled_at, timezone, recurrence_rule,
                    external_post_id, external_url, error_code, error_message,
                    validation_json, source_docx, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?
                )
                """,
                (
                    content_id,
                    workspace_id,
                    user_id,
                    data.get("title", ""),
                    data.get("body_html", ""),
                    data.get("excerpt", ""),
                    data.get("platform", ""),
                    data.get("destination", ""),
                    data.get("topic", ""),
                    data.get("tone", "chuyên nghiệp, kỹ thuật dễ hiểu"),
                    data.get("audience", "khách hàng cá nhân & doanh nghiệp"),
                    data.get("seo_title", ""),
                    data.get("meta_description", ""),
                    data.get("slug", ""),
                    data.get("focus_keyphrase", ""),
                    data.get("category", "Tin tức"),
                    data.get("featured_image", ""),
                    data.get("featured_alt", ""),
                    json.dumps(media_list, ensure_ascii=False),
                    status,
                    1 if data.get("reviewed") else 0,
                    data.get("approved_by"),
                    data.get("approved_at"),
                    data.get("scheduled_at"),
                    data.get("timezone", "Asia/Ho_Chi_Minh"),
                    data.get("recurrence_rule"),
                    data.get("external_post_id"),
                    data.get("external_url"),
                    data.get("error_code"),
                    redact_secrets(data.get("error_message") or "") or None,
                    json.dumps(validation, ensure_ascii=False),
                    data.get("source_docx"),
                    now,
                    now,
                ),
            )
        self.log_audit(workspace_id, user_id, "create_content", content_id, {"title": data.get("title", ""), "platform": data.get("platform", "")})
        return self.get_content_item(content_id, workspace_id)  # type: ignore

    def get_content_item(self, content_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM content_items WHERE id = ? AND workspace_id = ?",
                (content_id, workspace_id),
            ).fetchone()
            if not row:
                return None
            return self._row_to_content(row)

    def list_content_items(
        self,
        workspace_id: str,
        status: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM content_items WHERE workspace_id = ?"
        params: List[Any] = [workspace_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        if platform:
            query += " AND platform = ?"
            params.append(platform)
        query += " ORDER BY updated_at DESC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_content(r) for r in rows]

    def update_content_item(
        self,
        content_id: str,
        workspace_id: str,
        user_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_content_item(content_id, workspace_id)
        if not existing:
            return None

        allowed_columns = {
            "title", "body_html", "excerpt", "platform", "destination", "topic",
            "tone", "audience", "seo_title", "meta_description", "slug",
            "focus_keyphrase", "category", "featured_image", "featured_alt",
            "status", "reviewed", "approved_by", "approved_at", "scheduled_at",
            "timezone", "recurrence_rule", "external_post_id", "external_url",
            "error_code", "error_message", "source_docx"
        }
        set_clauses = []
        params: List[Any] = []

        for key, value in updates.items():
            if key == "media":
                set_clauses.append("media_json = ?")
                params.append(json.dumps(value, ensure_ascii=False))
            elif key == "validation":
                set_clauses.append("validation_json = ?")
                params.append(json.dumps(value, ensure_ascii=False))
            elif key in allowed_columns:
                if key == "status" and isinstance(value, ContentStatus):
                    value = value.value
                elif key == "reviewed":
                    value = 1 if value else 0
                elif key == "error_message" and isinstance(value, str):
                    value = redact_secrets(value)
                set_clauses.append(f"{key} = ?")
                params.append(value)

        if not set_clauses:
            return existing

        set_clauses.append("updated_at = ?")
        params.append(utc_now_iso())
        params.extend([content_id, workspace_id])

        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE content_items SET {', '.join(set_clauses)} WHERE id = ? AND workspace_id = ?",
                params,
            )

        self.log_audit(workspace_id, user_id, "update_content", content_id, {"updated_fields": list(updates.keys())})
        return self.get_content_item(content_id, workspace_id)

    def delete_content_item(self, content_id: str, workspace_id: str, user_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM content_items WHERE id = ? AND workspace_id = ?",
                (content_id, workspace_id),
            )
            deleted = cur.rowcount > 0
        if deleted:
            self.log_audit(workspace_id, user_id, "delete_content", content_id, {})
        return deleted

    def _row_to_content(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["reviewed"] = bool(d.get("reviewed", 0))
        d["media"] = json.loads(d.pop("media_json", "[]") or "[]")
        d["validation"] = json.loads(d.pop("validation_json", "{}") or "{}")
        return sanitize_dict(d)

    # ── Scheduled Jobs Operations (Persistent & Idempotent) ──

    @staticmethod
    def build_idempotency_key(workspace_id: str, content_id: str, platform: str, scheduled_at_utc: str) -> str:
        raw = f"{workspace_id}:{content_id}:{platform}:{scheduled_at_utc}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def create_scheduled_job(
        self,
        workspace_id: str,
        user_id: str,
        content_id: str,
        platform: str,
        scheduled_at_utc: str,
        scheduled_at_local: str,
        timezone_name: str = "Asia/Ho_Chi_Minh",
        recurrence_rule: Optional[str] = None,
        max_retries: int = 3,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        content = self.get_content_item(content_id, workspace_id)
        if not content:
            raise ValueError("Không tìm thấy bài viết trong workspace hiện tại.")

        idem_key = idempotency_key or self.build_idempotency_key(workspace_id, content_id, platform, scheduled_at_utc)
        now = utc_now_iso()

        with self._lock, self._connect() as conn:
            # Check idempotency key first to prevent duplicate jobs
            existing = conn.execute(
                "SELECT * FROM scheduled_jobs WHERE idempotency_key = ? AND workspace_id = ?",
                (idem_key, workspace_id),
            ).fetchone()
            if existing and existing["status"] in (JobStatus.QUEUED.value, JobStatus.RUNNING.value, JobStatus.SUCCEEDED.value):
                return self._row_to_job(existing)

            job_id = f"job_{uuid.uuid4().hex[:10]}"
            conn.execute(
                """
                INSERT INTO scheduled_jobs (
                    id, workspace_id, user_id, content_id, platform,
                    scheduled_at_utc, scheduled_at_local, timezone, recurrence_rule,
                    idempotency_key, status, retry_count, max_retries,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    job_id,
                    workspace_id,
                    user_id,
                    content_id,
                    platform,
                    scheduled_at_utc,
                    scheduled_at_local,
                    timezone_name,
                    recurrence_rule,
                    idem_key,
                    JobStatus.QUEUED.value,
                    max_retries,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)).fetchone()
        self.log_audit(
            workspace_id,
            user_id,
            "schedule_job",
            job_id,
            {"content_id": content_id, "scheduled_at_local": scheduled_at_local, "recurrence": recurrence_rule},
        )
        return self._row_to_job(row)

    def get_scheduled_job(self, job_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT j.*, c.title as content_title, c.slug as content_slug
                FROM scheduled_jobs j
                LEFT JOIN content_items c ON j.content_id = c.id
                WHERE j.id = ? AND j.workspace_id = ?
                """,
                (job_id, workspace_id),
            ).fetchone()
            return self._row_to_job(row) if row else None

    def list_scheduled_jobs(
        self,
        workspace_id: str,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT j.*, c.title as content_title, c.slug as content_slug, c.status as content_status
            FROM scheduled_jobs j
            LEFT JOIN content_items c ON j.content_id = c.id
            WHERE j.workspace_id = ?
        """
        params: List[Any] = [workspace_id]
        if status:
            query += " AND j.status = ?"
            params.append(status)
        query += " ORDER BY j.scheduled_at_utc ASC"
        with self._lock, self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_job(r) for r in rows]

    def claim_due_jobs(self, now_utc_iso: str, worker_id: str = "worker_main") -> List[Dict[str, Any]]:
        """Atomically claims queued jobs that are due for execution."""
        claimed: List[Dict[str, Any]] = []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_jobs
                WHERE status = 'queued'
                  AND scheduled_at_utc <= ?
                  AND (next_retry_at_utc IS NULL OR next_retry_at_utc <= ?)
                ORDER BY scheduled_at_utc ASC
                """,
                (now_utc_iso, now_utc_iso),
            ).fetchall()

            for row in rows:
                cur = conn.execute(
                    """
                    UPDATE scheduled_jobs
                    SET status = 'running', worker_id = ?, locked_at_utc = ?, updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (worker_id, now_utc_iso, now_utc_iso, row["id"]),
                )
                if cur.rowcount == 1:
                    updated = conn.execute("SELECT * FROM scheduled_jobs WHERE id = ?", (row["id"],)).fetchone()
                    claimed.append(self._row_to_job(updated))
        return claimed

    def recover_stalled_jobs(self, now_utc_iso: str) -> int:
        """Recovers jobs stuck in 'running' state after an unexpected crash/restart,
        while respecting already-published content items so duplicates never happen."""
        recovered_count = 0
        with self._lock, self._connect() as conn:
            running_rows = conn.execute(
                "SELECT * FROM scheduled_jobs WHERE status = 'running'"
            ).fetchall()

            for job in running_rows:
                content = conn.execute(
                    "SELECT status, external_url, external_post_id FROM content_items WHERE id = ?",
                    (job["content_id"],),
                ).fetchone()

                if content and content["status"] == ContentStatus.PUBLISHED.value and content["external_url"]:
                    # Content already published before crash! Mark job succeeded without re-publishing.
                    conn.execute(
                        """
                        UPDATE scheduled_jobs
                        SET status = 'succeeded', external_url = ?, external_post_id = ?,
                            completed_at_utc = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            content["external_url"],
                            content["external_post_id"],
                            now_utc_iso,
                            now_utc_iso,
                            job["id"],
                        ),
                    )
                else:
                    # Reset job back to queued so worker can safely execute it
                    conn.execute(
                        """
                        UPDATE scheduled_jobs
                        SET status = 'queued', worker_id = NULL, locked_at_utc = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (now_utc_iso, job["id"]),
                    )
                    if content and content["status"] == ContentStatus.PUBLISHING.value:
                        conn.execute(
                            "UPDATE content_items SET status = 'scheduled', updated_at = ? WHERE id = ?",
                            (now_utc_iso, job["content_id"]),
                        )
                    recovered_count += 1
        return recovered_count

    def update_job_status(
        self,
        job_id: str,
        workspace_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        allowed = {
            "status", "scheduled_at_utc", "scheduled_at_local", "timezone",
            "recurrence_rule", "idempotency_key", "retry_count", "max_retries",
            "next_retry_at_utc", "last_error_code", "last_error_message",
            "external_post_id", "external_url", "worker_id", "locked_at_utc",
            "completed_at_utc",
        }
        set_clauses = []
        params: List[Any] = []
        for k, v in updates.items():
            if k in allowed:
                if k == "status" and isinstance(v, JobStatus):
                    v = v.value
                elif k == "last_error_message" and isinstance(v, str):
                    v = redact_secrets(v)
                set_clauses.append(f"{k} = ?")
                params.append(v)

        if not set_clauses:
            return self.get_scheduled_job(job_id, workspace_id)

        set_clauses.append("updated_at = ?")
        params.append(utc_now_iso())
        params.extend([job_id, workspace_id])

        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE scheduled_jobs SET {', '.join(set_clauses)} WHERE id = ? AND workspace_id = ?",
                params,
            )
        return self.get_scheduled_job(job_id, workspace_id)

    def _row_to_job(self, row: sqlite3.Row) -> Dict[str, Any]:
        return sanitize_dict(dict(row))

    # ── Chat Session & Conversation History ──

    def get_or_create_chat_session(self, session_id: str, workspace_id: str, user_id: str) -> Dict[str, Any]:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE session_id = ? AND workspace_id = ?",
                (session_id, workspace_id),
            ).fetchone()
            if not row:
                conn.execute(
                    """
                    INSERT INTO chat_sessions (session_id, workspace_id, user_id, pending_context_json, created_at, updated_at)
                    VALUES (?, ?, ?, '{}', ?, ?)
                    """,
                    (session_id, workspace_id, user_id, now, now),
                )
                row = conn.execute(
                    "SELECT * FROM chat_sessions WHERE session_id = ? AND workspace_id = ?",
                    (session_id, workspace_id),
                ).fetchone()
            d = dict(row)
            d["pending_context"] = json.loads(d.pop("pending_context_json", "{}") or "{}")
            return d

    def update_chat_session(
        self,
        session_id: str,
        workspace_id: str,
        pending_context: Optional[Dict[str, Any]] = None,
        active_content_id: Optional[str] = None,
    ) -> None:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            if pending_context is not None and active_content_id is not None:
                conn.execute(
                    """
                    UPDATE chat_sessions
                    SET pending_context_json = ?, active_content_id = ?, updated_at = ?
                    WHERE session_id = ? AND workspace_id = ?
                    """,
                    (json.dumps(pending_context, ensure_ascii=False), active_content_id, now, session_id, workspace_id),
                )
            elif pending_context is not None:
                conn.execute(
                    """
                    UPDATE chat_sessions
                    SET pending_context_json = ?, updated_at = ?
                    WHERE session_id = ? AND workspace_id = ?
                    """,
                    (json.dumps(pending_context, ensure_ascii=False), now, session_id, workspace_id),
                )
            elif active_content_id is not None:
                conn.execute(
                    """
                    UPDATE chat_sessions
                    SET active_content_id = ?, updated_at = ?
                    WHERE session_id = ? AND workspace_id = ?
                    """,
                    (active_content_id, now, session_id, workspace_id),
                )

    def add_chat_message(
        self,
        session_id: str,
        workspace_id: str,
        user_id: str,
        sender: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        msg_id = f"msg_{uuid.uuid4().hex[:10]}"
        now = utc_now_iso()
        safe_message = redact_secrets(message)
        safe_meta = sanitize_dict(metadata or {})
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_messages (id, session_id, workspace_id, user_id, sender, message, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (msg_id, session_id, workspace_id, user_id, sender, safe_message, json.dumps(safe_meta, ensure_ascii=False), now),
            )
        return {
            "id": msg_id,
            "session_id": session_id,
            "sender": sender,
            "message": safe_message,
            "metadata": safe_meta,
            "created_at": now,
        }

    def list_chat_messages(self, session_id: str, workspace_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM chat_messages
                WHERE session_id = ? AND workspace_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (session_id, workspace_id, limit),
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["metadata"] = json.loads(d.pop("metadata_json", "{}") or "{}")
                result.append(sanitize_dict(d))
            return result
