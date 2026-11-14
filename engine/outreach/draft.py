"""The send queue: approve, draft, sent, done (docs/PLAN.md Steps 54, 55, 70, 72;
protocol/CONDUCT.md Part 3; protocol/TEARDOWN_SPEC.md v1.2, "The email, two
variants").

    te queue approve <company_id>                     built -> approved
    te queue draft --pilot | --company <id> [--limit n] [--dry-run] [--role-checked]
    te queue sent <company_id> [--at ISO]             a person sent touch 1
    te queue done <company_id> --minutes <n>          review minutes (Step 55)

No code path here sends anything. `draft` makes a Gmail draft through
engine/guard/gmail.py (users.drafts.create only); a person reads it, sends it
by hand, and then runs `te queue sent`.

approve. The human gate between built and approved (states.py; Step 69:
"review tomorrow's batch ... and approve"). Run it after reading the page and
the email preview it prints (or from the review queue, `te queue open`, which
calls the same function). It refuses a teardown killed in review whose file has
not changed since the kill, and refuses unless `te teardown check` passes, the
rendered page under site/<slug>/ is current, and the email block of the
teardown file fills the spec's variant cleanly. It sets the company to
approved and teardowns.review_state to approved. A teardown pulled back for
review by the send-day recheck (review_state pending again) is re-approved
the same way.

draft. For each approved company whose teardown is approved and has no touch
1 yet, in this order (a refusal stops that company and changes nothing unless
said):
  0. the stop: two bounces on one day (Step 74, engine/outreach/bounces.py)
     stop every draft until `te queue resume` -> refused.
  1. suppressions: the recipient's address, its domain or the company's
     domain is suppressed (Step 72) -> refused.
  2. local checks: TEARDOWN_BASE_URL set, an https URL, not a shortener; the
     teardown check passes; the page is rendered and current; the email block
     fills the variant for the best signal's offer_type (candidate|adjacent)
     with no problem; a candidate variant needs the application recorded on
     touch 1 (touches.applied, CONDUCT Part 3; `te queue applied` is Step 75);
     a recipient with a channel.
  3. the role (Step 70): the best signal's ATS board is re-polled fresh through
     the collectors (every ATS board holding the company's GTM signals is
     polled with it). Open -> go on. Closed and another fresh GTM role open on
     those boards (classified, fresh by date, India-located for us_india_team;
     chosen by qualify.best_signal) -> best_signal_id switches to it, the
     teardown goes back to review (review_state pending) and nothing is
     drafted: the page names the old role. Closed with no other -> dropped
     stale_signal (stage send_day). A board that cannot be read -> refused,
     nothing changed. A signal with no collector (careers page, manual) ->
     refused unless --role-checked says a person saw the post open today.
  4. the evidence (Step 70): only the artifact kinds the stored diagnosis cites
     are re-fetched fresh (capture's recheck logic: capture_company with only=
     and fresh=True), their text extracted in memory, and the faithfulness
     check re-run on the stored claims. Any claim failing -> refused, the
     claims named, the teardown back to review. The artifacts rows are not
     rewritten: they are the capture the page's section 2 cites by date.
  5. the message. Email: the spec's variant with {first_name} filled locally
     from the contact's name, {link} = $TEARDOWN_BASE_URL/<slug>/, the sender
     block from data/sender.yaml; under 120 words, exactly one link, no banned
     phrase, plain text only. Then gmail.create_draft and a touches row (touch
     1, channel email, draft_id, drafted_at, status drafted). LinkedIn: the
     hand-send text (one sentence and the link) is printed, no draft is made,
     and the touches row is written with channel linkedin and no draft_id.
Idempotent: a company with a touch 1 already drafted or sent is skipped before
any request. A run that would put more than the CONDUCT daily pace (8; 5 with
--pilot, the pilot ten going out five a morning) in one UTC day warns; it
does not block. --dry-run makes no request, no draft and no write: it runs the
local checks and prints what it would re-poll, re-fetch and draft.

Contacts never reach a log: messages name no person, address or LinkedIn URL;
the body is printed only with {first_name} unfilled, and the address never.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from engine import states
from engine.build import render as R
from engine.build import sender as S
from engine.build import teardown as T
from engine.capture.extract_text import extract as extract_artifact
from engine.extract.base import Art
from engine.guard.cache import Cache
from engine.judge import faithfulness as F
from engine.resolve import qualify as Q
from engine.resolve.domain import registrable_domain
from engine.resolve.segment import US_INDIA_TEAM
from engine.signal import ats
from engine.signal.boards import COLLECTORS
from engine.signal.filter import is_fresh
from engine.states import Status

QUEUE_VERSION = "queue-1.0"
SPEC_VERSION = "1.2"  # protocol/TEARDOWN_SPEC.md the templates below are copied from

WORD_LIMIT = 120  # under this
PACE = 8  # CONDUCT Part 3: five to eight reviewed sends a day
PILOT_PACE = 5  # the pilot ten: five one morning, five the next
LINKEDIN_NOTE_MAX = 300  # a LinkedIn connection note's character limit
STALE_REASON = "stale_signal"
STALE_STAGE = "send_day"
APPROVED, PENDING = "approved", "pending"  # teardowns.review_state
SHORTENERS = frozenset({"bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "buff.ly",
                        "rebrand.ly", "cutt.ly", "is.gd", "shorturl.at", "rb.gy", "lnkd.in"})
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_SIGNOFF_NAME = "Saurabh"  # the spec's sign-off line, filled from data/sender.yaml's name

# protocol/TEARDOWN_SPEC.md v1.2, "The email, two variants", verbatim (a test checks).
CANDIDATE_SUBJECT = "Applied for {role} at {Company}, with notes"
CANDIDATE_BODY = """Hi {first_name},

