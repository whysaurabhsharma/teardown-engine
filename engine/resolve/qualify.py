"""Apply the ICP rules and pick each company's signal (docs/PLAN.md Step 29).

Input: companies at status `resolved` with a segment set (Step 28) and no
review_reason, plus those whose only review_reason is this step's own
("qualify_hold: ..."), which are re-checked on every run because the check is
pure database work. A company with any other review_reason (name_mismatch,
segment_unclear, ...) waits for `te resolve review` and is skipped.

The rules (protocol/ICP.md, "A company is dropped ..."), in the fixed order of
its table; the first rule that does not pass decides, and later rules are not
looked at:

  1. out_of_scope        segment us_india_team needs a fresh GTM signal located
                         in India (segment.india_roles). None, with every GTM
                         signal filtered -> drop out_of_scope, stage "resolve"
                         (the stage ICP.md and Step 28 give this reason). Some
                         not yet through `te signal filter` -> hold
                         (location_unknown). in_saas passes: an Indian
                         company's role counts wherever it sits. Any other
                         segment value -> hold (unknown_segment).
  2. staffing_or_agency  any of the company's signals (title or description)
                         says the post is made on a client's behalf ("on behalf
                         of our client", "hiring for one of our clients", "our
                         client is a ...", "client of ours") -> drop
                         staffing_or_agency, stage "qualify". A staffing word
                         in the company's name only ("Staffing", "Recruitment",
                         "Manpower", "Placements", "Headhunters", "Executive
                         Search", "Outsourcing", "HR Services/Solutions") ->
                         hold (staffing_name): a recruiting-software company
                         is B2B software, and only a person can tell.
  3. consumer            never dropped by code. "Sells to consumers only" cannot
                         be read from job posts, and the site is not captured
                         until E3. A consumer marker (B2C, D2C, direct to
                         consumer, consumer app/brand) in the company's posts
                         with no business marker (B2B, SaaS, enterprise,
                         businesses, SMB/SME) in any of them -> hold
                         (consumer_markers).
  4. open GTM role       qualification rule 2: at least one fresh GTM signal
                         (fresh = 1, role_family set and not not_gtm, offer_type
                         set; for us_india_team also india_located = 1). None
                         -> hold (no_fresh_gtm_signal); ICP.md gives no drop
                         reason for this at E2 (stale_signal is a send-day
                         drop), so a person decides.

Not applied here (ICP.md): too_large and conflict are for the human review
(Step 30, `te resolve review`, where the headcount is typed in); the
do-not-contact list in OFFER.md is never read by code. no_site_evidence (E3),
no_recipient (Step 43), no_gap (E5) and stale_signal (send day) belong to
their stages. A company passing rules 1-4 advances to `qualified`; the review
can still drop it.

A hold writes review_reason "qualify_hold: <code>: <detail>" and leaves the
status at `resolved`. When a later run finds the rule passes (say the filter
has since run), the hold is cleared and the company moves on.

One company, one teardown (ICP.md): best_signal_id is chosen from the pool in
rule 4 by, in order:
  a. offer_type candidate before adjacent;
  b. among adjacent signals, the most senior title (ICP.md: "otherwise the most
     senior GTM role"): level 4 chief / CxO / VP / head / director / founding /
     first / general manager; 3 manager / lead (not lead gen) / leader /
     principal / supervisor; 2 senior / sr; 1 anything else; 0 intern /
     trainee / junior / associate / fresher. Same level: family sales_lead,
     ae, demand_gen, sdr. Candidate signals are not ranked by seniority:
     ICP.md keeps "a candidate role" without an order among them;
  c. the most recent reference date (posted_at, or first_seen when there is no
     post date, as in the freshness rule);
  d. the lowest signal_id.

No network and no model calls: this reads and writes the database only.
Idempotent: a re-run with the same inputs writes nothing.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from engine import states
from engine.resolve.segment import DROP_REASON as OUT_OF_SCOPE
from engine.resolve.segment import DROP_STAGE as SEGMENT_STAGE
from engine.resolve.segment import IN_SAAS, US_INDIA_TEAM, india_roles
from engine.signal.filter import NOT_GTM

QUALIFY_VERSION = "q-1.0"  # bump when a rule or the signal choice changes
STAGE = "qualify"
HOLD_PREFIX = "qualify_hold"
STAFFING = "staffing_or_agency"
CONSUMER = "consumer"

RULE_ORDER: tuple[str, ...] = (OUT_OF_SCOPE, STAFFING, CONSUMER, "open_gtm_role")
QUOTE_CONTEXT = 50

# -- text rules ---------------------------------------------------------------------

# "client" as a team or function ("Client Success", "client-facing") is not a client.
_NOT_A_CLIENT = (r"(?![\w-])(?!\s+(?:success|services?|servicing|relations(?:hip)?|partners?|"
                 r"experience|facing|delivery|onboarding|management|team)\b)")
_CLIENT_BEHALF_RX = re.compile(
    r"\bon\s+behalf\s+of\s+(?:our|a|an|one\s+of\s+our)\s+(?:\w+\s+){0,2}clients?"
    + _NOT_A_CLIENT
    + r"|\bhiring\s+for\s+(?:our|a|an|one\s+of\s+our)\s+(?:\w+\s+){0,3}clients?"
    + _NOT_A_CLIENT
    + r"|\bfor\s+one\s+of\s+our\s+(?:\w+\s+){0,2}clients"
    + _NOT_A_CLIENT
    + r"|\bour\s+client\s*,?\s+(?:is\s+)?(?:a|an)\s+"
    r"|\bclient\s+of\s+ours\b",
    re.IGNORECASE,
)
_STAFFING_NAME_RX = re.compile(
    r"\b(?:staffing|recruitment|recruiters|recruiting|manpower|placements?|headhunters?|"
    r"headhunting|executive\s+search|outsourcing|hr\s+(?:services|solutions|consultants?))\b",
    re.IGNORECASE,
)
_CONSUMER_RX = re.compile(
    r"\b(?:b2c|d2c|dtc|direct[\s-]+to[\s-]+consumers?|consumer\s+(?:app|apps|brand|brands|"
    r"product|products|internet))\b",
    re.IGNORECASE,
)
_BUSINESS_RX = re.compile(
    r"\b(?:b2b|saas|enterprises?|businesses|smbs?|smes?|mid[\s-]?market)\b", re.IGNORECASE)

# -- seniority (adjacent signals) ---------------------------------------------------------

_LEVELS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (4, re.compile(r"\b(?:chief|cro|cmo|cbo|ceo|vp|vice president|svp|evp|avp|head|director|"
                   r"founding|first|gm|general manager)\b")),
    (3, re.compile(r"\b(?:manager|lead(?! gen)|leader|principal|supervisor)\b")),
    (2, re.compile(r"\b(?:senior|sr)\b")),
    (0, re.compile(r"\b(?:intern|internship|trainee|junior|jr|associate|fresher)\b")),
)
_FAMILY_ORDER = {"sales_lead": 0, "ae": 1, "demand_gen": 2, "sdr": 3}
_OFFER_ORDER = {"candidate": 0, "adjacent": 1}


def _norm(title: str) -> str:
    t = unicodedata.normalize("NFKC", title).lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def seniority(title: str) -> int:
    """4 most senior .. 0 entry level; 1 when the title says nothing."""
    t = _norm(title)
    for level, rx in _LEVELS:
        if rx.search(t):
            return level
    return 1


def _quote(text: str, m: re.Match[str]) -> str:
    a, b = max(0, m.start() - QUOTE_CONTEXT), min(len(text), m.end() + QUOTE_CONTEXT)
    return " ".join(text[a:b].split())


# -- data ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Sig:
    signal_id: str
    title: str
    description: str | None
    role_family: str | None
    offer_type: str | None
    posted_at: str | None
    first_seen: str
    fresh: int | None
    india_located: int | None

    @property
    def ref_date(self) -> date:
        return date.fromisoformat((self.posted_at or self.first_seen)[:10])

    @property
    def gtm(self) -> bool:
        return self.role_family is not None and self.role_family != NOT_GTM \
            and self.offer_type in _OFFER_ORDER


@dataclass
class Target:
    company_id: str
    name: str
    domain: str | None
    segment: str
    review_reason: str | None
    best_signal_id: str | None


@dataclass
class Verdict:
    """One company's result: pass (qualified), drop or hold."""

    target: Target
    action: str  # "qualify" | "drop" | "hold"
    rule: str = ""  # the rule that decided a drop or hold
    reason: str = ""  # drop reason, or hold code
    stage: str = ""  # drop stage
    detail: str = ""
    best_signal_id: str | None = None
    gtm_roles: int = 0  # fresh GTM signals in the pool (a fact the judge can use later)
    changed: bool = False

    @property
    def review_reason(self) -> str | None:
        if self.action != "hold":
            return None
        return f"{HOLD_PREFIX}: {self.reason}: {self.detail}"


