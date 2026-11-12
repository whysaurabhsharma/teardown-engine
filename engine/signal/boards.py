"""Run the ATS collectors over a list of boards and store what they find.

A board is named `source:slug`, e.g. `greenhouse:zentrico`. Storing is
insert-only on the deterministic signal_id, so a re-run inserts nothing new
for posts already seen and never rewrites first_seen.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from types import ModuleType

from engine import db
from engine.guard.http import Guard
from engine.models import Signal
from engine.signal import ashby, ats, greenhouse, lever, workable

COLLECTORS: dict[str, ModuleType] = {
    m.SOURCE: m for m in (greenhouse, lever, ashby, workable)
}


def parse_spec(spec: str) -> tuple[str, str]:
    """'greenhouse:zentrico' -> ('greenhouse', 'zentrico')."""
    source, sep, slug = spec.strip().partition(":")
    source = source.strip().lower()
    if not sep or source not in COLLECTORS:
        raise ValueError(
            f"board must be SOURCE:SLUG with SOURCE one of {', '.join(COLLECTORS)}: {spec!r}"
        )
    return source, ats.check_slug(slug)


def read_boards_file(path: str | Path) -> list[str]:
    """One SOURCE:SLUG per line; blank lines and # comments ignored."""
    specs = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            specs.append(line)
    return specs


def unique_boards(specs: Iterable[str]) -> list[tuple[str, str]]:
    """Parsed boards in order, repeats removed (slugs compared case-insensitively)."""
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    for spec in specs:
        source, slug = parse_spec(spec)
        seen.setdefault((source, slug.lower()), (source, slug))
    return list(seen.values())


def board_urls(boards: Iterable[tuple[str, str]]) -> list[str]:
    return [COLLECTORS[source].board_url(slug) for source, slug in boards]


async def collect_board(
    source: str, slug: str, guard: Guard | None = None, *, today: date | None = None
) -> ats.BoardResult:
    return await COLLECTORS[source].fetch_board(slug, guard, today=today)


def store(conn: sqlite3.Connection, signals: Iterable[Signal]) -> int:
    """Insert signals not already present; returns how many were new."""
    new = 0
    with conn:
        for s in ats.dedupe(signals):
            hit = conn.execute(
                "SELECT 1 FROM signals WHERE signal_id = ?", (s.signal_id,)
            ).fetchone()
            if hit is None:
                db.insert(conn, s)
                new += 1
    return new
