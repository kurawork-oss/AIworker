"""Gemini provider, with the HTTP layer replaced so nothing leaves the machine."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from aiworker.core.errors import ConfigError, TransientError
from aiworker.generators.llm import GeminiProvider, LLMRequest, build_provider


class FakeHTTP:
    """Records requests and replays canned responses keyed by URL fragment."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        for fragment, response in self.routes.items():
            if fragment in req.full_url:
                if isinstance(response, Exception):
                    raise response
                return _Resp(response)
        raise AssertionError(f"unexpected URL {req.full_url}")


class _Resp(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def models(*names):
    return {"models": [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]}
                       for n in names]}


def answer(obj):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]},
                            "finishReason": "STOP"}]}


def provider(routes, model=""):
    return GeminiProvider(model=model, api_key="test-key", opener=FakeHTTP(routes))


def test_missing_key_is_a_config_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        GeminiProvider()


def test_resolves_the_most_preferred_available_model():
    p = provider({"/models?": models("gemini-3.5-flash-lite", "gemini-3.8-flash")})
    assert p.resolve_model() == "gemini-3.8-flash"


def test_falls_back_to_a_stable_flash_when_none_preferred_exist():
    """Model IDs churn. A retired preferred ID must not stop the pipeline."""
    p = provider({"/models?": models("gemini-9.0-flash", "gemini-9.1-flash-preview",
                                     "gemini-9.0-flash-image", "text-embedding-9")})
    assert p.resolve_model() == "gemini-9.0-flash"


def test_previews_and_image_models_are_never_auto_selected():
    p = provider({"/models?": models("gemini-9.1-flash-preview", "gemini-9.0-flash-image")})
    with pytest.raises(ConfigError, match="安定版"):
        p.resolve_model()


def test_a_pinned_model_skips_resolution():
    http = {"generateContent": answer({"title": "t", "body": "b"})}
    p = provider(http, model="models/gemini-custom")
    p.complete(LLMRequest(prompt="x"))
    assert all("/models?" not in r.full_url for r in p._open.requests)
    assert "gemini-custom:generateContent" in p._open.requests[0].full_url


def test_generates_json_with_the_key_in_a_header_not_the_url():
    p = provider({"/models?": models("gemini-3.8-flash"),
                  "generateContent": answer({"title": "見出し", "body": "本文"})})
    out = p.complete(LLMRequest(prompt="書いて", system="あなたは", schema={"title": "s"}))
    assert out == {"title": "見出し", "body": "本文"}
    req = p._open.requests[-1]
    # The key in the query string would end up in proxy logs and Actions logs.
    assert "key=" not in req.full_url
    assert req.get_header("X-goog-api-key") == "test-key"
    body = json.loads(req.data)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["systemInstruction"]["parts"][0]["text"] == "あなたは"


def test_rate_limits_are_transient():
    err = urllib.error.HTTPError("u", 429, "Too Many", {}, io.BytesIO(b"quota"))
    p = provider({"/models?": models("gemini-3.8-flash"), "generateContent": err})
    with pytest.raises(TransientError, match="429"):
        p.complete(LLMRequest(prompt="x"))


def test_auth_errors_are_config_errors_not_retried():
    err = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"bad key"))
    p = provider({"/models?": err})
    with pytest.raises(ConfigError, match="403"):
        p.complete(LLMRequest(prompt="x"))


def test_safety_blocks_are_unusable_output_not_crashes():
    p = provider({"/models?": models("gemini-3.8-flash"),
                  "generateContent": {"candidates": [{"finishReason": "SAFETY"}]}})
    with pytest.raises(ValueError, match="SAFETY"):
        p.complete(LLMRequest(prompt="x"))


def test_blocked_prompts_are_unusable_output():
    p = provider({"/models?": models("gemini-3.8-flash"),
                  "generateContent": {"promptFeedback": {"blockReason": "OTHER"}}})
    with pytest.raises(ValueError, match="OTHER"):
        p.complete(LLMRequest(prompt="x"))


def test_build_provider_ignores_a_claude_model_name(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    p = build_provider("gemini", "claude-opus-5")
    assert p.name == "gemini" and p.model == ""


def test_fallback_compares_versions_numerically():
    p = provider({"/models?": models("gemini-9.9-flash", "gemini-9.10-flash")})
    assert p.resolve_model() == "gemini-9.10-flash"