def signals_of(conn: sqlite3.Connection, company_id: str) -> list[Sig]:
    return [Sig(r["signal_id"], r["title"], r["description_text"], r["role_family"],
                r["offer_type"], r["posted_at"], r["first_seen"], r["fresh"],
                r["india_located"])
            for r in conn.execute(
                "SELECT signal_id, title, description_text, role_family, offer_type, "
                "posted_at, first_seen, fresh, india_located FROM signals "
                "WHERE company_id = ? ORDER BY signal_id", (company_id,))]


# -- the rules ---------------------------------------------------------------------------


def check_out_of_scope(conn: sqlite3.Connection, t: Target) -> Verdict | None:
    if t.segment == IN_SAAS:
        return None
    if t.segment != US_INDIA_TEAM:
        return Verdict(t, "hold", OUT_OF_SCOPE, "unknown_segment",
                       detail=f"segment {t.segment!r} is not in_saas or us_india_team")
    india, unknown = india_roles(conn, t.company_id)
    if india:
        return None
    if unknown:
        return Verdict(t, "hold", OUT_OF_SCOPE, "location_unknown",
                       detail=f"head office abroad; {unknown} GTM signal(s) not yet through "
                              "`te signal filter`")
    return Verdict(t, "drop", OUT_OF_SCOPE, OUT_OF_SCOPE, SEGMENT_STAGE,
                   detail="head office abroad and no fresh India-located GTM signal")


