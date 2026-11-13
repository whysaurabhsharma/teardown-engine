"""The gold set: hand-written diagnoses that teach and score the judge
(docs/PLAN.md Steps 44-45, Gate 4.1; protocol/GAPS.md "Gold set").

The person writes each diagnosis; this module only checks and stores it.

- `plan_add` checks one diagnosis against the company's captured bundle. Each
  found quote must be a verbatim substring, after whitespace normalisation, of
  that artifact's extracted text, its nav or footer text, or a string in its
  structure, checked with engine.extract.base.verify. A not-found item is valid
  only when that artifact's capture outcome is not_found: blocked, dead and
  robots_denied mean unseen, never absent. A gap needs passing evidence from at
  least two artifact kinds with at least one found quote; an abstention needs a
  reason starting no_evidence:, contradicted: or thin:, and any evidence it
  gives must pass the same checks.
- The split is per company, across every gaps_version: five `teach`, the rest
  `held_out`. `split_problems` holds the Step 45 rules (exactly five, both
  segments, at least three gap types, at least one abstention).
- `held_out_values` returns what must never reach a prompt: the held-out
  companies and their quotes. engine/llm/gateway.py refuses a prompt that
  holds one (protocol/GAPS.md: held_out rows are never shown to the judge).
- `teach_export` returns the five teach diagnoses for the judge prompt; it
  refuses when the split is not recorded and never includes a held_out row.

No network and no model call: reads the database and the cache only.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from engine.capture.extract_text import normalise_ws
from engine.extract.base import Art, FactOut, verify
from engine.guard.cache import Cache
from engine.models import GapId, Gold, GoldEvidence, abstain_reason_problem
from engine.resolve.segment import IN_SAAS, US_INDIA_TEAM

GAPS_PATH = Path(__file__).resolve().parents[2] / "protocol" / "GAPS.md"
GAP_IDS: tuple[str, ...] = GapId.__args__  # type: ignore[attr-defined]
TEACH_SIZE = 5
GATE_ROWS = 18
GATE_GAPS_MAJOR = 2
MIN_TEACH_GAP_TYPES = 3
SEGMENTS = (IN_SAAS, US_INDIA_TEAM)
MIN_GUARD_QUOTE = 20  # characters, after normalisation: shorter quotes are too generic to guard

RULE_TWO_KINDS = ("GAPS.md Evidence rules: a diagnosis needs passing evidence from at least "
                  "two artifact kinds, and at least one must be a found quote")
RULE_NOT_FOUND = ("GAPS.md Evidence rules: a not-found claim is valid only when that artifact's "
                  "capture outcome is not_found; blocked, dead or robots_denied means it could "
                  "not be seen, never that it is absent")
RULE_VERBATIM = ("GAPS.md Evidence rules: a found claim quotes the artifact text verbatim")
RULE_ABSTAIN = ("GAPS.md Abstain by default: abstain_reason starts with no_evidence:, "
                "contradicted: or thin:")

_VERSION_LINE = re.compile(r"^Version\s+(\d+)(?:\.(\d+))?", re.MULTILINE)
_GAPS_VERSION_ARG = re.compile(r"^v\d+(\.\d+)*$")
_SMART = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


class GoldError(RuntimeError):
    """A diagnosis or a split that the rules refuse. The message names the rule."""


# -- versions ----------------------------------------------------------------------------


def gaps_file_version(path: Path | None = None) -> tuple[int, int]:
    """(major, minor) from GAPS.md's "Version X.Y" line."""
    p = path or GAPS_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise GoldError(f"cannot read {p}: {e}") from e
    m = _VERSION_LINE.search(text)
    if not m:
        raise GoldError(f"{p.name} has no 'Version X.Y' line")
    return int(m.group(1)), int(m.group(2) or 0)


def default_gaps_version(path: Path | None = None) -> str:
    major, minor = gaps_file_version(path)
    return f"v{major}.{minor}"


def version_key(v: str) -> tuple[int, ...]:
    """'v1.2' -> (1, 2); anything unparsable sorts first."""
    try:
        return tuple(int(p) for p in v.lstrip("v").split("."))
    except ValueError:
        return (-1,)


# -- normalisation for the guard ---------------------------------------------------------


def guard_norm(s: str) -> str:
    """Whitespace, case and smart quotes, as the faithfulness check normalises them."""
    return normalise_ws(s.translate(_SMART)).casefold()


