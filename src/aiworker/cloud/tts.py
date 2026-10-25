"""Japanese narration with VOICEVOX -- free, commercial use allowed with credit.

The engine runs next to the pipeline (a service container in GitHub
Actions), so nothing leaves the runner and there is no quota. Each voice has
its own terms; the ones listed here allow commercial use as long as the
video credits ``VOICEVOX:<name>``, which the Shorts pipeline writes into
every description. An unlisted speaker needs VOICEVOX_CREDIT set explicitly
-- no credit, no video.
"""

from __future__ import annotations

import io
import json
import math
import os
import struct
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections.abc import Callable
from typing import Any, Protocol

from ..core.errors import ConfigError, TransientError

#: speaker id -> the name the credit must carry
KNOWN_SPEAKERS = {2: "四国めたん", 3: "ずんだもん", 8: "春日部つむぎ", 13: "青山龍星"}


class TTS(Protocol):
    name: str
    credit: str
    is_sample: bool

    def synth(self, text: str) -> bytes: ...


def wav_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / float(w.getframerate())


class VoicevoxTTS:
    name = "voicevox"
    is_sample = False

    def __init__(self, base_url: str = "http://127.0.0.1:50021", speaker: int = 3, *,
                 credit: str = "", speed: float = 1.15, timeout: int = 120,
                 opener: Callable[..., Any] | None = None):
        self.base_url = base_url.rstrip("/")
        self.speaker = int(speaker)
        name = KNOWN_SPEAKERS.get(self.speaker)
        self.credit = credit or (f"VOICEVOX:{name}" if name else "")
        if not self.credit:
            raise ConfigError(f"VOICEVOX speaker {speaker} の利用規約に沿った VOICEVOX_CREDIT を設定してください")
        self.speed = speed
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    @classmethod
    def from_env(cls) -> VoicevoxTTS:
        return cls(os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021"),
                   int(os.environ.get("VOICEVOX_SPEAKER", "3")),
                   credit=os.environ.get("VOICEVOX_CREDIT", ""))

    def _post(self, path: str, query: dict[str, Any], body: bytes | None = None) -> bytes:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, data=body if body is not None else b"", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise TransientError(f"VOICEVOX {exc.code}") from None
            raise ValueError(f"VOICEVOX {exc.code}: {exc.read()[:200]!r}") from None
        except urllib.error.URLError as exc:
            raise TransientError(f"VOICEVOX に接続できません: {exc.reason}") from None

    def synth(self, text: str) -> bytes:
        query = json.loads(self._post("/audio_query", {"text": text, "speaker": self.speaker}))
        query["speedScale"] = self.speed
        return self._post("/synthesis", {"speaker": self.speaker}, json.dumps(query).encode())


class ToneTTS:
    """Offline stand-in: a soft tone as long as the line would take to read.

    For tests and demos only -- flagged as a sample end to end.
    """

    name = "tone"
    credit = "（サンプル音声）"
    is_sample = True

    def __init__(self, chars_per_second: float = 8.0, rate: int = 22050):
        self.cps = chars_per_second
        self.rate = rate

    def synth(self, text: str) -> bytes:
        seconds = max(0.6, len(text) / self.cps)
        frames = int(seconds * self.rate)
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(b"".join(
                struct.pack("<h", int(2000 * math.sin(2 * math.pi * 220 * i / self.rate)))
                for i in range(frames)))
        return out.getvalue()
