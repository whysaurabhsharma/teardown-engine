"""Review companies by hand (docs/PLAN.md Step 30, `te resolve review`).

One company at a time: name, domain, segment, head office with its quotes,
review_reason, possible-duplicate flags (Step 27), signals and the homepage
title. The homepage title is read from Guard's cache only; nothing here
fetches. A person then accepts, drops with a reason, edits the segment or
types the approximate headcount.

The pure parts live here so they can be tested without a terminal:
render_company(conn, id) -> str and apply_decision(conn, id, decision) ->
Result. The key loop is in engine/cli.py.

Queue (select): companies not dropped and not hand-made (hand = 1) that carry
a review_reason, plus, unless only_review, those at `qualified` that no person
has reviewed yet (reviewed_at NULL): ICP.md leaves too_large and conflict to
this review, so every qualified company passes through it once.

Accept:
  - a company at `sourced` (a domain review code from Step 26) needs a domain;
    accepting says the domain is right. Without a segment it moves to
    `resolved` with review_reason cleared, so `te resolve segment` tags it next.
  - a company with no segment past `sourced` (segment_unclear) is refused:
    press s first.
  - with a segment, the qualify rules (engine/resolve/qualify.py) are run for
    this company, except the two holds only a person can settle and that this
    accept settles: staffing_name and consumer_markers (named in review_note).
    pass   -> review_reason cleared, best_signal_id set, status `qualified`,
              reviewed_at set.
    hold   -> status `resolved` (from sourced) and review_reason becomes the
              qualify hold, which says what is still missing.
    drop   -> refused; nothing written. A person accepting does not turn into
              a drop: the message names the rule, and d drops it.
  - a company at `qualified` is re-checked the same way; pass marks it
    reviewed (and refreshes best_signal_id), anything else is refused.
  - later statuses and hand-made companies are refused.
Drop: one of DROP_REASONS, stage "review", optional note; review_reason is
kept as the record of why it was in review.
Segment: in_saas or us_india_team; review_reason is left for accept to clear.
Headcount: a positive integer; above TOO_LARGE the result warns, it never drops.

No network, no model calls.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from engine import states
from engine.resolve import qualify
from engine.resolve.segment import IN_SAAS, US_INDIA_TEAM
from engine.states import Status

STAGE = "review"
SEGMENTS: tuple[str, ...] = (IN_SAAS, US_INDIA_TEAM)
# protocol/ICP.md drop reasons a person applies here (too_large and conflict are
# human-only); duplicate is for a row Step 27 flagged that is the same company.
DROP_REASONS: tuple[str, ...] = (
    "too_large", "conflict", "staffing_or_agency", "consumer", "out_of_scope", "duplicate")
TOO_LARGE = 1000  # ICP.md: "more than roughly 1,000 people"
# qualify holds that only a person can settle, and an accept settles.
OVERRIDABLE_HOLDS = frozenset({"staffing_name", "consumer_markers"})
_REVIEWABLE = (Status.SOURCED, Status.RESOLVED, Status.QUALIFIED)
_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"

Kind = Literal["accept", "drop", "segment", "headcount"]


@dataclass(frozen=True)
class Decision:
    kind: Kind
    reason: str | None = None  # drop reason
    note: str | None = None  # free text with a drop (or accept)
    segment: str | None = None
    headcount: int | None = None  # with accept: recorded first


@dataclass
class Result:
    ok: bool
    message: str
    status: str | None = None
    changed: bool = False
    warnings: list[str] = field(default_factory=list)


class ReviewError(ValueError):
    pass


# -- reading ----------------------------------------------------------------------------


def _company(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM companies WHERE company_id = ?", (company_id,)).fetchone()
    if r is None:
        raise ReviewError(f"no company {company_id!r}")
    return r


def select(conn: sqlite3.Connection, *, company_id: str | None = None,
           limit: int | None = None, only_review: bool = False,
           pilot: bool = False) -> list[str]:
    """company_ids to review, those with a review_reason first, then by id."""
    if company_id is not None:
        _company(conn, company_id)
        return [company_id]
    want = "review_reason IS NOT NULL"
    if not only_review:
        want = f"({want} OR (status = 'qualified' AND reviewed_at IS NULL))"
    sql = (f"SELECT company_id FROM companies WHERE status != 'dropped' AND hand = 0 AND {want}"
           + (" AND pilot = 1" if pilot else "")
           + " ORDER BY review_reason IS NULL, company_id")
    params: tuple[object, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return [r["company_id"] for r in conn.execute(sql, params)]


def homepage_urls(domain: str | None, seed_site: str | None = None) -> list[str]:
    out = [seed_site] if seed_site else []
    if domain:
        out += [f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"]
    return list(dict.fromkeys(out))


def homepage_title(cache: object | None, domain: str | None,
                   seed_site: str | None = None) -> str:
    """The homepage <title> / og:site_name from Guard's cache (any day). Never fetches."""
    if cache is None:
        return "(no cache given)"
    from engine.resolve.domain import page_names

    seen: list[str] = []
    for url in homepage_urls(domain, seed_site):
        entry = cache.latest(url)  # type: ignore[attr-defined]
        if entry is None:
            continue
        if entry.outcome != "ok" or entry.sha256 is None:
            seen.append(f"{url} {entry.outcome}")
            continue
        body = cache.get_body(entry.sha256)  # type: ignore[attr-defined]
        if body is None:
            seen.append(f"{url} body missing")
            continue
        names = page_names(body.decode("utf-8", errors="replace"))
        day = entry.fetched_at.date().isoformat()
        return " | ".join(f'"{n}"' for n in names) + f"  ({url}, cached {day})" if names \
            else f"(no title on {url}, cached {day})"
    return "(not in Guard's cache" + (": " + "; ".join(seen) if seen else "") + ")"