# -- checking one diagnosis --------------------------------------------------------------


@dataclass
class AddPlan:
    row: Gold
    existing: sqlite3.Row | None = None  # the row at (company_id, gaps_version), if any
    unchanged: bool = False
    warnings: list[str] = field(default_factory=list)


def _artifacts(conn: sqlite3.Connection, cache: Cache, company_id: str) -> dict[str, Art]:
    from engine.capture import extract_text as X

    arts: dict[str, Art] = {}
    for r in conn.execute("SELECT * FROM artifacts WHERE company_id = ?", (company_id,)):
        text = structure = None
        if r["outcome"] == "ok" and r["extractor_version"] == X.EXTRACTOR_VERSION:
            text, structure = X.stored_text(cache, r), X.stored_structure(cache, r)
        arts[r["kind"]] = Art(r["kind"], r["url"], r["outcome"], text, structure)
    return arts


def _text_fact(art: Art, quote: str) -> FactOut | None:
    """The quote as it stands in the text, whitespace runs allowed to differ."""
    words = quote.split()
    if not words or not art.text:
        return None
    m = re.search(r"\s+".join(re.escape(w) for w in words), art.text)
    if m is None:
        return None
    return FactOut("gold", None, art.kind, m.group(0), "text", True, m.start())


def check_quote(art: Art, quote: str) -> bool:
    """True when quote is in art's text, nav/footer text or structure (base.verify)."""
    if not quote.strip():
        return False
    candidates = [_text_fact(art, quote),
                  FactOut("gold", None, art.kind, quote, "removed_text", True, 0),
                  FactOut("gold", None, art.kind, quote, "structure", True, 0)]
    return any(f is not None and verify(f, art) for f in candidates)


def build_evidence(arts: dict[str, Art], quotes: list[tuple[str, str]],
                   not_found: list[str]) -> list[GoldEvidence]:
    """Check every item; raise GoldError naming the first failure and its rule."""
    seen: set[tuple[str, str]] = set()
    out: list[GoldEvidence] = []
    for i, (kind, quote) in enumerate(quotes, start=1):
        label = f"quote {i} ({kind})"
        q = normalise_ws(quote)
        if not q:
            raise GoldError(f"{label}: empty. {RULE_VERBATIM}")
        if (kind, q) in seen:
            raise GoldError(f"{label}: given twice")
        seen.add((kind, q))
        art = arts.get(kind)
        if art is None:
            raise GoldError(f"{label}: the company has no {kind} artifact. {RULE_VERBATIM}")
        if art.outcome != "ok":
            raise GoldError(f"{label}: the {kind} artifact's outcome is {art.outcome}; there is "
                            f"no text to quote. {RULE_VERBATIM}")
        if not art.ok:
            raise GoldError(f"{label}: the {kind} artifact is not extracted at the current "
                            "version; run `te extract text`")
        if not check_quote(art, q):
            raise GoldError(f"{label}: not found verbatim (after whitespace normalisation) in "
                            f"the {kind} artifact's text, nav/footer text or structure. "
                            f"{RULE_VERBATIM}")
        out.append(GoldEvidence(kind=kind, found=True, url=art.url, quote=q))
    quoted = {k for k, _ in quotes}
    for kind in not_found:
        label = f"not-found {kind}"
        if kind in quoted:
            raise GoldError(f"{label}: the same kind is also quoted as found")
        if (kind, "") in seen:
            raise GoldError(f"{label}: given twice")
        seen.add((kind, ""))
        art = arts.get(kind)
        if art is None:
            raise GoldError(f"{label}: the company has no {kind} artifact row. {RULE_NOT_FOUND}")
        if art.outcome != "not_found":
            raise GoldError(f"{label}: the {kind} artifact's outcome is {art.outcome}, not "
                            f"not_found. {RULE_NOT_FOUND}")
        out.append(GoldEvidence(kind=kind, found=False, url=art.url, quote=None))
    return out


def evidence_problem(evidence: list[GoldEvidence]) -> str | None:
    kinds = {e.kind for e in evidence}
    if len(kinds) < 2 or not any(e.found for e in evidence):
        return (f"evidence from {len(kinds)} kind(s), {sum(e.found for e in evidence)} found "
                f"quote(s). {RULE_TWO_KINDS}")
    return None


def _company(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT company_id, segment, pilot, hand, status FROM companies "
                       "WHERE company_id = ?", (company_id,)).fetchone()
    if row is None:
        raise GoldError(f"no company {company_id!r}")
    return row


