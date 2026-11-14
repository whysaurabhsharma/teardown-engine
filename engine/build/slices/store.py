"""Reading and writing the slices and slice_competitors tables (migration 0016).

- write_slice: one row per (company, kind); an unchanged slice is not touched.
- select: marks the slice the teardown uses (selected = 1, at most one).
- hold / clear_hold: a company whose primary slice and role_funnel both fail
  is held for review with review_reason "slice_thin: ...". protocol/ICP.md has
  no drop reason for a thin slice and cannot change in this step, so the
  company is held, not dropped (docs/PLAN.md Step 63 says "dropped"; a person
  decides). A review_reason another stage wrote is never overwritten.
- competitors: the two funnel_compare compares against, and whether a person
  confirmed them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from engine.build.slices.common import Slice

HOLD_CODE = "slice_thin"

_COLS = ("builder_version", "outcome", "reason", "title", "columns", "rows", "dropped", "meta",
         "inputs_key", "fetches", "searches")


def _dump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _values(sl: Slice) -> dict[str, Any]:
    return {"builder_version": sl.version, "outcome": "ok" if sl.ok else "failed",
            "reason": sl.reason, "title": sl.title, "columns": _dump(sl.columns),
            "rows": _dump(sl.rows), "dropped": _dump(sl.dropped), "meta": _dump(sl.meta),
            "inputs_key": sl.inputs_key, "fetches": sl.fetches, "searches": sl.searches}


def write_slice(conn: sqlite3.Connection, company_id: str, sl: Slice) -> bool:
    """Upsert; True when the row changed. A changed slice that is no longer ok
    loses its selected mark."""
    new = _values(sl)
    old = conn.execute("SELECT * FROM slices WHERE company_id = ? AND kind = ?",
                       (company_id, sl.kind)).fetchone()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        if old is None:
            cols = ("company_id", "kind", *_COLS, "built_at")
            conn.execute(f"INSERT INTO slices ({', '.join(cols)}) VALUES "
                         f"({', '.join('?' * len(cols))})",
                         (company_id, sl.kind, *(new[c] for c in _COLS), now))
            return True
        if all(old[c] == new[c] for c in _COLS):
            return False
        sel = old["selected"] if sl.ok else 0
        conn.execute(f"UPDATE slices SET {', '.join(f'{c} = ?' for c in _COLS)}, built_at = ?, "
                     "selected = ? WHERE slice_id = ?",
                     (*(new[c] for c in _COLS), now, sel, old["slice_id"]))
        return True


@dataclass
class Stored:
    kind: str
    version: str
    ok: bool
    reason: str | None
    title: str
    columns: list[str]
    rows: list[dict[str, Any]]
    dropped: list[dict[str, Any]]
    meta: dict[str, Any]
    inputs_key: str | None
    selected: bool
    fetches: int
    searches: int
    built_at: str


def _stored(r: sqlite3.Row) -> Stored:
    return Stored(r["kind"], r["builder_version"], r["outcome"] == "ok", r["reason"], r["title"],
                  json.loads(r["columns"]), json.loads(r["rows"]), json.loads(r["dropped"]),
                  json.loads(r["meta"]), r["inputs_key"], bool(r["selected"]), r["fetches"],
                  r["searches"], r["built_at"])


def slices(conn: sqlite3.Connection, company_id: str) -> list[Stored]:
    return [_stored(r) for r in conn.execute(
        "SELECT * FROM slices WHERE company_id = ? ORDER BY selected DESC, kind", (company_id,))]


def get(conn: sqlite3.Connection, company_id: str, kind: str) -> Stored | None:
    r = conn.execute("SELECT * FROM slices WHERE company_id = ? AND kind = ?",
                     (company_id, kind)).fetchone()
    return _stored(r) if r is not None else None


def selected(conn: sqlite3.Connection, company_id: str) -> Stored | None:
    r = conn.execute("SELECT * FROM slices WHERE company_id = ? AND selected = 1",
                     (company_id,)).fetchone()
    return _stored(r) if r is not None else None


def select(conn: sqlite3.Connection, company_id: str, kind: str | None) -> bool:
    """Mark kind selected (None: none). True when anything changed."""
    cur = conn.execute("SELECT kind FROM slices WHERE company_id = ? AND selected = 1",
                       (company_id,)).fetchone()
    if (cur["kind"] if cur else None) == kind:
        return False
    with conn:
        conn.execute("UPDATE slices SET selected = 0 WHERE company_id = ? AND selected = 1",
                     (company_id,))
        if kind is not None:
            conn.execute("UPDATE slices SET selected = 1 WHERE company_id = ? AND kind = ? "
                         "AND outcome = 'ok'", (company_id, kind))
    return True


def _own_reason(reason: str | None) -> bool:
    return reason is None or reason.startswith(f"{HOLD_CODE}:")


def hold(conn: sqlite3.Connection, company_id: str, detail: str) -> str:
    """held | unchanged | kept (another stage's review_reason stands)."""
    r = conn.execute("SELECT review_reason FROM companies WHERE company_id = ?",
                     (company_id,)).fetchone()
    want = f"{HOLD_CODE}: {detail}"
    if r["review_reason"] == want:
        return "unchanged"
    if not _own_reason(r["review_reason"]):
        return "kept"
    with conn:
        conn.execute("UPDATE companies SET review_reason = ?, updated_at = "
                     "strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE company_id = ?",
                     (want, company_id))
    return "held"


def clear_hold(conn: sqlite3.Connection, company_id: str) -> bool:
    r = conn.execute("SELECT review_reason FROM companies WHERE company_id = ?",
                     (company_id,)).fetchone()
    if r["review_reason"] is None or not r["review_reason"].startswith(f"{HOLD_CODE}:"):
        return False
    with conn:
        conn.execute("UPDATE companies SET review_reason = NULL, updated_at = "
                     "strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE company_id = ?",
                     (company_id,))
    return True


# -- competitors -----------------------------------------------------------------------


@dataclass
class Competitor:
    name: str
    domain: str | None
    source: str  # named_competitors | alternatives_search | manual
    evidence_url: str | None = None
    evidence_quote: str | None = None
    rank: int = 1
    confirmed: bool = False
    confirmed_at: str | None = None


def competitors(conn: sqlite3.Connection, company_id: str) -> list[Competitor]:
    return [Competitor(r["name"], r["domain"], r["source"], r["evidence_url"],
                       r["evidence_quote"], r["rank"], bool(r["confirmed"]), r["confirmed_at"])
            for r in conn.execute("SELECT * FROM slice_competitors WHERE company_id = ? "
                                  "ORDER BY rank, name", (company_id,))]


def write_competitors(conn: sqlite3.Connection, company_id: str,
                      comps: list[Competitor]) -> None:
    """Replace the company's competitor choice (unconfirmed unless marked)."""
    with conn:
        conn.execute("DELETE FROM slice_competitors WHERE company_id = ?", (company_id,))
        for c in comps:
            conn.execute("INSERT INTO slice_competitors (company_id, name, domain, source, "
                         "evidence_url, evidence_quote, rank, confirmed, confirmed_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (company_id, c.name, c.domain, c.source, c.evidence_url,
                          c.evidence_quote, c.rank, int(c.confirmed), c.confirmed_at))


def confirm(conn: sqlite3.Connection, company_id: str) -> int:
    """Mark every chosen competitor confirmed; returns how many changed."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        cur = conn.execute("UPDATE slice_competitors SET confirmed = 1, confirmed_at = ? "
                           "WHERE company_id = ? AND confirmed = 0 AND domain IS NOT NULL",
                           (now, company_id))
    return cur.rowcount


def confirmation(conn: sqlite3.Connection, company_id: str) -> dict[str, bool]:
    """name -> confirmed, for `te teardown check` (TEARDOWN_SPEC: competitors are
    named only after they have been confirmed in review)."""
    return {c.name: c.confirmed for c in competitors(conn, company_id)}