I applied for the {role} role at {Company} today. Alongside the CV, I looked
at what that hire will need on day one. {One sentence: the specific thing
the public pages show.}

I wrote it up and built {the proof slice, named concretely} for you: {link}

If it is useful, I would value 15 minutes. If I have misread something, I
would like to hear that too.

Saurabh
{one-line identity} · {postal address}
Not relevant? Reply "no" and I will not write again."""

ADJACENT_SUBJECT = "{Company}'s {role} hire: {three-word observation}"
ADJACENT_BODY = """Hi {first_name},

I saw {Company} is hiring a {role}. {One sentence: what that hire will need,
and what the public pages suggest is missing.}

I wrote up what I found and built {the proof slice, named concretely} for
you: {link}

I build outbound and enrichment systems. I am looking for a GTM engineer
role and am also open to a short project. If the notes are useful, could we
talk for 15 minutes?

Saurabh
{one-line identity} · {postal address}
Not relevant? Reply "no" and I will not write again."""

TEMPLATES = {"candidate": (CANDIDATE_SUBJECT, CANDIDATE_BODY),
             "adjacent": (ADJACENT_SUBJECT, ADJACENT_BODY)}


class QueueError(RuntimeError):
    """A refusal. The message never carries a contact value."""


# -- small helpers -------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def count_words(text: str) -> int:
    """Words a reader reads: tokens with a letter or a digit ("·" is not a word)."""
    return sum(1 for t in text.split() if any(ch.isalnum() for ch in t))


def links_in(text: str) -> list[str]:
    return [u.rstrip(".,;:!?)\"'") for u in _URL.findall(text)]


def first_name(name: str | None) -> str | None:
    parts = (name or "").split()
    return parts[0] if parts else None


def article(word: str) -> str:
    """'a' or 'an' before a role title ("an SDR", "an Account Executive")."""
    w = word.strip()
    if not w:
        return "a"
    first = w.split()[0]
    if first.isupper() and len(first) > 1:  # an acronym, read letter by letter
        return "an" if first[0] in "AEFHILMNORSX" else "a"
    return "an" if first[0].lower() in "aeiou" else "a"


def unwrap(body: str) -> str:
    """Join the spec's hard-wrapped lines within each paragraph; the last paragraph
    (sign-off, identity and address, opt-out) keeps its lines."""
    paras = body.split("\n\n")
    out = [" ".join(p.split("\n")) for p in paras[:-1]] + [paras[-1]]
    return "\n\n".join(out)


def link_for(slug: str) -> str:
    """$TEARDOWN_BASE_URL/<slug>/; refused when unset, not https, or a shortener."""
    url = R.future_url(slug)
    if url is None:
        raise QueueError(f"{R.BASE_URL_ENV_VAR} is not set; the email has no link to give")
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname:
        raise QueueError(f"{R.BASE_URL_ENV_VAR} must be an https URL")
    host = p.hostname.lower()
    if host in SHORTENERS or registrable_domain(url) in SHORTENERS:
        raise QueueError(f"{R.BASE_URL_ENV_VAR} is a link shortener; CONDUCT allows none")
    return url


# -- filling the email ---------------------------------------------------------------------


@dataclass
class Message:
    variant: str  # candidate | adjacent
    subject: str
    body: str  # {first_name} still a placeholder until fill_name
    link: str


def fill(variant: str, doc: T.TeardownDoc, snd: S.Sender, link: str) -> Message:
    """The spec's variant filled with everything but {first_name}."""
    if variant not in TEMPLATES:
        raise QueueError(f"offer_type {variant!r}; the email has a candidate and an adjacent "
                         "variant only")
    if doc.email is None:
        raise QueueError("the teardown file has no email block (observation, claims, slice)")
    e = doc.email
    subject_t, body_t = TEMPLATES[variant]
    role, company = doc.header.role.strip(), doc.header.company.strip()
    values = {
        "role": role, "Company": company, "link": link,
        "the proof slice, named concretely": e.slice.strip(),
        "one-line identity": snd.identity.strip(), "postal address": snd.postal_address.strip(),
        "three-word observation": (e.subject or "").strip(),
    }

    def sub(text: str) -> str:
        def one(m: re.Match[str]) -> str:
            key = m.group(1)
            if key == "first_name":
                return m.group(0)
            if key.startswith("One sentence:"):
                return " ".join(e.observation.split())
            if key not in values:
                raise QueueError(f"the template has an unknown field {{{key}}}")
            return values[key]
        return _PLACEHOLDER.sub(one, text)

    body = unwrap(body_t)
    body = body.replace(f"\n\n{_SIGNOFF_NAME}\n", f"\n\n{snd.name.strip()}\n", 1)
    if variant == "adjacent":
        body = body.replace("is hiring a {role}.", f"is hiring {article(role)} {{role}}.", 1)
    return Message(variant, sub(subject_t), sub(body), link)