def company_split(conn: sqlite3.Connection, company_id: str) -> str | None:
    splits = {r[0] for r in conn.execute("SELECT DISTINCT split FROM gold WHERE company_id = ?",
                                         (company_id,))}
    if len(splits) > 1:
        raise GoldError(f"{company_id}: gold rows disagree on the split; run `te gold split`")
    return next(iter(splits), None)


def teach_companies(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT company_id FROM gold "
                                       "WHERE split = 'teach'")}


def plan_add(conn: sqlite3.Connection, cache: Cache, company_id: str, *,
             gap: str | None, abstain_reason: str | None,
             quotes: list[tuple[str, str]], not_found: list[str],
             split: str | None = None, gaps_version: str | None = None,
             note: str | None = None) -> AddPlan:
    """Check one diagnosis and return the row to write. Raises GoldError."""
    company = _company(conn, company_id)
    warnings: list[str] = []
    if (gap is None) == (abstain_reason is None):
        raise GoldError("give either --gap or --abstain, not both and not neither")
    if gap is not None and gap not in GAP_IDS:
        raise GoldError(f"gap must be one of {', '.join(GAP_IDS)}")
    if abstain_reason is not None:
        abstain_reason = abstain_reason.strip()
        problem = abstain_reason_problem(abstain_reason)
        if problem:
            raise GoldError(f"{problem}. {RULE_ABSTAIN}")
    version = gaps_version or default_gaps_version()
    if not _GAPS_VERSION_ARG.match(version):
        raise GoldError(f"gaps version {version!r} must look like v1.2")
    current = default_gaps_version()
    if version != current:
        warnings.append(f"gaps version {version} is not GAPS.md's current {current}")
    if gap == "G6":
        warnings.append("G6 is provisional in GAPS.md; it is confirmed or removed at v2")
    if not (company["pilot"] or company["hand"]):
        warnings.append(f"{company_id} is neither a pilot nor a hand company; the gold set is "
                        "the pilot twelve and the Phase 0 six")

    evidence = build_evidence(_artifacts(conn, cache, company_id), quotes, not_found)
    if gap is not None:
        problem = evidence_problem(evidence)
        if problem:
            raise GoldError(problem)

    have = company_split(conn, company_id)
    if split is None:
        split = have or "held_out"
    elif have is not None and split != have:
        raise GoldError(f"{company_id} is {have} in its other gold rows; change the split with "
                        "`te gold split`")
    if split == "teach" and company_id not in teach_companies(conn) \
            and len(teach_companies(conn)) >= TEACH_SIZE:
        raise GoldError(f"there are already {TEACH_SIZE} teach companies (GAPS.md Gold set); "
                        "use `te gold split`")

    try:
        row = Gold(company_id=company_id, gap_type=gap, abstain=gap is None,
                   abstain_reason=abstain_reason, evidence=evidence, split=split,
                   gaps_version=version, notes=(note.strip() or None) if note else None)
    except ValidationError as e:
        raise GoldError(str(e)) from e
    existing = conn.execute("SELECT * FROM gold WHERE company_id = ? AND gaps_version = ?",
                            (company_id, version)).fetchone()
    plan = AddPlan(row, existing, warnings=warnings)
    if existing is not None:
        plan.unchanged = _same(existing, row)
    return plan


def _same(existing: sqlite3.Row, row: Gold) -> bool:
    return (existing["gap_type"] == row.gap_type
            and bool(existing["abstain"]) == row.abstain
            and existing["abstain_reason"] == row.abstain_reason
            and json.loads(existing["evidence"]) == [e.model_dump() for e in row.evidence]
            and existing["split"] == row.split
            and existing["notes"] == row.notes)


def write(conn: sqlite3.Connection, plan: AddPlan, *, replace: bool = False) -> str:
    """'added', 'replaced' or 'unchanged'. Raises GoldError when a different row is
    recorded and replace is not set."""
    from engine import db

    if plan.unchanged:
        return "unchanged"
    r = plan.row
    if plan.existing is not None:
        if not replace:
            raise GoldError(f"{r.company_id} already has a different gold row at "
                            f"{r.gaps_version}; pass --replace to overwrite it")
        with conn:
            conn.execute(
                "UPDATE gold SET gap_type = ?, abstain = ?, abstain_reason = ?, evidence = ?, "
                "split = ?, notes = ? WHERE gold_id = ?",
                (r.gap_type, int(r.abstain), r.abstain_reason,
                 json.dumps([e.model_dump() for e in r.evidence], ensure_ascii=False),
                 r.split, r.notes, plan.existing["gold_id"]))
        return "replaced"
    with conn:
        db.insert(conn, r)
    return "added"


