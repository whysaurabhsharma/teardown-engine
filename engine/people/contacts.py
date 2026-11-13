"""Record, list and check recipients (docs/PLAN.md Steps 41-43, Gate 3.3).

    te people add <company_id> --name .. --title .. --source-url URL
                  [--email ADDR --confidence high|medium|low
                   --email-source hunter|apollo|pattern|public|manual]
                  [--linkedin-url URL]
    te people drop <company_id>        drop as no_recipient (people stage)
    te people list [--pilot]           no names or addresses unless asked
    te people check [--pilot]          Gate 3.3

One recipient per company: a contacts row is added once; a second add with
the same values changes nothing, with different values it is refused unless
replace is set. The person found the recipient by hand; this only checks and
stores what was typed:

  refused  a recruiter, talent or HR title (rule.is_recruiter_title); no
           source URL, or one that is not http(s); an address that is not
           an address; a confidence without an address, or an address
           without a confidence and a source; a `pattern` address above
           `low` that was not checked with Hunter's verifier; no channel
           (no address at medium or better, and no LinkedIn URL); an unknown
           or dropped company.
  needs force  an address whose registrable domain is not the company's.
  warned   a title that does not look like the band's target; an unknown
           headcount.

Channel (Step 43): `email` at high or medium confidence, else `linkedin` when
a LinkedIn URL is given, else none (drop as no_recipient).

Messages never repeat a name, an address or a URL that was typed: contacts
values stay out of anything that could be captured by a log.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from engine import states
from engine.models import Contact
from engine.people.rule import (
    RECRUITER_NOTE,
    RecipientRule,
    is_recruiter_title,
    recipient_rule,
    title_fits,
)
from engine.resolve.domain import registrable_domain
from engine.states import PIPELINE, Status

DROP_REASON = "no_recipient"
DROP_STAGE = "people"
CONFIDENCES = ("high", "medium", "low")
EMAIL_SOURCES = ("hunter", "apollo", "pattern", "public", "manual")
EMAIL_CHANNEL_CONFIDENCE = frozenset({"high", "medium"})
# 0001's email_confidence is REAL 0..1; contacts.confidence (0012) is the
# authoritative word and this map fills the number from it.
CONFIDENCE_VALUE = {"high": 0.9, "medium": 0.6, "low": 0.3}
GATE_SIZE = 12  # the pilot twelve (engine/resolve/pilot.py TOTAL)

_EMAIL = re.compile(
    r"^(?!\.)(?!.*\.\.)[A-Za-z0-9._%+'-]{1,64}(?<!\.)@"
    r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
_FROM_QUALIFIED = frozenset(s.value for s in PIPELINE[PIPELINE.index(Status.QUALIFIED):])
_COLUMNS = ("name", "title", "source_url", "email", "confidence", "email_confidence",
            "email_source", "linkedin_url", "channel")


class PeopleError(ValueError):
    """A refusal. The message never contains a typed name, address or URL."""


class NeedsForce(PeopleError):
    """A check a person may override with --force."""


# --- small checks ------------------------------------------------------------------


def valid_email(addr: str) -> bool:
    return bool(_EMAIL.match(addr))


def email_domain(addr: str) -> str:
    return addr.rsplit("@", 1)[1].lower()


def _http_url(url: str) -> bool:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname) and " " not in url.strip()


def _linkedin_url(url: str) -> bool:
    return (_http_url(url) and urlsplit(url.strip()).scheme == "https"
            and registrable_domain(url) == "linkedin.com")


def channel_for(confidence: str | None, linkedin_url: str | None) -> str | None:
    """Step 43: email at high/medium, else linkedin with a URL, else None."""
    if confidence in EMAIL_CHANNEL_CONFIDENCE:
        return "email"
    if linkedin_url:
        return "linkedin"
    return None


# --- company ---------------------------------------------------------------------


@dataclass(frozen=True)
class CompanyRow:
    company_id: str
    name: str
    domain: str | None
    status: str
    pilot: int
    hand: int
    headcount: int | None
    drop_reason: str | None
    best_signal_id: str | None

    @property
    def rule(self) -> RecipientRule:
        return recipient_rule(self.headcount)


_COMPANY_SQL = ("SELECT company_id, name, domain, status, pilot, hand, headcount, drop_reason, "
                "best_signal_id FROM companies")


def company(conn: sqlite3.Connection, company_id: str) -> CompanyRow:
    r = conn.execute(_COMPANY_SQL + " WHERE company_id = ?", (company_id,)).fetchone()
    if r is None:
        raise PeopleError(f"no company {company_id!r}")
    return CompanyRow(**{k: r[k] for k in r.keys()})


def recipient(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    """The company's contacts row (the lowest contact_id if, unexpectedly, several)."""
    return conn.execute("SELECT * FROM contacts WHERE company_id = ? ORDER BY contact_id LIMIT 1",
                        (company_id,)).fetchone()


