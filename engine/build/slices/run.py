"""`te slice build`: build each company's proof slice and hold it to the
evidence rule (docs/PLAN.md Steps 60-63).

For a company with an attempted diagnosis at the judge's current prompt
version, the gap names the primary slice (protocol/GAPS.md v1.2):

    G1, G3, G4 -> icp_accounts     G2, G5 -> funnel_compare     G6 -> dns_inventory

The primary is built and stored (ok or failed, with its reason). When it
fails, role_funnel is built as the fallback. The first ok slice is selected
for the teardown (`te teardown init` pre-fills it). When role_funnel is thin
too (under its three rows), the company is held for review with
review_reason "slice_thin: ...": protocol/ICP.md has no drop reason for it, so
a person decides (Step 63 says drop; see store.py).

funnel_compare waits for a person's yes to the two competitors
(`te slice competitors`); until then it is pending: nothing is captured,
nothing stored, no fallback.

Idempotent: a pure slice (role_funnel, dns_inventory) is rebuilt and written
only when it changed; a network slice (icp_accounts, funnel_compare) already
stored at the builder's version with the same inputs is not rebuilt (and
fetches nothing) unless --rebuild. --dry-run sends nothing and writes nothing.
No model calls: the run counts llm_calls before and after.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from engine.build.slices import common as C
from engine.build.slices import dns_inventory, funnel_compare, icp_accounts, role_funnel
from engine.build.slices import store as S
from engine.extract.base import Bundle
from engine.guard.budget import Budget
from engine.guard.cache import Cache
from engine.states import PIPELINE, Status

PRIMARY = {"G1": icp_accounts.KIND, "G3": icp_accounts.KIND, "G4": icp_accounts.KIND,
           "G2": funnel_compare.KIND, "G5": funnel_compare.KIND, "G6": dns_inventory.KIND}
VERSIONS = {icp_accounts.KIND: icp_accounts.VERSION, funnel_compare.KIND: funnel_compare.VERSION,
            role_funnel.KIND: role_funnel.VERSION, dns_inventory.KIND: dns_inventory.VERSION}
NETWORK = frozenset({icp_accounts.KIND, funnel_compare.KIND})
RETRY_REASONS = ("search not configured",)  # a stored failure that config can change

_FROM_DIAGNOSED = frozenset(PIPELINE[PIPELINE.index(Status.DIAGNOSED):])


class SliceError(RuntimeError):
    pass


def default_runtime(cache: Cache, today: date | None = None) -> C.Runtime:
    from engine.guard.http import Guard

    return C.Runtime(cache=cache,
                     make_guard=lambda budget: Guard(cache=cache, budget=budget),
                     make_searcher=lambda budget: C.GuardSearcher(cache, budget),
                     today=today or datetime.now(UTC).date())


def select_companies(conn: sqlite3.Connection, *, company_id: str | None = None,
                     pilot: bool = False, limit: int | None = None) -> list[str]:
    if company_id is not None:
        if conn.execute("SELECT 1 FROM companies WHERE company_id = ?",
                        (company_id,)).fetchone() is None:
            raise SliceError(f"no company {company_id!r}")
        return [company_id]
    sql = "SELECT company_id, status FROM companies WHERE status != 'dropped'"
    if pilot:
        sql += " AND pilot = 1"
    ids = [r["company_id"] for r in conn.execute(sql + " ORDER BY company_id")
           if Status(r["status"]) in _FROM_DIAGNOSED]
    return ids[:limit] if limit is not None else ids


@dataclass
class Target:
    company_id: str
    name: str
    gap: str
    primary: str
    bundle: Bundle


def load(conn: sqlite3.Connection, cache: Cache, company_id: str) -> Target | str:
    """The company ready for a slice, or why not."""
    from engine.extract.run import load_bundle
    from engine.judge import judge as J

    c = conn.execute("SELECT name, status FROM companies WHERE company_id = ?",
                     (company_id,)).fetchone()
    if c is None:
        return "no such company"
    if c["status"] == Status.DROPPED:
        return "dropped"
    if Status(c["status"]) not in _FROM_DIAGNOSED:
        return f"status {c['status']}; slices need a diagnosis"
    d = J.stored(conn, company_id, J.PROMPT_VERSION)
    if d is None:
        return f"no diagnosis at {J.PROMPT_VERSION}"
    if not d["attempted"] or not d["gap_type"]:
        return "the diagnosis abstained; no teardown, no slice"
    b = load_bundle(conn, cache, company_id)
    if isinstance(b, str):
        return f"bundle not ready: {b}"
    gap = d["gap_type"]
    if gap not in PRIMARY:
        return f"no slice for gap {gap}"
    return Target(company_id, c["name"], gap, PRIMARY[gap], b)


@dataclass
class Result:
    company_id: str
    outcome: str  # selected | held | kept_hold | pending | refused | dry_run
    gap: str | None = None
    primary: str | None = None
    selected: str | None = None
    lines: list[str] = field(default_factory=list)
    changed: int = 0


def _line(sl: C.Slice, state: str = "") -> str:
    s = f"{sl.kind} {sl.version}: " + (f"ok, {len(sl.rows)} rows" if sl.ok else
                                        f"failed: {sl.reason}")
    if sl.dropped:
        s += f"; {len(sl.dropped)} row(s) dropped"
    if sl.fetches or sl.searches:
        s += f"; {sl.fetches} fetches, {sl.searches} searches"
    return s + (f" ({state})" if state else "")


def _stored_current(conn: sqlite3.Connection, cid: str, kind: str, key: str | None) -> S.Stored | None:
    old = S.get(conn, cid, kind)
    if old is None or key is None or old.version != VERSIONS[kind] or old.inputs_key != key:
        return None
    if not old.ok and (old.reason or "").startswith(RETRY_REASONS):
        return None
    return old


def _as_slice(st: S.Stored) -> C.Slice:
    return C.Slice(st.kind, st.version, st.title, st.columns, st.rows, st.dropped, st.reason,
                   st.meta, st.inputs_key, st.fetches, st.searches)


def network_key(conn: sqlite3.Connection, t: Target) -> str | None:
    """The inputs key a network slice would be built from, computed without the network."""
    if t.primary == funnel_compare.KIND:
        two, why = funnel_compare.ready(S.competitors(conn, t.company_id))
        if why or not all(c.confirmed for c in two):
            return None
        return C.key_of([(c.name, c.domain) for c in two])
    d, why = icp_accounts.descriptor(conn, t.bundle, t.gap)
    if why:
        return None
    customers = icp_accounts.customer_domains(t.bundle)
    dom, slugs = icp_accounts.excluded(conn, t.bundle, customers)
    return C.key_of(icp_accounts.inputs(d, t.gap, customers, dom | slugs))


async def build_primary(conn: sqlite3.Connection, rt: C.Runtime, t: Target,
                        rebuild: bool) -> tuple[C.Slice | funnel_compare.Pending, bool]:
    """(slice or pending, reused from storage)."""
    if t.primary == dns_inventory.KIND:
        return dns_inventory.build(t.bundle), False
    if not rebuild:
        old = _stored_current(conn, t.company_id, t.primary, network_key(conn, t))
        if old is not None:
            return _as_slice(old), True
    if t.primary == funnel_compare.KIND:
        return await funnel_compare.build(conn, rt, t.bundle, t.name), False
    return await icp_accounts.build(conn, rt, t.bundle, t.name, t.gap), False


def _dry(conn: sqlite3.Connection, rt: C.Runtime, t: Target) -> list[str]:
    from engine.guard import search_api

    lines = [f"gap {t.gap}: primary {t.primary}"]
    if t.primary == dns_inventory.KIND:
        lines.append(_line(dns_inventory.build(t.bundle), "not written"))
    elif t.primary == funnel_compare.KIND:
        comps = S.competitors(conn, t.company_id)
        if not comps:
            n = len(funnel_compare.named(conn, t.bundle))
            lines.append(f"funnel_compare: would choose competitors ({n} named on the site"
                         + ("" if n else "; one alternatives search: configured "
                            f"{'yes' if search_api.is_configured() else 'no'}") + ")")
        else:
            two, why = funnel_compare.ready(comps)
            if why:
                lines.append(f"funnel_compare: would fail: {why}")
            elif not all(c.confirmed for c in two):
                lines.append("funnel_compare: pending your yes (te slice competitors)")
            else:
                lines.append(f"funnel_compare: would capture {len(two)} competitors; at most "
                             f"{funnel_compare.FETCH_CEILING} requests, 0 searches")
    else:
        lines.append(f"icp_accounts: search configured {'yes' if search_api.is_configured() else 'no'}; "
                     f"at most {icp_accounts.SEARCHES} searches and "
                     f"{icp_accounts.FETCH_CEILING} requests")
    rf = role_funnel.build(conn, t.bundle, t.name)
    lines.append(_line(rf, "fallback; not written"))
    return lines


async def build_company(conn: sqlite3.Connection, rt: C.Runtime, company_id: str, *,
                        rebuild: bool = False, dry_run: bool = False) -> Result:
    t = load(conn, rt.cache, company_id)
    if isinstance(t, str):
        return Result(company_id, "refused", lines=[t])
    res = Result(company_id, "dry_run" if dry_run else "", t.gap, t.primary)
    if dry_run:
        res.lines = _dry(conn, rt, t)
        return res

    primary, reused = await build_primary(conn, rt, t, rebuild)
    if isinstance(primary, funnel_compare.Pending):
        res.outcome = "pending"
        res.lines.append(f"funnel_compare: {primary.reason}")
        cur = S.selected(conn, company_id)
        res.selected = cur.kind if cur else None
        return res
    if not reused:
        res.changed += S.write_slice(conn, company_id, primary)
    res.lines.append(_line(primary, "stored, unchanged inputs" if reused else ""))
    if primary.ok:
        res.changed += S.select(conn, company_id, primary.kind)
        S.clear_hold(conn, company_id)
        res.outcome, res.selected = "selected", primary.kind
        return res

    rf = role_funnel.build(conn, t.bundle, t.name)
    res.changed += S.write_slice(conn, company_id, rf)
    res.lines.append(_line(rf, "fallback"))
    if rf.ok:
        res.changed += S.select(conn, company_id, rf.kind)
        S.clear_hold(conn, company_id)
        res.outcome, res.selected = "selected", rf.kind
        return res
    res.changed += S.select(conn, company_id, None)
    detail = f"{primary.kind} {primary.reason}; role_funnel {rf.reason}"
    state = S.hold(conn, company_id, detail)
    res.outcome = "kept_hold" if state == "kept" else "held"
    res.lines.append(f"held for review: slice_thin: {detail}" if state != "kept" else
                     "both slices failed; another review_reason stands, not overwritten")
    return res


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    model_calls: int = 0


def _llm_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0])


async def run(conn: sqlite3.Connection, rt: C.Runtime, company_ids: list[str], *,
              rebuild: bool = False, dry_run: bool = False) -> Report:
    rep = Report()
    before = _llm_count(conn)
    for cid in company_ids:
        rep.results.append(await build_company(conn, rt, cid, rebuild=rebuild, dry_run=dry_run))
    rep.model_calls = _llm_count(conn) - before
    return rep


# -- competitors (Step 61) ---------------------------------------------------------------


@dataclass
class Choice:
    company_id: str
    competitors: list[S.Competitor]
    note: str
    ready: bool  # two with a domain
    why: str | None = None


def _parse_set(spec: str) -> tuple[str, str]:
    from engine.resolve.domain import registrable_domain

    name, sep, dom = spec.partition("=")
    name, dom = name.strip(), dom.strip().lower()
    d = registrable_domain(dom if "//" in dom else f"https://{dom}/")
    if not sep or not name or d is None:
        raise SliceError(f"--set takes NAME=DOMAIN, e.g. Ledgerly=ledgerly.example: {spec!r}")
    return name, d


async def competitors(conn: sqlite3.Connection, rt: C.Runtime, company_id: str, *,
                      sets: tuple[str, ...] = (), dry_run: bool = False) -> Choice:
    """The two competitors for funnel_compare: stored, or chosen now (named
    first, else one search), with --set NAME=DOMAIN applied. Any change clears
    the confirmation of what changed."""
    t = load(conn, rt.cache, company_id)
    if isinstance(t, str):
        raise SliceError(f"{company_id}: {t}")
    comps = S.competitors(conn, company_id)
    note = "stored"
    if not comps:
        if dry_run:
            named = funnel_compare.named(conn, t.bundle)[:2]
            comps, note = named, (f"{len(named)} named on the site" if named
                                  else "none named; would run one alternatives search")
        else:
            comps, note = await funnel_compare.choose(conn, rt, t.bundle, t.name)
    if sets:
        comps = [S.Competitor(c.name, c.domain, c.source, c.evidence_url, c.evidence_quote,
                              c.rank, c.confirmed, c.confirmed_at) for c in comps]
        for spec in sets:
            name, dom = _parse_set(spec)
            hit = next((c for c in comps if c.name.casefold() == name.casefold()), None)
            if hit is None:
                comps.append(S.Competitor(name, dom, "manual", None, None, len(comps) + 1))
            elif hit.domain != dom:
                hit.domain, hit.confirmed, hit.confirmed_at = dom, False, None
        if not dry_run:
            S.write_competitors(conn, company_id, comps)
        note += f"; {len(sets)} set by hand"
    two, why = funnel_compare.ready(comps)
    return Choice(company_id, comps, note, why is None, why)


def confirm(conn: sqlite3.Connection, company_id: str) -> int:
    two, why = funnel_compare.ready(S.competitors(conn, company_id))
    if why:
        raise SliceError(f"{company_id}: cannot confirm: {why}")
    extra = [c for c in S.competitors(conn, company_id) if c not in two]
    if extra:  # only the two shown are kept, so a yes covers exactly what was shown
        S.write_competitors(conn, company_id, two)
    return S.confirm(conn, company_id)