def _flags(conn: sqlite3.Connection, company_id: str) -> list[str]:
    out = []
    for f in conn.execute(
            "SELECT f.kind, f.score, f.detail, o.company_id AS other, o.name, o.domain, o.status "
            "FROM company_flags f JOIN companies o ON o.company_id = CASE "
            "WHEN f.company_id = ? THEN f.other_company_id ELSE f.company_id END "
            "WHERE f.company_id = ? OR f.other_company_id = ? ORDER BY f.flag_id",
            (company_id, company_id, company_id)):
        out.append(f"{f['kind']} with {f['other']} ({f['name']}, {f['domain'] or 'no domain'}, "
                   f"{f['status']}) score {f['score']:.0f}: {f['detail']}")
    return out


def _yn(v: object) -> str:
    return "?" if v is None else ("yes" if v else "no")


def render_company(conn: sqlite3.Connection, company_id: str, *, cache: object | None = None,
                   seed_site: str | None = None) -> str:
    c = _company(conn, company_id)
    lines = [
        f"== {c['name']}  [{c['company_id']}]",
        f"domain     {c['domain'] or '(none)'}",
        f"status     {c['status']}" + (f"  ({c['drop_reason']}, {c['drop_stage']})"
                                       if c["drop_reason"] else "")
        + ("  pilot" if c["pilot"] else "") + ("  hand" if c["hand"] else ""),
        f"segment    {c['segment'] or '(none)'}",
        f"hq         {c['hq'] or '(unknown)'}" + (f"  basis {c['hq_basis']}" if c["hq_basis"]
                                                 else ""),
    ]
    for m in json.loads(c["hq_evidence"] or "[]"):
        lines.append(f"  - {m.get('marker', '?')} ({m.get('side', '?')}): \"{m['quote']}\"  "
                     f"{m['url']}")
    lines.append(f"review     {c['review_reason'] or '(none)'}")
    lines.append(f"headcount  {c['headcount'] if c['headcount'] is not None else '(not typed)'}"
                 + (f"  > {TOO_LARGE}: too_large?" if (c["headcount"] or 0) > TOO_LARGE else ""))
    if c["reviewed_at"]:
        lines.append(f"reviewed   {c['reviewed_at']}"
                     + (f"  note: {c['review_note']}" if c["review_note"] else ""))
    flags = _flags(conn, company_id)
    lines.append(f"duplicates {len(flags) or '(none flagged)'}")
    lines += [f"  - {f}" for f in flags]
    lines.append(f"homepage   {homepage_title(cache, c['domain'], seed_site)}")
    sigs = conn.execute(
        "SELECT signal_id, title, role_family, offer_type, location, fresh, india_located, url, "
        "posted_at, first_seen FROM signals WHERE company_id = ? ORDER BY signal_id",
        (company_id,)).fetchall()
    lines.append(f"signals    {len(sigs)}")
    for s in sigs:
        best = "*" if s["signal_id"] == c["best_signal_id"] else " "
        lines.append(
            f" {best}{s['title']} | {s['role_family'] or '?'} | {s['offer_type'] or '?'} | "
            f"{s['location'] or '(no location)'} | fresh {_yn(s['fresh'])} | india "
            f"{_yn(s['india_located'])} | {s['posted_at'] or 'seen ' + s['first_seen']}")
        lines.append(f"    {s['url'] or '(no url)'}")
    return "\n".join(lines)


