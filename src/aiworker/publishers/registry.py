"""Publisher lookup.

The global ``dry_run`` switch is applied *here*, in one place. Anything that
would otherwise touch a network is replaced with the no-op publisher, so
turning dry-run on cannot be defeated by an adapter that forgot to check it.
"""

from __future__ import annotations

from pathlib import Path

from ..core.config import Settings
from ..core.errors import ConfigError
from .base import Publisher
from .dryrun import DryRunPublisher, ManualPublisher

#: Register real adapters here as they are built and authorised, e.g.
#: ``"x": lambda s: XPublisher(...)``. Keeping the table explicit means an
#: adapter cannot become reachable just by existing on disk.
ADAPTERS: dict[str, object] = {}


def build_publisher(name: str, settings: Settings) -> Publisher:
    if name == "dryrun":
        return DryRunPublisher()
    if name == "manual":
        return ManualPublisher(Path(settings.state_dir) / "outbox", settings.timezone)
    factory = ADAPTERS.get(name)
    if factory is None:
        raise ConfigError(
            f"no publisher adapter registered for '{name}'. "
            f"available: {['dryrun', 'manual', *sorted(ADAPTERS)]}"
        )
    return factory(settings)  # type: ignore[operator]


def publisher_for(settings: Settings, platform: str) -> Publisher:
    cfg = settings.platform(platform)
    publisher = build_publisher(cfg.publisher, settings)
    if settings.dry_run and getattr(publisher, "performs_network_io", True):
        return DryRunPublisher()
    return publisher
