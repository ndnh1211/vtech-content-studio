"""Bước 3: kiểm tra manifest trước khi đăng (offline, KHÔNG gọi mạng).
Dùng: python tools/preflight.py content/<slug>/vtechfilm.json [content/<slug>/onex.json]
Thoát mã 1 nếu có lỗi (FAIL). Link nội bộ chỉ liệt kê để kiểm online ở bước verify.
"""
import json, re, sys, unicodedata
from pathlib import Path
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
SITES = json.loads((Path(__file__).parent / "sites.json").read_text(encoding="utf-8"))
fails = warns = 0

def R(level, msg):
    global fails, warns
    if level == "FAIL": fails += 1
    if level == "WARN": warns += 1
    print(f"  [{level}] {msg}")

def check(path):
    p = Path(path); m = json.loads(p.read_text(encoding="utf-8"))
    print(f"== {p}  ({m.get('hostname')})")
    site = SITES.get(m.get("site"))
    if not site or site["hostname"] != m.get("hostname"): R("FAIL", "hostname không khớp sites.json")
    if not m.get("reviewed"): R("FAIL", "reviewed=false (chưa có người duyệt)")
    s = m["seo"]; slug = m["slug"]
    n = len(s["title"]); R("OK" if 30 <= n <= 60 else "WARN", f"SEO title {n} ký tự (nên 30-60)")
    n = len(s["meta_description"]); R("OK" if 120 <= n <= 160 else "WARN", f"Meta {n} ký tự (nên 120-160)")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug): R("FAIL", f"slug không hợp lệ (chữ thường, không dấu, gạch nối): {slug!r}")
    kw = s["focus_keyphrase"].lower()
    if not kw: R("FAIL", "thiếu focus keyphrase")
    else:
        for name, txt in (("H1", m["title"]), ("SEO title", s["title"]), ("meta", s["meta_description"]), ("đoạn đầu", m.get("first_paragraph", ""))):
            if kw not in txt.lower(): R("WARN", f"keyphrase {kw!r} không có trong {name}")
    fi = m["featured_image"]
    if not fi.get("alt"): R("FAIL", "ảnh đại diện thiếu alt")
    f = p.parent / fi["file"] if fi.get("file") else None
    if not f or not f.exists(): R("FAIL", f"file ảnh đại diện không tồn tại: {fi.get('file')!r}")
    else:
        kb = f.stat().st_size / 1024
        R("OK" if f.suffix == ".webp" and kb <= 150 else "WARN", f"ảnh {f.name} {kb:.0f} KB ({f.suffix})")
    cf = p.parent / m["content_file"]
    if not cf.exists(): R("FAIL", "thiếu content_file"); return m
    html = cf.read_text(encoding="utf-8")
    if re.search(r"<h1", html, re.I): R("FAIL", "nội dung chứa H1 (H1 do theme sinh từ title)")
    for im in re.findall(r"<img[^>]*>", html):
        if not re.search(r'alt="[^"]+"', im): R("FAIL", f"img thiếu alt: {im[:60]}")
    if re.search(r"<!--image:", html): R("WARN", "còn placeholder ảnh <!--image:..--> chưa thay bằng ảnh đã upload")
    allowed = {re.sub(r"\D", "", h) for h in site["hotlines"]} if site else set()
    for ph in set(re.findall(r"\b0\d{2,3}[ .]?\d{3}[ .]?\d{3,4}\b", re.sub(r"<[^>]+>", " ", html))):
        if re.sub(r"\D", "", ph) not in allowed: R("FAIL", f"số điện thoại không thuộc hotline chuẩn: {ph}")
    links = sorted(set(re.findall(r'href="([^"]+)"', html)))
    internal = [l for l in links if m["hostname"] in l or l.startswith("/")]
    R("INFO", f"{len(links)} link ({len(internal)} nội bộ) — kiểm HTTP 200 ở bước verify online: {internal[:5]}")
    if m["status"] == "future":
        try:
            if datetime.fromisoformat(m["schedule_at"]) <= datetime.now(): R("FAIL", "schedule_at đã ở quá khứ")
        except Exception: R("FAIL", "schedule_at thiếu/sai định dạng ISO")
    if m["status"] not in ("draft", "future"): R("FAIL", "status chỉ được draft/future (không publish trực tiếp)")
    return m

ms = [check(a) for a in sys.argv[1:]]
if len(ms) > 1:
    print("== So khớp chéo giữa các site")
    for k in ("slug",):
        if len({x[k] for x in ms}) < len(ms): R("FAIL", f"{k} trùng giữa các site")
    if len({x["seo"]["title"] for x in ms}) < len(ms): R("FAIL", "SEO title trùng giữa các site (trùng nội dung)")
    if len({x["seo"]["meta_description"] for x in ms}) < len(ms): R("FAIL", "meta description trùng giữa các site")
    if len({x["title"] for x in ms}) < len(ms): R("WARN", "H1/title trùng giữa các site")
print(f"\nKẾT QUẢ: {fails} FAIL, {warns} WARN"); sys.exit(1 if fails else 0)