def fill_name(msg: Message, name: str) -> str:
    """The body with {first_name} filled. Done locally, at the last moment; the
    result is never printed or logged."""
    return msg.body.replace("{first_name}", name)


def message_problems(msg: Message, doc: T.TeardownDoc, ctx: T.Context,
                     body: str | None = None) -> list[str]:
    """What breaks the spec in this email. body: the name-filled body, else the
    placeholder one. Messages never quote a contact value."""
    out: list[str] = []
    e = doc.email
    text = body if body is not None else msg.body
    if e is None:
        return ["the teardown file has no email block"]
    n = count_words(text)
    if n >= WORD_LIMIT:
        out.append(f"the email has {n} words; it must be under {WORD_LIMIT}")
    links = links_in(msg.subject + "\n" + text)
    if len(links) != 1:
        out.append(f"the email carries {len(links)} links; exactly one, to the page")
    elif links[0] != msg.link:
        out.append("the email's one link is not the page's URL")
    for t in (msg.subject, text):
        for p in T.banned_in(t):
            out.append(f"banned phrase {p if p.startswith(chr(34)) else repr(p)}")
    for s in sorted({x for t in (e.observation, e.slice, e.subject or "")
                     for x in T.superlatives_in(t)}):
        out.append(f"superlative {s!r}")
    left = [m for m in _PLACEHOLDER.findall(msg.subject + "\n" + text)
            if body is not None or m != "first_name"]
    for m in sorted(set(left)):
        out.append(f"{{{m}}} is left unfilled")
    obs = " ".join(e.observation.split())
    if not obs or T.TODO in obs:
        out.append("email.observation is not written")
    elif len(T.sentences(obs)) != 1:
        out.append("email.observation must be one sentence")
    elif obs[-1] not in ".!?":
        out.append("email.observation must end its sentence")
    if not e.slice.strip() or T.TODO in e.slice:
        out.append("email.slice is not written")
    if not e.claims:
        out.append("email.observation names no claim; it must trace to a passing claim")
    for i in e.claims:
        p = T.claim_problem(ctx, i)
        if p is not None:
            out.append(f"email.observation cites claim [{i}]: {p}")
    if msg.variant == "adjacent":
        sw = (e.subject or "").split()
        if len(sw) != 3 or T.TODO in (e.subject or ""):
            out.append("email.subject must be the three-word observation")
    elif e.subject:
        out.append("email.subject is for the adjacent variant only; the candidate subject "
                   "is fixed")
    if "\n" in msg.subject:
        out.append("the subject must be one line")
    return out


def linkedin_text(doc: T.TeardownDoc, link: str) -> str:
    """The hand-send note (CONDUCT Part 3): one sentence and the link, no name."""
    e = doc.email
    slice_name = e.slice.strip() if e is not None else doc.slice.title.strip().rstrip(".")
    return (f"I wrote up notes on what {doc.header.company}'s {doc.header.role} hire will "
            f"need, and built {slice_name} for you: {link}")


def linkedin_problems(text: str, link: str) -> list[str]:
    out = [f"banned phrase {p}" for p in T.banned_in(text)]
    links = links_in(text)
    if links != [link]:
        out.append(f"the note carries {len(links)} links; exactly one, to the page")
    if len(text) > LINKEDIN_NOTE_MAX:
        out.append(f"the note has {len(text)} characters; a connection note takes "
                   f"{LINKEDIN_NOTE_MAX} (send it as a message instead)")
    return out


# -- reading the database ------------------------------------------------------------------


