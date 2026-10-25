"""Turn a generated image into something a stock site will accept, and check it.

Adobe Stock's minimum is 4 megapixels (contributor upload guidelines, updated
June 2026). The free FLUX model on Cloudflare returns 1024x1024 -- 1MP -- so
every image would be rejected on size alone without this step.

Upscaling is capped. Contributors consistently report that heavy upscaling
amplifies artefacts and gets rejected for quality, and Adobe's older guidance
was not to go past roughly 6-8MP when enlarging. The default here is the
smallest factor that clears 4MP (2x for a 1024 square), and anything that
would need more than MAX_FACTOR is refused rather than stretched.

Checks are deliberately limited to what a program can actually tell: size,
a blank or near-flat frame, and near-duplicates by perceptual hash. Whether a
hand has six fingers is not on that list. 要件 F02 says AI checks do not
guarantee quality and anything unresolved is held for a person -- so
`needs_human` is always set for anatomy and artefacts, never inferred away.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

from PIL import Image, ImageFilter, ImageStat

MIN_PIXELS = 4_000_000          # Adobe Stock minimum
MAX_FACTOR = 2.5                # beyond this, refuse instead of stretching
FLAT_STDDEV = 6.0               # luminance stddev below this reads as blank
DUPLICATE_DISTANCE = 6          # dHash Hamming distance at or below = same image


class PostprocessError(ValueError):
    """The image cannot be made acceptable without degrading it."""


@dataclass
class ImageReport:
    width: int
    height: int
    megapixels: float
    luminance_stddev: float
    dhash: str
    issues: list[str] = field(default_factory=list)
    #: Things a program cannot judge and a person must look at. Never empty
    #: for a stock asset: see the module docstring.
    needs_human: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict:
        return {
            "width": self.width, "height": self.height,
            "megapixels": round(self.megapixels, 2),
            "luminance_stddev": round(self.luminance_stddev, 2),
            "dhash": self.dhash, "issues": self.issues,
            "needs_human": self.needs_human, "ok": self.ok,
        }


def _open(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # truncated, not an image, unsupported format
        raise PostprocessError(f"画像として読めません: {exc}") from exc
    return img.convert("RGB")


def dhash(img: Image.Image, size: int = 8) -> str:
    """64-bit difference hash. Robust to resizing and recompression, which is
    exactly what distinguishes "the same picture again" from "a new picture"."""
    small = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    # tobytes() on an "L" image is one byte per pixel on every Pillow version;
    # getdata() is removed in Pillow 14.
    px = small.tobytes()
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def inspect(data: bytes, *, min_pixels: int = MIN_PIXELS,
            recent_hashes: list[str] | None = None) -> ImageReport:
    img = _open(data)
    w, h = img.size
    stddev = ImageStat.Stat(img.convert("L")).stddev[0]
    report = ImageReport(width=w, height=h, megapixels=w * h / 1_000_000,
                         luminance_stddev=stddev, dhash=dhash(img))

    if w * h < min_pixels:
        report.issues.append(
            f"解像度不足: {w}x{h}（{w * h / 1e6:.1f}MP）。最低 {min_pixels / 1e6:.0f}MP 必要")
    if stddev < FLAT_STDDEV:
        report.issues.append(f"ほぼ単色の画像です（輝度のばらつき {stddev:.1f}）")
    ratio = max(w, h) / max(1, min(w, h))
    if ratio > 4:
        report.issues.append(f"縦横比が極端です（{ratio:.1f}:1）")
    for other in recent_hashes or []:
        distance = hamming(report.dhash, other)
        if distance <= DUPLICATE_DISTANCE:
            report.issues.append(f"過去の画像とほぼ同一です（差 {distance}/64）")
            break

    report.needs_human.append("破綻（手指・文字化け・不自然な形状）の目視確認")
    report.needs_human.append("第三者の権利（ロゴ・実在の人物や建物）の目視確認")
    return report


def upscale_for_stock(data: bytes, *, min_pixels: int = MIN_PIXELS,
                      max_factor: float = MAX_FACTOR, quality: int = 92) -> bytes:
    """Return a JPEG of at least `min_pixels`, enlarged by the smallest factor
    that gets there. Raises PostprocessError rather than over-stretching."""
    img = _open(data)
    w, h = img.size
    pixels = w * h
    if pixels >= min_pixels:
        factor = 1.0
    else:
        # A hair over the exact root, so rounding cannot land just under.
        factor = math.sqrt(min_pixels / pixels) * 1.002
        if factor > max_factor:
            raise PostprocessError(
                f"{w}x{h} から {min_pixels / 1e6:.0f}MP にするには {factor:.1f}倍が必要で、"
                f"上限 {max_factor}倍を超えます。拡大しすぎると審査で品質不足になります")
        new = (math.ceil(w * factor), math.ceil(h * factor))
        img = img.resize(new, Image.Resampling.LANCZOS)
        # Lanczos enlargement is soft; a light unsharp mask restores edge
        # contrast without the halos a strong one produces.
        img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3))

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True, subsampling=0)
    return out.getvalue()


def make_preview(data: bytes, *, longest_side: int = 1024, quality: int = 82) -> bytes:
    """A small JPEG for reviewing on a phone. Keeps cloud storage inside the
    free tier: previews go to the cloud, finals are short-lived."""
    img = _open(data)
    img.thumbnail((longest_side, longest_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()
