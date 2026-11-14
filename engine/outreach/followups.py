"""Follow-ups by hand (docs/PLAN.md Step 71; protocol/TEARDOWN_SPEC.md v1.2
"Follow-ups"; protocol/CONDUCT.md Part 3, "Touches").

    te queue followups [--today YYYY-MM-DD] [--company <id>] [--pilot] [--limit n]
    te queue followup-sent <company_id> --touch 2|3 [--channel email-reply|linkedin] [--at ISO]

Three touches at most, then stop for good. The engine has no mail-reading
permission and makes no draft for a follow-up: `followups` prints the text and
the person presses Reply in the touch-1 thread in Gmail and pastes it (or, on
LinkedIn, sends it by hand; a connection request with no pitch may stand in for
touch 2). Then `followup-sent` records it.

When a thread is due (day 0 is the UTC date touch 1 was sent):
  touch 2   from day 4, when touch 1 is sent and touch 2 is not.
  touch 3   from day 9, and at least 5 days after touch 2 was sent (the spec's
            day 4 to day 9 spacing kept when touch 2 went late); touch 2 sent.
A thread is skipped, with the reason, when the company is dropped, replied,
in a meeting or closed; a touch has a reply or bounced; or the recipient's
address or domain, or the company's domain, is suppressed (Step 72). A thread
with three touches sent is never listed again, and `followup-sent` refuses a
fourth: the touch count enforces the limit. Recording touch 3 leaves the
company at `sent`, never `closed`: status only moves forward, and a reply that
arrives after touch 3 must still be recordable as `replied` (Steps 77-78).

The words:
  touch 2   one new observation from a secondary gap, two sentences. Each
            sentence is the text of one passing claim (kept by faithfulness,
            verified, not not_supported) whose artifact kind is evidence for
            that secondary gap (GAP_KINDS, from GAPS.md "Evidence required"),
            not one the touch-1 email cited; at least one is a found claim
            (absence alone never names a gap). Nothing is written that a claim
            does not say. With no such pair, a fallback line asks the person
            to write it; it is marked, never pasteable text.
  touch 3   one line offering the full proof slice (the email's slice name) as
            a file, and a clear close.
Both greet {first_name} and close with the sender block and the opt-out line
of the touch-1 email (CONDUCT: an opt-out in every email). {first_name} is a
placeholder in everything this module returns; the CLI fills it only when
printing to a terminal, never into a file or a log.

No network and no model call.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from engine.build import sender as S
from engine.build import teardown as T
from engine.outreach import draft as Q
from engine.states import Status

FOLLOWUPS_VERSION = "followups-1.0"
MAX_TOUCHES = 3  # CONDUCT Part 3
TOUCH2_DAY, TOUCH3_DAY = 4, 9
TOUCH3_AFTER_TOUCH2 = TOUCH3_DAY - TOUCH2_DAY
CHANNELS = ("email-reply", "linkedin")
SKIP_STATUSES = (Status.DROPPED, Status.REPLIED, Status.MEETING, Status.CLOSED)
OPT_OUT_LINE = 'Not relevant? Reply "no" and I will not write again.'

# The artifact kinds that are evidence for each gap (protocol/GAPS.md v1.2,
# "Evidence required"). A claim traces to a secondary gap through its kind.
GAP_KINDS: dict[str, frozenset[str]] = {
    "G1": frozenset({"job_post", "homepage", "product", "customers"}),
    "G2": frozenset({"pricing", "demo_form", "homepage", "contact"}),
    "G3": frozenset({"job_post", "ats_board", "careers", "homepage"}),
    "G4": frozenset({"job_post", "customers", "about"}),
    "G5": frozenset({"demo_form", "contact", "ats_board", "careers", "job_post"}),
    "G6": frozenset({"dns", "homepage"}),
}

FALLBACK_2 = ("[no evidenced secondary gap: write touch 2 yourself, one new observation in two "
              "sentences, each tracing to a passing claim; do not paste this line]")
FALLBACK_3 = ("[no proof slice named in the teardown's email block: write one line offering it "
              "as a file, and a clear close; do not paste this line]")


def is_terminal() -> bool:
    """True when stdout is a terminal: the only place {first_name} is filled."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# -- the words -------------------------------------------------------------------------------