def select(conn: sqlite3.Connection, *, pilot: bool = False, company_id: str | None = None,
           limit: int | None = None) -> list[CompanyRow]:
    """Companies the People step is for.

    --company: that one. --pilot: every pilot company (pilot = 1), dropped ones
    included so a gate sees them. Default: companies at qualified or later, not
    dropped, not hand-made (the Phase 0 six were sent by hand already).
    """
    if company_id is not None:
        return [company(conn, company_id)]
    if pilot:
        rows = conn.execute(_COMPANY_SQL + " WHERE pilot = 1 ORDER BY company_id").fetchall()
    else:
        rows = [r for r in conn.execute(_COMPANY_SQL + " WHERE hand = 0 ORDER BY company_id")
                if r["status"] in _FROM_QUALIFIED]
    out = [CompanyRow(**{k: r[k] for k in r.keys()}) for r in rows]
    return out[:limit] if limit is not None else out


# --- add ---------------------------------------------------------------------------


@dataclass
class AddPlan:
    company: CompanyRow
    row: Contact
    warnings: list[str] = field(default_factory=list)
    existing_id: int | None = None
    unchanged: bool = False


def plan_add(
    conn: sqlite3.Connection,
    company_id: str,
    *,
    name: str,
    title: str,
    source_url: str,
    email: str | None = None,
    confidence: str | None = None,
    email_source: str | None = None,
    linkedin_url: str | None = None,
    verified: bool = False,
    force: bool = False,
) -> AddPlan:
    """Check one typed recipient and build its contacts row. Writes nothing."""
    c = company(conn, company_id)
    if c.status == Status.DROPPED:
        raise PeopleError(f"{company_id}: dropped ({c.drop_reason}); no recipient is recorded")
    name = " ".join((name or "").split())
    title = " ".join((title or "").split())
    source_url = (source_url or "").strip()
    email = (email or "").strip() or None
    linkedin_url = (linkedin_url or "").strip() or None
    if not name:
        raise PeopleError("a recipient needs a name (--name)")
    if not title:
        raise PeopleError("a recipient needs a title (--title)")
    if is_recruiter_title(title):
        raise PeopleError(f"refused: the title reads as recruiting, talent or HR. {RECRUITER_NOTE}")
    if not source_url:
        raise PeopleError("a recipient needs the URL where they were found (--source-url)")
    if not _http_url(source_url):
        raise PeopleError("--source-url must be an http(s) URL")
    if linkedin_url is not None and not _linkedin_url(linkedin_url):
        raise PeopleError("--linkedin-url must be an https URL on linkedin.com")

    warnings: list[str] = []
    if email is None:
        if confidence is not None or email_source is not None:
            raise PeopleError("--confidence and --email-source describe an address; give --email")
    else:
        if not valid_email(email):
            raise PeopleError("--email is not a valid address")
        if confidence not in CONFIDENCES:
            raise PeopleError("an address needs --confidence high, medium or low")
        if email_source not in EMAIL_SOURCES:
            raise PeopleError("an address needs --email-source "
                              + ", ".join(EMAIL_SOURCES))
        if email_source == "pattern" and confidence != "low" and not verified:
            raise PeopleError("an address inferred from a pattern is `low` unless Hunter's "
                              "verifier checked it (--verified)")
        want = registrable_domain(c.domain) if c.domain else None
        got = registrable_domain(email_domain(email))
        if want is None or got != want:
            msg = (f"the address's domain ({got or email_domain(email)}) is not the company's "
                   f"({want or 'none recorded'})")
            if not force:
                raise NeedsForce(msg + "; check it, then re-run with --force")
            warnings.append(msg + "; stored because --force was given")

    channel = channel_for(confidence if email else None, linkedin_url)
    if channel is None:
        raise PeopleError("no channel: give an address at medium confidence or better, or "
                          f"--linkedin-url; with neither, `te people drop {company_id}` "
                          f"drops it as {DROP_REASON}")

    rule = c.rule
    if not rule.known:
        warnings.append("headcount unknown, so the recipient rule could not be checked: "
                        + (rule.ask or ""))
    elif title_fits(rule, title) is False:
        warnings.append(f"the title does not look like the rule's target for "
                        f"{rule.headcount} people ({rule.target}); stored as typed")
    elif title_fits(rule, title) is None:
        warnings.append(f"check by hand that this is {rule.target}")

    row = Contact(
        company_id=company_id, name=name, title=title, source_url=source_url, email=email,
        confidence=confidence if email else None,
        email_confidence=CONFIDENCE_VALUE[confidence] if email and confidence else None,
        email_source=email_source if email else None, linkedin_url=linkedin_url,
        channel=channel,
    )
    plan = AddPlan(c, row, warnings)
    old = recipient(conn, company_id)
    if old is not None:
        plan.existing_id = int(old["contact_id"])
        new = row.model_dump()
        plan.unchanged = all(old[k] == new[k] for k in _COLUMNS)
    return plan


