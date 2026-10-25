#!/usr/bin/env python3
"""Build the phone app (web/) into a static folder for GitHub Pages.

    SUPABASE_URL=https://xxxx.supabase.co SUPABASE_PUBLISHABLE_KEY=sb_publishable_... \\
        python3 scripts/build_pwa.py --out dist/pwa

* theme.css is generated from src/aiworker/webui/theme.py, so the phone app
  and the local UI share one stylesheet.
* config.js carries the project URL and the *publishable* key, which are
  public by design (the database's row-level security is what protects the
  data). The build refuses a secret or service-role key outright: one of
  those in a web page hands the whole database to anyone who opens it.
* Without the two variables it still builds, and the app says "未接続"
  instead of pretending to work.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aiworker.webui.theme import stylesheet  # noqa: E402


def refuse_secret(key: str) -> None:
    if key.startswith("sb_secret_"):
        raise SystemExit("refusing to build: that is a SECRET key; use the publishable key")
    if key.count(".") == 2:  # a legacy JWT key: read its role
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        try:
            role = json.loads(base64.urlsafe_b64decode(payload)).get("role")
        except ValueError:
            role = None
        if role != "anon":
            raise SystemExit(f"refusing to build: key role is {role!r}, expected 'anon'")


def icon_png(size: int) -> bytes:
    """The app icon as PNG (iOS home screens ignore SVG icons)."""
    import io

    from PIL import Image, ImageDraw

    scale = 4  # draw large, shrink: cheap anti-aliasing
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill="#1c5cab")
    k = s / 512
    d.line([(150 * k, 266 * k), (220 * k, 336 * k), (362 * k, 176 * k)],
           fill="white", width=int(44 * k), joint="curve")
    for x, y in ((150, 266), (362, 176)):
        r = 22 * k
        d.ellipse([x * k - r, y * k - r, x * k + r, y * k + r], fill="white")
    d.ellipse([(392 - 30) * k, (128 - 30) * k, (392 + 30) * k, (128 + 30) * k], fill="#fab219")
    out = io.BytesIO()
    img.resize((size, size), Image.Resampling.LANCZOS).save(out, format="PNG")
    return out.getvalue()


def build(out: Path, url: str, key: str) -> None:
    if key:
        refuse_secret(key)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "web", out)
    (out / "theme.css").write_text(stylesheet(), encoding="utf-8")
    config = {"supabaseUrl": url, "supabaseKey": key} if url and key else {}
    (out / "config.js").write_text(
        f"window.AIWORKER_CONFIG = {json.dumps(config)};\n", encoding="utf-8")
    for size in (192, 512):
        (out / f"icon-{size}.png").write_bytes(icon_png(size))
    (out / ".nojekyll").write_text("")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="dist/pwa")
    args = parser.parse_args()
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    build(Path(args.out), url, key)
    print(f"built {args.out} ({'connected' if url and key else '未接続: no Supabase settings'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
