"""Bước 4: ảnh -> WEBP (<=max px, chất lượng q). Offline.
Dùng: python tools/make_webp.py --src a.png [b.png ...] --out DIR --max 1440 --q 88
"""
import argparse, sys
from pathlib import Path
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("--src", nargs="+", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--max", type=int, default=1440)
ap.add_argument("--q", type=int, default=88)
ap.add_argument("--rename", help="tên không đuôi, dùng khi chỉ có 1 ảnh (slug ảnh SEO)")
a = ap.parse_args()
sys.stdout.reconfigure(encoding="utf-8")
out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
for s in a.src:
    sp = Path(s)
    name = (a.rename if a.rename and len(a.src) == 1 else sp.stem.lower().replace(" ", "-"))
    dp = out / f"{name}.webp"
    with Image.open(sp) as im:
        im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
        im.thumbnail((a.max, a.max), Image.Resampling.LANCZOS)
        im.save(dp, "WEBP", quality=a.q, method=6)
        w, h = im.size
    kb = dp.stat().st_size / 1024
    print(f"{sp.name} ({sp.stat().st_size/1024:.0f} KB) -> {dp.name} {w}x{h} {kb:.0f} KB" + ("  [>150KB]" if kb > 150 else ""))
