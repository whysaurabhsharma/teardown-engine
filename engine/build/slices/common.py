"""What every proof-slice builder shares (docs/PLAN.md Step 63; protocol/
TEARDOWN_SPEC.md "Every row in the proof slice carries a source URL and a
quote"; protocol/GAPS.md "Evidence rules").

A builder returns Draft rows: cells, each a Cell, and the row's own source
(Src). finalize() holds every row to the evidence rule before anything is
stored:

- a row with no source, or whose source is an absence (nothing to quote), is
  dropped: no url and quote, no row.
- the row's quote, and the quote behind every cell that has one, is checked
  with engine.extract.base.verify against the artifact it names (the company's
  own bundle, or a page fetched for the slice), and its url must be that
  artifact's url. A miss drops the row.
- a cell reads "not found" only when its source is an absence on a page that
  was looked at: a not_found capture, or a seen page where nothing matched,
  both checked by verify. A cell whose source was never seen (blocked, dead,
  robots_denied, no row) is UNSEEN, and its whole row is dropped: unseen is
  never written as absent.
- a quote of 25 words or more is cut, verbatim, to the 20 words at its
  centre (the facts' quotes are windows centred on their match).
- at most ten rows are kept (the page shows up to ten); the rest are listed as
  dropped. Fewer than the builder's minimum fails the slice.

Pages a slice fetches (competitors, candidate accounts) go through Guard only
(fetch_page), are stored in slice_artifacts and extracted with the same
extract_text code as the bundle, so the same verify works on them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Protocol
from urllib.parse import urlsplit

from engine.capture import extract_text as X
from engine.extract.base import Art, FactOut, verify
from engine.guard.budget import Budget
from engine.guard.cache import Cache
from engine.guard.http import FetchResult, Guard
from engine.signal.seeds import FORBIDDEN_HOSTS

NOT_FOUND = "not found"
MAX_ROWS = 10
QUOTE_LIMIT = 25  # words; a quote must be under this (TEARDOWN_SPEC)
QUOTE_KEEP = 20  # words kept from the centre of a longer quote

ABSENCE_SOURCES = frozenset({"not_found", "absent"})


# -- sources, cells, rows ----------------------------------------------------------------


@dataclass(frozen=True)
class Src:
    """Where a value was read: an artifact and a quote checkable in it."""

    scope: str  # "company": the company's bundle; "slice": a slice_artifacts page
    subject: str  # the artifact's domain (the company's, a competitor's, a candidate's)
    kind: str  # artifact kind
    url: str
    quote: str
    quote_source: str  # base.QUOTE_SOURCES
    char_start: int = 0
    found: bool = True

    @property
    def absence(self) -> bool:
        return (not self.found) or self.quote_source in ABSENCE_SOURCES


def src_of(f: FactOut, art: Art, *, scope: str, subject: str) -> Src:
    return Src(scope, subject, f.artifact_kind, art.url, f.quote, f.quote_source,
               f.char_start, f.found)


@dataclass(frozen=True)
class Cell:
    text: str | None  # None: the source was not seen
    src: Src | None = None
    not_found: bool = False


UNSEEN = Cell(None)


def cell(text: str, src: Src | None = None) -> Cell:
    return Cell(text, src)


def not_found(src: Src) -> Cell:
    """'not found', resting on an absence that verify will check."""
    return Cell(NOT_FOUND, src, True)


@dataclass
class Draft:
    cells: list[Cell]
    src: Src | None


@dataclass
class Slice:
    kind: str
    version: str
    title: str
    columns: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None  # None when ok
    meta: dict[str, Any] = field(default_factory=dict)
    inputs_key: str | None = None
    fetches: int = 0
    searches: int = 0

    @property
    def ok(self) -> bool:
        return self.reason is None


def failed(kind: str, version: str, title: str, columns: list[str], reason: str, **kw) -> Slice:
    return Slice(kind, version, title, columns, reason=reason, **kw)


# -- checking ----------------------------------------------------------------------------

Resolver = Callable[[Src], Art | None]


def check_src(s: Src, resolve: Resolver) -> str | None:
    """None when the source checks out; else why not."""
    art = resolve(s)
    if art is None:
        return f"no {s.kind} artifact for {s.subject}"
    if art.url != s.url:
        return f"url {s.url} is not the {s.kind} artifact's ({art.url})"
    fo = FactOut("slice", None, s.kind, s.quote, s.quote_source, s.found, s.char_start)
    if not verify(fo, art):
        return f"quote {s.quote[:60]!r} not found in {s.kind} {s.quote_source}"
    return None


def words(text: str) -> int:
    return len(text.split())


def shorten(s: Src) -> Src:
    """A quote under QUOTE_LIMIT words: the QUOTE_KEEP words at its centre, verbatim."""
    toks = list(re.finditer(r"\S+", s.quote))
    if len(toks) < QUOTE_LIMIT:
        return s
    a = (len(toks) - QUOTE_KEEP) // 2
    start, end = toks[a].start(), toks[a + QUOTE_KEEP - 1].end()
    cs = s.char_start + start if s.quote_source == "text" else s.char_start
    return Src(s.scope, s.subject, s.kind, s.url, s.quote[start:end], s.quote_source, cs,
               s.found)


def _url_ok(url: str) -> bool:
    p = urlsplit(url)
    if p.scheme in ("http", "https"):
        return bool(p.netloc)
    return url.startswith("dns:") and len(url) > 4


def _why_row(d: Draft, columns: list[str], resolve: Resolver) -> str | None:
    if len(d.cells) != len(columns):
        return f"{len(d.cells)} cells for {len(columns)} columns"
    for col, c in zip(columns, d.cells, strict=True):
        if c.text is None:
            return f"{col}: source not seen (blocked, dead or not captured)"
        if not c.text.strip():
            return f"{col}: empty cell"
        if c.not_found and (c.src is None or not c.src.absence):
            return f"{col}: 'not found' without a looked-at source"
        if c.src is not None:
            why = check_src(c.src, resolve)
            if why:
                return f"{col}: {why}"
    s = d.src
    if s is None or not s.url or not s.quote.strip():
        return "no source url and quote"
    if s.absence:
        return "the row's source is an absence; there is nothing to quote"
    if not _url_ok(s.url):
        return f"source url {s.url!r} is not a page or a DNS record"
    return check_src(s, resolve)


def finalize(kind: str, version: str, title: str, columns: list[str], drafts: list[Draft],
             resolve: Resolver, *, min_rows: int, meta: dict[str, Any] | None = None,
             inputs_key: str | None = None, fetches: int = 0, searches: int = 0) -> Slice:
    rows: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for d in drafts:
        cells_text = [c.text if c.text is not None else "" for c in d.cells]
        why = _why_row(d, columns, resolve)
        if why:
            dropped.append({"cells": cells_text, "why": why})
            continue
        assert d.src is not None
        s = shorten(d.src)
        if len(rows) >= MAX_ROWS:
            dropped.append({"cells": cells_text, "why": f"over {MAX_ROWS} rows"})
            continue
        rows.append({
            "cells": cells_text,
            "url": s.url,
            "quote": s.quote,
            "source": asdict(s),
            "cell_sources": [asdict(c.src) if c.src is not None else None for c in d.cells],
        })
    sl = Slice(kind, version, title, columns, rows, dropped, None, meta or {}, inputs_key,
               fetches, searches)
    if len(rows) < min_rows:
        sl.reason = f"thin: {len(rows)} rows, needs {min_rows}"
    return sl


# -- searching ---------------------------------------------------------------------------


class Searcher(Protocol):
    async def query(self, q: str, *, num_results: int) -> list[str]: ...

    async def similar(self, url: str, *, num_results: int) -> list[str]: ...


class GuardSearcher:
    """search_api through Guard, charged to its own budget (one search, one fetch).

    Constructing it raises search_api.NotConfigured when no key is set."""

    def __init__(self, cache: Cache, budget: Budget):
        from engine.guard import search_api

        search_api.provider()  # NotConfigured, before anything else
        self._api = search_api
        self.cache = cache
        self.budget = budget

    async def query(self, q: str, *, num_results: int) -> list[str]:
        return await self._api.search_query(q, num_results=num_results, cache=self.cache,
                                            budget=self.budget)

    async def similar(self, url: str, *, num_results: int) -> list[str]:
        return await self._api.search_similar(url, num_results=num_results, cache=self.cache,
                                              budget=self.budget)


@dataclass
class Runtime:
    """What a network-backed builder is given. make_guard gets the slice's own
    fetch budget; make_searcher its own search budget and raises
    search_api.NotConfigured when no provider key is set. Tests pass fakes."""

    cache: Cache
    make_guard: Callable[[Budget], Guard]
    make_searcher: Callable[[Budget], Searcher]
    today: date


# -- pages fetched for a slice -----------------------------------------------------------


@dataclass
class SliceArtifact:
    company_id: str
    slice_kind: str
    subject_domain: str
    kind: str
    url: str
    outcome: str
    fetched_at: str
    status: int | None = None
    body_sha256: str | None = None
    fetcher: str | None = None
    detail: str | None = None
    text_sha256: str | None = None
    structure_sha256: str | None = None
    extractor_version: str | None = None


def _is_html(r: FetchResult) -> bool:
    ct = (r.content_type or "").lower()
    if ct:
        return "html" in ct or "json" in ct
    return (r.body or b"").lstrip()[:1] in (b"<", b"{", b"[")


def _now(guard: Guard) -> str:
    return guard.now().isoformat()


async def fetch_page(guard: Guard, cache: Cache, *, company_id: str, slice_kind: str,
                     subject: str, kind: str, url: str) -> tuple[SliceArtifact, bytes | None]:
    """One page through Guard, as capture fetches a bundle page, without the
    playwright retry (a slice's fetch ceiling is small). LinkedIn and Naukri are
    never read. Returns the row and the body (None unless ok)."""
    base = dict(company_id=company_id, slice_kind=slice_kind, subject_domain=subject, kind=kind)
    host = (urlsplit(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in FORBIDDEN_HOSTS):
        return SliceArtifact(**base, url=url, outcome="robots_denied", fetched_at=_now(guard),
                             fetcher="none", detail=f"not fetched: {host} is never read by code"), None
    try:
        r = await guard.fetch(url, kind)
    except ValueError as e:
        return SliceArtifact(**base, url=url, outcome="dead", fetched_at=_now(guard),
                             fetcher="none", detail=f"not fetchable: {e}"), None
    a = SliceArtifact(**base, url=r.final_url or r.url, outcome=r.outcome,
                      fetched_at=r.fetched_at.isoformat(), status=r.status,
                      body_sha256=r.sha256, fetcher="http", detail=r.detail or None)
    if not r.ok or r.body is None or r.sha256 is None or not _is_html(r):
        if r.ok and not _is_html(r):
            a.outcome, a.detail = "dead", f"not a page: {r.content_type}"
        return a, None
    d = X.derive(cache, kind, a.url, r.sha256, r.body)
    a.text_sha256, a.structure_sha256 = d.text_sha256, d.structure_sha256
    a.extractor_version = X.EXTRACTOR_VERSION
    return a, r.body


def looked_for(company_id: str, slice_kind: str, subject: str, kind: str,
               home: SliceArtifact, readable: bool, where: str) -> SliceArtifact:
    """A page kind with no link: not_found when the homepage was read, else the
    homepage's own outcome (nothing was seen)."""
    if readable:
        return SliceArtifact(company_id, slice_kind, subject, kind, home.url, "not_found",
                             home.fetched_at, fetcher="none", detail=f"no matching link in {where}")
    return SliceArtifact(company_id, slice_kind, subject, kind, home.url, home.outcome,
                         home.fetched_at, fetcher="none",
                         detail=f"not discovered: homepage {home.outcome}")


_SA_COLS = ("url", "outcome", "fetched_at", "status", "body_sha256", "fetcher", "detail",
            "text_sha256", "structure_sha256", "extractor_version")


def write_artifact(conn: sqlite3.Connection, a: SliceArtifact) -> bool:
    """Upsert one slice_artifacts row; True when it changed."""
    old = conn.execute("SELECT * FROM slice_artifacts WHERE company_id = ? AND slice_kind = ? "
                       "AND subject_domain = ? AND kind = ?",
                       (a.company_id, a.slice_kind, a.subject_domain, a.kind)).fetchone()
    new = asdict(a)
    with conn:
        if old is None:
            cols = ("company_id", "slice_kind", "subject_domain", "kind", *_SA_COLS)
            conn.execute(f"INSERT INTO slice_artifacts ({', '.join(cols)}) VALUES "
                         f"({', '.join('?' * len(cols))})", tuple(new[c] for c in cols))
            return True
        if all(old[c] == new[c] for c in _SA_COLS):
            return False
        conn.execute(f"UPDATE slice_artifacts SET {', '.join(f'{c} = ?' for c in _SA_COLS)} "
                     "WHERE slice_artifact_id = ?",
                     (*(new[c] for c in _SA_COLS), old["slice_artifact_id"]))
        return True


def stored_artifacts(conn: sqlite3.Connection, company_id: str, slice_kind: str,
                     subject: str) -> dict[str, SliceArtifact]:
    out: dict[str, SliceArtifact] = {}
    for r in conn.execute("SELECT * FROM slice_artifacts WHERE company_id = ? AND "
                          "slice_kind = ? AND subject_domain = ?",
                          (company_id, slice_kind, subject)):
        out[r["kind"]] = SliceArtifact(**{k: r[k] for k in (
            "company_id", "slice_kind", "subject_domain", "kind", *_SA_COLS)})
    return out


def to_art(cache: Cache, a: SliceArtifact) -> Art:
    """The page as an extractor sees it; text None unless ok and extracted."""
    text = structure = None
    if a.outcome == "ok" and a.text_sha256 and a.structure_sha256:
        tb = cache.get_derived("text", a.text_sha256)
        sb = cache.get_derived("structure", a.structure_sha256)
        if tb is not None and sb is not None:
            text, structure = tb.decode("utf-8"), json.loads(sb.decode("utf-8"))
    return Art(a.kind, a.url, a.outcome, text, structure)


def key_of(obj: Any) -> str:
    """A stable fingerprint of a builder's inputs."""
    import hashlib

    data = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]
