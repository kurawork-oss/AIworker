"""Image generation backends for the stock-asset channel.

The channel produced prompts and stopped there, which left a person generating
every image by hand -- the bottleneck on the one channel cheap enough to run on
`approval: auto`.

Three backends, all free, none of which put an account at risk:

* ``mock``       - a deterministic placeholder PNG. Default, offline, keeps the
                   pipeline testable with no key and no spend.
* ``cloudflare`` - Workers AI (FLUX). Free daily allowance, no card, no GPU
                   needed, so it works without a machine of your own.
* ``local``      - an OpenAI-compatible or Automatic1111/ComfyUI endpoint on
                   localhost. Free and unlimited if you have the GPU; SDXL's
                   licence permits selling the output on stock sites.

Driving a consumer chat UI with a browser is deliberately **not** an option
here. It breaches those products' terms, risks the account, breaks whenever the
page changes, and is slower and more rate-limited than either real option above
-- it loses on every axis, including the one it was picked for.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..core.errors import ConfigError, TransientError
from ..core.logging_setup import get_logger

log = get_logger("imagegen")


@dataclass
class ImageRequest:
    prompt: str
    negative_prompt: str = ""
    width: int = 1024
    height: int = 1024
    seed: int | None = None


@dataclass
class ImageResult:
    ok: bool
    data: bytes = b""
    content_type: str = "image/png"
    message: str = ""
    detail: dict = field(default_factory=dict)


class ImageProvider(Protocol):
    name: str
    #: Roughly what one image costs, for the report. 0.0 for free backends.
    cost_per_image_usd: float

    def generate(self, request: ImageRequest) -> ImageResult:
        ...


# --------------------------------------------------------------------------
# mock
# --------------------------------------------------------------------------
def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A valid PNG with no dependencies, so the default backend needs none."""
    raw = b"".join(
        b"\x00" + bytes(rgb) * width for _ in range(height)
    )

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


class MockImageProvider:
    """Offline placeholder. Obviously not publishable, which is the point."""

    name = "mock"
    cost_per_image_usd = 0.0

    def generate(self, request: ImageRequest) -> ImageResult:
        seed = request.seed if request.seed is not None else abs(hash(request.prompt))
        rgb = ((seed * 37) % 200 + 40, (seed * 71) % 200 + 40, (seed * 113) % 200 + 40)
        # Small on purpose: a mock must never be mistaken for a sellable asset.
        return ImageResult(True, _solid_png(64, 64, rgb),
                           message="mock: placeholder image, not publishable")


# --------------------------------------------------------------------------
# Cloudflare Workers AI
# --------------------------------------------------------------------------
class CloudflareImageProvider:
    """Workers AI. Free daily allowance, no card, no GPU.

    Needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in the environment.
    """

    name = "cloudflare"
    cost_per_image_usd = 0.0
    DEFAULT_MODEL = "@cf/black-forest-labs/flux-1-schnell"

    def __init__(self, model: str = "", timeout: int = 120):
        self.model = model or self.DEFAULT_MODEL
        self.timeout = timeout
        self.account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        self.token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not self.account or not self.token:
            raise ConfigError(
                "image.provider が 'cloudflare' ですが CLOUDFLARE_ACCOUNT_ID / "
                "CLOUDFLARE_API_TOKEN が未設定です（.env に置いてください）"
            )

    def generate(self, request: ImageRequest) -> ImageResult:
        url = (f"https://api.cloudflare.com/client/v4/accounts/{self.account}"
               f"/ai/run/{self.model}")
        payload: dict = {"prompt": request.prompt}
        if request.seed is not None:
            payload["seed"] = request.seed
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (408, 429) or exc.code >= 500:
                raise TransientError(f"Cloudflare {exc.code}: {text}") from exc
            return ImageResult(False, message=f"Cloudflare {exc.code}: {text}")
        except urllib.error.URLError as exc:
            raise TransientError(f"Cloudflare に到達できません: {exc}") from exc

        # Workers AI returns either raw bytes or {"result": {"image": "<b64>"}}.
        if body[:8] == b"\x89PNG\r\n\x1a\n" or body[:2] == b"\xff\xd8":
            return ImageResult(True, body)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return ImageResult(False, message="Cloudflare の応答を解釈できません")
        image_b64 = (data.get("result") or {}).get("image")
        if not image_b64:
            return ImageResult(False, message=f"画像が返りませんでした: {str(data)[:200]}")
        return ImageResult(True, base64.b64decode(image_b64))


# --------------------------------------------------------------------------
# local (Automatic1111 / ComfyUI / any txt2img HTTP endpoint)
# --------------------------------------------------------------------------
class LocalImageProvider:
    """A txt2img server on your own machine -- free, unlimited, no account.

    Defaults to the Automatic1111 API shape (`/sdxl/txt2img`). SDXL's licence
    permits selling the output, which is why it is the recommended backend for
    anyone with a GPU.
    """

    name = "local"
    cost_per_image_usd = 0.0

    def __init__(self, endpoint: str = "", timeout: int = 300):
        self.endpoint = endpoint or os.environ.get(
            "LOCAL_IMAGE_ENDPOINT", "http://127.0.0.1:7860/sdapi/v1/txt2img"
        )
        self.timeout = timeout

    def generate(self, request: ImageRequest) -> ImageResult:
        payload = {
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "width": request.width,
            "height": request.height,
            "steps": 25,
        }
        if request.seed is not None:
            payload["seed"] = request.seed
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise TransientError(
                f"ローカルの画像サーバーに接続できません（{self.endpoint}）: {exc}"
            ) from exc
        images = data.get("images") or []
        if not images:
            return ImageResult(False, message="画像が返りませんでした")
        return ImageResult(True, base64.b64decode(images[0]))


PROVIDERS = {
    "mock": MockImageProvider,
    "cloudflare": CloudflareImageProvider,
    "local": LocalImageProvider,
}


def build_provider(name: str, model: str = "") -> ImageProvider:
    try:
        cls = PROVIDERS[name]
    except KeyError:
        raise ConfigError(
            f"未知の image.provider '{name}'（{'|'.join(PROVIDERS)}）"
        ) from None
    if cls is CloudflareImageProvider:
        return cls(model=model)
    if cls is LocalImageProvider:
        return cls()
    return cls()
