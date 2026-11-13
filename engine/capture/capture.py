"""E3 Capture: fetch and freeze each company's evidence bundle (docs/PLAN.md Step 34).

For a company: fetch the homepage, read its nav, footer and sitemap.xml
(discover.py), then fetch each bundle kind once through Guard and write one
artifacts row per kind in bundle.KINDS, whatever the outcome. No model calls.

- Network only through engine/guard/: guard.http for pages, guard.browser for
  the playwright fallback, guard.dns for the fixed DNS list. Raw bytes go to the
  cache; rows point at them by body_sha256. Nothing parsed is stored as source.
- Playwright fallback: an ok HTML page with under TEXT_FLOOR characters of
  visible text is retried once in headless chromium, at most
  MAX_PLAYWRIGHT_PAGES per company. If the retry is not ok, the plain fetch
  stands and the row's detail says why.
- not_found is a fact, so it is only written when the place it was looked for
  was seen. A discovered kind with no matching link is not_found only if the
  homepage was readable; otherwise it takes the homepage's own outcome
  (blocked, dead, robots_denied), because nothing was seen. The same holds for
  ats_board and the careers page.
- job_post is the best signal's URL, unless it is on LinkedIn or Naukri, which
  code never reads: that row is robots_denied with nothing fetched. ats_board is the company's board JSON
  (Greenhouse, Lever, Ashby, Workable) named by its signals or linked from its
  careers page or homepage; with no board it is not_found.
- Status: a qualified company with a complete bundle moves to captured, or is
  dropped no_site_evidence (stage capture) with fewer than MIN_USABLE ok
  artifacts. Companies already past qualified (the hand set at sent) only get
  rows; their status is never touched.
- Idempotent: a company whose bundle is complete at BUNDLE_VERSION is not
  selected again; a same-day re-capture is answered from the cache with zero
  requests, and rows are rewritten only when a value changed.
- --recheck re-fetches, fresh, only the kinds a company's latest diagnosis
  cites, and rewrites only those rows. A cited kind that was not_found by
  discovery needs the homepage (and sitemap) re-read to recheck the absence;
  those two are fetched fresh too, and nothing else is.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from engine import states
from engine.capture import bundle as B
from engine.capture.discover import Link, discover, sitemap_urls
from engine.guard.browser import PlaywrightRenderer, Renderer, fetch_rendered
from engine.guard.budget import Budget, BudgetExceeded, Projection, project
from engine.guard.cache import Cache
from engine.guard.dns import DnsAnswer, DnsGuard
from engine.guard.http import FetchResult, Guard, GuardConfigError
from engine.guard.ratelimit import MAX_IN_FLIGHT
from engine.models import Artifact, Outcome
from engine.resolve.domain import PLACEHOLDER_PREFIX, registrable_domain
from engine.resolve.segment import page_text
from engine.signal.boards import COLLECTORS
from engine.signal.careers import find_boards
from engine.signal.seeds import FORBIDDEN_HOSTS
from engine.states import PIPELINE, Status

_FROM_QUALIFIED = frozenset(s.value for s in PIPELINE[PIPELINE.index(Status.QUALIFIED):])


class CaptureError(ValueError):
    """A company cannot be captured as asked (unknown, dropped, no domain...)."""


# -- runtime ---------------------------------------------------------------------------


@dataclass
class Runtime:
    guard: Guard
    dns: DnsGuard
    renderer: Renderer

    async def aclose(self) -> None:
        try:
            await self.renderer.aclose()
        finally:
            await self.guard.aclose()


def make_runtime(cache: Cache, budget: Budget, max_in_flight: int = MAX_IN_FLIGHT) -> Runtime:
    """The real network stack. Tests replace this with one on a mock transport."""
    return Runtime(
        guard=Guard(cache=cache, budget=budget, max_in_flight=max_in_flight),
        dns=DnsGuard(cache=cache, budget=budget),
        renderer=PlaywrightRenderer(),
    )


# -- selecting companies ---------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    company_id: str
    domain: str
    status: str
    signal_id: str | None = None
    signal_url: str | None = None
    boards: tuple[tuple[str, str], ...] = ()  # (source, slug), best first

    @property
    def homepage(self) -> str:
        return B.homepage_url(self.domain)


def _boards(rows: Iterable[sqlite3.Row]) -> tuple[tuple[str, str], ...]:
    found: dict[tuple[str, str], tuple[str, str]] = {}
    for r in rows:
        if r["source"] in COLLECTORS and r["board_slug"]:
            found.setdefault((r["source"], r["board_slug"].lower()), (r["source"], r["board_slug"]))
        for b in find_boards(r["url"] or ""):
            found.setdefault((b[0], b[1].lower()), b)
    return tuple(found.values())


def load_target(conn: sqlite3.Connection, company_id: str) -> Target:
    row = conn.execute("SELECT company_id, domain, status, best_signal_id FROM companies "
                       "WHERE company_id = ?", (company_id,)).fetchone()
    if row is None:
        raise CaptureError(f"no company {company_id}")
    if row["status"] == Status.DROPPED:
        raise CaptureError(f"{company_id} is dropped")
    if row["status"] not in _FROM_QUALIFIED:
        raise CaptureError(f"{company_id} is {row['status']}; capture runs on qualified "
                           "companies and later")
    domain = (row["domain"] or "").strip().lower()
    if not domain or domain.startswith(PLACEHOLDER_PREFIX) or registrable_domain(domain) is None:
        raise CaptureError(f"{company_id} has no usable domain ({row['domain']!r})")
    sigs = conn.execute(
        "SELECT signal_id, source, board_slug, url FROM signals WHERE company_id = ? "
        "ORDER BY (signal_id = ?) DESC, first_seen, signal_id",
        (company_id, row["best_signal_id"] or ""),
    ).fetchall()
    best = next((s for s in sigs if s["signal_id"] == row["best_signal_id"]), None)
    return Target(
        company_id=company_id,
        domain=domain,
        status=row["status"],
        signal_id=row["best_signal_id"],
        signal_url=(best["url"] or None) if best is not None else None,
        boards=_boards(sigs),
    )


def bundle_complete(conn: sqlite3.Connection, company_id: str) -> bool:
    n = conn.execute(
        f"SELECT COUNT(DISTINCT kind) FROM artifacts WHERE company_id = ? AND bundle_version = ? "
        f"AND kind IN ({','.join('?' * len(B.KINDS))})",
        (company_id, B.BUNDLE_VERSION, *B.KINDS),
    ).fetchone()[0]
    return int(n) == len(B.KINDS)


@dataclass
class Selection:
    targets: list[Target] = field(default_factory=list)
    complete: list[str] = field(default_factory=list)  # bundle already captured, skipped
    refused: list[str] = field(default_factory=list)  # "<id>: why"


def select(conn: sqlite3.Connection, *, pilot: bool = False, company_id: str | None = None,
           limit: int | None = None) -> Selection:
    """Companies whose bundle is missing or older than BUNDLE_VERSION.

    Default: every company at qualified or later, not dropped. --pilot: the
    pilot and hand sets. --company: that one (refused if dropped, before
    qualified, or without a domain).
    """
    sel = Selection()
    if company_id is not None:
        ids = [company_id]
    else:
        sql = "SELECT company_id, status FROM companies WHERE status != 'dropped'"
        if pilot:
            sql += " AND (pilot = 1 OR hand = 1)"
        ids = [r["company_id"] for r in conn.execute(sql + " ORDER BY company_id")
               if r["status"] in _FROM_QUALIFIED]
    for cid in ids:
        if bundle_complete(conn, cid):
            sel.complete.append(cid)
            continue
        try:
            sel.targets.append(load_target(conn, cid))
        except CaptureError as e:
            sel.refused.append(str(e))
        if limit is not None and len(sel.targets) >= limit:
            break
    return sel


# -- one company -----------------------------------------------------------------------


def _is_html(r: FetchResult) -> bool:
    ct = (r.content_type or "").lower()
    if ct:
        return "html" in ct
    return (r.body or b"").lstrip()[:1] == b"<"


def visible_chars(body: bytes | None) -> int:
    return len(page_text((body or b"").decode("utf-8", errors="replace")))


@dataclass
class Page:
    row: Artifact
    body: bytes | None  # what to read: the rendered document when the retry was ok
    readable: bool  # ok, and not a page that needed a browser it did not get
    unseen: Outcome  # the outcome to give what could not be seen through this page


@dataclass
class CompanyResult:
    target: Target
    rows: dict[str, Artifact]
    playwright_pages: int = 0
    notes: list[str] = field(default_factory=list)


def _row(t: Target, kind: str, *, url: str, outcome: Outcome, fetched_at: datetime,
         fetcher: str, detail: str = "", status: int | None = None, sha: str | None = None,
         cache_path: str | None = None) -> Artifact:
    return Artifact(company_id=t.company_id, kind=kind, url=url, outcome=outcome,
                    fetched_at=fetched_at, status=status, body_sha256=sha,
                    cache_path=cache_path, fetcher=fetcher,  # type: ignore[arg-type]
                    detail=detail or None, bundle_version=B.BUNDLE_VERSION)


def _from_fetch(t: Target, kind: str, r: FetchResult, fetcher: str, detail: str) -> Artifact:
    return _row(t, kind, url=r.final_url or r.url, outcome=r.outcome, fetched_at=r.fetched_at,
                fetcher=fetcher, detail=detail, status=r.status, sha=r.sha256,
                cache_path=r.cache_path)


class _Capture:
    """One company's capture under one runtime."""

    def __init__(self, t: Target, rt: Runtime, *, fresh: bool):
        self.t = t
        self.rt = rt
        self.fresh = fresh
        self.playwright_left = B.MAX_PLAYWRIGHT_PAGES
        self.playwright_used = 0
        self.notes: list[str] = []

    async def page(self, kind: str, url: str) -> Page:
        host = (urlsplit(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in FORBIDDEN_HOSTS):
            # A person may read these by hand; code never does (CLAUDE.md, CONDUCT.md).
            row = _row(self.t, kind, url=url, outcome="robots_denied",
                       fetched_at=self.rt.guard.now(), fetcher="none",
                       detail=f"not fetched: {host} is never read by code")
            return Page(row, None, False, "robots_denied")
        try:
            r = await self.rt.guard.fetch(url, kind, fresh=self.fresh)
        except ValueError as e:  # not an http(s) URL
            row = _row(self.t, kind, url=url, outcome="dead", fetched_at=self.rt.guard.now(),
                       fetcher="none", detail=f"not fetchable: {e}")
            return Page(row, None, False, "dead")
        detail = r.detail
        if not r.ok or not _is_html(r):
            row = _from_fetch(self.t, kind, r, "http", detail)
            return Page(row, r.body, r.ok, r.outcome if not r.ok else "ok")
        chars = visible_chars(r.body)
        if chars >= B.TEXT_FLOOR:
            return Page(_from_fetch(self.t, kind, r, "http", detail), r.body, True, "ok")
        thin = f"{chars} chars of visible text"
        if self.playwright_left <= 0:
            row = _from_fetch(self.t, kind, r, "http",
                              f"{thin}; playwright cap of {B.MAX_PLAYWRIGHT_PAGES} reached")
            return Page(row, r.body, False, "dead")
        self.playwright_left -= 1
        self.playwright_used += 1
        rr = await fetch_rendered(self.rt.guard, r.url, kind, renderer=self.rt.renderer,
                                  fresh=self.fresh)
        if rr.ok:
            row = _from_fetch(self.t, kind, rr, "playwright", f"playwright retry: http had {thin}")
            return Page(row, rr.body, True, "ok")
        row = _from_fetch(self.t, kind, r, "http",
                          f"{thin}; playwright retry {rr.outcome}: {rr.detail}")
        return Page(row, r.body, False, rr.outcome)

    async def fetch_json(self, kind: str, url: str) -> FetchResult | Artifact:
        try:
            return await self.rt.guard.fetch(url, kind, fresh=self.fresh)
        except ValueError as e:
            return _row(self.t, kind, url=url, outcome="dead", fetched_at=self.rt.guard.now(),
                        fetcher="none", detail=f"not fetchable: {e}")


async def capture_dns(t: Target, d: DnsGuard, *, fresh: bool = False) -> tuple[Artifact, bytes]:
    """The dns artifact: every lookup on the fixed list, as one JSON document."""
    entries: list[dict[str, object]] = []
    gone: set[str] = set()  # names answered NXDOMAIN on their first lookup
    last: datetime | None = None
    for name, rdtype, purpose in B.dns_lookups(t.domain):
        if name in gone:
            entries.append({"name": name, "type": rdtype, "purpose": purpose,
                            "outcome": "not_found", "records": [],
                            "detail": "not sent: name is NXDOMAIN"})
            continue
        a: DnsAnswer = await d.lookup(name, rdtype, fresh=fresh)
        if purpose == "sending_subdomain" and a.nxdomain:
            gone.add(name)
        entries.append({**a.as_json(), "purpose": purpose})
        last = a.fetched_at if last is None or a.fetched_at > last else last
    doc = {"bundle_version": B.BUNDLE_VERSION, "domain": t.domain, "lookups": entries}
    body = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sha = d.cache.put_body(body)
    outcomes = Counter(str(e["outcome"]) for e in entries)
    if outcomes["ok"]:
        outcome: Outcome = "ok"
    elif outcomes["dead"]:
        outcome = "dead"
    else:
        outcome = "not_found"
    detail = (f"{len(entries)} lookups: ok {outcomes['ok']}, not_found {outcomes['not_found']}, "
              f"dead {outcomes['dead']}")
    row = _row(t, "dns", url=f"dns:{t.domain}", outcome=outcome,
               fetched_at=last or d._now(), fetcher="dns", detail=detail, sha=sha,
               cache_path=str(d.cache.body_path(sha)))
    return row, body


def _sitemap_url(page_url: str) -> str:
    p = urlsplit(page_url)
    return f"{p.scheme}://{p.netloc}/sitemap.xml"


async def capture_company(t: Target, rt: Runtime, *, only: Iterable[str] | None = None,
                          fresh: bool = False,
                          prior: dict[str, Artifact] | None = None) -> CompanyResult:
    """Capture one company's bundle; with `only`, just those kinds (for --recheck).

    Returns the rows; writing them is write_rows()'s job.
    """
    kinds = list(B.KINDS) if only is None else [k for k in B.KINDS if k in set(only)]
    prior = prior or {}
    c = _Capture(t, rt, fresh=fresh)
    rows: dict[str, Artifact] = {}

    dns_task = asyncio.create_task(capture_dns(t, rt.dns, fresh=fresh)) if "dns" in kinds \
        else None
    try:
        # A kind is rediscovered unless a recheck can reuse the URL it was fetched from.
        rediscover = [k for k in kinds if k in B.DISCOVERED_KINDS
                      and (only is None or k not in prior or prior[k].fetcher == "none")]
        home: Page | None = None
        found: dict[str, Link] = {}
        sitemap_note = ""
        if "homepage" in kinds or rediscover:
            home = await c.page("homepage", t.homepage)
            if "homepage" in kinds:
                rows["homepage"] = home.row
        if rediscover and home is not None:
            smap = await c.fetch_json("sitemap", _sitemap_url(home.row.url))
            smap_body = smap.body if isinstance(smap, FetchResult) and smap.ok else None
            if smap_body is None:
                sm_outcome = smap.outcome
                sitemap_note = f"; sitemap.xml {sm_outcome}"
            elif not sitemap_urls(smap_body):
                sitemap_note = "; sitemap.xml lists no pages (or is an index, not followed)"
            base = home.row.url if home.row.outcome == "ok" else t.homepage
            found = discover(home.body.decode("utf-8", errors="replace")
                             if home.body and home.row.outcome == "ok" else None,
                             base, t.domain, smap_body)

        pages: dict[str, Page] = {"homepage": home} if home is not None else {}
        for kind in B.DISCOVERED_KINDS:
            if kind not in kinds:
                continue
            if kind not in rediscover:
                pages[kind] = await c.page(kind, prior[kind].url)
                rows[kind] = pages[kind].row
                continue
            link = found.get(kind)
            if link is not None:
                p = await c.page(kind, link.url)
                via = f"found as {link.describe()}"
                p.row.detail = f"{via}; {p.row.detail}" if p.row.detail else via
                pages[kind] = p
                rows[kind] = p.row
                continue
            assert home is not None
            where = "primary CTA" if kind == "demo_form" else "nav, footer or sitemap.xml"
            if home.readable:
                rows[kind] = _row(t, kind, url=home.row.url, outcome="not_found",
                                  fetched_at=home.row.fetched_at, fetcher="none",
                                  detail=f"no matching link in {where}{sitemap_note}")
            else:
                rows[kind] = _row(t, kind, url=home.row.url, outcome=home.unseen,
                                  fetched_at=home.row.fetched_at, fetcher="none",
                                  detail=f"not discovered: homepage {home.unseen}, so "
                                         f"{where} unseen{sitemap_note}")

        # Rows with nothing fetched take the homepage's time, so a same-day re-run
        # writes identical rows.
        anchor = home.row.fetched_at if home is not None else rt.guard.now()
        if "job_post" in kinds:
            if t.signal_url:
                rows["job_post"] = (await c.page("job_post", t.signal_url)).row
            else:
                # Nothing was looked at, so this is unseen, not an absence.
                rows["job_post"] = _row(t, "job_post", url=f"signal:{t.signal_id or 'none'}",
                                        outcome="dead", fetched_at=anchor,
                                        fetcher="none", detail="no signal URL to fetch")

        if "ats_board" in kinds:
            careers_row = rows.get("careers") or prior.get("careers")
            rows["ats_board"] = await _ats_board(c, t, pages, prior, careers_row, anchor)
    except BaseException:
        if dns_task is not None:
            dns_task.cancel()
        raise
    if dns_task is not None:
        rows["dns"], _ = await dns_task
    return CompanyResult(t, rows, c.playwright_used, c.notes)


async def _ats_board(c: _Capture, t: Target, pages: dict[str, Page],
                     prior: dict[str, Artifact], careers_row: Artifact | None,
                     anchor: datetime) -> Artifact:
    old = prior.get("ats_board")
    if old is not None and old.fetcher != "none" and c.fresh:
        url = old.url
    else:
        boards = list(t.boards)
        texts: list[str] = []
        for kind in ("careers", "homepage"):
            p = pages.get(kind)
            if p is not None and p.body:
                texts.append(p.body.decode("utf-8", errors="replace"))
            elif kind in prior and prior[kind].body_sha256:
                # a recheck reads what it captured before; nothing is fetched for this
                body = c.rt.guard.cache.get_body(prior[kind].body_sha256 or "")
                if body:
                    texts.append(body.decode("utf-8", errors="replace"))
        for b in find_boards(*texts):
            if (b[0], b[1].lower()) not in {(s, x.lower()) for s, x in boards}:
                boards.append(b)
        if not boards:
            looked = careers_row
            if looked is not None and looked.outcome not in ("ok", "not_found"):
                return _row(t, "ats_board", url=looked.url, outcome=looked.outcome,
                            fetched_at=looked.fetched_at, fetcher="none",
                            detail=f"no board named by the signals; careers page "
                                   f"{looked.outcome}, so a linked board is unseen")
            where = looked.url if looked is not None else t.homepage
            when = looked.fetched_at if looked is not None else anchor
            return _row(t, "ats_board", url=where, outcome="not_found", fetched_at=when,
                        fetcher="none",
                        detail="no ATS board named by the signals or linked from the careers "
                               "page or homepage")
        source, slug = boards[0]
        url = COLLECTORS[source].board_url(slug)
    r = await c.fetch_json("ats_board", url)
    if isinstance(r, Artifact):
        return r
    return _from_fetch(t, "ats_board", r, "http", r.detail)


# -- writing ---------------------------------------------------------------------------

_COLUMNS = ("url", "outcome", "fetched_at", "cache_path", "status", "body_sha256", "fetcher",
            "detail", "bundle_version")
# Step 35 output, derived from body_sha256 (extract_text.py).
_DERIVED = ("text_sha256", "structure_sha256", "extractor_version")


def write_rows(conn: sqlite3.Connection, rows: Iterable[Artifact]) -> int:
    """Upsert one row per (company_id, kind); returns how many changed.

    An unchanged row is not touched. A changed body clears text_sha256,
    structure_sha256 and extractor_version, which Step 35 derives from the body.
    """
    changed = 0
    with conn:
        for a in rows:
            new = a.model_dump(mode="json")
            old = conn.execute("SELECT * FROM artifacts WHERE company_id = ? AND kind = ?",
                               (a.company_id, a.kind)).fetchone()
            if old is None:
                cols = ("company_id", "kind", *_COLUMNS)
                conn.execute(f"INSERT INTO artifacts ({', '.join(cols)}) VALUES "
                             f"({', '.join('?' * len(cols))})", tuple(new[k] for k in cols))
                changed += 1
                continue
            if all(old[k] == new[k] for k in _COLUMNS):
                continue
            sets = ", ".join(f"{k} = ?" for k in _COLUMNS)
            same = old["body_sha256"] == new["body_sha256"]
            derived = tuple(old[k] if same else None for k in _DERIVED)
            conn.execute(f"UPDATE artifacts SET {sets}, "
                         f"{', '.join(f'{k} = ?' for k in _DERIVED)} WHERE artifact_id = ?",
                         (*(new[k] for k in _COLUMNS), *derived, old["artifact_id"]))
            changed += 1
    return changed


def prior_rows(conn: sqlite3.Connection, company_id: str) -> dict[str, Artifact]:
    out: dict[str, Artifact] = {}
    for r in conn.execute("SELECT * FROM artifacts WHERE company_id = ?", (company_id,)):
        out[r["kind"]] = Artifact(**{k: r[k] for k in r.keys()})
    return out


def settle(conn: sqlite3.Connection, company_id: str, rows: dict[str, Artifact]) -> str:
    """captured | dropped | kept (status left alone: not qualified, or incomplete)."""
    if set(rows) != set(B.KINDS):
        return "kept"
    status = conn.execute("SELECT status FROM companies WHERE company_id = ?",
                          (company_id,)).fetchone()["status"]
    if status != Status.QUALIFIED:
        return "kept"
    usable = sum(1 for a in rows.values() if a.outcome == "ok")
    if usable < B.MIN_USABLE:
        states.drop(company_id, B.DROP_REASON, B.DROP_STAGE, conn)
        return "dropped"
    states.advance(company_id, Status.CAPTURED, conn)
    return "captured"


# -- a run -----------------------------------------------------------------------------


@dataclass
class Report:
    companies: int = 0
    results: list[tuple[str, str, Counter[str]]] = field(default_factory=list)  # id, state, outcomes
    outcomes: Counter[str] = field(default_factory=Counter)
    fetchers: Counter[str] = field(default_factory=Counter)
    states: Counter[str] = field(default_factory=Counter)
    changed: int = 0
    playwright_pages: int = 0
    requests: int = 0
    dns_queries: int = 0
    errors: list[str] = field(default_factory=list)
    stopped: str | None = None  # budget or config stop

    @property
    def rows(self) -> int:
        return sum(self.outcomes.values())

    @property
    def blocked_rate(self) -> float:
        return self.outcomes["blocked"] / self.rows if self.rows else 0.0


async def run(conn: sqlite3.Connection, targets: list[Target], rt: Runtime, *,
              max_in_flight: int = MAX_IN_FLIGHT) -> Report:
    """Capture every target; up to max_in_flight companies (so hosts) at once.

    Per-host pacing is Guard's and is the same at any max_in_flight.
    """
    rep = Report()
    gate = asyncio.Semaphore(max(1, min(max_in_flight, MAX_IN_FLIGHT)))
    stop = asyncio.Event()
    start_req, start_dns = rt.guard.requests_made, rt.dns.queries_made

    async def one(t: Target) -> None:
        async with gate:
            if stop.is_set():
                return
            try:
                res = await capture_company(t, rt)
            except (BudgetExceeded, GuardConfigError) as e:
                if rep.stopped is None:
                    rep.stopped = str(e)
                stop.set()
                return
            except Exception as e:  # one company's failure is reported, not fatal
                rep.errors.append(f"{t.company_id}: {type(e).__name__}: {e}")
                return
            rep.changed += write_rows(conn, res.rows.values())
            state = settle(conn, t.company_id, res.rows)
            counts = Counter(a.outcome for a in res.rows.values())
            rep.companies += 1
            rep.results.append((t.company_id, state, counts))
            rep.outcomes.update(counts)
            rep.fetchers.update(a.fetcher or "none" for a in res.rows.values())
            rep.states[state] += 1
            rep.playwright_pages += res.playwright_pages

    await asyncio.gather(*(one(t) for t in targets))
    rep.results.sort()
    rep.requests = rt.guard.requests_made - start_req
    rep.dns_queries = rt.dns.queries_made - start_dns
    return rep


# -- recheck ---------------------------------------------------------------------------


def cited_kinds(conn: sqlite3.Connection, company_id: str) -> list[str] | None:
    """Bundle kinds the latest diagnosis cites; None when there is no diagnosis."""
    row = conn.execute("SELECT claims FROM diagnoses WHERE company_id = ? "
                       "ORDER BY diagnosis_id DESC LIMIT 1", (company_id,)).fetchone()
    if row is None:
        return None
    try:
        claims = json.loads(row["claims"] or "[]")
    except ValueError:
        claims = []
    kinds = {c.get("artifact_kind") for c in claims if isinstance(c, dict)}
    return [k for k in B.KINDS if k in kinds]


@dataclass
class Recheck:
    company_id: str
    kinds: list[str]
    rows: dict[str, Artifact]
    changed: int
    requests: int
    dns_queries: int


async def recheck(conn: sqlite3.Connection, company_id: str, kinds: list[str],
                  rt: Runtime) -> Recheck:
    """Re-fetch, fresh, only `kinds`; rewrite only their rows. Status is not touched."""
    t = load_target(conn, company_id)
    r0, d0 = rt.guard.requests_made, rt.dns.queries_made
    res = await capture_company(t, rt, only=kinds, fresh=True,
                                prior=prior_rows(conn, company_id))
    changed = write_rows(conn, res.rows.values())
    return Recheck(company_id, kinds, res.rows, changed, rt.guard.requests_made - r0,
                   rt.dns.queries_made - d0)


# -- dry run ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DryRun:
    companies: int
    pages: Projection  # known URLs: homepage, sitemap, discovered from today's cache, job, board
    undiscovered: int  # companies whose homepage is not cached today
    dns_queries: int  # at most; NXDOMAIN subdomains send fewer

    @property
    def max_requests(self) -> int:
        per = len(B.DISCOVERED_KINDS) + B.MAX_PLAYWRIGHT_PAGES
        return self.pages.fetches + self.undiscovered * per


def dry_run(targets: list[Target], cache: Cache, dns: DnsGuard, *,
            day: date | None = None) -> DryRun:
    """Project the requests a run would send. Sends nothing."""
    day = day or datetime.now(UTC).date()
    urls: list[str] = []
    undiscovered = 0
    dns_queries = 0
    for t in targets:
        urls.append(t.homepage)
        home = cache.lookup(t.homepage, day)
        smap_url = _sitemap_url(home.final_url if home else t.homepage)
        urls.append(smap_url)
        smap = cache.lookup(smap_url, day)
        if home is not None and smap is not None:
            hbody = cache.get_body(home.sha256) if home.sha256 and home.outcome == "ok" else None
            sbody = cache.get_body(smap.sha256) if smap.sha256 and smap.outcome == "ok" else None
            found = discover(hbody.decode("utf-8", errors="replace") if hbody else None,
                             home.final_url, t.domain, sbody)
            urls += [link.url for link in found.values()]
        else:
            undiscovered += 1
        if t.signal_url:
            urls.append(t.signal_url)
        if t.boards:
            urls.append(COLLECTORS[t.boards[0][0]].board_url(t.boards[0][1]))
        dns_queries += sum(1 for n, ty, _ in B.dns_lookups(t.domain) if not dns.cached(n, ty))
    return DryRun(len(targets), project(urls, cache, day=day), undiscovered, dns_queries)


def format_dry_run(d: DryRun, budget: Budget) -> list[str]:
    p = d.pages
    lines = [
        f"dry run: {d.companies} companies; {p.urls} known urls, {p.cached} cached today; "
        f"{p.fetches} requests projected ({p.page_fetches} pages, {p.robots_fetches} robots.txt)",
        f"  plus up to {len(B.DISCOVERED_KINDS)} discovered pages for each of {d.undiscovered} "
        f"companies whose homepage is not cached today, and up to "
        f"{B.MAX_PLAYWRIGHT_PAGES} playwright pages per company (each loads its scripts)",
        f"  at most {d.max_requests} page requests; ceiling {budget.max_fetches} fetches",
        f"  at most {d.dns_queries} DNS queries; ceiling {budget.max_dns_queries}",
    ]
    if d.max_requests > budget.max_fetches or d.dns_queries > budget.max_dns_queries:
        lines.append("  OVER BUDGET: raise --max-fetches or --max-dns, or use --limit")
    return lines