# -- the split ---------------------------------------------------------------------------


def current_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Each company's gold row at its highest gaps_version, joined with its segment."""
    out: dict[str, sqlite3.Row] = {}
    for r in conn.execute("SELECT g.*, c.segment FROM gold g JOIN companies c "
                          "ON c.company_id = g.company_id"):
        have = out.get(r["company_id"])
        if have is None or version_key(r["gaps_version"]) > version_key(have["gaps_version"]):
            out[r["company_id"]] = r
    return out


def split_problems(rows: list[sqlite3.Row]) -> list[str]:
    """Step 45's rules for the teach five; [] when they hold."""
    problems = []
    if len(rows) != TEACH_SIZE:
        problems.append(f"teach needs exactly {TEACH_SIZE} companies, not {len(rows)}")
    segs = {r["segment"] for r in rows}
    missing = [s for s in SEGMENTS if s not in segs]
    if missing:
        problems.append(f"teach must cover both segments; missing {', '.join(missing)}")
    gaps = {r["gap_type"] for r in rows if not r["abstain"]}
    if len(gaps) < MIN_TEACH_GAP_TYPES:
        problems.append(f"teach needs at least {MIN_TEACH_GAP_TYPES} gap types, not {len(gaps)}")
    if not any(r["abstain"] for r in rows):
        problems.append("teach needs at least one abstention")
    return problems


def split_status(conn: sqlite3.Connection) -> list[str]:
    """[] when the split is recorded: one split per company, and the teach
    companies pass split_problems."""
    problems = []
    mixed = [r[0] for r in conn.execute("SELECT company_id FROM gold GROUP BY company_id "
                                        "HAVING COUNT(DISTINCT split) > 1")]
    if mixed:
        problems.append(f"gold rows disagree on the split for {len(mixed)} companies")
    rows = current_rows(conn)
    teach = [r for r in rows.values() if r["split"] == "teach"]
    problems += split_problems(teach)
    return problems


@dataclass
class SplitPlan:
    teach: list[str]
    held_out: list[str]
    changed: int  # gold rows whose split changes
    previous_teach: set[str]


def plan_split(conn: sqlite3.Connection, teach: list[str], *, force: bool = False) -> SplitPlan:
    if len(set(teach)) != len(teach):
        raise GoldError("a company is named twice")
    rows = current_rows(conn)
    missing = [c for c in teach if c not in rows]
    if missing:
        raise GoldError(f"no gold row for {', '.join(missing)}; `te gold add` it first")
    problems = split_problems([rows[c] for c in teach])
    if problems:
        raise GoldError("Step 45 split rules: " + "; ".join(problems))
    previous = teach_companies(conn)
    if previous and previous != set(teach) and not split_status(conn) and not force:
        raise GoldError("a different teach five is already recorded; re-splitting changes "
                        "which rows are held out. Pass --force to do it anyway")
    want = set(teach)
    changed = sum(1 for r in conn.execute("SELECT company_id, split FROM gold")
                  if r["split"] != ("teach" if r["company_id"] in want else "held_out"))
    held = sorted(c for c in rows if c not in want)
    return SplitPlan(sorted(teach), held, changed, previous)


def write_split(conn: sqlite3.Connection, plan: SplitPlan) -> None:
    marks = ", ".join("?" for _ in plan.teach)
    with conn:
        conn.execute(f"UPDATE gold SET split = CASE WHEN company_id IN ({marks}) "
                     "THEN 'teach' ELSE 'held_out' END", plan.teach)


# -- held out ----------------------------------------------------------------------------


@dataclass(frozen=True)
class HeldOut:
    company_ids: frozenset[str]
    # normalised quote (guard_norm, >= MIN_GUARD_QUOTE chars) -> the held-out company
    quotes: dict[str, frozenset[str]]


