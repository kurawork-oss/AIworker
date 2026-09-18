"""The bundled templates must ship, and must not drift from the repo copies."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiworker.core.resources import TEMPLATES, bundled, read_bundled

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_template_resolves(name):
    assert bundled(name).is_file()
    assert read_bundled(name).strip(), f"{name} is empty"


@pytest.mark.parametrize("name,relative", sorted(TEMPLATES.items()))
def test_bundled_copy_matches_the_repository_copy(name, relative):
    """The package copy is what the CLI reads; the repo copy is what a person
    browsing the project reads. If they drift, one of those two is a lie."""
    packaged = ROOT / "src" / "aiworker" / "data" / name
    repo = ROOT / relative
    if not packaged.exists():
        pytest.skip("running against an installed package, not the source tree")
    assert repo.exists(), f"{relative} is missing from the repository"
    assert packaged.read_text(encoding="utf-8") == repo.read_text(encoding="utf-8"), (
        f"{name} と {relative} が食い違っています。"
        f"リポジトリ側を正として `cp {relative} src/aiworker/data/{name}` で同期してください。"
    )


def test_checklist_is_the_document_the_cli_promises():
    assert "チェックリスト" in read_bundled("risk-checklist.md")
    assert "緊急停止" in read_bundled("risk-checklist.md")


def test_example_config_fallback_is_safe_to_run():
    """When nothing is configured, `load_settings()` falls back to the bundled
    example. That fallback must never be able to post anything."""
    import yaml

    cfg = yaml.safe_load(read_bundled("config.example.yaml"))
    assert cfg["dry_run"] is True
    for name, platform in cfg["platforms"].items():
        assert platform.get("publisher", "dryrun") in {"dryrun", "manual"}, name


def test_missing_template_raises_a_useful_error():
    with pytest.raises(FileNotFoundError, match="missing from the installation"):
        bundled("no-such-template.md")
