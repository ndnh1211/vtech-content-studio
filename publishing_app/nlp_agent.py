"""Bilingual (Vietnamese & English) AI Chat NLP Engine for intent detection,
date/time/recurrence extraction, concise missing-info follow-ups, and SEO content generation.
Integrates the 'vtechfilm-seo' skill (.agents/skills/vtechfilm-seo/SKILL.md)."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from publishing_app.models import ContentStatus
from publishing_app.validator import PLATFORM_REGISTRY, import_from_docx_pipeline, slugify_vn, validate_content_item


DEFAULT_TZ = "Asia/Ho_Chi_Minh"
VTECH_SEO_SKILL_PATH = Path(__file__).resolve().parent.parent / ".agents" / "skills" / "vtechfilm-seo" / "SKILL.md"

# Verified data from vtechfilm-seo/SKILL.md (never fabricate numbers marked with '?')
VTECH_BRAND_INFO = {
    "name": "VTECH FILM",
    "slogan": "Làm mát cho người Việt",
    "headquarters": "684 Phạm Văn Bạch, Phường An Hội Tây, TP.HCM",
    "store": "54/56 Tân Thới Hiệp 29, Phường Tân Thới Hiệp (Quận 12 cũ), TP.HCM",
    "hotlines": ["0967 725 714", "0967 707 383", "0522 187 187"],
    "zalo": "0967 725 714",
    "email": "mkt.vtechfilm@gmail.com",
    "working_hours": "Thứ 2 – Thứ 7, 8:00 – 20:00",
}

VTECH_CASE_STUDIES = [
    {
        "project": "An Gia Building",
        "type": "Văn phòng",
        "area": "Quận 1",
        "code": "VT7091",
        "highlight": "Kính cao tầng, thi công có giàn giáo",
        "slug": "/dan-phim-cach-nhiet-van-phong-an-gia-building-quan-1",
    },
    {
        "project": "CMC Creative Space",
        "type": "Văn phòng 2 khối kính",
        "area": "Quận 7",
        "code": "VT7091",
        "highlight": "Kính cao, thi công giàn giáo an toàn",
        "slug": "/dan-phim-cach-nhiet-van-phong-cmc-creative-space-quan-7",
    },
    {
        "project": "Central Premium",
        "type": "Chung cư",
        "area": "Quận 8",
        "code": "VT0595",
        "highlight": "Xử lý căn hộ hướng Tây nắng gắt",
        "slug": "/dan-phim-cach-nhiet-chung-cu-central-premium-quan-8",
    },
    {
        "project": "Pizza 4P's Lê Đại Hành",
        "type": "Nhà hàng",
        "area": "Quận 11",
        "code": "VT0591",
        "highlight": "Cột cẩm thạch, cửa mạ vàng sang trọng",
        "slug": "/dan-phim-cach-nhiet-nha-hang-pizza-4ps-le-dai-hanh",
    },
    {
        "project": "The Coffee House Sư Vạn Hạnh",
        "type": "Cà phê",
        "area": "Quận 10",
        "code": "VT6099",
        "highlight": "Mặt tiền kính dài, thi công ngoài giờ",
        "slug": "/dan-phim-cach-nhiet-quan-ca-phe-the-coffee-house",
    },
    {
        "project": "Steak Love",
        "type": "Nhà hàng",
        "area": "TP.HCM",
        "code": "VT1795",
        "highlight": "Thi công mái kính nghiêng",
        "slug": "/thi-cong-phim-cach-nhiet-nha-hang-steak-love",
    },
    {
        "project": "Sale Gallery The Gió",
        "type": "Showroom",
        "area": "Dĩ An, Bình Dương",
        "code": "VT5095",
        "highlight": "Mái kính tam giác hiện đại",
        "slug": "/dan-phim-cach-nhiet-showroom-the-gio-di-an-binh-duong",
    },
]

# Codes with '?' in vtechfilm-seo/SKILL.md that require asking the user if requested specifically
UNKNOWN_SPEC_ITEMS = {
    "vt4586": "Mã phim VT4586 hiện chưa có thông số kỹ thuật chi tiết (VLT, UV, IR, TSER) trong dữ liệu VTECH FILM. Bạn vui lòng cung cấp thông số VT4586 hoặc chọn mã khác nhé?",
    "vt8585": "Mã phim VT8585 hiện chưa có giá niêm yết và cần xác nhận lại độ phản gương (80%) trong dữ liệu VTECH FILM. Bạn muốn điền mức giá nào cho VT8585?",
}


def get_zoneinfo(tz_name: str = DEFAULT_TZ) -> timezone:
    try:
        return ZoneInfo(tz_name)  # type: ignore
    except Exception:
        if "Ho_Chi_Minh" in tz_name or "Bangkok" in tz_name or "Saigon" in tz_name:
            return timezone(timedelta(hours=7))
        if tz_name.upper() == "UTC":
            return timezone.utc
        return timezone(timedelta(hours=7))


def parse_natural_schedule(
    text: str,
    tz_name: str = DEFAULT_TZ,
    now_local: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Parses Vietnamese & English natural language date/time and recurrence expressions."""
    tz = get_zoneinfo(tz_name)
    if now_local is None:
        now_local = datetime.now(tz).replace(second=0, microsecond=0)
    else:
        if now_local.tzinfo is None:
            now_local = now_local.replace(tzinfo=tz)
        else:
            now_local = now_local.astimezone(tz)

    lower = text.lower()
    recurrence: Optional[str] = None

    if any(p in lower for p in ["mỗi ngày trong tuần tới", "every day next week"]):
        recurrence = "DAILY_7"
    elif any(p in lower for p in ["mỗi ngày", "hàng ngày", "hằng ngày", "every day", "daily"]):
        recurrence = "DAILY"
    elif any(p in lower for p in ["mỗi tuần", "hàng tuần", "every week", "weekly"]):
        recurrence = "WEEKLY"

    hour: Optional[int] = None
    minute: int = 0

    m_vn_time = re.search(
        r"\b(\d{1,2})\s*(?:giờ|h|:)\s*(\d{1,2})?\s*(sáng|trưa|chiều|tối|đêm|am|pm)?\b",
        lower,
    )
    if m_vn_time:
        h_val = int(m_vn_time.group(1))
        m_val = int(m_vn_time.group(2)) if m_vn_time.group(2) else 0
        meridiem = (m_vn_time.group(3) or "").strip()
        if meridiem in ("chiều", "tối", "đêm", "pm") and h_val < 12:
            h_val += 12
        elif meridiem in ("sáng", "am") and h_val == 12:
            h_val = 0
        if 0 <= h_val <= 23 and 0 <= m_val <= 59:
            hour, minute = h_val, m_val

    if hour is None:
        m_en_time = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", lower)
        if m_en_time:
            h_val = int(m_en_time.group(1))
            m_val = int(m_en_time.group(2)) if m_en_time.group(2) else 0
            mer = m_en_time.group(3)
            if mer == "pm" and h_val < 12:
                h_val += 12
            elif mer == "am" and h_val == 12:
                h_val = 0
            hour, minute = h_val, m_val

    day_offset: Optional[int] = None
    explicit_date: Optional[Tuple[int, int, int]] = None

    m_iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})(?:[t\s]+(\d{1,2}):(\d{2}))?", lower)
    if m_iso:
        explicit_date = (int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        if m_iso.group(4) and hour is None:
            hour = int(m_iso.group(4))
            minute = int(m_iso.group(5))

    if not explicit_date:
        m_dmy = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\b", lower)
        if m_dmy:
            d_val = int(m_dmy.group(1))
            mo_val = int(m_dmy.group(2))
            y_val = int(m_dmy.group(3)) if m_dmy.group(3) else now_local.year
            if 1 <= d_val <= 31 and 1 <= mo_val <= 12:
                explicit_date = (y_val, mo_val, d_val)

    if any(k in lower for k in ["ngày mai", "sáng mai", "chiều mai", "tối mai", "trưa mai", "tomorrow"]):
        day_offset = 1
    elif any(k in lower for k in ["ngày mốt", "ngày kia", "day after tomorrow"]):
        day_offset = 2
    elif any(k in lower for k in ["tuần tới", "tuần sau", "next week"]):
        day_offset = 1
    elif any(k in lower for k in ["hôm nay", "chiều nay", "tối nay", "today", "tonight"]):
        day_offset = 0

    has_schedule_keyword = any(
        w in lower
        for w in [
            "lên lịch", "hẹn giờ", "schedule", "vào lúc", "vào 9", "sáng mai",
            "chiều mai", "tối mai", "ngày mai", "tuần tới", "mỗi ngày", "hàng ngày",
            "tomorrow", "next week", "daily", "weekly",
        ]
    )

    if not (explicit_date or day_offset is not None or hour is not None or recurrence or has_schedule_keyword):
        return {
            "has_schedule": False,
            "needs_time_clarification": False,
            "scheduled_at_local": None,
            "scheduled_at_utc": None,
            "recurrence_rule": None,
            "timezone": tz_name,
        }

    if has_schedule_keyword and explicit_date is None and day_offset is None and hour is None and not recurrence:
        return {
            "has_schedule": True,
            "needs_time_clarification": True,
            "scheduled_at_local": None,
            "scheduled_at_utc": None,
            "recurrence_rule": None,
            "timezone": tz_name,
        }

    default_hour = hour if hour is not None else 9
    default_min = minute if hour is not None else 0

    if explicit_date:
        target_local = datetime(
            explicit_date[0], explicit_date[1], explicit_date[2],
            default_hour, default_min, tzinfo=tz
        )
    else:
        offset = day_offset if day_offset is not None else 1
        base_day = now_local + timedelta(days=offset)
        target_local = base_day.replace(hour=default_hour, minute=default_min, second=0, microsecond=0)
        if target_local <= now_local:
            target_local = target_local + timedelta(days=1)

    target_utc = target_local.astimezone(timezone.utc)
    return {
        "has_schedule": True,
        "needs_time_clarification": False,
        "scheduled_at_local": target_local.isoformat(),
        "scheduled_at_utc": target_utc.replace(microsecond=0).isoformat(),
        "recurrence_rule": recurrence,
        "timezone": tz_name,
        "human_summary": target_local.strftime("%d/%m/%Y %H:%M") + (f" ({recurrence})" if recurrence else ""),
    }


def extract_intent_and_entities(text: str, tz_name: str = DEFAULT_TZ, now_local: Optional[datetime] = None) -> Dict[str, Any]:
    """Extracts intent, platform, topic, post count, tone, audience, docx path, and schedule from natural language."""
    clean_text = text.strip()
    lower = clean_text.lower()

    platforms: List[str] = []
    if any(k in lower for k in ["2 web", "hai web", "cả 2 web", "both websites", "cả hai web"]):
        platforms = ["vtechfilm", "onex"]
    else:
        if any(k in lower for k in ["facebook", "fanpage", "fb"]):
            platforms.append("facebook")
        if any(k in lower for k in ["onex", "phimcachnhietdankinh", "web phụ", "web vệ tinh", "site vệ tinh"]):
            platforms.append("onex")
        if any(
            k in lower
            for k in ["vtechfilm", "web chính", "website chính", "wordpress", "website", "lên web"]
        ):
            if "onex" not in platforms:
                platforms.append("vtechfilm")

    count = 1
    m_count = re.search(r"\b(?:tạo|viết|làm|create|write|generate)\s+(\d+)\s*(?:bài|post|articles?)\b", lower)
    if m_count:
        count = max(1, min(int(m_count.group(1)), 10))

    docx_path: Optional[str] = None
    m_path = re.search(r'["\']([A-Za-z]:\\[^"\']+)["\']', clean_text)
    if m_path:
        docx_path = m_path.group(1).strip()
    else:
        m_unquoted = re.search(r'([A-Za-z]:\\[^\n\r"\']+?(?:\.docx\b|(?=\s+(?:cho|lên|vào|đặt lịch|hẹn|lúc)\b)|$))', clean_text, re.IGNORECASE)
        if m_unquoted:
            candidate_p = m_unquoted.group(1).strip()
            if Path(candidate_p).exists():
                docx_path = candidate_p

    schedule_info = parse_natural_schedule(clean_text, tz_name=tz_name, now_local=now_local)

    tone = "chuyên nghiệp, chuẩn Yoast SEO"
    if any(w in lower for w in ["thân thiện", "gần gũi", "friendly", "casual"]):
        tone = "thân thiện, gần gũi"
    elif any(w in lower for w in ["thuyết phục", "bán hàng", "quảng cáo", "persuasive", "sales"]):
        tone = "thuyết phục, thu hút chuyển đổi"
    elif any(w in lower for w in ["ngắn gọn", "súc tích", "concise", "short"]):
        tone = "ngắn gọn, súc tích"

    audience = "khách hàng cá nhân & doanh nghiệp tại TP.HCM"
    if any(w in lower for w in ["chung cư", "căn hộ", "chủ nhà", "nhà kính"]):
        audience = "cư dân chung cư & chủ căn hộ"
    elif any(w in lower for w in ["văn phòng", "công ty", "doanh nghiệp", "tòa nhà"]):
        audience = "quản lý tòa nhà & văn phòng doanh nghiệp"
    elif any(w in lower for w in ["ô tô", "xế hộp", "chủ xe", "vf3", "5 chỗ", "7 chỗ"]):
        audience = "chủ xe ô tô"

    topic = ""
    topic_patterns = [
        r"(?:về chủ đề|về|chủ đề|topic|about)\s+(.+?)(?=\s+(?:và đăng|đăng lên|trên|cho website|cho web|vào lúc|vào \d|mỗi ngày|hàng ngày|sáng mai|chiều mai|ngày mai|on facebook|for website|tomorrow)|[.?!]|$)",
        r"(?:viết bài|tạo \d+ bài seo|tạo bài|soạn bài|write a post|create a post)\s+(?:cho website\s+)?(?:về\s+)?(.+?)(?=\s+(?:và đăng|đăng lên|trên|cho website|vào lúc|vào \d|sáng mai|ngày mai)|[.?!]|$)",
    ]
    for pat in topic_patterns:
        m_top = re.search(pat, clean_text, re.IGNORECASE)
        if m_top:
            candidate = m_top.group(1).strip(" \"'.,")
            candidate = re.sub(
                r"\s+(?:và\s+)?(?:đăng|up)\s+(?:lên|trên)\s+(?:facebook|website|web|fanpage).*$",
                "",
                candidate,
                flags=re.IGNORECASE,
            ).strip()
            if candidate and candidate.lower() not in ("này", "đó", "mới", "bài này", "this", "this post"):
                topic = candidate
                break

    intent = "create_post"
    if docx_path or ("up bài" in lower and m_path):
        intent = "import_docx"
    elif any(k in lower for k in ["hủy lịch", "xoá lịch", "xóa lịch", "cancel schedule", "cancel job"]):
        intent = "cancel_job"
    elif any(k in lower for k in ["thử lại", "chạy lại", "retry"]):
        intent = "retry_job"
    elif any(k in lower for k in ["duyệt bài", "phê duyệt", "approve", "đồng ý đăng", "ok duyệt"]):
        intent = "approve_post"
    elif any(k in lower for k in ["viết lại", "tạo lại", "regenerate", "đổi giọng văn"]):
        intent = "regenerate_post"
    elif any(k in lower for k in ["sửa tiêu đề", "đổi tiêu đề", "cập nhật tiêu đề", "chỉnh sửa", "edit title"]):
        intent = "edit_post"
    elif (
        any(k in lower for k in ["đăng bài này", "lên lịch đăng bài", "lên lịch bài", "schedule this", "publish this"])
        and schedule_info["has_schedule"]
        and not topic
    ):
        intent = "schedule_post"
    elif any(k in lower for k in ["đăng ngay", "xuất bản ngay", "publish now", "publish immediately"]) and not topic:
        intent = "publish_now"
    elif count > 1:
        intent = "create_multiple_posts"
    elif schedule_info["has_schedule"] and not topic and any(k in lower for k in ["lên lịch", "schedule"]):
        intent = "schedule_post"

    # Check if topic references a product code with '?' in vtechfilm-seo/SKILL.md
    unknown_spec_question = None
    for code_key, q_msg in UNKNOWN_SPEC_ITEMS.items():
        if code_key in lower:
            unknown_spec_question = q_msg
            break

    return {
        "intent": intent,
        "topic": topic,
        "platforms": platforms,
        "primary_platform": platforms[0] if platforms else "",
        "count": count,
        "tone": tone,
        "audience": audience,
        "docx_path": docx_path,
        "schedule": schedule_info,
        "unknown_spec_question": unknown_spec_question,
    }


def _select_matching_case_study(topic_lower: str) -> Dict[str, str]:
    """Finds the best matching real VTECH case study from vtechfilm-seo/SKILL.md."""
    for cs in VTECH_CASE_STUDIES:
        if cs["area"].lower() in topic_lower or cs["project"].lower() in topic_lower:
            return cs
    if "chung cư" in topic_lower or "căn hộ" in topic_lower:
        return VTECH_CASE_STUDIES[2]  # Central Premium Quận 8 (VT0595)
    if "nhà hàng" in topic_lower or "quán" in topic_lower:
        return VTECH_CASE_STUDIES[3]  # Pizza 4P's (VT0591)
    if "cà phê" in topic_lower or "coffee" in topic_lower:
        return VTECH_CASE_STUDIES[4]  # The Coffee House (VT6099)
    if "showroom" in topic_lower or "bình dương" in topic_lower:
        return VTECH_CASE_STUDIES[6]  # Sale Gallery The Gió (VT5095)
    return VTECH_CASE_STUDIES[1]  # Default: CMC Creative Space Quận 7 (VT7091)


def generate_content_draft(
    topic: str,
    platform: str,
    tone: str = "chuyên nghiệp, chuẩn Yoast SEO",
    audience: str = "khách hàng cá nhân & doanh nghiệp",
    variant_index: int = 0,
    scheduled_at_local: Optional[str] = None,
    timezone_name: str = DEFAULT_TZ,
    recurrence_rule: Optional[str] = None,
) -> Dict[str, Any]:
    """Generates a structured, Yoast-SEO-validated content item grounded in vtechfilm-seo/SKILL.md data."""
    cap = PLATFORM_REGISTRY.get(platform, PLATFORM_REGISTRY["vtechfilm"])
    clean_topic = topic.strip()
    core_kw = clean_topic[11:].strip() if clean_topic.lower().startswith("giới thiệu ") else clean_topic
    focus_kw = core_kw.lower()

    variant_suffixes = [
        "Bảng Giá & Thông Số Thực Tế",
        "Kinh Nghiệm Chọn Mã Phim & Quy Trình Chuẩn",
        "Giải Pháp Chống Nóng & Bảo Hành 10 Năm",
        "Đánh Giá Chi Tiết & Công Trình Thực Tế",
    ]
    site_tag = "VTECH FILM" if platform != "onex" else "OneX"
    suffix = variant_suffixes[variant_index % len(variant_suffixes)]
    if platform == "onex" and variant_index == 0:
        suffix = variant_suffixes[1]

    cs = _select_matching_case_study(focus_kw)
    is_car_topic = any(k in focus_kw for k in ["ô tô", "xe 5 chỗ", "xe 7 chỗ", "vf3", "kính lái"])

    if platform == "facebook":
        title = f"🔥 {clean_topic.upper()} – VTECH FILM LÀM MÁT CHO NGƯỜI VIỆT"
        slug = slugify_vn(f"fb-{clean_topic}-{variant_index + 1}")
        seo_title = ""
        meta_desc = ""
        body_html = (
            f"<p>🌟 <strong>{clean_topic}</strong> – Giải pháp chống nắng nóng chính hãng từ <strong>VTECH FILM</strong> "
            f"(<em>Slogan: Làm mát cho người Việt</em>) dành cho {audience}!</p>\n"
            f"<ul>\n"
            f"  <li>✅ Công nghệ <strong>Nano Ceramic</strong> không cản sóng điện thoại, GPS, 4G hay thẻ VETC/ePass.</li>\n"
            f"  <li>✅ Cản tia UV tới 99,9% – 100%, cách nhiệt hồng ngoại IR tới 99,5% (các mã <strong>VT7091, VT0595, VT5095</strong>).</li>\n"
            f"  <li>✅ Giá niêm yết minh bạch từ <strong>220.000đ/m²</strong> (phản quang) và <strong>320.000đ – 800.000đ/m²</strong> (Nano Ceramic).</li>\n"
            f"  <li>✅ Bảo hành điện tử chính hãng lên đến <strong>10 năm</strong>, khảo sát &amp; đo máy tận nơi miễn phí.</li>\n"
            f"</ul>\n"
            f"<p>📍 Trụ sở: 684 Phạm Văn Bạch, Phường An Hội Tây, TP.HCM | Cửa hàng: 54/56 Tân Thới Hiệp 29, TP.HCM.<br>"
            f"📞 Liên hệ ngay Hotline/Zalo: <strong>0967 725 714</strong> – <strong>0967 707 383</strong> (hoặc <strong>0522 187 187</strong>)!</p>"
        )
    else:
        title = f"{clean_topic[:1].upper() + clean_topic[1:]}: {suffix}"
        base_slug = slugify_vn(core_kw)
        if variant_index > 0:
            slug = f"{base_slug}-phan-{variant_index + 1}"
        elif platform == "onex":
            slug = f"dich-vu-{base_slug}-chuyen-nghiep"
        else:
            slug = base_slug

        # Yoast SEO Title (30-60 chars) with focus_kw
        raw_seo = f"{clean_topic[:1].upper() + clean_topic[1:]} Giá Tốt | {site_tag}"
        if len(raw_seo) > 58:
            raw_seo = f"{clean_topic[:40].strip()} | {site_tag}"
        if len(raw_seo) < 30:
            raw_seo = f"{raw_seo} Chính Hãng"
        seo_title = raw_seo[:60]

        # Yoast Meta Description (120-160 chars) with focus_kw
        raw_meta = (
            f"Báo giá {focus_kw} chính hãng {site_tag}. "
            f"Phim Nano Ceramic cản 99% UV, bảo hành điện tử 10 năm, khảo sát tận nơi TP.HCM. "
            f"Gọi ngay 0967 725 714."
        )
        if len(raw_meta) < 120:
            raw_meta += " Thi công nhanh gọn, chuẩn kỹ thuật."
        meta_desc = raw_meta[:158]

        img_alt = f"Thi công {focus_kw} chính hãng {site_tag}"

        if is_car_topic:
            spec_table_html = (
                "<table>\n"
                "  <thead><tr><th>Gói Phim Ô Tô</th><th>VinFast VF3</th><th>Xe 5 Chỗ</th><th>Xe 7 Chỗ</th><th>Tổ Hợp Mã Phim (Kính lái / Sườn trước / Sau)</th></tr></thead>\n"
                "  <tbody>\n"
                "    <tr><td><strong>Trung cấp</strong></td><td>2.500.000đ</td><td>3.300.000đ</td><td>4.000.000đ</td><td>VT4586 (VF3: VT6086) / VT2086 / VT0586</td></tr>\n"
                "    <tr><td><strong>Cao cấp (BH 7 năm)</strong></td><td>3.500.000đ</td><td>4.800.000đ</td><td>5.700.000đ</td><td>VT7091 / VT1591 / VT0591</td></tr>\n"
                "    <tr><td><strong>Siêu cao cấp (BH 10 năm)</strong></td><td>5.500.000đ</td><td>6.800.000đ</td><td>7.800.000đ</td><td>VT5095 / VT1795 / VT0595</td></tr>\n"
                "  </tbody>\n"
                "</table>"
            )
        else:
            spec_table_html = (
                "<table>\n"
                "  <thead><tr><th>Mã Phim</th><th>Màu Sắc</th><th>Độ Truyền Sáng (VLT)</th><th>Cản Tia UV</th><th>Cản Hồng Ngoại (IR)</th><th>Tổng Cản Nhiệt (TSER)</th><th>Bảo Hành</th><th>Đơn Giá (đ/m²)</th></tr></thead>\n"
                "  <tbody>\n"
                "    <tr><td><strong>VT7091</strong></td><td>Xanh nhạt</td><td>67%</td><td>99,6%</td><td>90,5%</td><td>59,8%</td><td>7 năm</td><td>500.000đ</td></tr>\n"
                "    <tr><td><strong>VT0591</strong></td><td>Đen đậm</td><td>8%</td><td>99,8%</td><td>95,5%</td><td>93,6%</td><td>7 năm</td><td>450.000đ</td></tr>\n"
                "    <tr><td><strong>VT0595</strong></td><td>Đen đậm</td><td>4,8%</td><td>100%</td><td>99,1%</td><td>92,7%</td><td>10 năm</td><td>750.000đ</td></tr>\n"
                "    <tr><td><strong>VT5095</strong></td><td>Xanh ngọc nhạt</td><td>50,5%</td><td>100%</td><td>98,7%</td><td>72,9%</td><td>10 năm</td><td>800.000đ</td></tr>\n"
                "    <tr><td><strong>Black Silver</strong></td><td>Phản quang</td><td>5,1%</td><td>94,6%</td><td>85,2%</td><td>86,5%</td><td>Chính hãng</td><td>220.000đ</td></tr>\n"
                "  </tbody>\n"
                "</table>"
            )

        body_html = (
            f"<p>Giải pháp <strong>{focus_kw}</strong> do <strong>VTECH FILM</strong> (<em>Làm mát cho người Việt</em>) "
            f"cung cấp giúp {audience} xử lý triệt để tình trạng nắng gắt, tia cực tím (UV) và tiết kiệm chi phí điện năng điều hòa. "
            f"Dòng phim Nano Ceramic thế hệ mới hoàn toàn không chứa kim loại nên không cản sóng điện thoại, GPS, 4G hay thẻ thu phí tự động VETC/ePass.</p>\n"
            f"<h2>1. Bảng thông số kỹ thuật và báo giá {focus_kw} mới nhất</h2>\n"
            f"<p>Để lựa chọn mã phim phù hợp nhất cho nhu cầu <strong>{focus_kw}</strong>, quý khách có thể tham khảo bảng thông số đo thực tế và giá niêm yết chính hãng dưới đây:</p>\n"
            f"{spec_table_html}\n"
            f'<figure class="wp-block-image"><img src="https://vtechfilm.com/wp-content/uploads/{slug}.webp" '
            f'alt="{img_alt}" loading="lazy" /></figure>\n'
            f"<h2>2. Công trình thực tế &amp; lý do khách hàng tin chọn {focus_kw} tại VTECH FILM</h2>\n"
            f"<p>Trong thực tế thi công tại TP.HCM, đội ngũ thợ 3–5 năm kinh nghiệm của VTECH FILM đã triển khai thành công nhiều dự án tiêu biểu như "
            f'<a href="https://vtechfilm.com{cs["slug"]}/" style="color: #e06400; font-weight: 600; text-decoration: underline;"><strong>{cs["project"]} ({cs["area"]})</strong></a> sử dụng mã phim <strong>{cs["code"]}</strong> '
            f"({cs['highlight']}). Ngoài ra, quý khách có thể xem thêm tại chuyên mục "
            f'<a href="https://vtechfilm.com/giai-phap-thi-cong/" style="color: #e06400; font-weight: 600; text-decoration: underline;">Nhật ký thi công</a> và '
            f'<a href="https://vtechfilm.com/tra-cuu-bao-hanh/" style="color: #e06400; font-weight: 600; text-decoration: underline;">Tra cứu bảo hành điện tử</a> trên website.</p>\n'
            f"<ul>\n"
            f"  <li><strong>Chống nóng tối đa:</strong> Các mã chủ lực như VT0595, VT0591, VT0586 đạt tổng cản nhiệt TSER từ 92,7% đến 94%.</li>\n"
            f"  <li><strong>Giữ sáng tự nhiên cho văn phòng &amp; kính lái:</strong> Các mã VT7091 (VLT 67%), VT6099 (VLT 60%), VT5095 (VLT 50,5%) giữ tầm nhìn thoáng đãng.</li>\n"
            f"  <li><strong>Cam kết bảo hành điện tử đến 10 năm:</strong> Khảo sát và đem mẫu đo máy trực tiếp tận nơi hoàn toàn miễn phí.</li>\n"
            f"</ul>\n"
            f"<h2>3. Câu hỏi thường gặp (FAQ) và liên hệ khảo sát {focus_kw}</h2>\n"
            f"<p><strong>Phim cách nhiệt Nano Ceramic có làm tối phòng không?</strong><br>"
            f"Nếu bạn muốn giữ ánh sáng tự nhiên, kỹ thuật viên sẽ tư vấn mã <strong>VT7091</strong> (500.000đ/m²) hoặc <strong>VT5095</strong> (800.000đ/m²). "
            f"Nếu cần chống nắng gắt hướng Tây, mã <strong>VT0595</strong> hoặc <strong>VT0591</strong> là lựa chọn tối ưu.</p>\n"
            f"<p>Quý khách cần khảo sát &amp; báo giá <strong>{focus_kw}</strong> vui lòng liên hệ <strong>VTECH FILM</strong>:<br>"
            f"📍 <strong>Trụ sở:</strong> 684 Phạm Văn Bạch, Phường An Hội Tây, TP.HCM<br>"
            f"📍 <strong>Cửa hàng:</strong> 54/56 Tân Thới Hiệp 29, Phường Tân Thới Hiệp, TP.HCM<br>"
            f"📞 <strong>Hotline / Zalo tư vấn (8:00 – 20:00, Thứ 2 – Thứ 7):</strong> <strong>0967 725 714</strong> – <strong>0967 707 383</strong> – <strong>0522 187 187</strong>.</p>"
        )

    draft = {
        "title": title,
        "body_html": body_html,
        "excerpt": f"Giải pháp {focus_kw} chính hãng VTECH FILM – Làm mát cho người Việt. Liên hệ 0967 725 714.",
        "platform": platform,
        "destination": cap.hostname,
        "topic": clean_topic,
        "tone": tone,
        "audience": audience,
        "seo_title": seo_title,
        "meta_description": meta_desc,
        "slug": slug,
        "focus_keyphrase": focus_kw,
        "category": "Tin tức",
        "featured_image": f"{slug}.webp",
        "featured_alt": f"Thi công {focus_kw} - {site_tag}",
        "media": [{"name": f"{slug}.webp", "alt": f"Thi công {focus_kw} - {site_tag}"}],
        "status": ContentStatus.PENDING_APPROVAL.value,
        "reviewed": False,
        "scheduled_at": scheduled_at_local,
        "timezone": timezone_name,
        "recurrence_rule": recurrence_rule,
    }
    draft["validation"] = validate_content_item(draft, require_approval=False)
    return draft
