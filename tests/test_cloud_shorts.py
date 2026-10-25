"""YouTube Shorts, end to end on both stores, plus one real ffmpeg render."""

from __future__ import annotations

import random
import shutil

import pytest

from aiworker.cloud.shorts import ShortsPipeline
from aiworker.cloud.tts import ToneTTS, VoicevoxTTS
from aiworker.cloud.video import Video, compose, probe, wrap
from aiworker.core.errors import ConfigError, TransientError
from cloud_harness import NOW, harness
from test_cloud_pipeline import FakeImages


class FakeLLM:
    name = "fake-llm"

    def __init__(self, scenes=4, **override):
        self.n_scenes = scenes
        self.override = override
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        out = {"title": "朝が変わる5分の小ワザ", "description": "毎朝の小さな工夫。",
               "tags": ["生活", "小ワザ"],
               "scenes": [{"narration": f"これはシーン{i}の読み上げです。", "caption": f"シーン{i}",
                           "image_prompt": f"calm morning window light, scene {i}, no text"}
                          for i in range(self.n_scenes)]}
        out.update(self.override)
        return out


class FakeVoice:
    name = "fake-voice"
    credit = "VOICEVOX:ずんだもん"
    is_sample = False

    def __init__(self, fail=0):
        self.fail = fail
        self.lines = []

    def synth(self, text):
        if self.fail:
            self.fail -= 1
            raise TransientError("VOICEVOX に接続できません")
        self.lines.append(text)
        return ToneTTS().synth(text)


class FakeComposer:
    def __init__(self, seconds=35.0, size=2_000_000):
        self.seconds, self.size = seconds, size
        self.calls = []

    def __call__(self, scenes, title):
        self.calls.append((scenes, title))
        return Video(b"\0" * self.size, self.seconds, 1080, 1920, True)


@pytest.fixture(params=["memory", "postgres"])
def h(request):
    yield from harness(request.param)


def run(h, llm=None, images=None, tts=None, composer=None, seed=7):
    pipe = ShortsPipeline(h.store, llm or FakeLLM(), images or FakeImages(), tts or FakeVoice(),
                          composer=composer or FakeComposer(), rng=random.Random(seed), now=NOW)
    return pipe.run()


def test_a_short_is_rendered_and_waits_for_the_owner(h):
    voice, composer = FakeVoice(), FakeComposer()
    report = run(h, tts=voice, composer=composer)
    assert report.created == 1 and report.to_review == 1, report.summary()
    [item] = h.all_items()
    assert item["kind"] == "short_video" and item["status"] == "pending_review"
    assert len(voice.lines) == 4 and len(composer.calls[0][0]) == 4
    assert item["final_path"].endswith("/short.mp4") and item["preview_path"].endswith(".jpg")
    desc = item["meta"]["description"]
    assert "VOICEVOX:ずんだもん" in desc and "AIで生成した画像" in desc and "#shorts" in desc


def test_an_approved_short_is_ready_to_post(h):
    run(h)
    [item] = h.all_items()
    h.approve(item["id"])
    assert h.item(item["id"])["status"] == "awaiting_manual"


def test_a_risky_script_is_blocked_before_anything_is_rendered(h):
    composer = FakeComposer()
    llm = FakeLLM(scenes=4)
    llm.override = {"scenes": [{"narration": "城の話", "caption": "城",
                                "image_prompt": "castle in the style of Ghibli"}] * 4}
    report = run(h, llm=llm, composer=composer)
    assert report.blocked == 1 and not composer.calls


def test_a_video_of_the_wrong_length_is_blocked(h):
    report = run(h, composer=FakeComposer(seconds=75))
    assert report.blocked == 1
    [item] = h.all_items()
    assert any("長さ" in x for x in item["quality_report"]["issues"])


def test_a_thin_script_is_not_rendered(h):
    report = run(h, llm=FakeLLM(scenes=1))
    assert report.created == 0 and report.errors


def test_a_missing_voice_engine_is_retried_then_given_up(h):
    run(h, tts=FakeVoice(fail=99))
    [item] = h.all_items()
    assert item["status"] == "image_queued" and item["meta"]["render_attempts"] == 1
    run(h, tts=FakeVoice(fail=99), seed=8)
    run(h, tts=FakeVoice(fail=99), seed=9)
    assert h.item(item["id"])["status"] == "failed"


def test_a_tone_voice_makes_a_sample(h):
    run(h, tts=ToneTTS())
    [item] = h.all_items()
    assert item["is_sample"] is True


def test_a_revision_is_rerendered_and_comes_back(h):
    run(h)
    [item] = h.all_items()
    h.ask_revision(item["id"], "最初の一言をもっと短く")
    llm = FakeLLM()
    report = run(h, llm=llm, seed=8)
    assert report.revised == 1 and "もっと短く" in llm.requests[0].prompt
    assert h.item(item["id"])["status"] == "pending_review"


# --------------------------------------------------------------------------
# the pieces
# --------------------------------------------------------------------------
def test_voicevox_needs_a_credit_for_an_unknown_voice():
    with pytest.raises(ConfigError):
        VoicevoxTTS(speaker=999)
    assert VoicevoxTTS(speaker=3).credit == "VOICEVOX:ずんだもん"


def test_japanese_lines_never_start_with_punctuation():
    assert wrap("今日から試してみてください！") == ["今日から試してみてください！"]
    for line in wrap("あいうえおかきくけこさしす、せそたちつてと。なにぬねの" * 2):
        assert line[0] not in "、。！？"


def _has_font():
    from aiworker.cloud.video import find_font
    try:
        return bool(find_font())
    except ConfigError:
        return False


@pytest.mark.skipif(not shutil.which("ffmpeg") or not _has_font(),
                    reason="ffmpeg or a Japanese font not installed")
def test_a_real_render_is_a_vertical_video_with_sound(tmp_path):
    from test_cloud_pipeline import picture
    from aiworker.cloud.video import Scene

    tts = ToneTTS(chars_per_second=20)
    video = compose([Scene(picture(i), tts.synth("短い読み上げ。"), "字幕です") for i in range(2)],
                    "テストの動画")
    out = tmp_path / "v.mp4"
    out.write_bytes(video.data)
    width, height, seconds, audio = probe(out)
    assert (width, height) == (1080, 1920) and audio
    assert 1.0 < seconds < 4.0


def test_voicevox_asks_for_a_query_then_synthesises_it_faster():
    import io
    import json as _json
    import urllib.parse

    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        if "/audio_query" in req.full_url:
            return io.BytesIO(_json.dumps({"speedScale": 1.0, "accent_phrases": []}).encode())
        return io.BytesIO(ToneTTS().synth("あ"))

    wav = VoicevoxTTS("http://engine:50021", 3, opener=opener).synth("こんにちは")
    assert wav[:4] == b"RIFF"
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0].full_url).query)
    assert q == {"text": ["こんにちは"], "speaker": ["3"]}
    assert _json.loads(calls[1].data)["speedScale"] == pytest.approx(1.15)
