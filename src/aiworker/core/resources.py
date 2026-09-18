"""Files the CLI needs at runtime, resolved wherever the package is installed.

`aiworker init` and `aiworker checklist` used to read these by walking up from
`__file__` to a repository root. That works from a clone and breaks the moment
somebody runs `pip install aiworker`, which is how the two commands an operator
runs *first* -- set the thing up, read the safety checklist -- both failed on
the main install path.

Templates therefore ship inside the package (`aiworker/data/`). The copies at
the repository root stay, because that is where a person browsing the project
looks for them; `tests/test_resources.py` asserts the two are byte-identical so
they cannot drift.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

#: bundled name -> path in the source tree, for the editable/clone case.
TEMPLATES: dict[str, str] = {
    "config.example.yaml": "config/config.example.yaml",
    "banned_terms.example.yaml": "config/policy/banned_terms.example.yaml",
    "env.example": ".env.example",
    "risk-checklist.md": "docs/05-risk-checklist.md",
}


def bundled(name: str) -> Path:
    """Return a readable path for a bundled template.

    Prefers the copy inside the installed package; falls back to the source
    tree so a working copy with an un-synced `data/` still behaves.
    """
    try:
        path = resources.files("aiworker").joinpath("data", name)
        if path.is_file():
            return Path(str(path))
    except (ModuleNotFoundError, AttributeError, TypeError):
        pass

    relative = TEMPLATES.get(name)
    if relative:
        candidate = Path(__file__).resolve().parents[3] / relative
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"bundled template '{name}' is missing from the installation. "
        "Reinstall the package, or run from a source checkout."
    )


def read_bundled(name: str) -> str:
    return bundled(name).read_text(encoding="utf-8")
