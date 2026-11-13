"""`te extract facts`: run every extractor over each company's bundle and write
the facts table (docs/PLAN.md Steps 37-38).

- Reads the artifacts rows and their extracted text and structure from the
  cache (Step 35). Zero network. Zero model calls: the title classifier is
  used through its rules and the answers already cached in llm_calls, read
  with gateway.cached_output, which never calls a provider. The run counts
  llm_calls rows before and after and reports any difference.
- A company is ready when every ok artifact is extracted at the current
  extract_text.EXTRACTOR_VERSION with its files in the cache. A company that
  is not ready is reported and left alone (`te extract text` first): facts
  from half a bundle would read as absences.
- Every fact is checked with base.verify before it is written; a fact whose
  quote cannot be found where it says is dropped and reported, never written.
- Idempotent: a company's facts are compared with what the table holds; when
  they are the same nothing is written. When anything differs (an input, a
  rule, patterns.yaml or lexicons.yaml changed), the company's facts are
  replaced in one transaction, older extractor versions included, so the
  table holds one current set per company.
- Status: a company at captured moves to extracted once its facts are
  written (or found already current). A company with no fact at all (every
  artifact unseen) stays at captured and is reported. Companies past
  captured (the hand set at sent) get facts and keep their status.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from engine import db, states
from engine.capture import extract_text as X
from engine.extract import EXTRACTORS, extractor_version
from engine.extract.base import Art, Bundle, FactOut, verify
from engine.guard.cache import Cache
from engine.models import Fact
from engine.signal import classify as C
from engine.states import PIPELINE, Status

_FROM_CAPTURED = frozenset(PIPELINE[PIPELINE.index(Status.CAPTURED):])


class ExtractFactsError(RuntimeError):
    pass


@dataclass
class Report:
    version: str = ""
    companies: int = 0
    changed: int = 0  # companies whose facts were (or would be) rewritten
    unchanged: int = 0
    rows: int = 0  # facts rows written (or that would be)
    advanced: int = 0  # captured -> extracted
    not_ready: list[str] = field(default_factory=list)
    no_facts: list[str] = field(default_factory=list)  # ready, but nothing seen: not advanced
    unseen: Counter = field(default_factory=Counter)  # fact -> companies skipped as unseen
    rejected: list[str] = field(default_factory=list)  # facts dropped by verify
    by_fact: Counter = field(default_factory=Counter)  # fact -> rows
    unclassified_titles: int = 0
    model_calls: int = 0  # llm_calls rows added during the run; must be 0


def select(conn: sqlite3.Connection, *, company_id: str | None = None, pilot: bool = False,
           limit: int | None = None) -> list[str]:
    """Companies at captured or later, not dropped, with artifacts rows."""
    if company_id is not None:
        row = conn.execute("SELECT status FROM companies WHERE company_id = ?",
                           (company_id,)).fetchone()
        if row is None:
            raise ExtractFactsError(f"no company {company_id!r}")
        if Status(row["status"]) not in _FROM_CAPTURED:
            raise ExtractFactsError(f"{company_id}: status {row['status']}; facts need a "
                                    "captured bundle")
        return [company_id]
    sql = ("SELECT c.company_id, c.status FROM companies c WHERE EXISTS "
           "(SELECT 1 FROM artifacts a WHERE a.company_id = c.company_id)")
    if pilot:
        sql += " AND (c.pilot = 1 OR c.hand = 1)"
    ids = [r["company_id"] for r in conn.execute(sql + " ORDER BY c.company_id")
           if Status(r["status"]) in _FROM_CAPTURED]
    return ids[:limit] if limit is not None else ids


def classifier(conn: sqlite3.Connection):
    """title -> family from the rules, else from the cached model answer, else None.
    Reads llm_calls; never calls a model."""
    memo: dict[str, str | None] = {}

    def fam(title: str) -> str | None:
        if title not in memo:
            f = C.rule_family(title)
            if f is None:
                hit, out = C.gateway.cached_output(conn, C.STAGE, None,
                                                   C.prompt_version_for(title))
                if hit and isinstance(out, dict) and out.get("role_family") in C.FAMILIES:
                    f = out["role_family"]
            memo[title] = f
        return memo[title]

    return fam


def load_bundle(conn: sqlite3.Connection, cache: Cache, company_id: str) -> Bundle | str:
    """The company's bundle, or why it is not ready."""
    c = conn.execute("SELECT c.*, s.title AS signal_title FROM companies c LEFT JOIN signals s "
                     "ON s.signal_id = c.best_signal_id WHERE c.company_id = ?",
                     (company_id,)).fetchone()
    rows = conn.execute("SELECT * FROM artifacts WHERE company_id = ? ORDER BY artifact_id",
                        (company_id,)).fetchall()
    if not rows:
        return "no artifacts"
    arts: dict[str, Art] = {}
    for r in rows:
        text = structure = None
        if r["outcome"] == "ok":
            if r["extractor_version"] != X.EXTRACTOR_VERSION:
                return (f"{r['kind']} not extracted at {X.EXTRACTOR_VERSION}; "
                        "run `te extract text`")
            text, structure = X.stored_text(cache, r), X.stored_structure(cache, r)
            if text is None or structure is None:
                return f"{r['kind']}: extracted text missing from the cache; run `te extract text`"
        arts[r["kind"]] = Art(r["kind"], r["url"], r["outcome"], text, structure)
    return Bundle(company_id=company_id, domain=c["domain"], name=c["name"], arts=arts,
                  signal_title=c["signal_title"], classify=classifier(conn))