def write(conn: sqlite3.Connection, plan: AddPlan, *, replace: bool = False) -> str:
    """Store a planned row. Returns 'added', 'replaced' or 'unchanged'."""
    if plan.unchanged:
        return "unchanged"
    data = plan.row.model_dump()
    with conn:
        if plan.existing_id is not None:
            if not replace:
                raise PeopleError(f"{plan.company.company_id} already has a recipient with "
                                  "different values; re-run with --replace to overwrite it")
            sets = ", ".join(f"{k} = ?" for k in _COLUMNS)
            conn.execute(f"UPDATE contacts SET {sets} WHERE contact_id = ?",
                         (*(data[k] for k in _COLUMNS), plan.existing_id))
            return "replaced"
        cols = ("company_id", *_COLUMNS)
        conn.execute(f"INSERT INTO contacts ({', '.join(cols)}) VALUES "
                     f"({', '.join('?' * len(cols))})", tuple(data[k] for k in cols))
        return "added"


# --- drop --------------------------------------------------------------------------


def drop(conn: sqlite3.Connection, company_id: str, *, dry_run: bool = False) -> bool:
    """Drop a company as no_recipient at the people stage. True if it changed."""
    c = company(conn, company_id)
    if c.status == Status.DROPPED:
        stage = conn.execute("SELECT drop_stage FROM companies WHERE company_id = ?",
                             (company_id,)).fetchone()[0]
        if (c.drop_reason, stage) == (DROP_REASON, DROP_STAGE):
            return False  # a re-run changes nothing
        raise PeopleError(f"{company_id}: already dropped ({c.drop_reason})")
    if dry_run:
        return True
    return states.drop(company_id, DROP_REASON, DROP_STAGE, conn=conn)


# --- list and gate -----------------------------------------------------------------


@dataclass(frozen=True)
class Overview:
    company: CompanyRow
    title: str | None
    channel: str | None
    confidence: str | None
    email_source: str | None
    has_name: bool
    has_source: bool
    has_email: bool
    has_linkedin: bool
    name: str | None  # for --show-contacts only
    email: str | None  # for --show-contacts only


def overview(conn: sqlite3.Connection, companies: list[CompanyRow]) -> list[Overview]:
    out = []
    for c in companies:
        r = recipient(conn, c.company_id)
        get = (lambda k: r[k]) if r is not None else (lambda k: None)
        out.append(Overview(
            c, get("title"), get("channel"), get("confidence"), get("email_source"),
            bool((get("name") or "").strip()), bool((get("source_url") or "").strip()),
            bool((get("email") or "").strip()), bool((get("linkedin_url") or "").strip()),
            get("name"), get("email"),
        ))
    return out


def gate_problem(o: Overview) -> str | None:
    """Why one company fails Gate 3.3, or None when it passes."""
    c = o.company
    if c.status == Status.DROPPED:
        return f"dropped ({c.drop_reason})"
    if o.title is None and not o.has_name:
        return "no recipient recorded"
    if not o.has_name:
        return "recipient has no name"
    if not o.has_source:
        return "recipient has no source URL"
    if o.title and is_recruiter_title(o.title):
        return "recipient's title reads as recruiting, talent or HR"
    if o.channel == "email" and o.has_email and o.confidence in EMAIL_CHANNEL_CONFIDENCE:
        return None
    if o.channel == "linkedin" and o.has_linkedin:
        return None
    return "no address at medium confidence or better, and no LinkedIn channel"
