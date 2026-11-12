"""Dedupe companies (docs/PLAN.md Step 27). Reports and flags; never merges.

The registrable domain is the key: each one has exactly one company row, and
every signal for that domain points at it. Step 26 (engine/resolve/domain.py)
already writes that way and companies.domain is UNIQUE, so this step checks
the invariant rather than repairs it. Violations are reported, never fixed:

  unnormalised_domain  companies.domain is not its own registrable domain
                       (www.acme.com, Acme.com); the UNIQUE index cannot see
                       that it is the same site as acme.com.
  duplicate_domain     two or more rows whose domains reduce to the same
                       registrable domain.
  split_board          signals from one ATS board (source + slug) point at
                       more than one company.
  signal_elsewhere     a careers signal's site domain has a company row, but
                       the signal points at another company (a placeholder
                       included).
  missing_company      a signal points at a company_id with no row.

Then pairs of different companies that may be one are flagged for a person
(Step 30's review), never merged:

  same_stem         both have a domain, the registrable label is the same and
                    the suffix differs (acme.com / acme.in / acme.io).
  similar_name      the normalised names (domain.normalise_name: lower case,
                    punctuation and legal words dropped) score >= 90 on
                    rapidfuzz token_set_ratio.
  placeholder_name  as similar_name, where at least one side is an unresolved
                    placeholder (domain NULL): its signals may belong to the
                    other company, which a person decides.

A pair that is both same_stem and similar_name is stored once, as same_stem.
Flags live in company_flags (migration 0006), one row per unordered pair and
kind (company_id < other_company_id). Re-running inserts nothing new unless the
companies changed; a flag whose pair no longer matches is left for the review
rather than deleted. Nothing here touches the network or calls a model.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from engine.resolve.domain import (
    _EXTRACT,
    ATS_SOURCES,
    normalise_name,
    registrable_domain,
)

NAME_THRESHOLD = 90
KIND_STEM = "same_stem"
KIND_NAME = "similar_name"
KIND_PLACEHOLDER = "placeholder_name"


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class Flag:
    company_id: str  # sorts before other_company_id
    other_company_id: str
    kind: str
    score: float
    detail: str


@dataclass
class Report:
    companies: int = 0
    violations: list[Violation] = field(default_factory=list)
    flags: list[Flag] = field(default_factory=list)
    new_flags: int = 0  # flags not already stored (written unless dry_run)

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class _Co:
    company_id: str
    domain: str | None
    name: str
    norm: str

    @property
    def placeholder(self) -> bool:
        return self.domain is None


def _companies(conn: sqlite3.Connection) -> list[_Co]:
    return [
        _Co(r["company_id"], r["domain"], r["name"], normalise_name(r["name"]))
        for r in conn.execute("SELECT company_id, domain, name FROM companies ORDER BY company_id")
    ]


# -- (a) the invariant -------------------------------------------------------------


def check_invariant(conn: sqlite3.Connection) -> list[Violation]:
    """Every registrable domain has one row and all its signals point at it."""
    out: list[Violation] = []
    by_domain: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT company_id, domain FROM companies WHERE domain IS NOT NULL "
                          "ORDER BY company_id"):
        reg = registrable_domain(r["domain"])
        if reg != r["domain"]:
            out.append(Violation("unnormalised_domain",
                                 f"{r['company_id']} has domain {r['domain']!r}, "
                                 f"registrable {reg or 'none'}"))
        by_domain[reg or r["domain"]].append(r["company_id"])
    for reg, ids in sorted(by_domain.items()):
        if len(ids) > 1:
            out.append(Violation("duplicate_domain", f"{reg} has {len(ids)} rows: "
                                 + ", ".join(ids)))
    owner = {reg: ids[0] for reg, ids in by_domain.items() if len(ids) == 1}

    marks = ", ".join("?" for _ in ATS_SOURCES)
    for r in conn.execute(
        "SELECT source, lower(board_slug) AS slug, group_concat(DISTINCT company_id) AS ids, "
        "count(DISTINCT company_id) AS n FROM signals "
        f"WHERE company_id IS NOT NULL AND source IN ({marks}) AND board_slug IS NOT NULL "
        "GROUP BY source, lower(board_slug) HAVING n > 1 ORDER BY source, slug",
        ATS_SOURCES,
    ):
        ids = ", ".join(sorted(r["ids"].split(",")))
        out.append(Violation("split_board", f"{r['source']}:{r['slug']} signals point at "
                             f"{r['n']} companies: {ids}"))

    stray: dict[tuple[str, str, str], int] = defaultdict(int)
    for r in conn.execute("SELECT board_slug, company_id FROM signals WHERE source = 'careers' "
                          "AND company_id IS NOT NULL AND board_slug IS NOT NULL"):
        reg = registrable_domain(r["board_slug"])
        if reg in owner and owner[reg] != r["company_id"]:
            stray[(reg, r["company_id"], owner[reg])] += 1
    for (reg, at, should), n in sorted(stray.items()):
        out.append(Violation("signal_elsewhere", f"{n} careers signal(s) for {reg} point at "
                             f"{at}, not {should}"))

    for r in conn.execute(
        "SELECT s.company_id, count(*) AS n FROM signals s LEFT JOIN companies c "
        "ON c.company_id = s.company_id WHERE s.company_id IS NOT NULL AND c.company_id IS NULL "
        "GROUP BY s.company_id ORDER BY s.company_id"
    ):
        out.append(Violation("missing_company",
                             f"{r['n']} signal(s) point at {r['company_id']}, which has no row"))
    return out


# -- (b) possible duplicates -------------------------------------------------------


def _stem(domain: str) -> tuple[str, str]:
    r = _EXTRACT(domain)
    return r.domain, r.suffix


def name_similarity(a: str, b: str) -> float:
    """token_set_ratio of two already-normalised names; 0 when either is empty."""
    if not a or not b:
        return 0.0
    return float(fuzz.token_set_ratio(a, b))


def compare(a: _Co, b: _Co) -> Flag | None:
    """The flag for one pair, or None. a and b are ordered by company_id."""
    score = name_similarity(a.norm, b.norm)
    if a.domain and b.domain:
        (sa, xa), (sb, xb) = _stem(a.domain), _stem(b.domain)
        if sa and sa == sb and xa != xb:
            return Flag(a.company_id, b.company_id, KIND_STEM, score,
                        f"same name stem '{sa}' on {a.domain} and {b.domain}; "
                        f"names '{a.name}' / '{b.name}' score {score:.0f}")
    if score < NAME_THRESHOLD:
        return None
    if a.placeholder or b.placeholder:
        ph, other = (a, b) if a.placeholder else (b, a)
        where = f"domain {other.domain}" if other.domain else "also unresolved"
        return Flag(a.company_id, b.company_id, KIND_PLACEHOLDER, score,
                    f"unresolved '{ph.name}' ({ph.company_id}) matches '{other.name}' "
                    f"({other.company_id}, {where}) at {score:.0f}; not merged")
    return Flag(a.company_id, b.company_id, KIND_NAME, score,
                f"names '{a.name}' / '{b.name}' score {score:.0f} >= {NAME_THRESHOLD} "
                f"on different domains {a.domain} and {b.domain}")


def find_flags(conn: sqlite3.Connection, company_id: str | None = None) -> list[Flag]:
    """Every flagged pair (only those involving company_id, when given)."""
    cos = _companies(conn)
    flags: list[Flag] = []
    for i, a in enumerate(cos):
        for b in cos[i + 1:]:
            if company_id is not None and company_id not in (a.company_id, b.company_id):
                continue
            if a.domain and b.domain and a.domain == b.domain:
                continue  # cannot happen (UNIQUE); the invariant check reports it if it does
            f = compare(a, b)
            if f is not None:
                flags.append(f)
    return flags


def _stored(conn: sqlite3.Connection, f: Flag) -> bool:
    return conn.execute(
        "SELECT 1 FROM company_flags WHERE company_id = ? AND other_company_id = ? AND kind = ?",
        (f.company_id, f.other_company_id, f.kind)).fetchone() is not None


def store(conn: sqlite3.Connection, flags: list[Flag]) -> int:
    """Insert flags not already stored; an existing flag is left as it is."""
    n = 0
    with conn:
        for f in flags:
            cur = conn.execute(
                "INSERT INTO company_flags (company_id, other_company_id, kind, score, detail) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (company_id, other_company_id, kind) DO NOTHING",
                (f.company_id, f.other_company_id, f.kind, f.score, f.detail))
            n += cur.rowcount
    return n


def run(conn: sqlite3.Connection, *, company_id: str | None = None,
        dry_run: bool = False) -> Report:
    rep = Report(companies=conn.execute("SELECT count(*) FROM companies").fetchone()[0])
    rep.violations = check_invariant(conn)
    rep.flags = find_flags(conn, company_id)
    if dry_run:
        rep.new_flags = sum(1 for f in rep.flags if not _stored(conn, f))
    else:
        rep.new_flags = store(conn, rep.flags)
    return rep