def compute(b: Bundle) -> tuple[list[FactOut], list[str], list[str]]:
    """(verified facts, facts skipped because an artifact was unseen, rejections)."""
    out: list[FactOut] = []
    unseen: list[str] = []
    rejected: list[str] = []
    keys: set[tuple] = set()
    for name, fn, kinds in EXTRACTORS:
        got = fn(b)
        if not got and any(b.state(k) == "unseen" for k in kinds):
            unseen.append(name)
        for f in got:
            if not verify(f, b.arts.get(f.artifact_kind)):
                rejected.append(f"{b.company_id}/{f.fact}: quote {f.quote[:60]!r} not found "
                                f"in {f.artifact_kind} {f.quote_source}")
                continue
            key = (f.fact, f.value_text(), f.artifact_kind, f.char_start)
            if key in keys:
                continue
            keys.add(key)
            out.append(f)
    return out, unseen, rejected


_COLS = ("fact", "value", "artifact_kind", "quote", "char_start", "extractor_version",
         "found", "quote_source")


def _rows(company_id: str, facts: list[FactOut], version: str) -> list[Fact]:
    return [Fact(company_id=company_id, fact=f.fact, value=f.value_text(),
                 artifact_kind=f.artifact_kind, quote=f.quote, char_start=f.char_start,
                 extractor_version=version, found=f.found, quote_source=f.quote_source)
            for f in facts]


def _key(r: Fact | sqlite3.Row) -> tuple:
    get = (lambda k: getattr(r, k)) if isinstance(r, Fact) else (lambda k: r[k])
    return tuple(int(get(k)) if k == "found" else get(k) for k in _COLS)


def _llm_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0])


def run(conn: sqlite3.Connection, cache: Cache, company_ids: list[str], *,
        dry_run: bool = False) -> Report:
    version = extractor_version()
    rep = Report(version=version, companies=len(company_ids))
    before = _llm_count(conn)
    for cid in company_ids:
        b = load_bundle(conn, cache, cid)
        if isinstance(b, str):
            rep.not_ready.append(f"{cid}: {b}")
            continue
        facts, unseen, rejected = compute(b)
        rep.unseen.update(unseen)
        rep.rejected += rejected
        rep.unclassified_titles += sum(f.value["count"] for f in facts
                                       if f.fact == "gtm_roles_by_family"
                                       and f.value["family"] == "unclassified")
        rows = _rows(cid, facts, version)
        rep.by_fact.update(r.fact for r in rows)
        old = conn.execute("SELECT * FROM facts WHERE company_id = ?", (cid,)).fetchall()
        same = Counter(map(_key, old)) == Counter(map(_key, rows))
        if same:
            rep.unchanged += 1
        else:
            rep.changed += 1
            rep.rows += len(rows)
            if not dry_run:
                with conn:
                    conn.execute("DELETE FROM facts WHERE company_id = ?", (cid,))
                    for r in rows:
                        db.insert(conn, r)
        if not rows:
            rep.no_facts.append(cid)
            continue
        status = conn.execute("SELECT status FROM companies WHERE company_id = ?",
                              (cid,)).fetchone()["status"]
        if status == Status.CAPTURED:
            if not dry_run:
                states.advance(cid, Status.EXTRACTED, conn)
            rep.advanced += 1
    rep.model_calls = _llm_count(conn) - before
    return rep
