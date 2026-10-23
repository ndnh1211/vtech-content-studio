"""Content validation and DOCX/WebP pipeline integration reusing existing VTECH tools/."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

from publishing_app.models import PlatformCapability, load_platform_registry


TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
MEDIA_STORAGE_DIR = Path(__file__).resolve().parent / "data" / "media"
PLATFORM_REGISTRY = load_platform_registry(TOOLS_DIR / "sites.json")


def slugify_vn(text: str) -> str:
    """Converts Vietnamese text into a clean URL slug matching tools/build_manifest.py & preflight.py."""
    if not text:
        return ""
    s = text.lower().replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return re.sub(r"-+", "-", s)


def convert_image_to_webp(src_img: Path, dst_webp: Path, max_width: int = 1200, quality: int = 82) -> Path:
    """Converts an image to optimized .webp format matching tools/make_webp.py."""
    dst_webp.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src_img) as im:
        im = im.convert("RGB")
        if im.width > max_width:
            new_h = int(im.height * max_width / im.width)
            im = im.resize((max_width, new_h), Image.LANCZOS)
        im.save(dst_webp, "WEBP", quality=quality, method=6)
    return dst_webp


def validate_content_item(
    item: Dict[str, Any],
    require_approval: bool = False,
    other_site_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Validates a content item using the rules from tools/preflight.py plus vtechfilm-seo skill checks."""
    fails: List[str] = []
    warns: List[str] = []
    infos: List[str] = []

    platform_key = (item.get("platform") or "").strip().lower()
    cap: Optional[PlatformCapability] = PLATFORM_REGISTRY.get(platform_key)

    if not platform_key or not cap:
        fails.append(f"Nền tảng đích chưa hợp lệ hoặc chưa được chọn: '{platform_key}'.")
    else:
        infos.append(f"Nền tảng đích: {cap.name} ({cap.hostname})")
        if not cap.supports_seo_meta and (item.get("seo_title") or item.get("meta_description")):
            infos.append(f"Lưu ý: {cap.name} không sử dụng thẻ Yoast SEO Title/Meta Description.")

    title = (item.get("title") or "").strip()
    body_html = (item.get("body_html") or "").strip()

    if not title:
        fails.append("Thiếu tiêu đề bài viết (Title/H1).")
    if not body_html:
        fails.append("Thiếu nội dung bài viết (Body).")

    if require_approval and not item.get("reviewed"):
        fails.append("Bài viết chưa được người dùng duyệt (reviewed=false). Cần phê duyệt trước khi đăng hoặc lên lịch.")

    if cap and cap.supports_seo_meta:
        slug = (item.get("slug") or "").strip()
        seo_title = (item.get("seo_title") or "").strip()
        meta_desc = (item.get("meta_description") or "").strip()
        kw = (item.get("focus_keyphrase") or "").strip().lower()

        if not slug or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug):
            fails.append(f"Slug không hợp lệ (chỉ dùng chữ thường, không dấu, gạch nối): '{slug}'")

        n_seo = len(seo_title)
        if n_seo == 0:
            fails.append("Thiếu SEO Title.")
        elif not (30 <= n_seo <= 65):
            warns.append(f"SEO title dài {n_seo} ký tự (khuyến nghị 30-60 ký tự).")

        n_meta = len(meta_desc)
        if n_meta == 0:
            fails.append("Thiếu Meta Description.")
        elif not (110 <= n_meta <= 165):
            warns.append(f"Meta description dài {n_meta} ký tự (khuyến nghị 120-160 ký tự).")

        if not kw:
            fails.append("Thiếu Focus Keyphrase (từ khóa trọng tâm SEO).")
        else:
            plain_first_p = ""
            m_p = re.search(r"<p[^>]*>(.*?)</p>", body_html, re.IGNORECASE | re.DOTALL)
            if m_p:
                plain_first_p = re.sub(r"<[^>]+>", "", m_p.group(1))
            h2_texts = " ".join(
                re.sub(r"<[^>]+>", "", h)
                for h in re.findall(r"<h2[^>]*>(.*?)</h2>", body_html, re.IGNORECASE | re.DOTALL)
            )
            for label, txt in (
                ("Tiêu đề H1", title),
                ("SEO Title", seo_title),
                ("Meta Description", meta_desc),
                ("Đoạn mở đầu", plain_first_p),
                ("Thẻ tiêu đề H2", h2_texts),
            ):
                if txt and kw not in txt.lower():
                    warns.append(f"Từ khóa '{kw}' chưa xuất hiện trong {label} (theo chuẩn vtechfilm-seo).")

    if body_html:
        if re.search(r"<h1\b", body_html, re.IGNORECASE):
            fails.append("Nội dung thân bài chứa thẻ <h1> (H1 đã do giao diện tự sinh từ tiêu đề bài viết).")

        for img_tag in re.findall(r"<img[^>]*>", body_html, re.IGNORECASE):
            if not re.search(r'alt="[^"]+"', img_tag, re.IGNORECASE):
                fails.append(f"Thẻ <img> thiếu thuộc tính alt mô tả: {img_tag[:60]}")

        if re.search(r"<!--image:", body_html):
            warns.append("Còn placeholder <!--image:...--> sẽ được thay thế tự động khi tải ảnh lên.")

        allowed_hotlines = {
            re.sub(r"\D", "", h)
            for h in (cap.hotlines if cap else ["0967 725 714", "0967 707 383", "0522 187 187"])
        }
        plain_body = re.sub(r"<[^>]+>", " ", body_html)
        for ph in set(re.findall(r"\b0\d{2,3}[ .]?\d{3}[ .]?\d{3,4}\b", plain_body)):
            digits = re.sub(r"\D", "", ph)
            if digits not in allowed_hotlines:
                fails.append(
                    f"Số điện thoại '{ph}' không thuộc hotline chuẩn VTECH (0967 725 714 / 0967 707 383 / 0522 187 187)."
                )

    if item.get("featured_image") and not (item.get("featured_alt") or "").strip():
        warns.append("Ảnh đại diện (featured_image) nên có thẻ alt mô tả.")

    if other_site_items:
        for other in other_site_items:
            if other.get("id") == item.get("id"):
                continue
            if other.get("platform") != platform_key and cap and cap.supports_seo_meta:
                if item.get("slug") and other.get("slug") == item.get("slug"):
                    fails.append(f"Slug '{item.get('slug')}' bị trùng với bài trên site '{other.get('platform')}'.")
                if item.get("seo_title") and other.get("seo_title") == item.get("seo_title"):
                    fails.append("SEO Title trùng lặp giữa các website (vi phạm quy tắc chống trùng lặp SEO).")
                if item.get("meta_description") and other.get("meta_description") == item.get("meta_description"):
                    fails.append("Meta Description trùng lặp giữa các website.")

    score = max(0, 100 - len(fails) * 25 - len(warns) * 5)
    return {
        "passed": len(fails) == 0,
        "score": score,
        "fails": fails,
        "warns": warns,
        "infos": infos,
    }


