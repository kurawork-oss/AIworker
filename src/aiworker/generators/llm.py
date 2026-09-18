"""LLM provider abstraction.

Three providers, one interface:

* ``mock``       - offline, seeded, deterministic-per-seed. The default, so the
                   whole pipeline is runnable and testable with no API key and
                   no spend. Content is obviously placeholder-grade.
* ``claude_cli`` - shells out to the ``claude`` binary (Claude Code). Best fit
                   when this system runs on a machine that already has it.
* ``anthropic``  - direct Messages API call over ``urllib`` (no SDK needed).

Every provider returns **parsed JSON**, because free-form prose is impossible to
guard mechanically: the guardrails need discrete fields (title, body, keywords)
to measure.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.errors import ConfigError, TransientError
from ..core.logging_setup import get_logger

log = get_logger("llm")

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class LLMRequest:
    prompt: str
    system: str = ""
    schema: dict[str, str] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    max_tokens: int = 2000
    temperature: float = 1.0
    seed: int | None = None


class LLMProvider(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> dict[str, Any]:
        ...


def extract_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a model response.

    Models wrap JSON in prose or fences often enough that failing on it would
    make the pipeline flaky for no good reason.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError(f"model returned unparseable JSON: {exc}") from exc
    raise ValueError("model response contained no JSON object")


# --------------------------------------------------------------------------
# mock
# --------------------------------------------------------------------------
_HOOKS = [
    "{theme}で最初にやるべきことは、{angle}の見直しでした。",
    "{theme}を3週間続けて分かったのは、{angle}が結果を決めるということ。",
    "{theme}について「よくある助言」を試したら、{angle}だけが効きました。",
    "{theme}で遠回りした話。原因は{angle}を後回しにしたことでした。",
    "{theme}を始める前の自分に伝えたいのは、{angle}の一点だけです。",
    "{theme}の相談でいちばん多いのが{angle}。ここだけ先に整理します。",
]
_BODIES = [
    "手順は3つあります。\n1. 目的を1行で書く\n2. 最小の実験を1つ決める\n3. 結果を記録して次に渡す\n"
    "特に3番目を飛ばすと、同じ失敗を繰り返します。",
    "やることを減らすほど続きます。\n・毎日やることは1つだけ決める\n・うまくいかない日は記録だけ残す\n"
    "・週末に5分だけ振り返る\nこの形にしてから、途中でやめなくなりました。",
    "つまずきやすいのは次の順番です。\n最初 : 情報を集めすぎる\n次 : 完璧な形を作ろうとする\n"
    "最後 : 比較して手が止まる\n先に小さく出して、後から直すほうが早く進みます。",
    "計測する項目を1つに絞ると判断が速くなります。\n数字が動かないときは、やり方ではなく対象を疑う。\n"
    "対象が合っていれば、雑なやり方でも数字は動きます。",
]
_CLOSERS = [
    "同じところで止まっている方の参考になれば。",
    "詳しい手順は別でまとめています。",
    "続きは次の投稿で書きます。",
    "試した方は結果を教えてください。",
]
_KEYWORD_POOL = [
    "business", "workspace", "minimal", "japan", "technology", "lifestyle",
    "productivity", "abstract", "gradient", "modern", "clean", "concept",
    "background", "copyspace", "professional", "calm",
]


class MockProvider:
    """Offline generator. Varied enough to exercise the duplicate detector,
    obviously placeholder-grade so nobody mistakes it for shippable copy."""

    name = "mock"

    def complete(self, request: LLMRequest) -> dict[str, Any]:
        rng = random.Random(request.seed if request.seed is not None else random.random())
        ctx = request.context
        theme = ctx.get("theme", "AI副業")
        angle = ctx.get("angle", "計測の仕方")
        tone = ctx.get("tone", "実務的")
        channel = ctx.get("channel", "social_post")

        hook = rng.choice(_HOOKS).format(theme=theme, angle=angle)
        body = rng.choice(_BODIES)
        closer = rng.choice(_CLOSERS)
        title = f"{theme}:{angle}({tone})"

        if channel == "note_article":
            sections = [
                f"## {i+1}. {s}"
                for i, s in enumerate(
                    rng.sample(
                        ["前提を揃える", "最小構成を作る", "計測する", "改善する", "外に出す",
                         "続ける仕組みにする", "やめる基準を決める"],
                        5,
                    )
                )
            ]
            long_body = "\n\n".join(
                f"{s}\n\n{rng.choice(_BODIES)}\n\n{rng.choice(_BODIES)}\n\n{rng.choice(_CLOSERS)}"
                for s in sections
            )
            return {
                "title": f"{theme}を{angle}から立て直す実践ノート",
                "headline": hook,
                "outline": [s.lstrip("# ") for s in sections],
                "body": f"{hook}\n\n{long_body}\n\n{closer}\n\n（この記事はAI支援で作成し、公開前に人間が確認しています）",
                "price_hint_jpy": rng.choice([0, 300, 500, 980]),
            }

        if channel == "shorts_script":
            return {
                "title": f"{theme}の{angle}",
                "video_title": f"【{tone}】{theme}で{angle}を直す3つの手順",
                "body": (
                    f"[0-2秒] {hook}\n[2-10秒] {body}\n"
                    f"[10-25秒] 実際にやった結果をここで見せる\n[25-30秒] {closer}"
                ),
                "description": (
                    f"{theme}の{angle}について30秒で解説します。\n\n"
                    "※この動画はAI生成の音声・画像を含みます\n#shorts"
                ),
                "tags": rng.sample(["副業", "AI活用", "作業効率", "初心者向け", "実験ログ"], 3),
            }

        if channel == "stock_asset":
            kws = rng.sample(_KEYWORD_POOL, 10)
            return {
                "title": f"{theme} concept image {rng.randint(100, 999)}",
                "body": (
                    f"{theme}をイメージした抽象ビジュアル。{angle}を色面と余白で表現し、"
                    "文字入れ用のコピースペースを右側に確保した構図です。"
                ),
                "prompt": (
                    f"abstract {rng.choice(['gradient', 'geometric', 'organic'])} composition, "
                    f"{rng.choice(['soft blue', 'warm neutral', 'monochrome'])} palette, "
                    "generous copy space, no text, no logos, no recognisable people"
                ),
                "negative_prompt": "text, watermark, logo, brand, celebrity, real person, signature",
                "keywords": kws,
                "description": f"{theme} concept background with copy space.",
                "ai_generated": True,
            }

        if channel == "social_thread":
            parts = [hook] + [rng.choice(_BODIES) for _ in range(3)] + [closer]
            return {"title": title, "body": "\n\n---\n\n".join(parts), "parts": parts}

        return {"title": title, "body": f"{hook}\n\n{body}\n\n{closer}"}


# --------------------------------------------------------------------------
# claude CLI
# --------------------------------------------------------------------------
class ClaudeCLIProvider:
    """Drive the local ``claude`` binary in headless mode."""

    name = "claude_cli"

    def __init__(self, model: str = "", timeout: int = 120, binary: str = "claude"):
        self.model = model
        self.timeout = timeout
        self.binary = binary

    def complete(self, request: LLMRequest) -> dict[str, Any]:
        prompt = _with_schema(request)
        cmd = [self.binary, "-p", prompt, "--output-format", "text"]
        if self.model:
            cmd += ["--model", self.model]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except FileNotFoundError as exc:
            raise ConfigError(
                f"generation.provider is 'claude_cli' but '{self.binary}' is not on PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TransientError(f"claude CLI timed out after {self.timeout}s") from exc
        if proc.returncode != 0:
            raise TransientError(f"claude CLI exited {proc.returncode}: {proc.stderr[:400]}")
        return extract_json(proc.stdout)


# --------------------------------------------------------------------------
# Anthropic Messages API
# --------------------------------------------------------------------------
class AnthropicProvider:
    name = "anthropic"
    ENDPOINT = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, model: str = "claude-opus-5", timeout: int = 120):
        self.model = model
        self.timeout = timeout
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise ConfigError(
                "generation.provider is 'anthropic' but ANTHROPIC_API_KEY is not set. "
                "Put it in .env (never in config.yaml)."
            )

    def complete(self, request: LLMRequest) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [{"role": "user", "content": _with_schema(request)}],
        }
        if request.system:
            payload["system"] = request.system
        req = urllib.request.Request(
            self.ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": self.API_VERSION,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            if exc.code in (408, 409, 429) or exc.code >= 500:
                raise TransientError(f"anthropic API {exc.code}: {body}") from exc
            raise ConfigError(f"anthropic API {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise TransientError(f"anthropic API unreachable: {exc}") from exc
        text = "".join(b.get("text", "") for b in data.get("content", []))
        return extract_json(text)


def _with_schema(request: LLMRequest) -> str:
    if not request.schema:
        return request.prompt
    spec = json.dumps(request.schema, ensure_ascii=False, indent=2)
    return (
        f"{request.prompt}\n\n"
        "Respond with a single JSON object and nothing else. "
        f"Keys and value types:\n{spec}"
    )


def build_provider(name: str, model: str = "", timeout: int = 120) -> LLMProvider:
    if name == "mock":
        return MockProvider()
    if name == "claude_cli":
        return ClaudeCLIProvider(model=model, timeout=timeout)
    if name == "anthropic":
        return AnthropicProvider(model=model or "claude-opus-5", timeout=timeout)
    raise ConfigError(f"unknown generation.provider '{name}' (mock|claude_cli|anthropic)")
