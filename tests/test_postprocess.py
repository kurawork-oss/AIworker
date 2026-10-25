"""Upscaling to the stock-site minimum, and what the image checks will and
will not claim to know."""

from __future__ import annotations

import io
import random

import pytest

pytest.importorskip("PIL.Image")
from PIL import Image, ImageDraw  # noqa: E402

from aiworker.imagegen import postprocess as pp  # noqa: E402


def png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def scene(size=(1024, 1024), seed=1) -> bytes:
    """Something with real structure, unlike a flat fill."""
    rnd = random.Random(seed)
    img = Image.new("RGB", size, (rnd.randint(0, 255), 90, 160))
    d = ImageDraw.Draw(img)
    for _ in range(40):
        x, y = rnd.randint(0, size[0]), rnd.randint(0, size[1])
        r = rnd.randint(20, 160)
        d.ellipse((x - r, y - r, x + r, y + r),
                  fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    return png(img)


def size_of(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


# --------------------------------------------------------------------------
# upscaling
# --------------------------------------------------------------------------
def test_a_1mp_image_is_raised_to_at_least_4mp():
    w, h = size_of(pp.upscale_for_stock(scene()))
    assert w * h >= pp.MIN_PIXELS


def test_it_uses_the_smallest_factor_that_clears_the_minimum():
    """Every extra bit of enlargement costs quality at review."""
    w, h = size_of(pp.upscale_for_stock(scene()))
    assert w * h < pp.MIN_PIXELS * 1.05


def test_the_output_is_a_jpeg():
    assert pp.upscale_for_stock(scene())[:3] == b"\xff\xd8\xff"


def test_an_image_already_large_enough_is_not_enlarged():
    data = scene((2400, 2000))
    assert size_of(pp.upscale_for_stock(data)) == (2400, 2000)


def test_non_square_images_keep_their_aspect_ratio():
    w, h = size_of(pp.upscale_for_stock(scene((1344, 768))))
    assert abs(w / h - 1344 / 768) < 0.01


def test_an_image_needing_too_much_enlargement_is_refused_not_stretched():
    """A 512px image would need ~3.9x. Stretched that far it gets rejected for
    quality anyway, after wasting a review slot -- so refuse up front."""
    with pytest.raises(pp.PostprocessError, match="上限"):
        pp.upscale_for_stock(scene((512, 512)))


def test_garbage_bytes_are_a_clear_error():
    with pytest.raises(pp.PostprocessError, match="読めません"):
        pp.upscale_for_stock(b"not an image")


# --------------------------------------------------------------------------
# inspection
# --------------------------------------------------------------------------
def test_a_good_final_image_passes():
    report = pp.inspect(pp.upscale_for_stock(scene()))
    assert report.ok, report.issues


def test_an_undersized_image_is_flagged():
    report = pp.inspect(scene())
    assert any("解像度不足" in i for i in report.issues)


def test_a_blank_frame_is_flagged():
    """A model failure often returns a solid or near-solid frame."""
    blank = png(Image.new("RGB", (2100, 2100), (128, 128, 128)))
    assert any("単色" in i for i in pp.inspect(blank).issues)


def test_the_same_picture_resaved_is_a_duplicate():
    original = pp.upscale_for_stock(scene(seed=5))
    resized = png(Image.open(io.BytesIO(original)).resize((1800, 1800)))
    report = pp.inspect(resized, min_pixels=1, recent_hashes=[pp.inspect(original).dhash])
    assert any("同一" in i for i in report.issues)


def test_a_different_picture_is_not_a_duplicate():
    a = pp.inspect(pp.upscale_for_stock(scene(seed=1)))
    b = pp.inspect(pp.upscale_for_stock(scene(seed=2)), recent_hashes=[a.dhash])
    assert not any("同一" in i for i in b.issues)


def test_anatomy_and_rights_always_go_to_a_person():
    """A program cannot tell a six-fingered hand or a lookalike logo. Claiming
    it checked would be the dangerous kind of wrong."""
    report = pp.inspect(pp.upscale_for_stock(scene()))
    assert report.ok
    joined = " ".join(report.needs_human)
    assert "破綻" in joined and "権利" in joined


# --------------------------------------------------------------------------
# previews
# --------------------------------------------------------------------------
def test_previews_are_small_jpegs():
    preview = pp.make_preview(pp.upscale_for_stock(scene()))
    assert preview[:3] == b"\xff\xd8\xff"
    assert max(size_of(preview)) <= 1024
    assert len(preview) < 400_000, "previews must stay small to fit free storage"


def test_dhash_survives_jpeg_recompression():
    data = scene(seed=9)
    jpeg = io.BytesIO()
    Image.open(io.BytesIO(data)).save(jpeg, format="JPEG", quality=70)
    h1 = pp.dhash(Image.open(io.BytesIO(data)))
    h2 = pp.dhash(Image.open(io.BytesIO(jpeg.getvalue())))
    assert pp.hamming(h1, h2) <= pp.DUPLICATE_DISTANCE