# -- the accept check ----------------------------------------------------------------------


def evaluate_accepted(conn: sqlite3.Connection, t: qualify.Target
                      ) -> tuple[qualify.Verdict, list[str]]:
    """qualify.evaluate, with the holds in OVERRIDABLE_HOLDS settled by the person.
    Returns (verdict, overridden hold codes)."""
    sigs = qualify.signals_of(conn, t.company_id)
    overridden: list[str] = []
    for check in (lambda: qualify.check_out_of_scope(conn, t),
                  lambda: qualify.check_staffing(t, sigs),
                  lambda: qualify.check_consumer(t, sigs)):
        v = check()
        if v is None:
            continue
        if v.action == "hold" and v.reason in OVERRIDABLE_HOLDS:
            overridden.append(v.reason)
            continue
        return v, overridden
    fresh = qualify.pool(t, sigs)
    best = qualify.best_signal(fresh)
    if best is None:
        stale = sum(1 for s in sigs if s.fresh == 0 and s.gtm)
        unfiltered = sum(1 for s in sigs if s.fresh is None)
        unclassified = sum(1 for s in sigs if s.role_family is None)
        return qualify.Verdict(
            t, "hold", "open_gtm_role", "no_fresh_gtm_signal",
            detail=f"{len(sigs)} signal(s): {stale} stale GTM, {unfiltered} not filtered, "
                   f"{unclassified} not classified"), overridden
    return qualify.Verdict(t, "qualify", best_signal_id=best.signal_id,
                           gtm_roles=len(fresh)), overridden


# -- writing ------------------------------------------------------------------------------


def _set(conn: sqlite3.Connection, company_id: str, **cols: object) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    with conn:
        conn.execute(f"UPDATE companies SET {sets}, updated_at = {_NOW} WHERE company_id = ?",
                     (*cols.values(), company_id))


def _headcount(conn: sqlite3.Connection, c: sqlite3.Row, n: int) -> Result:
    if n < 1:
        return Result(False, "headcount must be a positive number", c["status"])
    changed = c["headcount"] != n
    if changed:
        _set(conn, c["company_id"], headcount=n)
    res = Result(True, f"headcount {n}", c["status"], changed)
    if n > TOO_LARGE:
        res.warnings.append(f"headcount {n} > {TOO_LARGE}: protocol/ICP.md drops this as "
                            "too_large; press d if it is")
    return res


def _segment(conn: sqlite3.Connection, c: sqlite3.Row, segment: str | None) -> Result:
    if segment not in SEGMENTS:
        return Result(False, f"segment must be one of {', '.join(SEGMENTS)}", c["status"])
    if Status(c["status"]) not in _REVIEWABLE:
        return Result(False, f"status {c['status']}: segment is only edited before capture",
                      c["status"])
    changed = c["segment"] != segment
    if changed:
        _set(conn, c["company_id"], segment=segment)
    res = Result(True, f"segment {segment}" + ("" if changed else " (unchanged)"),
                 c["status"], changed)
    if c["status"] == Status.QUALIFIED and changed:
        v, _ = evaluate_accepted(conn, _target(conn, c["company_id"]))
        if v.action != "qualify":
            res.warnings.append(f"with segment {segment} the qualify rules say {v.action} "
                                f"{v.reason}: {v.detail}")
    return res


def _drop(conn: sqlite3.Connection, c: sqlite3.Row, reason: str | None,
          note: str | None) -> Result:
    if reason not in DROP_REASONS:
        return Result(False, f"drop reason must be one of {', '.join(DROP_REASONS)}",
                      c["status"])
    changed = states.drop(c["company_id"], reason, STAGE, conn)
    note = (note or "").strip() or None
    cols: dict[str, object] = {"reviewed_at": _now(conn)}
    if note:
        cols["review_note"] = note
    _set(conn, c["company_id"], **cols)
    return Result(True, f"dropped: {reason}" + (f" ({note})" if note else ""),
                  Status.DROPPED.value, changed)


