"""Configuration loading.

Two-file split, on purpose:

* ``config/config.yaml``  - behaviour. Committed-shaped, reviewable, no secrets.
* ``.env`` / environment  - secrets. Never read from YAML, never logged.

A YAML value written as ``${ENV:SOME_VAR}`` is resolved from the environment at
load time and is the *only* supported way to pull a credential into config, so
a stray ``grep -r`` over the repo can never turn up a live key.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .resources import bundled

_ENV_REF = re.compile(r"^\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}$")

REPO_ROOT = Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------
# dataclasses
# --------------------------------------------------------------------------
@dataclass
class PlatformConfig:
    """Rate-limit and safety envelope for one publishing target."""

    name: str
    enabled: bool = False
    publisher: str = "dryrun"
    accounts: list[str] = field(default_factory=lambda: ["main"])
    daily_limit: int = 3
    weekly_limit: int = 15
    min_interval_minutes: int = 60
    jitter_minutes: int = 20
    active_hours: tuple[int, int] = (9, 22)
    #: "required" -- every item waits for a person (the default).
    #: "auto"     -- an item that passes EVERY guardrail is approved without
    #:              waiting; anything a guardrail flags still goes to a human.
    #: Pick per platform by what a mistake costs: a rejected stock asset costs
    #: a re-upload, a bad post under your own name costs reputation or the
    #: account. Uniform review across both is just a bottleneck on the cheap one.
    approval: str = "required"
    require_ai_disclosure: bool = False
    disclosure_text: str = ""
    max_retries: int = 3
    retry_backoff_seconds: int = 30

    APPROVAL_POLICIES = ("required", "auto")

    def validate(self) -> None:
        if self.approval not in self.APPROVAL_POLICIES:
            raise ConfigError(
                f"[{self.name}] approval は {self.APPROVAL_POLICIES} のいずれか: "
                f"{self.approval!r}"
            )
        if self.daily_limit < 0 or self.weekly_limit < 0:
            raise ConfigError(f"[{self.name}] limits must be >= 0")
        if self.weekly_limit and self.daily_limit > self.weekly_limit:
            raise ConfigError(
                f"[{self.name}] daily_limit({self.daily_limit}) > weekly_limit({self.weekly_limit})"
            )
        lo, hi = self.active_hours
        if not (0 <= lo < hi <= 24):
            raise ConfigError(f"[{self.name}] active_hours must satisfy 0 <= lo < hi <= 24")
        if self.min_interval_minutes < 0 or self.jitter_minutes < 0:
            raise ConfigError(f"[{self.name}] interval/jitter must be >= 0")
        if self.require_ai_disclosure and not self.disclosure_text:
            raise ConfigError(
                f"[{self.name}] require_ai_disclosure is on but disclosure_text is empty"
            )


@dataclass
class AnomalyConfig:
    consecutive_failure_threshold: int = 3
    auto_halt_on_failures: bool = True
    reach_drop_ratio: float = 0.4
    reach_min_samples: int = 5
    quota_warn_ratio: float = 0.8
    warning_keywords: list[str] = field(
        default_factory=lambda: [
            "suspend", "suspended", "violation", "strike", "restricted",
            "shadowban", "appeal", "policy violation", "アカウント停止",
            "利用制限", "違反", "警告", "凍結",
        ]
    )


@dataclass
class QualityConfig:
    min_chars: dict[str, int] = field(default_factory=dict)
    max_chars: dict[str, int] = field(default_factory=dict)
    max_similarity: float = 0.75
    similarity_lookback: int = 200
    banned_artifacts: list[str] = field(
        default_factory=lambda: [
            "lorem ipsum", "as an ai", "as an ai language model", "[insert",
            "todo:", "xxxxx", "```", "私はaiアシスタント",
        ]
    )
    min_distinct_hashtags: int = 0


@dataclass
class PolicyConfig:
    banned_terms: list[str] = field(default_factory=list)
    banned_patterns: list[str] = field(default_factory=list)
    real_person_terms: list[str] = field(default_factory=list)
    trademark_terms: list[str] = field(default_factory=list)
    affiliate_markers: list[str] = field(default_factory=lambda: ["#PR", "#広告", "[PR]", "広告"])
    affiliate_link_hosts: list[str] = field(
        default_factory=lambda: ["a8.net", "px.a8.net", "rentracks", "afi-b.com", "moshimo"]
    )
    require_affiliate_disclosure: bool = True


@dataclass
class GenerationConfig:
    provider: str = "mock"
    model: str = "claude-opus-5"
    max_pending_per_channel: int = 30
    themes: list[str] = field(default_factory=list)
    angles: list[str] = field(default_factory=list)
    tones: list[str] = field(default_factory=list)
    timeout_seconds: int = 120


@dataclass
class NotifyConfig:
    channels: list[str] = field(default_factory=lambda: ["console"])
    discord_webhook_url: str = ""
    slack_webhook_url: str = ""
    min_level: str = "warning"


@dataclass
class Settings:
    timezone: str = "Asia/Tokyo"
    state_dir: Path = REPO_ROOT / "var"
    db_path: Path = REPO_ROOT / "var" / "aiworker.db"
    log_dir: Path = REPO_ROOT / "var" / "logs"
    log_retention_days: int = 30
    dry_run: bool = True
    require_human_approval: bool = True
    platforms: dict[str, PlatformConfig] = field(default_factory=dict)
    anomaly: AnomalyConfig = field(default_factory=AnomalyConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    source_path: Path | None = None

    def platform(self, name: str) -> PlatformConfig:
        try:
            return self.platforms[name]
        except KeyError:
            raise ConfigError(
                f"unknown platform '{name}'. configured: {sorted(self.platforms)}"
            ) from None

    def validate(self) -> None:
        if self.require_human_approval is not True:
            raise ConfigError(
                "require_human_approval must stay true. It is the master switch: "
                "with it on, a platform may still use approval: auto, but only "
                "items that pass every guardrail skip the human -- anything "
                "flagged always waits for one."
            )
        for plat in self.platforms.values():
            plat.validate()
        if not 0 < self.quality.max_similarity <= 1:
            raise ConfigError("quality.max_similarity must be in (0, 1]")
        if not 0 < self.anomaly.reach_drop_ratio < 1:
            raise ConfigError("anomaly.reach_drop_ratio must be in (0, 1)")


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def _resolve_env(value: Any) -> Any:
    """Recursively swap ``${ENV:NAME}`` / ``${ENV:NAME:default}`` for real values."""
    if isinstance(value, str):
        m = _ENV_REF.match(value.strip())
        if m:
            return os.environ.get(m.group(1), m.group(2) or "")
        return value
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader (no dependency on python-dotenv).

    Existing environment variables always win, so `FOO=1 aiworker ...` overrides
    the file rather than the other way round.
    """
    path = path or REPO_ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _platforms_from(raw: dict[str, Any]) -> dict[str, PlatformConfig]:
    out: dict[str, PlatformConfig] = {}
    for name, cfg in (raw or {}).items():
        cfg = dict(cfg or {})
        hours = cfg.pop("active_hours", [9, 22])
        if not isinstance(hours, (list, tuple)) or len(hours) != 2:
            raise ConfigError(f"[{name}] active_hours must be a 2-element list, got {hours!r}")
        known = {f.name for f in PlatformConfig.__dataclass_fields__.values()}
        unknown = set(cfg) - known
        if unknown:
            raise ConfigError(f"[{name}] unknown platform keys: {sorted(unknown)}")
        out[name] = PlatformConfig(name=name, active_hours=(int(hours[0]), int(hours[1])), **cfg)
    return out