@dataclass
class Observation:
    gap: str
    claims: list[int]
    sentences: list[str]


def _usable(text: str) -> str | None:
    t = " ".join(text.split())
    if (not t or t[-1] not in ".!?" or len(T.sentences(t)) != 1 or T.banned_in(t)
            or T.superlatives_in(t) or Q.links_in(t) or "{" in t):
        return None
    return t


def secondary_observation(ctx: T.Context, exclude: set[int]) -> Observation | None:
    """Two sentences from one secondary gap, each a passing claim's text; or None."""
    d = ctx.diagnosis
    if d is None or not d["attempted"]:
        return None
    try:
        secondary = [g for g in json.loads(d["secondary"] or "[]") if g != d["gap_type"]]
    except (TypeError, ValueError):
        return None
    passing = [i for i in range(len(ctx.claims)) if T.claim_problem(ctx, i) is None]
    for gap in secondary:
        kinds = GAP_KINDS.get(gap, frozenset())
        found: list[tuple[int, str]] = []
        absent: list[tuple[int, str]] = []
        seen: set[str] = set()
        for i in passing:
            c = ctx.claims[i]
            if i in exclude or c.artifact_kind not in kinds:
                continue
            t = _usable(c.text)
            if t is None or t.lower() in seen:
                continue
            seen.add(t.lower())
            (found if c.found else absent).append((i, t))
        if not found:
            continue  # absence alone never names a gap
        first = found[0]
        rest = found[1:] + absent
        k1 = ctx.claims[first[0]].artifact_kind
        rest.sort(key=lambda x: ctx.claims[x[0]].artifact_kind == k1)  # another kind first
        if not rest:
            continue
        pair = sorted([first, rest[0]])
        return Observation(gap, [i for i, _ in pair], [t for _, t in pair])
    return None


def footer(snd: S.Sender) -> str:
    return (f"{snd.name.strip()}\n{snd.identity.strip()} · {snd.postal_address.strip()}\n"
            f"{OPT_OUT_LINE}")


def touch2_text(obs: Observation | None, snd: S.Sender) -> tuple[str, bool]:
    """(text, fallback). The text greets {first_name}; a fallback is not pasteable."""
    if obs is None:
        return FALLBACK_2, True
    return f"Hi {{first_name}},\n\n{' '.join(obs.sentences)}\n\n{footer(snd)}", False


def touch3_text(slice_name: str | None, snd: S.Sender) -> tuple[str, bool]:
    name = " ".join((slice_name or "").split()).rstrip(".")
    if not name or T.TODO in name:
        return FALLBACK_3, True
    line = (f"If it would help, I can send {name} in full, as a file. This is my last note, and "
            "I will not write again unless you reply.")
    return f"Hi {{first_name}},\n\n{line}\n\n{footer(snd)}", False


def text_problems(text: str) -> list[str]:
    out = [f"banned phrase {p}" for p in T.banned_in(text)]
    out += [f"superlative {s!r}" for s in T.superlatives_in(text)]
    if Q.links_in(text):
        out.append("a follow-up carries no link; the thread has it")
    left = {m for m in Q._PLACEHOLDER.findall(text) if m != "first_name"}
    out += [f"{{{m}}} is left unfilled" for m in sorted(left)]
    return out


def fill_name(text: str, name: str | None) -> str:
    return text.replace("{first_name}", name) if name else text


def recipient_first_name(conn: sqlite3.Connection, company_id: str) -> str | None:
    c = Q.recipient(conn, company_id)
    return Q.first_name(c["name"]) if c is not None else None


# -- which threads are due -------------------------------------------------------------------


def _day(ts: str | None) -> date | None:
    return date.fromisoformat(ts[:10]) if ts else None