def _now(conn: sqlite3.Connection) -> str:
    return conn.execute(f"SELECT {_NOW}").fetchone()[0]


def _target(conn: sqlite3.Connection, company_id: str) -> qualify.Target:
    c = _company(conn, company_id)
    return qualify.Target(c["company_id"], c["name"], c["domain"], c["segment"], None,
                          c["best_signal_id"])


def _accept(conn: sqlite3.Connection, c: sqlite3.Row, note: str | None) -> Result:
    cid, status = c["company_id"], Status(c["status"])
    if status not in _REVIEWABLE:
        return Result(False, f"status {status}: accept applies to sourced, resolved and "
                             "qualified companies only", status.value)
    if status is Status.SOURCED and not c["domain"]:
        return Result(False, "no domain: an unresolved placeholder cannot be accepted; "
                             "resolve its domain first, or drop it", status.value)
    if c["segment"] is None:
        if status is Status.SOURCED:
            _set(conn, cid, review_reason=None)
            states.advance(cid, Status.RESOLVED, conn)
            return Result(True, "domain accepted; now resolved with no segment: run "
                                "`te resolve segment`, or press s", Status.RESOLVED.value, True)
        return Result(False, "no segment: press s to set it, then accept", status.value)
    if c["segment"] not in SEGMENTS:
        return Result(False, f"segment {c['segment']!r} is not one of {', '.join(SEGMENTS)}: "
                             "press s", status.value)

    t = _target(conn, cid)
    v, overridden = evaluate_accepted(conn, t)
    if v.action == "drop":
        return Result(False, f"not accepted: the qualify rule {v.rule} drops it "
                             f"({v.reason}: {v.detail}); press d to drop it", status.value)
    if status is Status.QUALIFIED and v.action != "qualify":
        return Result(False, f"not accepted: qualify now holds it ({v.reason}: {v.detail}); "
                             "it stays qualified and unreviewed; press d to drop it",
                      status.value)
    # The person's accept clears whatever review_reason was there; a hold below
    # writes its own.
    if c["review_reason"] is not None:
        _set(conn, cid, review_reason=None)
    if status is Status.SOURCED:
        states.advance(cid, Status.RESOLVED, conn)
    qualify.apply(conn, v)
    if v.action == "hold":
        return Result(True, f"accepted but held at resolved: {v.review_reason}",
                      Status.RESOLVED.value, True)
    parts = [p for p in ((note or "").strip(),
                         ("accepted over qualify hold: " + ", ".join(overridden))
                         if overridden else "") if p]
    cols: dict[str, object] = {"reviewed_at": _now(conn)}
    if parts:
        cols["review_note"] = "; ".join(parts)
    _set(conn, cid, **cols)
    msg = f"accepted: qualified, best signal {v.best_signal_id} (of {v.gtm_roles} fresh GTM)"
    if overridden:
        msg += f"; overrode {', '.join(overridden)}"
    return Result(True, msg, Status.QUALIFIED.value, True)


def apply_decision(conn: sqlite3.Connection, company_id: str, decision: Decision) -> Result:
    c = _company(conn, company_id)
    if c["status"] == Status.DROPPED:
        return Result(False, f"already dropped ({c['drop_reason']}, {c['drop_stage']})",
                      c["status"])
    if c["hand"]:
        return Result(False, "a hand-made Phase 0 company; not reviewed here", c["status"])
    if decision.kind == "headcount":
        return _headcount(conn, c, decision.headcount or 0)
    if decision.kind == "segment":
        return _segment(conn, c, decision.segment)
    if decision.kind == "drop":
        return _drop(conn, c, decision.reason, decision.note)
    if decision.kind == "accept":
        warnings: list[str] = []
        if decision.headcount is not None:
            hc = _headcount(conn, c, decision.headcount)
            if not hc.ok:
                return hc
            warnings = hc.warnings
            c = _company(conn, company_id)
        res = _accept(conn, c, decision.note)
        res.warnings = warnings + res.warnings
        return res
    raise ReviewError(f"unknown decision {decision.kind!r}")
