"""Alerting.

Deliberately dependency-free: Discord/Slack incoming webhooks are plain HTTPS
POSTs, which ``urllib`` handles. A delivery failure must never take the caller
down -- an alert that crashes the publisher would turn a small problem into an
outage -- so every send is wrapped and reported as a boolean.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..core.config import NotifyConfig
from ..core.logging_setup import get_logger
from ..core.models import Severity

log = get_logger("notify")

_EMOJI = {
    "debug": "·", "info": "ℹ️", "warning": "⚠️", "error": "🛑", "critical": "🚨",
}


@dataclass
class Alert:
    level: Severity
    title: str
    body: str = ""
    platform: str = ""

    def render(self) -> str:
        head = f"{_EMOJI.get(self.level.value, '')} [{self.level.value.upper()}]"
        if self.platform:
            head += f" ({self.platform})"
        return f"{head} {self.title}\n{self.body}".rstrip()


class Notifier:
    def __init__(self, config: NotifyConfig):
        self.config = config

    def _enabled(self, level: Severity) -> bool:
        try:
            return level.rank >= Severity(self.config.min_level).rank
        except ValueError:
            return True

    def send(self, alert: Alert) -> dict[str, bool]:
        if not self._enabled(alert.level):
            return {}
        results: dict[str, bool] = {}
        for channel in self.config.channels:
            try:
                if channel == "console":
                    print(alert.render(), flush=True)
                    results[channel] = True
                elif channel == "discord":
                    results[channel] = self._post_json(
                        self.config.discord_webhook_url, {"content": alert.render()[:1900]}
                    )
                elif channel == "slack":
                    results[channel] = self._post_json(
                        self.config.slack_webhook_url, {"text": alert.render()[:3000]}
                    )
                else:
                    log.warning("unknown notify channel: %s", channel)
                    results[channel] = False
            except Exception as exc:  # never let alerting break the caller
                log.warning("notify channel %s failed: %s", channel, exc)
                results[channel] = False
        return results

    @staticmethod
    def _post_json(url: str, payload: dict) -> bool:
        if not url:
            log.warning("webhook URL not configured; alert dropped")
            return False
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "aiworker/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return 200 <= resp.status < 300
        except urllib.error.URLError as exc:
            log.warning("webhook post failed: %s", exc)
            return False


def build_notifier(config: NotifyConfig) -> Notifier:
    return Notifier(config)