def _section(raw: dict[str, Any], key: str, cls):
    data = dict(raw.get(key) or {})
    known = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"[{key}] unknown keys: {sorted(unknown)}")
    return cls(**data)


def config_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("AIWORKER_CONFIG")
    if env:
        return Path(env)
    # The working directory wins over the source tree. With an editable install
    # from a clone, REPO_ROOT is that clone -- so checking it first would make
    # `cd ~/myops && aiworker status` silently read the clone's config instead
    # of the one in ~/myops.
    cwd_local = Path.cwd() / "config" / "config.yaml"
    if cwd_local.exists():
        return cwd_local
    local = REPO_ROOT / "config" / "config.yaml"
    if local.exists():
        return local
    # Nothing configured yet: fall back to the shipped example, which is safe
    # to run (dry_run on, every publisher inert).
    return bundled("config.example.yaml")


def load_settings(path: str | Path | None = None, *, load_env: bool = True) -> Settings:
    if load_env:
        load_dotenv()
    p = config_path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping: {p}")
    raw = _resolve_env(raw)

    state_dir = Path(raw.get("state_dir") or (REPO_ROOT / "var")).expanduser()
    settings = Settings(
        timezone=raw.get("timezone", "Asia/Tokyo"),
        state_dir=state_dir,
        db_path=Path(raw.get("db_path") or (state_dir / "aiworker.db")).expanduser(),
        log_dir=Path(raw.get("log_dir") or (state_dir / "logs")).expanduser(),
        log_retention_days=int(raw.get("log_retention_days", 30)),
        dry_run=bool(raw.get("dry_run", True)),
        require_human_approval=bool(raw.get("require_human_approval", True)),
        platforms=_platforms_from(raw.get("platforms") or {}),
        anomaly=_section(raw, "anomaly", AnomalyConfig),
        quality=_section(raw, "quality", QualityConfig),
        policy=_section(raw, "policy", PolicyConfig),
        generation=_section(raw, "generation", GenerationConfig),
        notify=_section(raw, "notify", NotifyConfig),
        source_path=p,
    )
    _merge_policy_files(settings, p)
    settings.validate()
    return settings


def _merge_policy_files(settings: Settings, cfg_path: Path) -> None:
    """Pull banned-term lists out of ``config/policy/*.yaml`` so the deny-list can
    grow without touching the main config."""
    policy_dir = cfg_path.parent / "policy"
    if not policy_dir.is_dir():
        return
    files = sorted(policy_dir.glob("*.yaml"))
    real = [f for f in files if not f.name.endswith(".example.yaml")]
    for f in real or files:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for key in ("banned_terms", "banned_patterns", "real_person_terms", "trademark_terms"):
            values = data.get(key) or []
            if values:
                getattr(settings.policy, key).extend(str(v) for v in values)