def import_from_docx_pipeline(
    docx_path: str,
    platform: str = "vtechfilm",
    extra_image_paths: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    """Reuses tools/docx_extract.py and converts all embedded/folder images to .webp."""
    src_path = Path(docx_path)
    folder_images: List[Path] = []

    if src_path.is_dir():
        docx_files = [f for f in src_path.glob("*.docx") if not f.name.startswith("~$")]
        if not docx_files:
            raise FileNotFoundError(f"Không tìm thấy file .docx nào trong thư mục: {docx_path}")
        # Also collect any sibling image files in the folder or subfolders
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            folder_images.extend(src_path.rglob(ext))
        src_path = docx_files[0]
    elif src_path.exists():
        # Check if parent folder has sibling images when user points to a .docx inside a project folder
        parent_dir = src_path.parent
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            folder_images.extend(parent_dir.glob(ext))
    else:
        raise FileNotFoundError(f"Không tìm thấy file hoặc thư mục: {docx_path}")

    if extra_image_paths:
        folder_images.extend(extra_image_paths)

    with tempfile.TemporaryDirectory(prefix="vtech_docx_") as tmp_dir:
        out_dir = Path(tmp_dir) / "extracted"
        cmd = [
            sys.executable,
            str(TOOLS_DIR / "docx_extract.py"),
            "--docx",
            str(src_path),
            "--out",
            str(out_dir),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"Lỗi trích xuất DOCX: {proc.stderr or proc.stdout}")

        content_html = (out_dir / "content.html").read_text(encoding="utf-8")
        seo_meta = json.loads((out_dir / "seo.json").read_text(encoding="utf-8"))

        seo_fields = seo_meta.get("seo", {})
        title = seo_meta.get("h1") or src_path.stem
        kw = seo_fields.get("focus_keyphrase") or title.lower()
        slug = slugify_vn(seo_fields.get("slug") or title)
        seo_title = seo_fields.get("seo_title") or f"{title[:42]} | VTECH FILM"
        meta_desc = seo_fields.get("meta_description") or (seo_meta.get("first_paragraph") or title)[:155]
        default_alt = seo_fields.get("image_alt") or title or "Phim cách nhiệt VTECH FILM"

        # Prepare persistent media directory for this slug
        slug_media_dir = MEDIA_STORAGE_DIR / slug
        slug_media_dir.mkdir(parents=True, exist_ok=True)

        extracted_imgs = sorted((out_dir / "images").glob("*.*"))
        source_images = extracted_imgs if extracted_imgs else sorted(set(folder_images))

        media_items: List[Dict[str, Any]] = []
        placeholder_to_webp: Dict[str, Dict[str, str]] = {}

        for idx, img_p in enumerate(source_images):
            webp_name = f"{slug}-{idx + 1}.webp"
            dst_webp = slug_media_dir / webp_name
            try:
                convert_image_to_webp(img_p, dst_webp)
            except Exception:
                shutil.copy2(img_p, dst_webp)
            alt_text = f"{default_alt} - hình {idx + 1}"
            url_path = f"/media/{slug}/{webp_name}"
            item_info = {
                "name": webp_name,
                "local_path": str(dst_webp),
                "url": url_path,
                "alt": alt_text,
            }
            media_items.append(item_info)
            placeholder_to_webp[img_p.name] = item_info

        # Replace <!--image:...--> placeholders with converted .webp images
        def _replace_img_placeholder(match: re.Match) -> str:
            orig_name = match.group(1)
            mapped = placeholder_to_webp.get(orig_name)
            if mapped:
                return (
                    f'<figure class="wp-block-image"><img src="{mapped["url"]}" '
                    f'alt="{mapped["alt"]}" loading="lazy" /></figure>'
                )
            return (
                f'<figure class="wp-block-image"><img src="/media/{slug}/{orig_name}" '
                f'alt="{default_alt}" loading="lazy" /></figure>'
            )

        clean_html = re.sub(r"<!--image:([^>]+)-->", _replace_img_placeholder, content_html)

        # If DOCX had no inline image placeholders, but folder had images, insert them evenly after H2 sections
        if not extracted_imgs and media_items:
            h2_positions = [m.end() for m in re.finditer(r"</h2>\s*(?:<p>.*?</p>)?", clean_html, flags=re.DOTALL)]
            if h2_positions:
                parts = []
                last_idx = 0
                for i, pos in enumerate(h2_positions):
                    parts.append(clean_html[last_idx:pos])
                    if i < len(media_items):
                        m_item = media_items[i]
                        parts.append(
                            f'\n<figure class="wp-block-image"><img src="{m_item["url"]}" '
                            f'alt="{m_item["alt"]}" loading="lazy" /></figure>'
                        )
                    last_idx = pos
                parts.append(clean_html[last_idx:])
                clean_html = "".join(parts)

        return {
            "title": title,
            "body_html": clean_html,
            "excerpt": (seo_meta.get("first_paragraph") or "")[:220],
            "platform": platform,
            "destination": PLATFORM_REGISTRY.get(platform, PLATFORM_REGISTRY["vtechfilm"]).hostname,
            "topic": title,
            "seo_title": seo_title[:60],
            "meta_description": meta_desc[:158],
            "slug": slug,
            "focus_keyphrase": kw,
            "featured_image": media_items[0]["url"] if media_items else f"{slug}.webp",
            "featured_alt": default_alt,
            "media": media_items,
            "source_docx": str(src_path),
        }