def company_row(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM companies WHERE company_id = ?", (company_id,)).fetchone()
    if r is None:
        raise QueueError(f"{company_id}: no such company")
    return r


def teardown_row(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM teardowns WHERE company_id = ? AND page_slug IS NOT NULL "
                        "ORDER BY teardown_id LIMIT 1", (company_id,)).fetchone()


def touch_one(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM touches WHERE company_id = ? AND touch_no = 1",
                        (company_id,)).fetchone()


def touch_done(t: sqlite3.Row | None) -> str | None:
    """Why touch 1 needs nothing more, or None."""
    if t is None:
        return None
    if t["sent_at"] is not None or t["status"] == "sent":
        return "touch 1 is already sent"
    if t["draft_id"] is not None or t["status"] == "drafted":
        return "touch 1 is already drafted"
    return None


def recipient(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    from engine.people.contacts import recipient as _recipient

    return _recipient(conn, company_id)


def _domain_forms(domain: str | None) -> set[str]:
    if not domain:
        return set()
    d = domain.strip().lower().rstrip(".")
    d = d[4:] if d.startswith("www.") else d
    out = {d}
    reg = registrable_domain(d)
    if reg:
        out.add(reg.lower())
    return out


def suppression_problem(conn: sqlite3.Connection, company: sqlite3.Row,
                        contact: sqlite3.Row | None) -> str | None:
    """Why this company may not be written to (Step 72), naming no address."""
    rows = {r["email_or_domain"].strip().lower() for r in
            conn.execute("SELECT email_or_domain FROM suppressions")}
    if not rows:
        return None
    email = (contact["email"] or "").strip().lower() if contact is not None else ""
    if email and email in rows:
        return "the recipient's address is suppressed"
    if email and "@" in email:
        hit = _domain_forms(email.rsplit("@", 1)[1]) & rows
        if hit:
            return f"the recipient's domain {sorted(hit)[0]} is suppressed"
    hit = _domain_forms(company["domain"]) & rows
    if hit:
        return f"the company's domain {sorted(hit)[0]} is suppressed"
    return None


# -- the local checks ----------------------------------------------------------------------


@dataclass
class Ready:
    """Everything the local checks established for one company."""

    company: sqlite3.Row
    chk: T.Checked
    doc: T.TeardownDoc
    ctx: T.Context
    snd: S.Sender
    slug: str
    page: Path
    msg: Message | None = None  # None when no link is known yet (approve without the URL)
    warnings: list[str] = field(default_factory=list)


def page_problem(chk: T.Checked, page: Path) -> str | None:
    if not page.is_file():
        return f"no rendered page at {page}; run te teardown render"
    if page.read_bytes() != R.html(R.build_page(chk)).encode("utf-8"):
        return f"{page} is not the current render; run te teardown render (and deploy)"
    return None


def prepare(conn: sqlite3.Connection, company_id: str, *, teardowns: Path | None = None,
            sender_file: Path | None = None, site: Path | None = None,
            need_link: bool = True) -> Ready:
    """The checks that need no network. Raises QueueError with every problem."""
    company = company_row(conn, company_id)
    chk = T.check(conn, company_id, directory=teardowns, sender_file=sender_file)
    if not chk.ok:
        raise QueueError("te teardown check fails:\n  " +
                         "\n  ".join(str(f) for f in chk.failures))
    assert chk.doc is not None and chk.ctx is not None and chk.sender is not None
    td = teardown_row(conn, company_id)
    if td is None:
        raise QueueError("no rendered page (no slug); run te teardown render")
    page = R.site_dir(site) / td["page_slug"] / "index.html"
    p = page_problem(chk, page)
    if p:
        raise QueueError(p)
    r = Ready(company, chk, chk.doc, chk.ctx, chk.sender, td["page_slug"], page)
    try:
        link = link_for(r.slug)
    except QueueError:
        if need_link:
            raise
        r.warnings.append(f"{R.BASE_URL_ENV_VAR} is not set; checked with a placeholder link")
        link = "https://teardown.invalid/" + r.slug + "/"
    variant = r.doc.header.offer_type
    msg = fill(variant, r.doc, r.snd, link)
    problems = message_problems(msg, r.doc, r.ctx)
    if problems:
        raise QueueError("the email does not meet TEARDOWN_SPEC:\n  " + "\n  ".join(problems))
    r.msg = msg
    return r


# -- approve, sent, done -------------------------------------------------------------------


@dataclass
class Result:
    company_id: str
    outcome: str
    detail: str = ""
    lines: list[str] = field(default_factory=list)  # printed for the person
    warnings: list[str] = field(default_factory=list)


def preview(msg: Message) -> list[str]:
    """The email as the person will read it, {first_name} unfilled, no address."""
    return [f"Subject: {msg.subject}", "", *msg.body.split("\n")]


def approve(conn: sqlite3.Connection, company_id: str, *, teardowns: Path | None = None,
            sender_file: Path | None = None, site: Path | None = None,
            dry_run: bool = False) -> Result:
    c = company_row(conn, company_id)
    status = Status(c["status"])
    td = teardown_row(conn, company_id)
    if status is Status.DROPPED:
        return Result(company_id, "refused", f"dropped ({c['drop_reason']})")
    if status is Status.APPROVED and td is not None and td["review_state"] == APPROVED:
        return Result(company_id, "unchanged", "already approved")
    if status not in (Status.BUILT, Status.APPROVED):
        return Result(company_id, "refused", f"status {status}; approve takes a built teardown")
    from engine.outreach.review import kill_hold

    held = kill_hold(conn, company_id, teardowns=teardowns)
    if held:  # Step 66: a killed teardown is approved only once its file has changed
        return Result(company_id, "refused", held)
    try:
        r = prepare(conn, company_id, teardowns=teardowns, sender_file=sender_file, site=site,
                    need_link=False)
    except (QueueError, T.TeardownError) as e:
        return Result(company_id, "refused", str(e))
    assert r.msg is not None
    lines = [f"page: {r.page}", *preview(r.msg)]
    if dry_run:
        return Result(company_id, "dry_run", "checks pass; nothing written", lines, r.warnings)
    with conn:
        conn.execute("UPDATE teardowns SET review_state = ? WHERE company_id = ? AND "
                     "page_slug = ?", (APPROVED, company_id, r.slug))
    if status is Status.BUILT:
        states.advance(company_id, Status.APPROVED, conn)
    return Result(company_id, "approved", "", lines, r.warnings)


def sent(conn: sqlite3.Connection, company_id: str, *, at: datetime | None = None,
         dry_run: bool = False) -> Result:
    """A person sent touch 1 by hand: record sent_at and move to sent."""
    c = company_row(conn, company_id)
    t = touch_one(conn, company_id)
    if t is None or t["status"] not in ("drafted", "sent"):
        return Result(company_id, "refused", "no drafted touch 1; run te queue draft first")
    if t["status"] == "sent":
        return Result(company_id, "unchanged", f"touch 1 already recorded sent at {t['sent_at']}")
    status = Status(c["status"])
    if status is Status.DROPPED:
        return Result(company_id, "refused", f"dropped ({c['drop_reason']}); was it sent?")
    when = at or now_utc()
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if t["drafted_at"] and when < datetime.fromisoformat(t["drafted_at"].replace("Z", "+00:00")):
        return Result(company_id, "refused", "the send time is before the draft was made")
    if when > now_utc():
        return Result(company_id, "refused", "the send time is in the future")
    if dry_run:
        return Result(company_id, "dry_run", f"would record touch 1 sent at {stamp(when)}")
    with conn:
        conn.execute("UPDATE touches SET sent_at = ?, status = 'sent' WHERE touch_id = ?",
                     (stamp(when), t["touch_id"]))
    if states.can_advance(status, Status.SENT):
        states.advance(company_id, Status.SENT, conn)
    return Result(company_id, "sent", f"touch 1 ({t['channel']}) sent at {stamp(when)}")


def done(conn: sqlite3.Connection, company_id: str, minutes: float, *,
         dry_run: bool = False) -> Result:
    """Record the minutes the review took (Step 55) in teardowns.review_minutes."""
    company_row(conn, company_id)
    if not minutes > 0:
        return Result(company_id, "refused", "minutes must be more than zero")
    td = teardown_row(conn, company_id) or conn.execute(
        "SELECT * FROM teardowns WHERE company_id = ? ORDER BY teardown_id LIMIT 1",
        (company_id,)).fetchone()
    if td is None:
        return Result(company_id, "refused", "no teardown row; render the teardown first")
    old = td["review_minutes"]
    if old is not None and float(old) == float(minutes):
        return Result(company_id, "unchanged", f"review minutes already {old:g}")
    if dry_run:
        return Result(company_id, "dry_run", f"would record {minutes:g} review minutes")
    with conn:
        conn.execute("UPDATE teardowns SET review_minutes = ? WHERE teardown_id = ?",
                     (float(minutes), td["teardown_id"]))
    was = f" (was {float(old):g})" if old is not None else ""
    return Result(company_id, "recorded", f"{minutes:g} review minutes{was}")


# -- the send-day recheck (Step 70) --------------------------------------------------------


class Checks(Protocol):
    """The network side of the send-day recheck; tests pass a fake."""

    async def poll_board(self, source: str, slug: str) -> ats.BoardResult: ...

    async def fresh_arts(self, conn: sqlite3.Connection, company_id: str,
                         kinds: list[str]) -> dict[str, Art]: ...

    async def aclose(self) -> None: ...


class LiveChecks:
    """Everything through engine/guard: boards via the collectors, pages via
    capture's recheck path. Fresh: today's cache is bypassed."""

    def __init__(self, cache: Cache, budget: Any = None,
                 make_runtime: Callable[..., Any] | None = None) -> None:
        from engine.capture import capture as cap
        from engine.guard.budget import Budget

        self.cache = cache
        self.budget = budget or Budget()
        self._make = make_runtime or cap.make_runtime
        self._rt: Any = None

    @property
    def rt(self) -> Any:
        if self._rt is None:
            self._rt = self._make(self.cache, self.budget)
        return self._rt

    async def poll_board(self, source: str, slug: str) -> ats.BoardResult:
        from engine.signal.boards import collect_board

        return await collect_board(source, slug, self.rt.guard, fresh=True)

    async def fresh_arts(self, conn: sqlite3.Connection, company_id: str,
                         kinds: list[str]) -> dict[str, Art]:
        """The cited kinds re-fetched fresh, their text extracted in memory. Bodies go
        to the cache as every fetch does; no artifacts row is rewritten."""
        from engine.capture import capture as cap

        t = cap.load_target(conn, company_id)
        res = await cap.capture_company(t, self.rt, only=kinds, fresh=True,
                                        prior=cap.prior_rows(conn, company_id))
        out: dict[str, Art] = {}
        for kind, a in res.rows.items():
            text = structure = None
            body = self.cache.get_body(a.body_sha256) if (a.outcome == "ok" and
                                                          a.body_sha256) else None
            if body is not None:
                text, structure = extract_artifact(kind, a.url, body)
            out[kind] = Art(kind, a.url, a.outcome, text, structure)
        return out

    async def aclose(self) -> None:
        if self._rt is not None:
            await self._rt.aclose()


@dataclass
class RoleCheck:
    outcome: str  # open | switched | closed | unknown
    detail: str
    signal_id: str | None = None  # the new best signal when switched


def _signal(conn: sqlite3.Connection, signal_id: str | None) -> sqlite3.Row | None:
    if not signal_id:
        return None
    return conn.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()


def _boards_to_poll(conn: sqlite3.Connection, company_id: str,
                    best: sqlite3.Row) -> list[tuple[str, str]]:
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    seen[(best["source"], best["board_slug"].lower())] = (best["source"], best["board_slug"])
    for s in Q.signals_of(conn, company_id):
        r = _signal(conn, s.signal_id)
        if r is None or not s.gtm or r["source"] not in COLLECTORS or not r["board_slug"]:
            continue
        seen.setdefault((r["source"], r["board_slug"].lower()), (r["source"], r["board_slug"]))
    return list(seen.values())


async def check_role(conn: sqlite3.Connection, company: sqlite3.Row, checks: Checks, *,
                     today: date, role_checked: bool = False) -> RoleCheck:
    """Is the best signal's role still open? Polls only; writes nothing."""
    cid = company["company_id"]
    best = _signal(conn, company["best_signal_id"])
    if best is None:
        return RoleCheck("unknown", "the company has no best signal to recheck")
    if best["source"] not in COLLECTORS or not best["board_slug"]:
        if role_checked:
            return RoleCheck("open", f"a {best['source']} signal; open by --role-checked")
        return RoleCheck("unknown", f"the best signal comes from {best['source']}, which no "
                                    "collector re-polls; check the post is open today by hand "
                                    "and pass --role-checked")
    open_ids: set[str] = set()
    posted: dict[str, date | None] = {}
    best_board: ats.BoardResult | None = None
    for source, slug in _boards_to_poll(conn, cid, best):
        try:
            res = await checks.poll_board(source, slug)
        except ats.BoardFormatError as e:
            res = None
            if (source, slug.lower()) == (best["source"], best["board_slug"].lower()):
                return RoleCheck("unknown", f"{source}:{slug}: {e}")
        if res is None:
            continue
        if (source, slug.lower()) == (best["source"], best["board_slug"].lower()):
            best_board = res
        if res.outcome == "ok":
            for s in res.signals:
                open_ids.add(s.signal_id)
                posted[s.signal_id] = s.posted_at
    if best_board is None or best_board.outcome != "ok":
        what = best_board.outcome if best_board is not None else "not read"
        return RoleCheck("unknown", f"the board {best['source']}:{best['board_slug']} was "
                                    f"{what}; cannot tell whether the role is open")
    if best["signal_id"] in open_ids:
        return RoleCheck("open", f"{best['signal_id']} is open on {best['source']}:"
                                 f"{best['board_slug']}")
    alts = []
    for s in Q.signals_of(conn, cid):
        if s.signal_id == best["signal_id"] or s.signal_id not in open_ids or not s.gtm:
            continue
        if company["segment"] == US_INDIA_TEAM and s.india_located != 1:
            continue
        p = posted.get(s.signal_id) or (date.fromisoformat(s.posted_at[:10])
                                        if s.posted_at else None)
        if not is_fresh(p, date.fromisoformat(s.first_seen[:10]), today):
            continue
        alts.append(s)
    alt = Q.best_signal(alts)
    known = {s.signal_id for s in Q.signals_of(conn, cid)}
    unknown = len(open_ids - known)
    note = (f"; {unknown} open role(s) on the board are not in the database (te signal "
            "collect, classify, filter)" if unknown else "")
    if alt is not None:
        return RoleCheck("switched", f"{best['signal_id']} has closed; {alt.signal_id} "
                                     f"({alt.title}, {alt.offer_type}) is open{note}",
                         alt.signal_id)
    return RoleCheck("closed", f"{best['signal_id']} has closed and no other fresh GTM role "
                               f"is open{note}")


def cited_kinds(ctx: T.Context) -> list[str]:
    from engine.capture import bundle as B

    kinds = {c.artifact_kind for c in ctx.claims}
    return [k for k in B.KINDS if k in kinds] + sorted(kinds - set(B.KINDS))


async def check_evidence(conn: sqlite3.Connection, cache: Cache, ctx: T.Context,
                         checks: Checks) -> list[str]:
    """Re-fetch the cited kinds fresh and re-run faithfulness on the stored claims;
    the failing claims, described."""
    kinds = cited_kinds(ctx)
    arts = F.load_arts(conn, cache, ctx.company_id)
    arts.update(await checks.fresh_arts(conn, ctx.company_id, kinds))
    _, f = F.check_claims([c.model_dump() for c in ctx.claims], arts)
    out = []
    for x in f.failed:
        what = f"quote {x.quote!r}" if x.quote else "not found"
        out.append(f"claim [{x.index}] ({x.artifact_kind}, {what}): {x.category}")
    return out


def pull_back(conn: sqlite3.Connection, company_id: str) -> None:
    """Back to review: the teardown must be approved again (te queue approve)."""
    with conn:
        conn.execute("UPDATE teardowns SET review_state = ? WHERE company_id = ? AND "
                     "page_slug IS NOT NULL", (PENDING, company_id))


# -- draft ---------------------------------------------------------------------------------


Drafter = Callable[[str, str, str], str]  # (to, subject, body_text) -> draft id


def select(conn: sqlite3.Connection, *, company_id: str | None = None, pilot: bool = False,
           limit: int | None = None) -> list[str]:
    if company_id is not None:
        return [company_id]
    sql = "SELECT company_id FROM companies WHERE status = 'approved'"
    if pilot:
        sql += " AND pilot = 1"
    ids = [r["company_id"] for r in conn.execute(sql + " ORDER BY company_id")]
    return ids[:limit] if limit is not None else ids


def drafted_on(conn: sqlite3.Connection, day: date) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM touches WHERE drafted_at LIKE ?",
                            (day.isoformat() + "%",)).fetchone()[0])