def touches_of(conn: sqlite3.Connection, company_id: str) -> dict[int, sqlite3.Row]:
    return {r["touch_no"]: r for r in conn.execute(
        "SELECT * FROM touches WHERE company_id = ? ORDER BY touch_no", (company_id,))}


def skip_reason(conn: sqlite3.Connection, company: sqlite3.Row,
                touches: dict[int, sqlite3.Row]) -> str | None:
    """Why no further touch may go to this company, or None."""
    st = Status(company["status"])
    if st in SKIP_STATUSES:
        return f"status {st}" + (f" ({company['drop_reason']})" if st is Status.DROPPED else "")
    for t in touches.values():
        if t["bounced_on"] is not None or t["outcome"] == "bounced":
            return f"touch {t['touch_no']} bounced"
        if t["reply_at"] is not None or t["outcome"] in ("replied", "opted_out"):
            return f"a reply on touch {t['touch_no']}"
    sup = Q.suppression_problem(conn, company, Q.recipient(conn, company["company_id"]))
    if sup:
        return sup
    sent = [n for n, t in touches.items() if t["status"] == "sent"]
    if len(sent) >= MAX_TOUCHES or MAX_TOUCHES in sent:
        return f"{MAX_TOUCHES} touches sent; stop for good"
    return None


def next_touch(touches: dict[int, sqlite3.Row]) -> tuple[int, date] | None:
    """(the next touch number, the day it is due), or None when touch 1 is not sent."""
    t1 = touches.get(1)
    if t1 is None or t1["status"] != "sent" or not t1["sent_at"]:
        return None
    d0 = _day(t1["sent_at"])
    assert d0 is not None
    t2 = touches.get(2)
    if t2 is None or t2["status"] != "sent":
        return 2, d0 + timedelta(days=TOUCH2_DAY)
    d2 = _day(t2["sent_at"]) or d0 + timedelta(days=TOUCH2_DAY)
    return 3, max(d0 + timedelta(days=TOUCH3_DAY), d2 + timedelta(days=TOUCH3_AFTER_TOUCH2))


@dataclass
class FollowUp:
    company_id: str
    touch_no: int
    day: int  # days since touch 1 was sent
    due_on: date
    thread_channel: str  # touch 1's channel: reply in that thread
    text: str = ""  # {first_name} unfilled
    fallback: bool = False
    gap: str | None = None
    claims: list[int] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass
