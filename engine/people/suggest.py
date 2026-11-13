"""Where to look for the recipient: `te people suggest <company_id>` (Steps 40-41).

Prints the recipient rule for the company's headcount, then the places to
look, in the plan's order:

1. the company's team or about page: the captured `about` artifact's URL (or
   the homepage's, when no about page was captured);
2. any "reports to" line in the job post: found in the job_post artifact's
   extracted text (or, without one, the signal's own description text) and
   printed as written. It is public job text; nothing is stored;
3. press and funding announcements, and 4. your own LinkedIn search: both by
   hand. LinkedIn is never fetched or automated.

Reads the database and the cache only: no request, no model call. Names on
pages are never extracted or stored here; the person reads the page and types
the recipient with `te people add`.
"""

from __future__ import annotations

import re
import sqlite3

from engine.capture import extract_text as X
from engine.guard.cache import Cache
from engine.people.contacts import company, recipient
from engine.people.rule import RECRUITER_NOTE, RULE_VERSION

REPORTS_TO = re.compile(
    r"\breport(?:s|ing)?\s+(?:directly\s+|in\s+)?(?:in)?to\b|\breporting\s+line\b",
    re.IGNORECASE,
)
SNIPPET = 220  # characters around a match on a long line
MAX_LINES = 5


def _snippets(text: str) -> list[str]:
    out: list[str] = []
    for line in text.split("\n"):
        line = " ".join(line.split())
        m = REPORTS_TO.search(line)
        if not m:
            continue
        if len(line) > SNIPPET:
            start = max(0, m.start() - SNIPPET // 3)
            end = min(len(line), start + SNIPPET)
            line = ("..." if start else "") + line[start:end] + ("..." if end < len(line) else "")
        if line not in out:
            out.append(line)
    return out


def _artifact(conn: sqlite3.Connection, company_id: str, kind: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM artifacts WHERE company_id = ? AND kind = ?",
                        (company_id, kind)).fetchone()


def reports_to_lines(conn: sqlite3.Connection, cache: Cache, company_id: str
                     ) -> tuple[str, list[str]]:
    """(where the text came from, matching lines) for the company's job post."""
    c = company(conn, company_id)
    job = _artifact(conn, company_id, "job_post")
    if job is not None and job["outcome"] == "ok":
        text = X.stored_text(cache, job)
        if text is not None:
            return f"job_post {job['url']}", _snippets(text)
    if c.best_signal_id:
        s = conn.execute("SELECT url, description_text FROM signals WHERE signal_id = ?",
                         (c.best_signal_id,)).fetchone()
        if s is not None and s["description_text"]:
            return f"signal description {s['url'] or ''}".rstrip(), _snippets(s["description_text"])
    return "", []


def suggest_lines(conn: sqlite3.Connection, cache: Cache, company_id: str) -> list[str]:
    c = company(conn, company_id)
    rule = c.rule
    out = [f"{c.company_id}  {c.name}  {c.domain or '-'}  status {c.status}  "
           f"headcount {c.headcount if c.headcount is not None else 'unknown'}"]
    if c.status == "dropped":
        out.append(f"dropped ({c.drop_reason}): no recipient is needed")
    out.append(f"recipient rule ({RULE_VERSION}): {rule.target}")
    if rule.ask:
        out.append(f"  ASK: {rule.ask}")
    out.append(f"  {RECRUITER_NOTE}")
    r = recipient(conn, company_id)
    if r is not None:
        out.append(f"recorded: title {r['title'] or '-'}, channel {r['channel'] or '-'}, "
                   f"confidence {r['confidence'] or '-'} (name and address not shown)")
    else:
        out.append("recorded: none")

    out.append("")
    out.append("where to look, in order:")
    about = _artifact(conn, company_id, "about")
    home = _artifact(conn, company_id, "homepage")
    out.append("1. the team or about page")
    if about is not None and about["outcome"] == "ok":
        out.append(f"   {about['url']}")
    else:
        if about is not None:
            out.append(f"   about page {about['outcome']}: {about['url']}")
        else:
            out.append("   no about page captured")
        if home is not None:
            out.append(f"   look from the homepage: {home['url']}")
        elif c.domain:
            out.append(f"   look from the homepage: https://{c.domain}/")
    out.append('2. a "reports to" line in the job post')
    source, lines = reports_to_lines(conn, cache, company_id)
    if not source:
        out.append("   no job post text captured or extracted")
    elif not lines:
        out.append(f"   none in {source}")
    else:
        out.append(f"   from {source}:")
        out += [f"   | {line}" for line in lines[:MAX_LINES]]
        if len(lines) > MAX_LINES:
            out.append(f"   | ... {len(lines) - MAX_LINES} more")
    out.append("3. press and funding announcements: by hand")
    out.append("4. your own LinkedIn search: by hand, human-paced (about fifteen a day); "
               "never automated")
    out.append("")
    out.append(f"record what you find: te people add {company_id} --name \"...\" --title \"...\" "
               "--source-url URL [--email ADDR --confidence high|medium|low "
               "--email-source hunter|apollo|pattern|public|manual] [--linkedin-url URL]")
    out.append(f"nobody to reach: te people drop {company_id}")
    return out