def check_staffing(t: Target, sigs: list[Sig]) -> Verdict | None:
    for s in sigs:
        for text in (s.title, s.description or ""):
            m = _CLIENT_BEHALF_RX.search(text)
            if m:
                return Verdict(t, "drop", STAFFING, STAFFING, STAGE,
                               detail=f"{s.signal_id}: \"{_quote(text, m)}\"")
    m = _STAFFING_NAME_RX.search(t.name)
    if m:
        return Verdict(t, "hold", STAFFING, "staffing_name",
                       detail=f"name \"{t.name}\" has \"{m.group(0)}\"; recruiter or "
                              "software for recruiters?")
    return None


def check_consumer(t: Target, sigs: list[Sig]) -> Verdict | None:
    texts = [(s, x) for s in sigs for x in (s.title, s.description or "") if x]
    if any(_BUSINESS_RX.search(x) for _, x in texts):
        return None
    for s, x in texts:
        m = _CONSUMER_RX.search(x)
        if m:
            return Verdict(t, "hold", CONSUMER, "consumer_markers",
                           detail=f"{s.signal_id}: \"{_quote(x, m)}\"; no B2B marker in any "
                                  "post")
    return None


def pool(t: Target, sigs: list[Sig]) -> list[Sig]:
    """Fresh GTM signals the teardown may be written against."""
    out = [s for s in sigs if s.fresh == 1 and s.gtm]
    if t.segment == US_INDIA_TEAM:
        out = [s for s in out if s.india_located == 1]
    return out


def best_signal(candidates: list[Sig]) -> Sig | None:
    """The signal for the one teardown; see the module docstring for the order."""
    def key(s: Sig) -> tuple[int, int, int, int, str]:
        offer = _OFFER_ORDER[s.offer_type or "adjacent"]
        if offer == 0:
            senior, family = 0, 0
        else:
            senior, family = -seniority(s.title), _FAMILY_ORDER.get(s.role_family or "", 9)
        return (offer, senior, family, -s.ref_date.toordinal(), s.signal_id)

    return min(candidates, key=key, default=None)


