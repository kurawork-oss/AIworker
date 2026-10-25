"""Turning scenes (image + narration + caption) into a vertical Shorts MP4.

Layout, 1080x1920 at 30 fps:

    +-------------------+
    |      title        |   white on a translucent box
    |  +-------------+  |
    |  |  the image, |  |   1000x1000, slow push-in
    |  |  zooming    |  |
    |  +-------------+  |
    |  caption line 1   |   white with a dark outline
    |  caption line 2   |
    +-------------------+   behind it all: the same image, blurred and darkened

Each scene is encoded on its own with identical settings, then the scenes
are joined without re-encoding. Only ffmpeg is needed; text goes in through
files, so nothing in a caption can be read as a filter instruction.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import ConfigError
from .tts import wav_seconds

W, H, FPS = 1080, 1920, 30
FG = 1000
FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
)


@dataclass
class Scene:
    image: bytes          # PNG or JPEG, any size (square is best)
    audio: bytes          # WAV narration
    caption: str


@dataclass
class Video:
    data: bytes
    seconds: float
    width: int
    height: int
    has_audio: bool


def find_font() -> str:
    for path in (os.environ.get("AIWORKER_FONT", ""), *FONT_CANDIDATES):
        if path and Path(path).is_file():
            return path
    raise ConfigError("日本語フォントが見つかりません（fonts-noto-cjk を入れるか AIWORKER_FONT を設定）")


def wrap(text: str, width: int = 13, max_lines: int = 3) -> list[str]:
    """Greedy Japanese line breaking, preferring to break after punctuation."""
    text = " ".join(text.split())
    lines: list[str] = []
    while text and len(lines) < max_lines:
        if len(text) <= width:
            lines.append(text)
            break
        cut = width
        for i in range(width, max(width - 5, 1), -1):
            if text[i - 1] in "、。！？!?」』）) ":
                cut = i
                break
        # 禁則: a line never starts with closing punctuation or a small kana.
        while cut < len(text) and text[cut] in "、。！？!?」』）)ーッャュョァィゥェォっゃゅょ":
            cut += 1
        lines.append(text[:cut].strip())
        text = text[cut:].strip()
    if text and len(lines) == max_lines and not lines[-1].endswith("…"):
        lines[-1] = lines[-1][: width - 1] + "…"
    return lines


def _q(path: Path | str) -> str:
    s = str(path)
    if "'" in s:
        raise ConfigError(f"path not usable in a filter: {s}")
    return f"'{s}'"


def _ffmpeg(args: list[str]) -> None:
    if not shutil.which("ffmpeg"):
        raise ConfigError("ffmpeg がありません")
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-400:]}")


def probe(path: Path) -> tuple[int, int, float, bool]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    info = json.loads(out.stdout)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = any(s["codec_type"] == "audio" for s in info["streams"])
    return int(video["width"]), int(video["height"]), float(info["format"]["duration"]), audio


def _texts(work: Path, prefix: str, lines: list[str]) -> list[Path]:
    paths = []
    for n, line in enumerate(lines):
        p = work / f"{prefix}{n}.txt"
        p.write_text(line, encoding="utf-8")
        paths.append(p)
    return paths


def _scene(work: Path, n: int, scene: Scene, title: list[Path], font: str) -> Path:
    img = work / f"img{n}.png"
    img.write_bytes(scene.image)
    voice = work / f"voice{n}.wav"
    voice.write_bytes(scene.audio)
    seconds = wav_seconds(scene.audio) + 0.25
    frames = int(seconds * FPS) + 1
    caption = _texts(work, f"cap{n}_", wrap(scene.caption))

    style = f"fontfile={_q(font)}:fontcolor=white"
    draw = []
    for i, t in enumerate(title):
        draw.append(f"drawtext={style}:textfile={_q(t)}:fontsize=62:box=1:boxcolor=black@0.45:"
                    f"boxborderw=16:x=(w-text_w)/2:y={150 + i * 92}")
    top = 1440 if len(caption) < 3 else 1400
    for i, t in enumerate(caption):
        draw.append(f"drawtext={style}:textfile={_q(t)}:fontsize=64:borderw=6:"
                    f"bordercolor=black@0.9:x=(w-text_w)/2:y={top + i * 88}")
    graph = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"boxblur=24:2,eq=brightness=-0.15,setsar=1[bg];"
        f"[1:v]scale={FG}:{FG}:force_original_aspect_ratio=increase,crop={FG}:{FG},"
        f"zoompan=z='min(zoom+0.0006,1.08)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={FG}x{FG}:fps={FPS},setsar=1[fg];"
        f"[bg][fg]overlay=x=(W-w)/2:y=330:shortest=0[base];"
        f"[base]{','.join(draw) if draw else 'null'},format=yuv420p[v]")
    out = work / f"scene{n}.mp4"
    _ffmpeg(["-loop", "1", "-framerate", str(FPS), "-i", str(img), "-i", str(img),
             "-i", str(voice), "-filter_complex", graph, "-map", "[v]", "-map", "2:a",
             "-af", "apad", "-t", f"{seconds:.3f}", "-r", str(FPS),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", str(out)])
    return out


def compose(scenes: list[Scene], title: str, *, font: str | None = None) -> Video:
    if not scenes:
        raise ValueError("no scenes")
    font = font or find_font()
    with tempfile.TemporaryDirectory(prefix="aiw-video-") as tmp:
        work = Path(tmp)
        title_files = _texts(work, "title", wrap(title, width=14, max_lines=2))
        parts = [_scene(work, n, s, title_files, font) for n, s in enumerate(scenes)]
        listing = work / "parts.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        final = work / "short.mp4"
        _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                 "-movflags", "+faststart", str(final)])
        width, height, seconds, audio = probe(final)
        return Video(final.read_bytes(), seconds, width, height, audio)
