"""What must never appear in anything published (docs/PLAN.md Steps 84, 87).

The values come from the database: company names (companies.name and the raw
names on their signals), company domains, contacts (name, each name part of
3+ characters, email, title) and the free text typed with replies, meetings
and closes. `te report anonymise-check` scans a write-up for them and
`te verify` scans the tracked files and the git history.

Matching:
    company_name, contact_name, contact_title
                    case-insensitive, whole word (no letter, digit or _ on
                    either side); whitespace inside a value matches any run
                    of whitespace
    domain          case-insensitive, label-bounded: zentrico.example matches
                    app.zentrico.example and mail@zentrico.example, never
                    notzentrico.example or zentrico.examples
    contact_email   case-insensitive, not inside a longer address
    contact_title   only titles of two or more words ("Head of Growth"); a
                    one-word title (Founder, CEO) names a role, not a person
    note            a reply, meeting or close note of 20+ characters, found
                    whole (case-insensitive, whitespace-normalised)

A hit carries where it is and what kind of value it is. Never the value: no
function here returns one, and the CLI prints only location and kind.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

KINDS = ("company_name", "domain", "contact_name", "contact_email", "contact_title", "note")
MIN_WORD_LEN = 3
MIN_NOTE_LEN = 20


@dataclass(frozen=True)
class Hit:
    line: int
    kind: str


def _norm_ws(s: str) -> str:
    return " ".join(s.split())


def normalise_domain(domain: str) -> str:
    d = domain.strip().lower()
    d = re.sub(r"^[a-z][a-z0-9+.-]*://", "", d)
    d = d.split("/", 1)[0].split(":", 1)[0].rstrip(".")
    return d[4:] if d.startswith("www.") else d


class Terms:
    """The sensitive values, compiled into one regex per matching rule."""

    def __init__(self, values: Iterable[tuple[str, str]]):
        self.by_value: dict[tuple[str, str], set[str]] = {}  # (rule, lowered) -> kinds
        words: set[str] = set()
        domains: set[str] = set()
        emails: set[str] = set()
        notes: set[str] = set()
        for kind, raw in values:
            if kind not in KINDS or not isinstance(raw, str):
                continue
            if kind == "domain":
                v = normalise_domain(raw)
                if "." not in v:
                    continue
                domains.add(v)
                self.by_value.setdefault(("domain", v), set()).add(kind)
            elif kind == "contact_email":
                v = raw.strip().lower()
                if "@" not in v:
                    continue
                emails.add(v)
                self.by_value.setdefault(("email", v), set()).add(kind)
            elif kind == "note":
                v = _norm_ws(raw).lower()
                if len(v) < MIN_NOTE_LEN:
                    continue
                notes.add(v)
            else:
                v = _norm_ws(raw)
                if len(v) < MIN_WORD_LEN or (kind == "contact_title" and " " not in v):
                    continue
                words.add(v.lower())
                self.by_value.setdefault(("word", v.lower()), set()).add(kind)
        self.notes = sorted(notes)
        self.word_re = self._alt(words, r"(?<!\w)", r"(?!\w)", ws=True)
        self.domain_re = self._alt(domains, r"(?<![a-z0-9-])", r"(?![a-z0-9-])")
        self.email_re = self._alt(emails, r"(?<![\w.+-])", r"(?![\w-])")

    @staticmethod
    def _alt(values: set[str], pre: str, post: str, ws: bool = False) -> re.Pattern[str] | None:
        if not values:
            return None
        # longest first, so "Zentrico Labs" wins over "Zentrico"
        ordered = sorted(values, key=lambda v: (-len(v), v))
        parts = [r"\s+".join(re.escape(w) for w in v.split(" ")) if ws else re.escape(v)
                 for v in ordered]
        return re.compile(pre + "(?:" + "|".join(parts) + ")" + post, re.IGNORECASE)

    def __bool__(self) -> bool:
        return bool(self.by_value or self.notes)

    def __len__(self) -> int:
        return len(self.by_value) + len(self.notes)

    def kinds_in(self, text: str) -> set[str]:
        """The kinds of value found in one piece of text (one line)."""
        found: set[str] = set()
        for rule, rx in (("word", self.word_re), ("domain", self.domain_re),
                         ("email", self.email_re)):
            if rx is None:
                continue
            for m in rx.finditer(text):
                found |= self.by_value.get((rule, _norm_ws(m.group(0)).lower()), set())
        if self.notes:
            low = _norm_ws(text).lower()
            if any(n in low for n in self.notes):
                found.add("note")
        return found

    def scan(self, text: str) -> list[Hit]:
        """Every (line, kind) in a whole text, sorted. Notes are found across line
        breaks and reported at the line they start on."""
        hits: set[Hit] = set()
        for i, line in enumerate(text.splitlines(), start=1):
            for k in self.kinds_in(line) - {"note"}:
                hits.add(Hit(i, k))
        if self.notes:
            flat, starts = _flatten(text)
            low = flat.lower()
            for n in self.notes:
                pos = low.find(n)
                while pos != -1:
                    hits.add(Hit(text.count("\n", 0, starts[pos]) + 1, "note"))
                    pos = low.find(n, pos + 1)
        return sorted(hits, key=lambda h: (h.line, h.kind))


def _flatten(text: str) -> tuple[str, list[int]]:
    """Whitespace-collapsed text and, per output character, its original offset."""
    out: list[str] = []
    starts: list[int] = []
    in_ws = True  # drop leading whitespace
    for i, ch in enumerate(text):
        if ch.isspace():
            if not in_ws:
                out.append(" ")
                starts.append(i)
            in_ws = True
        else:
            out.append(ch)
            starts.append(i)
            in_ws = False
    return "".join(out), starts


def _rows(conn: sqlite3.Connection, sql: str) -> Iterator[sqlite3.Row]:
    try:
        yield from conn.execute(sql)
    except sqlite3.OperationalError:  # a table from a later migration is missing
        return


def values(conn: sqlite3.Connection, kinds: Iterable[str] = KINDS) -> list[tuple[str, str]]:
    """(kind, value) for every sensitive value in the database."""
    want = set(kinds)
    out: list[tuple[str, str]] = []
    if "company_name" in want:
        out += [("company_name", r[0]) for r in _rows(conn, "SELECT name FROM companies")]
        out += [("company_name", r[0]) for r in _rows(
            conn, "SELECT company_name_raw FROM signals WHERE company_id IS NOT NULL")]
    if "domain" in want:
        out += [("domain", r[0]) for r in _rows(
            conn, "SELECT domain FROM companies WHERE domain IS NOT NULL")]
    for r in _rows(conn, "SELECT name, email, title FROM contacts"):
        if "contact_name" in want and r["name"]:
            name = _norm_ws(r["name"])
            out.append(("contact_name", name))
            out += [("contact_name", p) for p in name.split(" ")]
        if "contact_email" in want and r["email"]:
            out.append(("contact_email", r["email"]))
        if "contact_title" in want and r["title"]:
            out.append(("contact_title", r["title"]))
    if "note" in want:
        for sql in ("SELECT note FROM replies", "SELECT note FROM meetings",
                    "SELECT next_step FROM closes"):
            out += [("note", r[0]) for r in _rows(conn, sql) if r[0]]
    return [(k, v) for k, v in out if isinstance(v, str) and v.strip()]


def terms(conn: sqlite3.Connection, kinds: Iterable[str] = KINDS) -> Terms:
    return Terms(values(conn, kinds))