class Listing:
    due: list[FollowUp] = field(default_factory=list)
    upcoming: list[tuple[str, int, date]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def select(conn: sqlite3.Connection, *, company_id: str | None = None, pilot: bool = False,
           limit: int | None = None) -> list[str]:
    """Companies with a sent touch 1."""
    if company_id is not None:
        return [company_id]
    sql = ("SELECT DISTINCT c.company_id FROM companies c JOIN touches t ON t.company_id = "
           "c.company_id WHERE t.touch_no = 1 AND t.status = 'sent'")
    if pilot:
        sql += " AND c.pilot = 1"
    ids = [r[0] for r in conn.execute(sql + " ORDER BY c.company_id")]
    return ids[:limit] if limit is not None else ids


def words_for(conn: sqlite3.Connection, f: FollowUp, *, teardowns: Path | None = None,
              sender_file: Path | None = None) -> None:
    """Fill f.text (and gap, claims, problems) from the stored diagnosis and the file."""
    try:
        snd = S.load(sender_file)
    except S.SenderError as e:
        f.fallback, f.problems = True, [str(e)]
        return
    doc: T.TeardownDoc | None = None
    try:
        _, doc = T.load(T.file_for(f.company_id, teardowns))
    except T.TeardownError as e:
        f.problems.append(f"the teardown file: {e}")
    if f.touch_no == 2:
        ctx = T.load_context(conn, f.company_id)
        used = set(doc.email.claims) if doc is not None and doc.email is not None else set()
        obs = secondary_observation(ctx, used)
        f.text, f.fallback = touch2_text(obs, snd)
        if obs is not None:
            f.gap, f.claims = obs.gap, obs.claims
    else:
        name = doc.email.slice if doc is not None and doc.email is not None else None
        f.text, f.fallback = touch3_text(name, snd)
    if not f.fallback:
        bad = text_problems(f.text)
        if bad:
            f.problems += bad
            f.text, f.fallback = (FALLBACK_2 if f.touch_no == 2 else FALLBACK_3), True


def followups(conn: sqlite3.Connection, today: date, *, company_id: str | None = None,
              pilot: bool = False, limit: int | None = None, teardowns: Path | None = None,
              sender_file: Path | None = None) -> Listing:
    out = Listing()
    for cid in select(conn, company_id=company_id, pilot=pilot, limit=limit):
        c = Q.company_row(conn, cid)
        ts = touches_of(conn, cid)
        why = skip_reason(conn, c, ts)
        if why:
            out.skipped.append((cid, why))
            continue
        nt = next_touch(ts)
        if nt is None:
            out.skipped.append((cid, "touch 1 is not recorded sent (te queue sent)"))
            continue
        n, due_on = nt
        if today < due_on:
            out.upcoming.append((cid, n, due_on))
            continue
        d0 = _day(ts[1]["sent_at"])
        assert d0 is not None
        f = FollowUp(cid, n, (today - d0).days, due_on, ts[1]["channel"])
        words_for(conn, f, teardowns=teardowns, sender_file=sender_file)
        out.due.append(f)
    return out


# -- recording a sent follow-up --------------------------------------------------------------


def followup_sent(conn: sqlite3.Connection, company_id: str, touch_no: int, *,
                  channel: str | None = None, at: datetime | None = None,
                  dry_run: bool = False) -> Q.Result:
    if touch_no not in (2, 3):
        return Q.Result(company_id, "refused", f"touch {touch_no}: follow-ups are touches 2 and "
                                               f"3; there is no fourth message")
    c = Q.company_row(conn, company_id)
    ts = touches_of(conn, company_id)
    t = ts.get(touch_no)
    if t is not None and t["status"] == "sent":
        return Q.Result(company_id, "unchanged", f"touch {touch_no} already recorded sent at "
                                                 f"{t['sent_at']}")
    why = skip_reason(conn, c, ts)
    if why:
        return Q.Result(company_id, "refused", f"{why}; no follow-up goes to this company")
    nt = next_touch(ts)
    if nt is None:
        return Q.Result(company_id, "refused", "touch 1 is not recorded sent (te queue sent)")
    n, due_on = nt
    if n != touch_no:
        return Q.Result(company_id, "refused", f"the next touch is {n}, not {touch_no}")
    when = at or Q.now_utc()
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if when > Q.now_utc():
        return Q.Result(company_id, "refused", "the send time is in the future")
    if when.astimezone(UTC).date() < due_on:
        return Q.Result(company_id, "refused", f"touch {touch_no} is not due until {due_on}")
    ch = channel or ("linkedin" if ts[1]["channel"] == "linkedin" else "email-reply")
    if ch not in CHANNELS:
        return Q.Result(company_id, "refused", f"channel {ch!r}; one of {', '.join(CHANNELS)}")
    if dry_run:
        return Q.Result(company_id, "dry_run", f"would record touch {touch_no} ({ch}) sent at "
                                               f"{Q.stamp(when)}")
    with conn:
        conn.execute("INSERT INTO touches (company_id, touch_no, channel, sent_at, status) "
                     "VALUES (?, ?, ?, ?, 'sent')", (company_id, touch_no, ch, Q.stamp(when)))
    lines: list[str] = []
    if touch_no == MAX_TOUCHES:
        lines.append("three touches sent: no fourth message, on any channel (the company "
                     "stays at sent, so a late reply can still be recorded)")
    return Q.Result(company_id, "sent", f"touch {touch_no} ({ch}) sent at {Q.stamp(when)}",
                    lines)
