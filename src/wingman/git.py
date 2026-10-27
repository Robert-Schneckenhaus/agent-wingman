"""Small helpers around the git CLI. All functions return None outside a git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:  # git not installed
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def toplevel(path: Path) -> Path | None:
    out = _git(path, "rev-parse", "--show-toplevel")
    return Path(out).resolve() if out else None


def remote_url(path: Path, remote: str = "origin") -> str | None:
    return _git(path, "remote", "get-url", remote)


def head_commit(path: Path) -> str | None:
    return _git(path, "rev-parse", "HEAD")
