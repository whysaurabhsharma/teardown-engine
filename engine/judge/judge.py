"""E5, the judge (docs/PLAN.md Steps 46-47; protocol/GAPS.md v1.2).

    te judge --pilot | --company <id> | --limit <n> | --dry-run [--no-examples]

For each company at `extracted` (or later, not dropped):

1. Build the prompt: engine/judge/prompt.md (version PROMPT_VERSION) with its
   comment blocks stripped, the teach examples, and the company block.
   - Teach examples come from engine/judge/gold.py teach_export (the same rows
     `te gold export-teach` prints): never a held_out row. When the split is
     not recorded the judge refuses to run. `--no-examples` runs without them,
     for dry testing only: the prompt says so, the call is keyed on
     PROMPT_VERSION + NO_EXAMPLES_SUFFIX (so it never fills the real cache key),
     and no company status changes. When the company being judged is itself a
     teach company, its own example is left out.
   - The company block: name, domain, segment, the signal role and its offer
     type, every fact with its quote, and each artifact of the bundle (kind,
     URL, capture outcome). Only the company's public pages and its public DNS
     record are included, never a contacts field. Text is cut deterministically
     at a word boundary, keeping the original characters so quotes stay
     verbatim (budget per artifact):
       job_post            full text, at most JOB_POST_CHARS characters
       every other kind    the first TEXT_WORDS words, at most TEXT_CHARS chars
       nav and footer text the first NAV_WORDS words, at most NAV_CHARS chars
     A cut is marked "[cut: N of M words shown]". Ceiling: about
     9 * (4,000 + 1,000) + 20,000 + 4,000 (dns) characters of artifacts, plus
     facts, examples and the fixed text; `--dry-run` prints each prompt's size.
   - The gap table in prompt.md must equal protocol/GAPS.md's table; the judge
     refuses to run when they differ (bump the prompt).
2. Mask contacts: any value from the contacts table (gateway.contact_values,
   matched with gateway.contact_pattern) is replaced with "[redacted]", so a
   page that happens to contain a recipient's title does not fail the call.
   The gateway's own refusal check still runs as a backstop.
3. Call llm.gateway.call(stage="judge", company_id, prompt_version, route
   "judge", prompt, schema.json). Same key, no call. A missing
   structured_output (the provider returns no output twice) is SchemaInvalid:
   the company is reported as scoring_failed, no diagnosis row is written, the
   status is kept, and free text is never parsed. An output that validates
   but breaks the diagnosis contract (attempted true with no gap, an
   abstention whose reason does not start with a GAPS.md code) is also
   scoring_failed.
4. Confidence floor: a named gap below CONFIDENCE_FLOOR becomes an abstention
   "thin: confidence ..." (GAPS.md: below 0.6, abstain).
5. Faithfulness (engine/judge/faithfulness.py) runs on the claims; failed
   claims are removed, and a gap left without evidence becomes an abstention.
6. One diagnoses row per (company_id, prompt_version). Then the status, only
   after faithfulness: a named gap moves extracted -> diagnosed; an abstention
   drops the company as no_gap at stage judge (protocol/ICP.md). A company
   already past diagnosed (the hand set) keeps its status.

Idempotent: a company with a diagnoses row at this prompt_version is not
judged again (its status is re-applied); a re-run after a deleted row reads
the gateway cache and makes no call. Changing prompt.md, schema.json, the
truncation budget or the fact rules' meaning bumps PROMPT_VERSION.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from engine import db, states
from engine.capture import bundle as B
from engine.extract.base import Art
from engine.guard.budget import Budget
from engine.guard.cache import Cache
from engine.judge import faithfulness as F
from engine.judge import gold as G
from engine.llm import gateway
from engine.llm.base import ContactInPrompt, HeldOutInPrompt, LlmError, SchemaInvalid
from engine.models import Diagnosis, abstain_reason_problem
from engine.states import PIPELINE, Status

HERE = Path(__file__).parent
PROMPT_PATH = HERE / "prompt.md"
SCHEMA_PATH = HERE / "schema.json"

PROMPT_VERSION = "j-1.0"
NO_EXAMPLES_SUFFIX = "+noex"
STAGE = "judge"
ROUTE = "judge"
DROP_REASON = "no_gap"
DROP_STAGE = "judge"
CONFIDENCE_FLOOR = 0.6
MASK = "[redacted]"

JOB_POST_CHARS = 20_000
TEXT_WORDS = 600
TEXT_CHARS = 4_000
NAV_WORDS = 150
NAV_CHARS = 1_000

_FROM_EXTRACTED = frozenset(PIPELINE[PIPELINE.index(Status.EXTRACTED):])
_COMMENT = re.compile(r"<!--.*?-->\s*", re.DOTALL)
_VERSION_COMMENT = re.compile(r"<!--\s*prompt_version:\s*(\S+)\s*-->")
_WORD = re.compile(r"\S+")

UNSEEN_NOTE = ("Could not be seen ({outcome}). It is not known to be absent: make no claim "
               "about it.")
NO_EXAMPLES_TEXT = ("(No worked examples in this run: it is a dry test without the gold "
                    "split. Apply the rules above as written.)")


class JudgeError(RuntimeError):
    """The judge refuses to run (no teach split, a stale gap table, a bad company)."""


class JudgeOutputInvalid(ValueError):
    """A schema-valid output that breaks the diagnosis contract."""


# -- the prompt template -----------------------------------------------------------------


def template_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def template_version(text: str | None = None) -> str | None:
    m = _VERSION_COMMENT.search(text if text is not None else template_text())
    return m.group(1) if m else None


def template() -> str:
    """prompt.md without its comment blocks."""
    return _COMMENT.sub("", template_text()).strip() + "\n"


@cache
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def gap_table(markdown: str) -> list[str]:
    """The lines of the table whose header starts "| ID |", whitespace-normalised."""
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("| ID |"):
            out = []
            for row in lines[i:]:
                if not row.strip().startswith("|"):
                    break
                out.append(" ".join(row.split()))
            return out
    return []


def table_problem() -> str | None:
    """None when prompt.md's gap table equals protocol/GAPS.md's."""
    if template_version() != PROMPT_VERSION:
        return (f"prompt.md says version {template_version()!r}; judge.py PROMPT_VERSION is "
                f"{PROMPT_VERSION!r}")
    mine, theirs = gap_table(template_text()), gap_table(G.GAPS_PATH.read_text(encoding="utf-8"))
    if not theirs:
        return "no gap table found in protocol/GAPS.md"
    if mine != theirs:
        return ("the gap table in engine/judge/prompt.md differs from protocol/GAPS.md; copy it "
                "and bump PROMPT_VERSION")
    return None


# -- text budget -------------------------------------------------------------------------


def cut(text: str, max_words: int | None, max_chars: int) -> str:
    """text up to max_words words and max_chars characters, cut at the end of a
    word with the original characters kept; a cut is marked."""
    words = list(_WORD.finditer(text))
    end = len(text.rstrip())
    shown = len(words)
    if max_words is not None and len(words) > max_words:
        end, shown = words[max_words - 1].end(), max_words
    if end > max_chars:
        within = [m for m in words[:shown] if m.end() <= max_chars]
        end, shown = (within[-1].end(), len(within)) if within else (0, 0)
    body = text[:end].strip()
    if shown < len(words):
        body += f"\n[cut: {shown} of {len(words)} words shown]"
    return body


# -- the company block -------------------------------------------------------------------


def _company(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT c.company_id, c.name, c.domain, c.segment, c.status, s.title AS signal_title, "
        "s.offer_type AS offer_type FROM companies c LEFT JOIN signals s "
        "ON s.signal_id = c.best_signal_id WHERE c.company_id = ?", (company_id,)).fetchone()
    if row is None:
        raise JudgeError(f"no company {company_id!r}")
    return row


def _facts_block(conn: sqlite3.Connection, company_id: str) -> str:
    rows = conn.execute("SELECT * FROM facts WHERE company_id = ? ORDER BY artifact_kind, fact, "
                        "fact_id", (company_id,)).fetchall()
    if not rows:
        return "(no facts)"
    out = []
    for r in rows:
        if r["found"]:
            out.append(f"- {r['fact']} = {r['value']} [{r['artifact_kind']}] "
                       f"quote: {json.dumps(r['quote'], ensure_ascii=False)}")
        else:
            out.append(f"- {r['fact']} = {r['value']} [{r['artifact_kind']}] "
                       f"absence, no quote ({r['quote']})")
    return "\n".join(out)


def _artifact_block(kind: str, art: Art | None) -> str:
    if art is None:
        return f"### {kind}\nNo artifact row: not captured. Make no claim about it."
    head = f"### {kind}\nURL: {art.url}\nOutcome: {art.outcome}"
    if art.outcome == "not_found":
        return head + ("\nNot found: capture found no such page or record. A not-found claim "
                       "may rest on this.")
    if art.outcome != "ok":
        return head + "\n" + UNSEEN_NOTE.format(outcome=art.outcome)
    if art.text is None:
        return head + "\nFetched but its text is not extracted. Make no claim about it."
    if kind == "job_post":
        text = cut(art.text, None, JOB_POST_CHARS)
    else:
        text = cut(art.text, TEXT_WORDS, TEXT_CHARS)
    block = f"{head}\nText:\n{text or '(empty)'}"
    removed = art.removed.strip()
    if removed:
        block += f"\nNav and footer text:\n{cut(removed, NAV_WORDS, NAV_CHARS)}"
    return block


def company_block(conn: sqlite3.Connection, cache: Cache, company_id: str) -> str:
    c = _company(conn, company_id)
    arts = F.load_arts(conn, cache, company_id)
    lines = [
        f"Company: {c['name']}",
        f"Domain: {c['domain'] or 'unknown'}",
        f"Segment: {c['segment'] or 'unknown'}",
        f"Signal role: {c['signal_title'] or 'unknown'}",
        f"Offer type: {c['offer_type'] or 'unknown'}",
        "",
        "### Facts",
        _facts_block(conn, company_id),
        "",
        "Artifacts follow, one per kind.",
    ]
    kinds = list(B.KINDS) + sorted(k for k in arts if k not in B.KINDS)
    blocks = [_artifact_block(k, arts.get(k)) for k in kinds]
    return "\n".join(lines) + "\n\n" + "\n\n".join(blocks)


# -- the teach examples ------------------------------------------------------------------


def teach_examples(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """gold.teach_export, or JudgeError when the split is not recorded."""
    try:
        return G.teach_export(conn)
    except G.GoldError as e:
        raise JudgeError(f"no teach examples: {e}. Record the split (`te gold split`), or pass "
                         "--no-examples for a dry test") from e


def examples_block(examples: list[dict[str, Any]] | None, company_id: str) -> str:
    if examples is None:
        return NO_EXAMPLES_TEXT
    shown = [e for e in examples if e["company_id"] != company_id]
    out = []
    for n, e in enumerate(shown, start=1):
        call = (f"abstain. abstain_reason: {e['abstain_reason']}" if e["abstain"]
                else f"gap {e['gap_type']}")
        lines = [f"### Example {n} (segment {e['segment'] or 'unknown'})", f"Call: {call}",
                 "Evidence:"]
        for ev in e["evidence"]:
            if ev["found"]:
                lines.append(f"- {ev['kind']}, found, {ev['url']}, quote: "
                             f"{json.dumps(ev['quote'], ensure_ascii=False)}")
            else:
                lines.append(f"- {ev['kind']}, not found, {ev['url']}")
        if not e["evidence"]:
            lines.append("- (none recorded)")
        out.append("\n".join(lines))
    return "\n\n".join(out) if out else NO_EXAMPLES_TEXT


# -- contacts ----------------------------------------------------------------------------


def mask_contacts(conn: sqlite3.Connection, text: str) -> tuple[str, int]:
    """Every contacts value in text replaced with MASK, longest first.
    Returns (text, replacements). Never logs a value."""
    n = 0
    for v in sorted(gateway.contact_values(conn), key=lambda s: (-len(s), s)):
        text, k = gateway.contact_pattern(v).subn(MASK, text)
        n += k
    return text, n


def build_prompt(conn: sqlite3.Connection, cache: Cache, company_id: str,
                 examples: list[dict[str, Any]] | None) -> tuple[str, int]:
    """(the masked prompt, contact values masked)."""
    t = template()
    filled = (t.replace("{{teach_examples}}", examples_block(examples, company_id))
               .replace("{{company}}", company_block(conn, cache, company_id)))
    return mask_contacts(conn, filled)


# -- the output --------------------------------------------------------------------------


def to_diagnosis(company_id: str, prompt_version: str,
                 output: Any) -> tuple[Diagnosis, list[Any], str | None]:
    """(the diagnosis without claims, the raw claims, a note when the confidence
    floor turned a gap into an abstention). Raises JudgeOutputInvalid."""
    if not isinstance(output, dict):
        raise JudgeOutputInvalid("output is not an object")
    attempted = output.get("attempted")
    if not isinstance(attempted, bool):
        raise JudgeOutputInvalid("attempted is not an explicit boolean")
    gap = output.get("gap_type")
    reason = output.get("abstain_reason")
    conf = output.get("confidence")
    note = None
    if attempted:
        if gap is None:
            raise JudgeOutputInvalid("attempted is true but gap_type is null")
        if isinstance(conf, int | float) and conf < CONFIDENCE_FLOOR:
            note = f"confidence {conf} below {CONFIDENCE_FLOOR}"
            attempted, reason = False, (f"thin: confidence {conf} is below {CONFIDENCE_FLOOR}; "
                                        f"the judge named {gap}")
            gap = None
        else:
            reason = None
    else:
        if gap is not None:
            raise JudgeOutputInvalid("attempted is false but gap_type is set")
        problem = abstain_reason_problem(reason)
        if problem:
            raise JudgeOutputInvalid(problem)
    secondary = [g for g in output.get("secondary") or [] if g != gap]
    claims = output.get("claims")
    if not isinstance(claims, list):
        raise JudgeOutputInvalid("claims is not an array")
    try:
        diag = Diagnosis(company_id=company_id, prompt_version=prompt_version,
                         attempted=attempted, gap_type=gap, confidence=conf,
                         abstain_reason=reason, need=output.get("need"),
                         secondary=secondary, cannot_see=output.get("cannot_see"))
    except ValidationError as e:
        raise JudgeOutputInvalid(e.errors()[0].get("msg", "invalid")) from e
    return diag, claims, note


def finalise(conn: sqlite3.Connection, cache: Cache, company_id: str, prompt_version: str,
             output: Any) -> tuple[Diagnosis, str | None]:
    """Contract, confidence floor, then faithfulness: the row to store."""
    diag, raws, note = to_diagnosis(company_id, prompt_version, output)
    return F.apply(diag, raws, F.load_arts(conn, cache, company_id)), note


# -- storing and status ------------------------------------------------------------------


def stored(conn: sqlite3.Connection, company_id: str, prompt_version: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM diagnoses WHERE company_id = ? AND prompt_version = ?",
                        (company_id, prompt_version)).fetchone()


def write(conn: sqlite3.Connection, diag: Diagnosis) -> None:
    with conn:
        db.insert(conn, diag)


def update(conn: sqlite3.Connection, diag: Diagnosis) -> None:
    f = diag.faithfulness.model_dump(mode="json") if diag.faithfulness else None
    with conn:
        conn.execute(
            "UPDATE diagnoses SET attempted = ?, gap_type = ?, abstain_reason = ?, claims = ?, "
            "faithfulness = ? WHERE company_id = ? AND prompt_version = ?",
            (int(diag.attempted), diag.gap_type, diag.abstain_reason,
             json.dumps([c.model_dump(mode="json") for c in diag.claims], ensure_ascii=False),
             json.dumps(f, ensure_ascii=False) if f is not None else None,
             diag.company_id, diag.prompt_version))


def apply_status(conn: sqlite3.Connection, company_id: str, attempted: bool) -> str:
    """Move the company after a stored diagnosis. Returns what happened."""
    status = Status(_company(conn, company_id)["status"])
    if attempted:
        if status is Status.EXTRACTED:
            states.advance(company_id, Status.DIAGNOSED, conn)
            return "extracted -> diagnosed"
        return f"kept {status}"
    if status in (Status.EXTRACTED, Status.DIAGNOSED):
        states.drop(company_id, DROP_REASON, DROP_STAGE, conn)
        return f"{status} -> dropped ({DROP_REASON})"
    return f"kept {status}"


# -- selection ---------------------------------------------------------------------------


def select(conn: sqlite3.Connection, *, company_id: str | None = None, pilot: bool = False,
           limit: int | None = None) -> list[str]:
    """Companies at extracted or later, not dropped."""
    if company_id is not None:
        status = _company(conn, company_id)["status"]
        if Status(status) not in _FROM_EXTRACTED:
            raise JudgeError(f"{company_id}: status {status}; the judge needs extracted facts")
        return [company_id]
    sql = "SELECT company_id, status FROM companies"
    if pilot:
        sql += " WHERE pilot = 1 OR hand = 1"
    ids = [r["company_id"] for r in conn.execute(sql + " ORDER BY company_id")
           if Status(r["status"]) in _FROM_EXTRACTED]
    return ids[:limit] if limit is not None else ids


def version_for(no_examples: bool) -> str:
    return PROMPT_VERSION + (NO_EXAMPLES_SUFFIX if no_examples else "")


# -- one company -------------------------------------------------------------------------


@dataclass
class Result:
    company_id: str
    outcome: str  # diagnosed | abstained | existing | scoring_failed | refused | error
    detail: str = ""
    status: str = ""
    calls: int = 0  # provider calls recorded in llm_calls for this company
    masked: int = 0
    diagnosis: Diagnosis | None = None


def _calls(conn: sqlite3.Connection, company_id: str, prompt_version: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM llm_calls WHERE stage = ? AND company_id = ? AND "
                        "prompt_version = ?", (STAGE, company_id, prompt_version)).fetchone()[0]


def judge_company(conn: sqlite3.Connection, cache: Cache, company_id: str, *,
                  examples: list[dict[str, Any]] | None, budget: Budget | None = None) -> Result:
    version = version_for(examples is None)
    move = examples is not None  # a --no-examples run never moves a status
    row = stored(conn, company_id, version)
    if row is not None:
        status = apply_status(conn, company_id, bool(row["attempted"])) if move else ""
        return Result(company_id, "existing", row["gap_type"] or "abstained", status)
    prompt, masked = build_prompt(conn, cache, company_id, examples)
    before = _calls(conn, company_id, version)
    try:
        output = gateway.call(STAGE, company_id, version, ROUTE, prompt, schema(), conn=conn,
                              budget=budget)
    except SchemaInvalid as e:
        return Result(company_id, "scoring_failed", str(e), masked=masked,
                      calls=_calls(conn, company_id, version) - before)
    except (ContactInPrompt, HeldOutInPrompt) as e:
        return Result(company_id, "refused", str(e), masked=masked)
    except LlmError as e:
        return Result(company_id, "error", f"{type(e).__name__}: {e}", masked=masked,
                      calls=_calls(conn, company_id, version) - before)
    calls = _calls(conn, company_id, version) - before
    try:
        diag, note = finalise(conn, cache, company_id, version, output)
    except JudgeOutputInvalid as e:
        return Result(company_id, "scoring_failed", f"output breaks the diagnosis contract: {e}",
                      calls=calls, masked=masked)
    write(conn, diag)
    status = apply_status(conn, company_id, diag.attempted) if move else ""
    detail = diag.gap_type or (diag.abstain_reason or "")
    if note:
        detail += f" ({note})"
    return Result(company_id, "diagnosed" if diag.attempted else "abstained", detail, status,
                  calls, masked, diag)


def refaithful(conn: sqlite3.Connection, cache: Cache, company_id: str, *,
               prompt_version: str = PROMPT_VERSION, dry_run: bool = False) -> Result:
    """Re-run the faithfulness check on a stored diagnosis, from the judge's
    original output in llm_calls when it is there, else from the stored claims."""
    row = stored(conn, company_id, prompt_version)
    if row is None:
        raise JudgeError(f"{company_id}: no diagnosis at {prompt_version}")
    hit, output = gateway.cached_output(conn, STAGE, company_id, prompt_version)
    if hit:
        try:
            diag, _ = finalise(conn, cache, company_id, prompt_version, output)
        except JudgeOutputInvalid as e:
            raise JudgeError(f"{company_id}: stored judge output breaks the contract: {e}") from e
        source = "judge output"
    else:
        base = Diagnosis(company_id=company_id, prompt_version=prompt_version,
                         attempted=bool(row["attempted"]), gap_type=row["gap_type"],
                         confidence=row["confidence"], abstain_reason=row["abstain_reason"],
                         need=row["need"], secondary=json.loads(row["secondary"]),
                         cannot_see=row["cannot_see"])
        diag = F.apply(base, json.loads(row["claims"]), F.load_arts(conn, cache, company_id))
        source = "stored claims"
    status = ""
    if not dry_run:
        update(conn, diag)
        if not prompt_version.endswith(NO_EXAMPLES_SUFFIX):
            status = apply_status(conn, company_id, diag.attempted)
    detail = f"{diag.gap_type or diag.abstain_reason} (from {source})"
    return Result(company_id, "diagnosed" if diag.attempted else "abstained", detail, status,
                  0, 0, diag)


# -- dry run -----------------------------------------------------------------------------


@dataclass
class Projection:
    company_id: str
    chars: int
    would_call: bool
    reason: str = ""  # why no call, or why it would be refused
    masked: int = 0


@dataclass
class DryRun:
    rows: list[Projection] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return sum(1 for r in self.rows if r.would_call)


def dry_run(conn: sqlite3.Connection, cache: Cache, ids: list[str], *,
            examples: list[dict[str, Any]] | None) -> DryRun:
    """Prompt sizes and projected calls. No call, no write."""
    version = version_for(examples is None)
    out = DryRun()
    for cid in ids:
        prompt, masked = build_prompt(conn, cache, cid, examples)
        if stored(conn, cid, version) is not None:
            out.rows.append(Projection(cid, len(prompt), False, "diagnosed already", masked))
            continue
        if gateway.cached_output(conn, STAGE, cid, version)[0]:
            out.rows.append(Projection(cid, len(prompt), False, "cached output", masked))
            continue
        try:
            gateway.check_no_contacts(conn, prompt)
            gateway.check_no_held_out(conn, cid, prompt)
        except (ContactInPrompt, HeldOutInPrompt) as e:
            out.rows.append(Projection(cid, len(prompt), False, f"would be refused: {e}", masked))
            continue
        out.rows.append(Projection(cid, len(prompt), True, "", masked))
    return out