def held_out_values(conn: sqlite3.Connection) -> HeldOut:
    """The companies of every held_out gold row, and their found quotes, normalised
    with guard_norm; quotes shorter than MIN_GUARD_QUOTE are left out."""
    ids: set[str] = set()
    quotes: dict[str, set[str]] = {}
    for r in conn.execute("SELECT company_id, evidence FROM gold WHERE split = 'held_out'"):
        ids.add(r["company_id"])
        try:
            items = json.loads(r["evidence"])
        except json.JSONDecodeError:
            continue
        for e in items if isinstance(items, list) else []:
            q = e.get("quote") if isinstance(e, dict) else None
            if isinstance(q, str):
                n = guard_norm(q)
                if len(n) >= MIN_GUARD_QUOTE:
                    quotes.setdefault(n, set()).add(r["company_id"])
    return HeldOut(frozenset(ids), {q: frozenset(c) for q, c in quotes.items()})


def held_out_quote_in(conn: sqlite3.Connection, company_id: str | None, *texts: str) -> bool:
    """True when any text holds a held-out quote from a company other than company_id.

    A held-out company's own pages hold its own quotes, so a prompt about that
    company (the judge scoring it, Step 50) may contain them; any other prompt
    may not."""
    h = held_out_values(conn)
    if not h.quotes:
        return False
    normed = [guard_norm(t) for t in texts]
    for q, owners in h.quotes.items():
        if owners == {company_id}:
            continue
        if any(q in t for t in normed):
            return True
    return False


# -- export ------------------------------------------------------------------------------


def teach_export(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The five teach diagnoses, each company's row at its highest gaps_version.
    Raises GoldError when the split is not recorded or a held-out value leaks in."""
    problems = split_status(conn)
    if problems:
        raise GoldError("the split is not recorded (`te gold split`): " + "; ".join(problems))
    rows = [r for r in current_rows(conn).values() if r["split"] == "teach"]
    out = []
    for r in sorted(rows, key=lambda r: r["company_id"]):
        if r["split"] != "teach":  # never; the filter above is the rule
            raise GoldError("refused: a held_out row reached the export")
        out.append({
            "company_id": r["company_id"],
            "segment": r["segment"],
            "gaps_version": r["gaps_version"],
            "gap_type": r["gap_type"],
            "abstain": bool(r["abstain"]),
            "abstain_reason": r["abstain_reason"],
            "evidence": json.loads(r["evidence"]),
        })
    h = held_out_values(conn)
    blob = json.dumps(out, ensure_ascii=False)
    leaked = [c for c in h.company_ids
              if re.search(r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", blob)]
    if leaked or held_out_quote_in(conn, None, blob):
        raise GoldError("refused: the teach export would contain a held-out company or quote")
    return out


# -- listing and the gate ----------------------------------------------------------------


@dataclass
class GateLine:
    ok: bool
    text: str


def gate(conn: sqlite3.Connection, gaps_path: Path | None = None) -> tuple[list[GateLine],
                                                                         list[str]]:
    """Gate 4.1: eighteen gold rows, the split recorded, GAPS.md at v2.
    Returns (lines, warnings)."""
    lines: list[GateLine] = []
    warnings: list[str] = []
    rows = current_rows(conn)
    n_rows = conn.execute("SELECT COUNT(*) FROM gold").fetchone()[0]
    lines.append(GateLine(len(rows) == GATE_ROWS,
                          f"gold diagnoses: {len(rows)} companies ({n_rows} rows); "
                          f"need {GATE_ROWS}"))
    splits = Counter(r["split"] for r in rows.values())
    problems = split_status(conn)
    held_ok = splits["held_out"] == GATE_ROWS - TEACH_SIZE
    lines.append(GateLine(not problems and held_ok,
                          f"split: teach {splits['teach']}, held_out {splits['held_out']}; need "
                          f"{TEACH_SIZE} and {GATE_ROWS - TEACH_SIZE}"
                          + (f" ({'; '.join(problems)})" if problems else "")))
    try:
        major, minor = gaps_file_version(gaps_path)
        lines.append(GateLine(major >= GATE_GAPS_MAJOR,
                              f"GAPS.md at version {major}.{minor}; need {GATE_GAPS_MAJOR} or later"))
        current = f"v{major}.{minor}"
        stale = sorted(c for c, r in rows.items() if r["gaps_version"] != current)
        if stale:
            warnings.append(f"{len(stale)} companies' latest gold row is not at GAPS.md's "
                            f"{current}")
    except GoldError as e:
        lines.append(GateLine(False, str(e)))
    no_segment = sorted(c for c, r in rows.items() if r["segment"] not in SEGMENTS)
    if no_segment:
        warnings.append(f"{len(no_segment)} companies have no segment")
    return lines, warnings