def pace_warning(conn: sqlite3.Connection, ids: list[str], *, pilot: bool,
                 day: date) -> str | None:
    """A warning when today's drafts would pass the CONDUCT daily pace."""
    todo = sum(1 for i in ids if touch_done(touch_one(conn, i)) is None)
    have = drafted_on(conn, day)
    pace = PILOT_PACE if pilot else PACE
    if todo and have + todo > pace:
        return (f"{have} drafted today and {todo} more to draft: over the daily pace of "
                f"{pace} (CONDUCT Part 3{'; the pilot goes five a morning' if pilot else ''}). "
                "Send at most the pace each day; drafts can wait in Gmail.")
    return None


async def draft_company(conn: sqlite3.Connection, company_id: str, *, checks: Checks | None,
                        cache: Cache, drafter: Drafter | None,
                        teardowns: Path | None = None, sender_file: Path | None = None,
                        site: Path | None = None, dry_run: bool = False,
                        role_checked: bool = False, today: date | None = None,
                        now: datetime | None = None) -> Result:
    today = today or datetime.now(UTC).date()
    try:
        c = company_row(conn, company_id)
    except QueueError as e:
        return Result(company_id, "refused", str(e))
    from engine.outreach.bounces import stop_problem

    stopped = stop_problem(conn)
    if stopped:  # Step 74: two bounces in one day stop every draft until te queue resume
        return Result(company_id, "refused", stopped)
    status = Status(c["status"])
    t1 = touch_one(conn, company_id)
    done_why = touch_done(t1)
    if done_why:
        return Result(company_id, "unchanged", f"{done_why}; nothing to draft")
    if status is not Status.APPROVED:
        return Result(company_id, "refused", f"status {status}; only an approved teardown is "
                                             "drafted (te queue approve)")
    td = teardown_row(conn, company_id)
    if td is None or td["review_state"] != APPROVED:
        return Result(company_id, "refused", "the teardown is back in review; read it, then "
                                             "te queue approve " + company_id)
    contact = recipient(conn, company_id)
    sup = suppression_problem(conn, c, contact)
    if sup:
        return Result(company_id, "refused", f"{sup} (Step 72); nothing drafted")
    if contact is None or contact["channel"] not in ("email", "linkedin"):
        return Result(company_id, "refused", "no recipient with a channel (te people add)")
    channel = contact["channel"]
    if channel == "email" and not (contact["email"] or "").strip():
        return Result(company_id, "refused", "the recipient's channel is email but there is "
                                             "no address")
    name = first_name(contact["name"])
    if channel == "email" and not name:
        return Result(company_id, "refused", "the recipient has no name for {first_name}")
    try:
        r = prepare(conn, company_id, teardowns=teardowns, sender_file=sender_file, site=site)
    except (QueueError, T.TeardownError) as e:
        return Result(company_id, "refused", str(e))
    assert r.msg is not None
    sig = _signal(conn, c["best_signal_id"])
    offer = sig["offer_type"] if sig is not None else None
    if offer != r.doc.header.offer_type:
        return Result(company_id, "refused", f"the best signal's offer_type is {offer}; the "
                                             f"teardown says {r.doc.header.offer_type}")
    if offer == "candidate" and not (t1 is not None and t1["applied"] == 1):
        return Result(company_id, "refused", "a candidate signal: apply through the company's "
                                             "own form first; the candidate variant is refused "
                                             "until the application is recorded on touch 1 "
                                             "(CONDUCT Part 3; te queue applied, Step 75)")
    if channel == "email":
        filled = fill_name(r.msg, name or "")
        problems = message_problems(r.msg, r.doc, r.ctx, filled)
        if problems:  # the name pushed it over: say so without the name
            return Result(company_id, "refused", "with the recipient's name filled in: " +
                          "; ".join(problems))
        note = None
    else:
        note = linkedin_text(r.doc, r.msg.link)
        problems = linkedin_problems(note, r.msg.link)
        if problems:
            return Result(company_id, "refused", "the LinkedIn note: " + "; ".join(problems))

    kinds = cited_kinds(r.ctx)
    if dry_run:
        best = sig["signal_id"] if sig is not None else "-"
        lines = [f"would re-poll the board of {best} fresh",
                 f"would re-fetch fresh: {', '.join(kinds) or '(nothing cited)'}"]
        if channel == "email":
            lines += [f"would draft ({r.msg.variant}) to the recipient:", *preview(r.msg)]
        else:
            lines += ["would print the LinkedIn note:", note or ""]
        return Result(company_id, "dry_run", "local checks pass; no request, no draft, no write",
                      lines, r.warnings)

    assert checks is not None
    from engine.guard.budget import BudgetExceeded
    from engine.guard.http import GuardConfigError

    try:
        role = await check_role(conn, c, checks, today=today, role_checked=role_checked)
    except (GuardConfigError, BudgetExceeded) as e:
        return Result(company_id, "refused", f"the role recheck could not run: {e}")
    if role.outcome == "unknown":
        return Result(company_id, "refused", f"role recheck: {role.detail}; nothing changed")
    if role.outcome == "closed":
        states.drop(company_id, STALE_REASON, STALE_STAGE, conn)
        return Result(company_id, "dropped", f"{STALE_REASON}: {role.detail}",
                      [f"delete site/{r.slug}/ before the next deploy"])
    if role.outcome == "switched":
        with conn:
            conn.execute("UPDATE companies SET best_signal_id = ?, "
                         "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                         "WHERE company_id = ?", (role.signal_id, company_id))
        pull_back(conn, company_id)
        return Result(company_id, "switched", role.detail, [
            "the page and the email name the closed role: the teardown is back in review.",
            f"re-draft against the new role: edit the header (role, offer_type), why_now and "
            f"the email block; re-capture the job post (te capture --recheck {company_id}) and "
            "re-judge if a claim cites it; te teardown check, te teardown render, deploy, "
            f"te queue approve {company_id}, then te queue draft again."])
    try:
        failing = await check_evidence(conn, cache, r.ctx, checks)
    except (GuardConfigError, BudgetExceeded) as e:
        return Result(company_id, "refused", f"the evidence recheck could not run: {e}")
    except Exception as e:  # capture refusing the company, say
        return Result(company_id, "refused", f"the evidence recheck failed: "
                                             f"{type(e).__name__}: {e}")
    if failing:
        pull_back(conn, company_id)
        return Result(company_id, "refused", "evidence recheck: a cited claim no longer holds; "
                                             "the teardown is back in review", failing)

    when = now or now_utc()
    if channel == "linkedin":
        _write_touch(conn, company_id, t1, channel="linkedin", draft_id=None, when=when)
        return Result(company_id, "linkedin", "no draft: send this by hand on LinkedIn, then "
                                              f"te queue sent {company_id}", [note or ""],
                      r.warnings)
    assert drafter is not None
    from engine.guard.gmail import GmailError

    try:
        draft_id = drafter(contact["email"].strip(), r.msg.subject, fill_name(r.msg, name or ""))
    except GmailError as e:
        return Result(company_id, "refused", f"Gmail: {e}")
    _write_touch(conn, company_id, t1, channel="email", draft_id=draft_id, when=when)
    return Result(company_id, "drafted", f"Gmail draft {draft_id} ({r.msg.variant}); read it "
                                         f"and send it by hand, then te queue sent {company_id}",
                  [], r.warnings)


def _write_touch(conn: sqlite3.Connection, company_id: str, t1: sqlite3.Row | None, *,
                 channel: str, draft_id: str | None, when: datetime) -> None:
    from engine import db
    from engine.models import Touch

    with conn:
        if t1 is None:
            db.insert(conn, Touch(company_id=company_id, touch_no=1, channel=channel,
                                  draft_id=draft_id, drafted_at=when, status="drafted"))
        else:  # a touch 1 holding only the application flag (Step 75)
            conn.execute("UPDATE touches SET channel = ?, draft_id = ?, drafted_at = ?, "
                         "status = 'drafted' WHERE touch_id = ?",
                         (channel, draft_id, stamp(when), t1["touch_id"]))


def default_drafter() -> Drafter:
    from engine.guard import gmail

    return gmail.create_draft
