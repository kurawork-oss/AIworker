"""Legal / platform-policy screening.

This is a *screen*, not a legal opinion: it catches the cheap, high-frequency
mistakes (a celebrity's name in an image prompt, a trademark in a title, an
affiliate link with no disclosure) before a human ever sees the item, so the
human's review time goes to judgement calls rather than to obvious rejects.

Anything flagged BLOCKING never reaches the approval queue as "ready"; it lands
in ``blocked`` and needs an explicit human override.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from ..core.config import PlatformConfig, PolicyConfig
from ..core.models import ContentItem

BLOCKING = "blocking"
WARNING = "warning"

# Categories of prompt that are simply not worth the risk for a stock/AI pipeline.
_INHERENT_RISK_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)\bin the style of\s+\w+", "imitating a named artist's style"),
    (r"(?i)\b(disney|pixar|ghibli|marvel|pokemon|pokémon|nintendo|star wars)\b",
     "well-known IP reference"),
    (r"(?i)\b(getty|shutterstock|adobe\s*stock)\s+watermark", "watermark reference"),
    (r"(?i)\b(celebrity|famous actor|famous singer|有名人|芸能人)\b",
     "real-person likeness reference"),
    (r"(?i)\b(logo of|brand logo|ロゴ入り)\b", "brand logo reference"),
]

# Claims that invite consumer-protection trouble in JP/US alike.
_CLAIM_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)(絶対に?稼げ|必ず儲か|100%稼げ|確実に稼げ|不労所得で月\d+万円確定)",
     "guaranteed-income claim"),
    (r"(?i)(guaranteed\s+(income|profit|returns))", "guaranteed-income claim"),
    (r"(?i)(がん|癌|うつ病|糖尿病)(が|を)?(治る|治療|完治)", "medical efficacy claim"),
    (r"(?i)(cures?\s+(cancer|depression|diabetes))", "medical efficacy claim"),
]

_URL_RE = re.compile(r"https?://[^\s\]\)>\"']+")

# Wording that counts as an AI-generation disclosure. Matching the platform's
# exact configured sentence would be brittle -- a writer who rephrases it has
# still disclosed -- so any of these satisfies the requirement.
_AI_DISCLOSURE_PATTERNS = [
    r"(?i)generated\s+with\s+ai",
    r"(?i)\bai[- ]generated\b",
    r"(?i)made\s+with\s+(generative\s+)?ai",
    r"AI(で|によって)?生成",
    r"AI生成",
    r"生成AI",
    r"AI(を)?(使用|活用|支援)",
]


@dataclass
class Finding:
    rule: str
    severity: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.rule}: {self.detail}"


@dataclass
class PolicyReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(f.severity == BLOCKING for f in self.findings)

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARNING]

    def as_dict(self) -> dict:
        return {
            "blocking": self.blocking,
            "findings": [{"rule": f.rule, "severity": f.severity, "detail": f.detail}
                         for f in self.findings],
        }

    def reasons(self) -> list[str]:
        return [str(f) for f in self.findings]


def _contains_any(text: str, terms: Iterable[str]) -> list[str]:
    low = text.lower()
    return [t for t in terms if t and t.lower() in low]


def check(item: ContentItem, policy: PolicyConfig,
          platform_cfg: PlatformConfig | None = None) -> PolicyReport:
    report = PolicyReport()
    text = item.full_text
    # `negative_prompt` is deliberately excluded: it lists what the image must
    # NOT contain ("no logos, no real person"), so scanning it for those very
    # words flags the safety instruction as the risk.
    prompt_text = " ".join(
        str(item.meta.get(k, "")) for k in ("prompt", "image_prompt", "video_prompt")
    )
    scan = f"{text}\n{prompt_text}"

    for term in _contains_any(scan, policy.banned_terms):
        report.findings.append(Finding("banned_term", BLOCKING, f"contains banned term '{term}'"))
    for term in _contains_any(scan, policy.real_person_terms):
        report.findings.append(
            Finding("real_person", BLOCKING, f"references a real person '{term}'")
        )
    for term in _contains_any(scan, policy.trademark_terms):
        report.findings.append(
            Finding("trademark", BLOCKING, f"references trademark '{term}'")
        )
    for pattern in policy.banned_patterns:
        try:
            if re.search(pattern, scan, re.IGNORECASE):
                report.findings.append(
                    Finding("banned_pattern", BLOCKING, f"matches configured pattern /{pattern}/")
                )
        except re.error:
            report.findings.append(
                Finding("bad_config", WARNING, f"invalid regex in policy config: /{pattern}/")
            )
    for pattern, why in _INHERENT_RISK_PATTERNS:
        if re.search(pattern, scan):
            report.findings.append(Finding("ip_risk", BLOCKING, why))
    for pattern, why in _CLAIM_PATTERNS:
        if re.search(pattern, scan):
            report.findings.append(Finding("unsupported_claim", BLOCKING, why))

    # --- affiliate disclosure -------------------------------------------------
    urls = _URL_RE.findall(scan)
    has_affiliate = bool(item.meta.get("affiliate_url")) or any(
        host.lower() in u.lower() for u in urls for host in policy.affiliate_link_hosts
    )
    if has_affiliate and policy.require_affiliate_disclosure:
        if not any(m.lower() in scan.lower() for m in policy.affiliate_markers):
            report.findings.append(
                Finding(
                    "affiliate_disclosure", BLOCKING,
                    "affiliate link present without a PR/広告 disclosure marker "
                    f"(expected one of {policy.affiliate_markers})",
                )
            )

    # --- AI disclosure --------------------------------------------------------
    if platform_cfg and platform_cfg.require_ai_disclosure:
        if not has_ai_disclosure(item, platform_cfg):
            report.findings.append(
                Finding(
                    "ai_disclosure", BLOCKING,
                    f"{platform_cfg.name} requires an AI-generation disclosure "
                    f"(e.g. '{platform_cfg.disclosure_text}') and none was found",
                )
            )

    # --- soft signals ---------------------------------------------------------
    if len(urls) > 3:
        report.findings.append(
            Finding("link_density", WARNING, f"{len(urls)} links in one item reads as spam")
        )
    if scan.count("#") > 12:
        report.findings.append(
            Finding("hashtag_stuffing", WARNING, f"{scan.count('#')} hashtags")
        )
    return report


def disclosure_scan_text(item: ContentItem) -> str:
    """Everywhere a disclosure could legitimately live."""
    parts = [item.title, item.body, str(item.meta.get("ai_disclosure", ""))]
    for key in ("description", "video_title", "headline"):
        value = item.meta.get(key)
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(p for p in parts if p)


def has_ai_disclosure(item: ContentItem, platform_cfg: PlatformConfig) -> bool:
    text = disclosure_scan_text(item)
    if platform_cfg.disclosure_text and platform_cfg.disclosure_text.lower() in text.lower():
        return True
    if item.meta.get("ai_generated") is True and re.search(
        r"(?i)ai", str(item.meta.get("description", ""))
    ):
        return True
    return any(re.search(p, text) for p in _AI_DISCLOSURE_PATTERNS)


def ensure_ai_disclosure(item: ContentItem, platform_cfg: PlatformConfig | None) -> bool:
    """Append the platform's disclosure sentence when it is required and absent.

    Disclosure is a mechanical requirement, not a creative one: making it
    automatic removes the most common reason an otherwise-fine item gets
    blocked, and the check above still verifies the result rather than trusting
    this ran. Returns True when the item was modified.
    """
    if not platform_cfg or not platform_cfg.require_ai_disclosure:
        return False
    if has_ai_disclosure(item, platform_cfg):
        return False
    text = platform_cfg.disclosure_text
    if not text:
        return False
    item.meta["ai_disclosure"] = text
    item.meta["ai_disclosure_auto_added"] = True
    if isinstance(item.meta.get("description"), str):
        item.meta["description"] = f"{item.meta['description']}\n\n{text}".strip()
    else:
        item.body = f"{item.body}\n\n{text}".strip()
    return True