def evaluate(conn: sqlite3.Connection, t: Target) -> Verdict:
    """Apply the rules in RULE_ORDER; the first that does not pass decides."""
    sigs = signals_of(conn, t.company_id)
    for check in (lambda: check_out_of_scope(conn, t), lambda: check_staffing(t, sigs),
                  lambda: check_consumer(t, sigs)):
        v = check()
        if v is not None:
            return v
    fresh = pool(t, sigs)
    best = best_signal(fresh)
    if best is None:
        unfiltered = sum(1 for s in sigs if s.fresh is None)
        unclassified = sum(1 for s in sigs if s.role_family is None)
        stale = sum(1 for s in sigs if s.fresh == 0 and s.gtm)
        return Verdict(t, "hold", "open_gtm_role", "no_fresh_gtm_signal",
                       detail=f"{len(sigs)} signal(s): {stale} stale GTM, {unfiltered} not "
                              f"filtered, {unclassified} not classified")
    return Verdict(t, "qualify", best_signal_id=best.signal_id, gtm_roles=len(fresh))


# -- writing -----------------------------------------------------------------------------


def _own(reason: str | None) -> bool:
    return reason is not None and reason.startswith(HOLD_PREFIX + ":")


def apply(conn: sqlite3.Connection, v: Verdict) -> bool:
    """Write a verdict. Returns True if anything changed."""
    t = v.target
    changed = False
    new_reason = v.review_reason
    if new_reason != t.review_reason and (t.review_reason is None or _own(t.review_reason)):
        with conn:
            conn.execute("UPDATE companies SET review_reason = ?, "
                         "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                         "WHERE company_id = ?", (new_reason, t.company_id))
        changed = True
    if v.action == "drop":
        changed = states.drop(t.company_id, v.reason, v.stage, conn) or changed
    elif v.action == "qualify":
        if t.best_signal_id != v.best_signal_id:
            with conn:
                conn.execute("UPDATE companies SET best_signal_id = ?, "
                             "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                             "WHERE company_id = ?", (v.best_signal_id, t.company_id))
            changed = True
        changed = states.advance(t.company_id, states.Status.QUALIFIED, conn) or changed
    return changed


# -- the run -----------------------------------------------------------------------------


def select(conn: sqlite3.Connection, *, company_id: str | None = None,
           limit: int | None = None) -> tuple[list[Target], list[Target]]:
    """(targets, skipped): resolved companies with a segment, by company_id.
    skipped are those with someone else's review_reason."""
    sql = ("SELECT company_id, name, domain, segment, review_reason, best_signal_id "
           "FROM companies WHERE status = 'resolved' AND segment IS NOT NULL")
    params: list[object] = []
    if company_id is not None:
        sql += " AND company_id = ?"
        params.append(company_id)
    sql += " ORDER BY company_id"
    targets: list[Target] = []
    skipped: list[Target] = []
    for r in conn.execute(sql, params):
        t = Target(r["company_id"], r["name"], r["domain"], r["segment"], r["review_reason"],
                   r["best_signal_id"])
        if t.review_reason is not None and not _own(t.review_reason):
            skipped.append(t)
        elif limit is None or len(targets) < limit:
            targets.append(t)
    return targets, skipped


@dataclass
class Report:
    companies: int = 0
    qualified: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    held: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)  # other review codes
    unsegmented: int = 0  # resolved, no segment, no review: run `te resolve segment`
    changed: int = 0
    errors: list[str] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)


def run(conn: sqlite3.Connection, *, company_id: str | None = None, limit: int | None = None,
        dry_run: bool = False) -> Report:
    targets, skipped = select(conn, company_id=company_id, limit=limit)
    rep = Report(companies=len(targets))
    for t in skipped:
        rep.skipped[(t.review_reason or "").split(":", 1)[0]] += 1
    sql = ("SELECT count(*) FROM companies WHERE status = 'resolved' AND segment IS NULL "
           "AND review_reason IS NULL")
    params: tuple[object, ...] = ()
    if company_id is not None:
        sql += " AND company_id = ?"
        params = (company_id,)
    rep.unsegmented = conn.execute(sql, params).fetchone()[0]
    for t in targets:
        try:
            v = evaluate(conn, t)
            if not dry_run:
                v.changed = apply(conn, v)
        except (ValueError, sqlite3.Error, states.TransitionError) as e:
            rep.errors.append(f"{t.company_id}: {type(e).__name__}: {e}")
            continue
        rep.verdicts.append(v)
        rep.changed += int(v.changed)
        if v.action == "qualify":
            rep.qualified += 1
        elif v.action == "drop":
            rep.dropped[v.reason] += 1
        else:
            rep.held[v.reason] += 1
    return rep
