"""Diversity control.

"Same theme, different angle" is what separates a body of work from a spam
run. Rather than trusting the model to vary itself across independent calls
(it will not -- independent calls regress to the same mean), we pick the
axes deliberately and steer *away* from whatever was used recently.

Axes: theme x angle x tone. Combinations used in the recent past are penalised;
exact repeats inside the cooldown window are excluded outright.
"""

from __future__ import annotations

import random
import sqlite3
from collections import Counter
from dataclasses import dataclass

DEFAULT_ANGLES = [
    "失敗談から逆算する", "数字で検証する", "初心者がつまずく順番", "やらないことを決める",
    "道具より手順", "1週間の記録", "他人の助言を疑う", "最小構成で始める",
    "続ける仕組み", "やめる基準",
]
DEFAULT_TONES = ["実務的", "対話的", "淡々と", "問いかけ中心", "体験ベース", "要点先出し"]


@dataclass(frozen=True)
class Variant:
    theme: str
    angle: str
    tone: str

    @property
    def key(self) -> str:
        return f"{self.theme}|{self.angle}|{self.tone}"


def recent_combinations(conn: sqlite3.Connection, channel: str, *, lookback: int = 60
                        ) -> Counter[str]:
    rows = conn.execute(
        "SELECT theme, angle, tone FROM content_items WHERE channel=? ORDER BY id DESC LIMIT ?",
        (channel, lookback),
    ).fetchall()
    return Counter(f"{r['theme']}|{r['angle']}|{r['tone']}" for r in rows)


def recent_axis(conn: sqlite3.Connection, channel: str, column: str, *, lookback: int = 20
                ) -> Counter[str]:
    if column not in {"theme", "angle", "tone"}:
        raise ValueError(f"not a diversity axis: {column}")
    rows = conn.execute(
        f"SELECT {column} v FROM content_items WHERE channel=? ORDER BY id DESC LIMIT ?",
        (channel, lookback),
    ).fetchall()
    return Counter(r["v"] for r in rows if r["v"])


def plan_variants(
    conn: sqlite3.Connection,
    channel: str,
    count: int,
    *,
    themes: list[str],
    angles: list[str] | None = None,
    tones: list[str] | None = None,
    rng: random.Random | None = None,
    cooldown: int = 60,
) -> list[Variant]:
    """Choose ``count`` distinct (theme, angle, tone) combinations.

    Scoring is "least recently / least often used wins", with a random
    tiebreak so two runs on the same day do not produce the same order.
    """
    rng = rng or random.Random()
    themes = [t for t in (themes or []) if t] or ["AI活用"]
    angles = [a for a in (angles or DEFAULT_ANGLES) if a] or DEFAULT_ANGLES
    tones = [t for t in (tones or DEFAULT_TONES) if t] or DEFAULT_TONES

    used_combo = recent_combinations(conn, channel, lookback=cooldown)
    used_theme = recent_axis(conn, channel, "theme")
    used_angle = recent_axis(conn, channel, "angle")

    candidates: list[tuple[float, Variant]] = []
    for theme in themes:
        for angle in angles:
            for tone in tones:
                v = Variant(theme, angle, tone)
                if used_combo.get(v.key):
                    continue  # exact repeat inside the cooldown window
                score = used_theme.get(theme, 0) * 2 + used_angle.get(angle, 0) + rng.random()
                candidates.append((score, v))

    if not candidates:  # everything is on cooldown: fall back to the oldest combos
        pool = [Variant(t, a, o) for t in themes for a in angles for o in tones]
        rng.shuffle(pool)
        return pool[:count]

    candidates.sort(key=lambda p: p[0])
    chosen: list[Variant] = []
    seen_themes: Counter[str] = Counter()
    for _, v in candidates:
        if len(chosen) >= count:
            break
        # don't let one theme eat the whole batch
        if seen_themes[v.theme] >= max(1, count // max(1, len(themes)) + 1):
            continue
        if any(c.angle == v.angle for c in chosen):
            continue
        chosen.append(v)
        seen_themes[v.theme] += 1

    i = 0
    while len(chosen) < count and i < len(candidates):
        v = candidates[i][1]
        if v not in chosen:
            chosen.append(v)
        i += 1
    return chosen[:count]
