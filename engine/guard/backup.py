"""Backups of data/engine.db.

`te backup` copies the database to data/backups/ under a UTC timestamp and
keeps the newest 14. It runs before every migration.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

KEEP = 14
PREFIX = "engine-"
SUFFIX = ".db"


def default_backup_dir(db_file: Path) -> Path:
    return db_file.parent / "backups"


def backup(
    db_file: str | os.PathLike[str] | None = None,
    backup_dir: str | os.PathLike[str] | None = None,
    keep: int = KEEP,
) -> Path:
    """Copy the database to backup_dir with a timestamp; prune to the newest `keep`.

    Uses SQLite's online backup API so the copy is consistent even if another
    connection is open. Returns the path of the new backup.
    """
    from engine.db import DatabaseError, db_path

    if keep < 1:
        raise ValueError("keep must be at least 1")
    src = db_path(db_file)
    if not src.exists():
        raise DatabaseError(f"no database at {src}; nothing to back up")
    dest_dir = Path(backup_dir) if backup_dir is not None else default_backup_dir(src)
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    dest = dest_dir / f"{PREFIX}{stamp}{SUFFIX}"
    n = 1
    while dest.exists():  # same microsecond; never overwrite a backup
        dest = dest_dir / f"{PREFIX}{stamp}-{n}{SUFFIX}"
        n += 1

    s = sqlite3.connect(src)
    try:
        d = sqlite3.connect(dest)
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()

    prune(dest_dir, keep)
    return dest


def list_backups(backup_dir: str | os.PathLike[str]) -> list[Path]:
    """Backups in backup_dir, oldest first (timestamped names sort by time)."""
    d = Path(backup_dir)
    if not d.exists():
        return []
    return sorted(
        (
            p for p in d.iterdir()
            if p.is_file() and p.name.startswith(PREFIX) and p.name.endswith(SUFFIX)
        ),
        key=lambda p: p.name[: -len(SUFFIX)],
    )


def prune(backup_dir: str | os.PathLike[str], keep: int = KEEP) -> list[Path]:
    """Delete all but the newest `keep` backups; returns what was deleted."""
    backups = list_backups(backup_dir)
    doomed = backups[:-keep] if len(backups) > keep else []
    for p in doomed:
        p.unlink()
    return doomed
