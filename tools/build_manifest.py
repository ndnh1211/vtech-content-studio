"""Bước 2: tạo manifest riêng từng site từ thư mục source (offline).
Dùng: python tools/build_manifest.py --src content/<slug>/source --sites vtechfilm,onex
Manifest onex được đánh dấu reviewed=false: người phải viết lại tiêu đề/slug/SEO/nội dung khác site 1.
"""
import argparse, json, sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--src", required=True)
ap.add_argument("--sites", default="vtechfilm,onex")
ap.add_argument("--categories", default="Tin tức")
a = ap.parse_args()
sys.stdout.reconfigure(encoding="utf-8")
src = Path(a.src); base = src.parent
sites = json.loads((Path(__file__).parent / "sites.json").read_text(encoding="utf-8"))
meta = json.loads((src / "seo.json").read_text(encoding="utf-8"))
seo = meta["seo"]

for key in a.sites.split(","):
    site = sites[key]
    m = {
        "site": key, "hostname": site["hostname"], "platform": site["platform"],
        "reviewed": key == "vtechfilm" and False,   # luôn false cho tới khi người duyệt
        "title": meta["h1"], "slug": seo.get("slug", ""),
        "seo": {"focus_keyphrase": seo.get("focus_keyphrase", ""), "title": seo.get("seo_title", ""),
                "meta_description": seo.get("meta_description", "")},
        "categories": [c.strip() for c in a.categories.split(",")],
        "featured_image": {"file": "", "alt": seo.get("image_alt", ""), "title": ""},
        "content_file": f"source/content.html", "first_paragraph": meta["first_paragraph"],
        "status": "draft", "schedule_at": None, "timezone": "Asia/Ho_Chi_Minh",
        "notes": ["Điền featured_image.file (WEBP) sau bước make_webp.", "Đặt reviewed=true sau khi người duyệt."],
    }
    if key != "vtechfilm":
        m["notes"].insert(0, "BẮT BUỘC viết lại title/slug/SEO/nội dung khác site vtechfilm để tránh trùng; "
                             "upload ảnh lên chính site này, không hotlink từ site khác.")
    p = base / f"{key}.json"
    if p.exists(): print("BỎ QUA (đã tồn tại, không ghi đè):", p); continue
    p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Tạo", p)
