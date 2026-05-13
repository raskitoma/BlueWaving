"""Screenshot retention GC (spec §9.3 / §10/M9).

The worker writes failure-PNGs to a configurable directory; this module
keeps at most ``MAX_KEPT`` of them, deleting the oldest by mtime.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable


log = logging.getLogger(__name__)


DEFAULT_DIR = "/var/lib/bluewave-worker/screenshots"
MAX_KEPT = 60


def list_pngs(directory: str | os.PathLike[str]) -> list[Path]:
    d = Path(directory)
    if not d.exists():
        return []
    return sorted(
        (p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".png"),
        key=lambda p: p.stat().st_mtime,
    )


def gc(
    directory: str | os.PathLike[str] = DEFAULT_DIR,
    *,
    keep: int = MAX_KEPT,
) -> int:
    """Delete the oldest PNGs until at most ``keep`` remain. Returns count
    deleted. Safe to call at startup and again after every run."""
    files = list_pngs(directory)
    if len(files) <= keep:
        return 0
    to_remove = files[: len(files) - keep]
    removed = 0
    for f in to_remove:
        try:
            f.unlink()
            removed += 1
        except OSError as e:
            log.warning("screenshot gc: could not unlink %s: %s", f, e)
    return removed
