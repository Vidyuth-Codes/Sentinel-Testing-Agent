"""Output path resolution.

All generated artifacts live under the sentinel-testing-agent repo (never inside
the project under test). Output goes to `<repo>/output/<run>/`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def repo_root() -> Path:
    # src/sentinel/paths.py -> parents[2] == repo root for an editable install.
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def output_base() -> Path:
    d = repo_root() / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_dir(label: str, stamp: str | None = None) -> Path:
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    d = output_base() / f"{label}-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d
