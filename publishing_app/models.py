"""Data models, platform capabilities, and exceptions for the publishing system."""
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Any
import json
from pathlib import Path


class ContentStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublishingError(Exception):
    """Base exception for publishing errors."""
    def __init__(self, message: str, code: str = "PUBLISHING_ERROR", retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable


class AuthExpiredError(PublishingError):
    """Raised when platform session or token has expired."""
    def __init__(self, platform: str, details: str = ""):
        msg = (
            f"Phiên đăng nhập trên nền tảng '{platform}' đã hết hạn. "
            f"Vui lòng đăng nhập lại để làm mới xác thực. {details}"
        ).strip()
        super().__init__(msg, code="AUTH_EXPIRED", retryable=False)


class RateLimitError(PublishingError):
    """Raised when platform API rate limit is hit."""
    def __init__(self, platform: str, retry_after_seconds: int = 60):
        msg = f"Nền tảng '{platform}' đang giới hạn tần suất (Rate Limit). Sẽ thử lại sau {retry_after_seconds}s."
        super().__init__(msg, code="RATE_LIMIT", retryable=True)
        self.retry_after_seconds = retry_after_seconds


class NetworkFailureError(PublishingError):
    """Raised when network timeout or connection failure occurs."""
    def __init__(self, platform: str, details: str = ""):
        msg = f"Lỗi kết nối mạng tới '{platform}'. Hệ thống sẽ tự động thử lại. {details}".strip()
        super().__init__(msg, code="NETWORK_ERROR", retryable=True)


class PlatformCapabilityError(PublishingError):
    """Raised when a platform or feature is not supported/configured."""
    def __init__(self, platform: str, reason: str):
        super().__init__(reason, code="UNSUPPORTED_CAPABILITY", retryable=False)


@dataclass
class PlatformCapability:
    site_key: str
    name: str
    hostname: str
    platform_type: str  # wordpress, onexeos, facebook
    supports_seo_meta: bool
    supports_media_upload: bool
    supports_native_schedule: bool
    supports_recurring: bool
    supports_categories: bool
    is_configured: bool
    hotlines: List[str] = field(default_factory=lambda: ["0967 725 714", "0967 707 383", "0522 187 187"])
    limitations_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def load_platform_registry(sites_json_path: Optional[Path] = None) -> Dict[str, PlatformCapability]:
    """Loads existing sites.json and builds the platform capability registry."""
    if sites_json_path is None:
        sites_json_path = Path(__file__).resolve().parent.parent / "tools" / "sites.json"

    raw_sites = {}
    if sites_json_path.exists():
        raw_sites = json.loads(sites_json_path.read_text(encoding="utf-8"))

    registry: Dict[str, PlatformCapability] = {}

    # 1. WordPress (vtechfilm.com)
    vt = raw_sites.get("vtechfilm", {})
    registry["vtechfilm"] = PlatformCapability(
        site_key="vtechfilm",
        name="Website Chính (vtechfilm.com - WordPress)",
        hostname=vt.get("hostname", "vtechfilm.com"),
        platform_type=vt.get("platform", "wordpress"),
        supports_seo_meta=True,
        supports_media_upload=True,
        supports_native_schedule=True,
        supports_recurring=True,
        supports_categories=True,
        is_configured=True,
        hotlines=vt.get("hotlines", ["0967 725 714", "0522 187 187"]),
        limitations_note="Hỗ trợ đầy đủ bài viết HTML, bảng biểu, Yoast SEO, ảnh WebP và lên lịch tự động."
    )

    # 2. OneXEOS (phimcachnhietdankinh.vn)
    ox = raw_sites.get("onex", {})
    registry["onex"] = PlatformCapability(
        site_key="onex",
        name="Website Vệ Tinh (phimcachnhietdankinh.vn - OneXEOS)",
        hostname=ox.get("hostname", "phimcachnhietdankinh.vn"),
        platform_type=ox.get("platform", "onexeos"),
        supports_seo_meta=True,
        supports_media_upload=True,
        supports_native_schedule=True,
        supports_recurring=True,
        supports_categories=True,
        is_configured=True,
        hotlines=ox.get("hotlines", ["0967 725 714", "0522 187 187"]),
        limitations_note=(
            "Yêu cầu nội dung/tiêu đề/slug khác biệt với vtechfilm.com để tránh trùng lặp SEO; "
            "ảnh phải được tải trực tiếp lên hệ thống OneX."
        )
    )

    # 3. Facebook Page (Optional / Sandbox connector for social requests)
    registry["facebook"] = PlatformCapability(
        site_key="facebook",
        name="Facebook Fanpage (VTECH FILM)",
        hostname="facebook.com",
        platform_type="facebook",
        supports_seo_meta=False,  # Facebook posts do not use Yoast SEO meta/slug
        supports_media_upload=True,
        supports_native_schedule=True,
        supports_recurring=True,
        supports_categories=False,
        is_configured=False,  # Requires Facebook Graph API Page Access Token
        hotlines=["0967 725 714", "0967 707 383", "0522 187 187"],
        limitations_note=(
            "Facebook không sử dụng thẻ SEO Meta/Slug của website. "
            "Cần cấu hình FACEBOOK_PAGE_ACCESS_TOKEN để đăng thật lên Graph API (hiện chạy ở chế độ xem trước/lưu nháp)."
        )
    )

    return registry
