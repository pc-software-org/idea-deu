"""Symlink-safe validation for generated output paths."""

from __future__ import annotations

import stat
import tempfile
from pathlib import Path


def unsafe_output_parent(path: Path) -> tuple[str, Path] | None:
    """Return the first unsafe component below a trusted control root."""
    parent = Path(path).absolute().parent
    roots = (Path.cwd().absolute(), Path(tempfile.gettempdir()).absolute())
    candidates = [root for root in roots if parent == root or root in parent.parents]
    if not candidates:
        return "outside trusted control roots", parent
    trusted = max(candidates, key=lambda item: len(item.parts))
    current = trusted
    for component in parent.relative_to(trusted).parts:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            return "symbolic-link parent", current
        if not stat.S_ISDIR(mode):
            return "unsafe non-directory parent", current
    return None
