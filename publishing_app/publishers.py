"""Platform publishing adapters for WordPress (vtechfilm.com), OneXEOS (phimcachnhietdankinh.vn),
and Facebook Page, reusing existing VTECH session states and preflight validation."""
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from publishing_app.models import (
    AuthExpiredError,
    NetworkFailureError,
    PlatformCapabilityError,
    PublishingError,
    RateLimitError,
)
from publishing_app.validator import PLATFORM_REGISTRY, validate_content_item


DEFAULT_SCRATCH_DIR = Path(
    r"C:\Users\Admin\.gemini\antigravity\brain\70ea601f-5645-4fea-9135-e21c13d96d4d\scratch"
)


class BasePlatformPublisher:
    platform_key: str = ""

    def __init__(self, live_mode: bool = False, auth_state_dir: Optional[Path] = None):
        self.live_mode = live_mode
        self.auth_state_dir = auth_state_dir or DEFAULT_SCRATCH_DIR

    def verify_Content_and_permissions(self, content: Dict[str, Any]) -> None:
        if not content.get("reviewed"):
            raise PublishingError(
                "Bài viết chưa được người dùng phê duyệt (reviewed=false). Không thể xuất bản.",
                code="APPROVAL_REQUIRED",
                retryable=False,
            )
        report = validate_content_item(content, require_approval=True)
        if not report["passed"]:
            raise PublishingError(
                "Bài viết chưa đạt kiểm định Preflight: " + "; ".join(report["fails"]),
                code="VALIDATION_FAILED",
                retryable=False,
            )

    def _check_simulated_faults(self, content: Dict[str, Any]) -> None:
        sim = content.get("_simulate_error")
        if sim == "auth_expired":
            raise AuthExpiredError(self.platform_key, "Token phiên làm việc đã hết hạn hoặc bị thu hồi.")
        if sim == "rate_limit":
            raise RateLimitError(self.platform_key, retry_after_seconds=30)
        if sim == "network_error":
            raise NetworkFailureError(self.platform_key, "Connection timed out after 15000ms.")

    def publish(self, content: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class WordPressPublisher(BasePlatformPublisher):
    """Publisher for vtechfilm.com (WordPress + Yoast SEO + WebP Media Upload)."""
    platform_key = "vtechfilm"

    def publish(self, content: Dict[str, Any]) -> Dict[str, Any]:
        self._check_simulated_faults(content)
        self.verify_Content_and_permissions(content)

        cap = PLATFORM_REGISTRY["vtechfilm"]
        slug = content["slug"]

        if not self.live_mode:
            post_id = f"wp_sandbox_{uuid.uuid4().hex[:6]}"
            return {
                "confirmed": True,
                "mode": "sandbox",
                "platform": self.platform_key,
                "external_post_id": post_id,
                "external_url": f"https://{cap.hostname}/{slug}/",
                "http_status": 200,
                "message": f"[Chế độ Nháp An Toàn] Đã kiểm định & mô phỏng xuất bản thành công lên {cap.hostname}.",
            }

        # Live mode: use saved WordPress session cookies from WP_AUTH_STATE_JSON env or wp_auth_state.json file
        wp_state_env = os.environ.get("WP_AUTH_STATE_JSON", "").strip()
        wp_state_path = Path(os.environ.get("WP_AUTH_STATE_PATH", str(self.auth_state_dir / "wp_auth_state.json")))

        if wp_state_env:
            state_data = json.loads(wp_state_env)
        elif wp_state_path.exists():
            state_data = json.loads(wp_state_path.read_text(encoding="utf-8"))
        else:
            raise AuthExpiredError("vtechfilm.com", "Không tìm thấy file trạng thái đăng nhập WordPress (wp_auth_state.json).")
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VTECH-Content-Studio/1.0"})
        has_login_cookie = False
        for c in state_data.get("cookies", []):
            if "vtechfilm.com" in c.get("domain", ""):
                sess.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
                if c["name"].startswith("wordpress_logged_in_"):
                    has_login_cookie = True

        if not has_login_cookie:
            raise AuthExpiredError("vtechfilm.com", "Cookie wordpress_logged_in_ không tồn tại hoặc đã hết hạn.")

        try:
            # 1. Fetch WP REST API Nonce from wp-admin
            admin_resp = sess.get(f"https://{cap.hostname}/wp-admin/post-new.php", timeout=20, allow_redirects=False)
            if admin_resp.status_code in (301, 302, 401, 403):
                raise AuthExpiredError("vtechfilm.com", "Phiên quản trị WordPress đã hết hạn (chuyển hướng đăng nhập).")
            m_nonce = re.search(r'wpApiSettings\s*=\s*\{[^}]*"nonce"\s*:\s*"([a-zA-Z0-9]+)"', admin_resp.text)
            if not m_nonce:
                raise AuthExpiredError("vtechfilm.com", "Không lấy được WP REST Nonce từ trang quản trị.")
            nonce = m_nonce.group(1)
            headers = {"X-WP-Nonce": nonce}

            # 2. Upload converted .webp images to WordPress Media Library
            body_html = content.get("body_html") or ""
            featured_media_id: Optional[int] = None
            for idx, m_item in enumerate(content.get("media") or []):
                local_p = Path(m_item.get("local_path") or "")
                if local_p.is_file():
                    with open(local_p, "rb") as f_img:
                        up_resp = sess.post(
                            f"https://{cap.hostname}/wp-json/wp/v2/media",
                            headers={
                                **headers,
                                "Content-Disposition": f'attachment; filename="{local_p.name}"',
                                "Content-Type": "image/webp",
                            },
                            data=f_img.read(),
                            timeout=30,
                        )
                    if up_resp.status_code in (200, 201):
                        media_json = up_resp.json()
                        media_id = media_json["id"]
                        source_url = media_json["source_url"]
                        if featured_media_id is None:
                            featured_media_id = media_id
                        # Set alt text on uploaded image
                        sess.post(
                            f"https://{cap.hostname}/wp-json/wp/v2/media/{media_id}",
                            headers=headers,
                            json={"alt_text": m_item.get("alt") or content["title"]},
                            timeout=15,
                        )
                        if m_item.get("url"):
                            body_html = body_html.replace(m_item["url"], source_url)

            # 3. Create & publish post via WordPress REST API
            post_payload: Dict[str, Any] = {
                "title": content["title"],
                "slug": slug,
                "content": body_html,
                "excerpt": content.get("excerpt") or "",
                "status": "publish",
            }
            if featured_media_id:
                post_payload["featured_media"] = featured_media_id

            post_resp = sess.post(
                f"https://{cap.hostname}/wp-json/wp/v2/posts",
                headers=headers,
                json=post_payload,
                timeout=30,
            )
            if post_resp.status_code == 429:
                raise RateLimitError("vtechfilm.com", retry_after_seconds=60)
            if post_resp.status_code in (401, 403):
                raise AuthExpiredError("vtechfilm.com", f"HTTP {post_resp.status_code}")
            if post_resp.status_code not in (200, 201):
                raise PublishingError(
                    f"WordPress trả về mã lỗi HTTP {post_resp.status_code}: {post_resp.text[:200]}",
                    code="WP_API_ERROR",
                    retryable=post_resp.status_code >= 500,
                )

            created_post = post_resp.json()
            post_id = str(created_post["id"])
            post_link = created_post.get("link") or f"https://{cap.hostname}/{slug}/"

            return {
                "confirmed": True,
                "mode": "live",
                "platform": self.platform_key,
                "external_post_id": post_id,
                "external_url": post_link,
                "http_status": post_resp.status_code,
                "message": f"Đã đăng bài thật thành công lên {cap.hostname} (Post ID: {post_id}).",
            }
        except requests.RequestException as exc:
            raise NetworkFailureError("vtechfilm.com", str(exc)) from exc


class OneXPublisher(BasePlatformPublisher):
    """Publisher for phimcachnhietdankinh.vn (OneXEOS CMS via app.onexbots.com)."""
    platform_key = "onex"

    def publish(self, content: Dict[str, Any]) -> Dict[str, Any]:
        self._check_simulated_faults(content)
        self.verify_Content_and_permissions(content)

        cap = PLATFORM_REGISTRY["onex"]
        slug = content["slug"]

        if not self.live_mode:
            post_id = f"onex_sandbox_{uuid.uuid4().hex[:6]}"
            return {
                "confirmed": True,
                "mode": "sandbox",
                "platform": self.platform_key,
                "external_post_id": post_id,
                "external_url": f"https://{cap.hostname}/{slug}/",
                "http_status": 200,
                "message": f"[Chế độ Nháp An Toàn] Đã kiểm định & mô phỏng xuất bản thành công lên {cap.hostname}.",
            }

        onex_state_path = self.auth_state_dir / "onex_auth_state.json"
        if not onex_state_path.exists():
            raise AuthExpiredError("phimcachnhietdankinh.vn (OneXEOS)", "Chưa có phiên đăng nhập OneXEOS.")

        raise PublishingError(
            "OneXEOS sử dụng trình soạn thảo quản trị app.onexbots.com. Vui lòng chạy ở chế độ Sandbox hoặc làm mới phiên Playwright OneX.",
            code="LIVE_PROTECTED",
            retryable=False,
        )


class FacebookPublisher(BasePlatformPublisher):
    """Publisher for Facebook Fanpage via Facebook Graph API."""
    platform_key = "facebook"

    def publish(self, content: Dict[str, Any]) -> Dict[str, Any]:
        self._check_simulated_faults(content)
        self.verify_Content_and_permissions(content)

        fb_token = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
        if self.live_mode and not fb_token:
            raise PlatformCapabilityError(
                "facebook",
                "Nền tảng Facebook Fanpage chưa được cấu hình FACEBOOK_PAGE_ACCESS_TOKEN. "
                "Hệ thống không bao giờ giả mạo kết quả đã đăng khi thiếu kết nối Graph API.",
            )

        if not self.live_mode:
            post_id = f"fb_sandbox_{uuid.uuid4().hex[:8]}"
            return {
                "confirmed": True,
                "mode": "sandbox",
                "platform": self.platform_key,
                "external_post_id": post_id,
                "external_url": f"https://facebook.com/vtechfilm/posts/{post_id}",
                "http_status": 200,
                "message": "[Chế độ Nháp An Toàn] Đã kiểm định & mô phỏng đăng bài Facebook Fanpage.",
            }

        raise PlatformCapabilityError("facebook", "Cần cấp quyền Graph API pages_manage_posts để đăng thật.")


class PublisherRouter:
    """Routes publishing requests to the appropriate platform adapter and verifies API confirmation."""

    def __init__(self, live_mode: bool = False, auth_state_dir: Optional[Path] = None):
        self.live_mode = live_mode
        self.auth_state_dir = auth_state_dir
        self.adapters: Dict[str, BasePlatformPublisher] = {
            "vtechfilm": WordPressPublisher(live_mode=live_mode, auth_state_dir=auth_state_dir),
            "onex": OneXPublisher(live_mode=live_mode, auth_state_dir=auth_state_dir),
            "facebook": FacebookPublisher(live_mode=live_mode, auth_state_dir=auth_state_dir),
        }
        self.fault_injections: Dict[str, str] = {}

    def set_live_mode(self, enabled: bool) -> None:
        self.live_mode = enabled
        for adapter in self.adapters.values():
            adapter.live_mode = enabled

    def inject_fault(self, content_id: str, error_type: Optional[str]) -> None:
        if error_type is None:
            self.fault_injections.pop(content_id, None)
        else:
            self.fault_injections[content_id] = error_type

    def publish_content(self, content: Dict[str, Any]) -> Dict[str, Any]:
        platform = (content.get("platform") or "").strip().lower()
        adapter = self.adapters.get(platform)
        if not adapter:
            raise PlatformCapabilityError(platform, f"Nền tảng '{platform}' không được hỗ trợ.")

        payload = dict(content)
        cid = payload.get("id", "")
        if cid in self.fault_injections:
            payload["_simulate_error"] = self.fault_injections[cid]

        result = adapter.publish(payload)
        if not result.get("confirmed") or not result.get("external_url"):
            raise PublishingError(
                "API xuất bản không trả về xác nhận thành công hoặc thiếu URL bài viết.",
                code="UNCONFIRMED_PUBLISH",
                retryable=True,
            )
        return result
