"""The verifier (docs/PLAN.md Step 49; protocol/GAPS.md v1.2 "Checks after the
judge", item 2).

    te judge verify [--company <id> | --pilot | --limit <n>] [--dry-run] [--no-examples]

Runs after the faithfulness check, on every stored diagnosis that names a gap.
Each surviving claim (the claims faithfulness kept) goes, one call per claim,
to the "verifier" route with:

- the claim's text, artifact kind and url;
- for a found claim, its quote and VERIFY_CONTEXT characters of the text around
  it, taken from where the quote was found: the artifact's extracted text, or
  its nav and footer text (structure removed_text). Half before the quote and
  half after, shifted to one side at an edge of the text, so the surrounding
  text totals VERIFY_CONTEXT characters whenever the text is long enough. A
  quote matched only in the page's structure (a link, a field label, a script
  src) gets that string as its context;
- for a not-found claim, the capture outcome of that artifact kind and where it
  was looked for;
- the gap's definition: its row of the gap table in protocol/GAPS.md.

The verifier answers {verdict: supported | not_supported | unsure, reason}.

Never the family that wrote the claim (CLAUDE.md). The writer is the provider
whose output the judge's diagnosis was built from: the last valid llm_calls row
for (judge, company_id, the diagnosis's prompt_version). Its family
(llm.routes.family) is passed to gateway.call as exclude_family. The verifier
refuses a diagnosis when that row is missing, when the writer's family is
unknown, or when no target on the route is of another family; and it checks
the family of the provider that actually answered (llm_calls, by the model it
reported) before using a verdict: the writer's family, or one that cannot be
told, is refused.

Contacts are masked before the call as the judge masks them
(judge.mask_contacts); the gateway's contacts and held-out guards still run.

Outcome (GAPS.md):
- two or more not_supported: the diagnosis becomes an abstention, abstain_reason
  "thin: verifier: ...", and the company is dropped as no_gap at stage judge
  (only from extracted or diagnosed, as the judge does);
- one not_supported: recorded as outcome "review": the Step 65 quality gate
  fails the teardown before human review; it is never sent as is;
- otherwise "pass".
Per-claim verdicts go to diagnoses.verifier_verdict as JSON (VerifierVerdict),
with the judge's named gap, so a verifier abstention can be re-applied.

Cache: each call is keyed on (stage "verify", company_id,
"v-1.0:<diagnosis key>:<claim index>"), where the diagnosis key hashes the
judge's prompt_version and the stored claims. A re-run makes no call; changed
claims (a faithfulness re-run) make a new key. Changing the verifier prompt,
schema or context rule bumps VERIFY_VERSION.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.extract.base import Art
from engine.guard.budget import Budget
from engine.guard.cache import Cache
from engine.judge import faithfulness as F
from engine.judge import gold as G
from engine.judge import judge as J
from engine.llm import gateway
from engine.llm.base import ContactInPrompt, HeldOutInPrompt, LlmError
from engine.llm.routes import Target, family, get_route
from engine.models import Claim

VERIFY_VERSION = "v-1.0"
STAGE = "verify"
ROUTE = "verifier"
VERIFY_CONTEXT = 300  # characters of surrounding text, beside the quote itself
MAX_NOT_SUPPORTED = 1  # more than this turns the diagnosis into an abstention
MAX_REASON = 300

Verdict = Literal["supported", "not_supported", "unsure"]
VERDICTS: tuple[str, ...] = ("supported", "not_supported", "unsure")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "reason": {"type": "string", "maxLength": MAX_REASON},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}

PROMPT = """\
You check one piece of evidence for a diagnosis written by another model.

The diagnosis says a company's public pages show this gap, framed as what a new
go-to-market hire will need:

{gap}

The claim, made about the {kind} page ({url}):
{claim}

{evidence}

Question: read as it stands, does this evidence support the claim, and the claim
as evidence for this gap? Judge only what the text shows, never what may happen
inside the company.
- supported: the evidence says what the claim says, and it counts toward the gap.
- not_supported: the evidence does not say it, says something else in context,
  or does not bear on this gap.
- unsure: you cannot tell from what is shown.
Answer with the verdict and one short reason (at most {max_reason} characters).
"""

FOUND_EVIDENCE = """\
Quote (verbatim from the page):
"{quote}"

Where it appears ({where}), with the surrounding text:
{context}"""

STRUCTURE_EVIDENCE = """\
Quote (verbatim from the page):
"{quote}"

It appears in the page's structure (a link, a form field label or a script
source), as this string:
{context}"""

NOT_FOUND_EVIDENCE = """\
This claim says something is absent, so there is no quote.
Capture outcome for the {kind} page: {outcome}. Looked for at: {looked}.
(A not_found outcome means the capture found no such page or record.)"""


class VerifyError(RuntimeError):
    """The verifier refuses a diagnosis (no writer on record, no other family)."""


# -- the record --------------------------------------------------------------------------


class ClaimVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    artifact_kind: str
    found: bool
    verdict: Verdict
    reason: str
    provider: str
    family: str


class VerifierVerdict(BaseModel):
    """diagnoses.verifier_verdict."""

    model_config = ConfigDict(extra="forbid")

    version: str
    key: str  # the diagnosis key the verdicts belong to
    gap_type: str  # the gap the judge named
    writer: str  # provider of the judge's output
    writer_family: str
    claims: list[ClaimVerdict]
    not_supported: int = Field(ge=0)
    outcome: Literal["pass", "review", "abstained"]


def outcome_for(not_supported: int) -> Literal["pass", "review", "abstained"]:
    if not_supported > MAX_NOT_SUPPORTED:
        return "abstained"
    return "review" if not_supported else "pass"


# -- keys and the writer -----------------------------------------------------------------


def diagnosis_key(prompt_version: str, claims_json: str) -> str:
    claims = json.loads(claims_json)
    blob = json.dumps([prompt_version, claims], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def call_version(key: str, index: int) -> str:
    return f"{VERIFY_VERSION}:{key}:{index}"


def writer(conn: sqlite3.Connection, company_id: str, prompt_version: str) -> Target:
    """The provider whose output the judge's diagnosis came from."""
    row = conn.execute(
        "SELECT provider, model FROM llm_calls WHERE stage = ? AND company_id = ? AND "
        "prompt_version = ? AND output IS NOT NULL ORDER BY call_id DESC LIMIT 1",
        (J.STAGE, company_id, prompt_version)).fetchone()
    if row is None:
        raise VerifyError(f"{company_id}: no judge call on record for {prompt_version}; the "
                          "family that wrote the claims is unknown, so no verifier can be "
                          "chosen")
    return Target(row["provider"], row["model"])


def allowed_targets(writer_family: str) -> list[Target]:
    """The verifier route's targets of another family; VerifyError when none."""
    if writer_family.startswith("unknown:"):
        raise VerifyError(f"the judge's model family is {writer_family}; the verifier cannot "
                          "prove it is a different family")
    targets = [t for t in get_route(ROUTE).targets if family(t) != writer_family]
    if not targets:
        raise VerifyError(f"every target on the {ROUTE} route is of the {writer_family} family "
                          "that wrote the claims")
    return targets


def answered_by(conn: sqlite3.Connection, company_id: str, version: str) -> Target | None:
    row = conn.execute(
        "SELECT provider, model FROM llm_calls WHERE stage = ? AND company_id = ? AND "
        "prompt_version = ? AND output IS NOT NULL ORDER BY call_id DESC LIMIT 1",
        (STAGE, company_id, version)).fetchone()
    return Target(row["provider"], row["model"]) if row else None


# -- the gap definition ------------------------------------------------------------------


def gap_definition(gap: str, gaps_md: str | None = None) -> str:
    """The gap's row of protocol/GAPS.md's gap table, as labelled lines."""
    text = gaps_md if gaps_md is not None else G.GAPS_PATH.read_text(encoding="utf-8")
    table = J.gap_table(text)
    if len(table) < 3:
        raise VerifyError("no gap table in protocol/GAPS.md")
    heads = [c.strip() for c in table[0].strip().strip("|").split("|")]
    for line in table[2:]:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0].split(" ")[0] == gap:
            return "\n".join(f"{h}: {c}" for h, c in zip(heads, cells, strict=False))
    raise VerifyError(f"gap {gap} is not in protocol/GAPS.md's gap table")


# -- context -----------------------------------------------------------------------------


def _norm_map(text: str) -> tuple[str, list[int]]:
    """text normalised as guard_norm does it (whitespace runs to one space, smart
    quotes folded, case folded), and for each normalised character the index of
    the original character it came from."""
    out: list[str] = []
    idx: list[int] = []
    space = True  # drop leading whitespace
    for i, ch in enumerate(text):
        if ch.isspace():
            if not space:
                out.append(" ")
                idx.append(i)
                space = True
            continue
        space = False
        for c in ch.translate(G._SMART).casefold():
            out.append(c)
            idx.append(i)
    if out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def locate(text: str, quote: str) -> tuple[int, int] | None:
    """(start, end) of the quote in the original text, matched after normalisation."""
    q = G.guard_norm(quote)
    if not q:
        return None
    norm, idx = _norm_map(text)
    at = norm.find(q)
    if at < 0:
        return None
    return idx[at], idx[at + len(q) - 1] + 1


def window(text: str, start: int, end: int, size: int = VERIFY_CONTEXT) -> tuple[str, str]:
    """(before, after): `size` characters around text[start:end], half on each side,
    the unused part of one side given to the other at an edge of the text."""
    half = size // 2
    before_n = min(start, half)
    after_n = min(len(text) - end, size - before_n)
    before_n = min(start, size - after_n)
    return text[start - before_n:start], text[end:end + after_n]


@dataclass
class Evidence:
    text: str  # the evidence block of the prompt
    where: str  # text | removed_text | structure | not_found
    context: str = ""  # the surrounding text alone (before + after), for tests and audit


def evidence(claim: Claim, arts: dict[str, Art]) -> Evidence:
    art = arts.get(claim.artifact_kind)
    if not claim.found:
        outcome = art.outcome if art is not None else "no artifact row"
        return Evidence(NOT_FOUND_EVIDENCE.format(kind=claim.artifact_kind, outcome=outcome,
                                                  looked=claim.url), "not_found")
    quote = claim.quote or ""
    if art is not None:
        for where, hay in (("text", art.text or ""), ("removed_text", art.removed)):
            span = locate(hay, quote) if hay else None
            if span is None:
                continue
            before, after = window(hay, *span)
            shown = f"...{before}[[{hay[span[0]:span[1]]}]]{after}..."
            label = ("the page text" if where == "text" else "the page's navigation or footer")
            return Evidence(FOUND_EVIDENCE.format(quote=quote, where=label, context=shown),
                            where, before + after)
        q = G.guard_norm(quote)
        for s in F._strings(art.structure):
            if q and q in G.guard_norm(s):
                return Evidence(STRUCTURE_EVIDENCE.format(quote=quote, context=s[:1000]),
                                "structure", s[:1000])
    raise VerifyError(f"claim quote not found in the {claim.artifact_kind} artifact; re-run "
                      "`te judge faithfulness` first")


def build_prompt(conn: sqlite3.Connection, claim: Claim, arts: dict[str, Art],
                 gap: str) -> tuple[str, int, Evidence]:
    """(masked prompt, contact values masked, the evidence)."""
    ev = evidence(claim, arts)
    prompt = PROMPT.format(gap=gap_definition(gap), kind=claim.artifact_kind, url=claim.url,
                           claim=claim.text, evidence=ev.text, max_reason=MAX_REASON)
    masked, n = J.mask_contacts(conn, prompt)
    return masked, n, ev


# -- selection ---------------------------------------------------------------------------


def select(conn: sqlite3.Connection, *, prompt_version: str, company_id: str | None = None,
           pilot: bool = False, limit: int | None = None) -> list[str]:
    """Companies with a diagnosis at prompt_version that names a gap, or that carries
    a verifier verdict (to re-apply it)."""
    sql = ("SELECT d.company_id FROM diagnoses d JOIN companies c ON c.company_id = d.company_id "
           "WHERE d.prompt_version = ? AND (d.attempted = 1 OR d.verifier_verdict IS NOT NULL)")
    params: list[Any] = [prompt_version]
    if company_id is not None:
        sql += " AND d.company_id = ?"
        params.append(company_id)
    if pilot:
        sql += " AND (c.pilot = 1 OR c.hand = 1)"
    ids = [r[0] for r in conn.execute(sql + " ORDER BY d.company_id", params)]
    return ids[:limit] if limit is not None else ids


# -- one diagnosis -----------------------------------------------------------------------


@dataclass
class Result:
    company_id: str
    outcome: str  # pass | review | abstained | existing | skipped | refused | error | dry_run
    detail: str = ""
    status: str = ""
    calls: int = 0
    masked: int = 0
    verdict: VerifierVerdict | None = None
    prompts: list[int] = field(default_factory=list)  # prompt sizes (dry run)


def _calls(conn: sqlite3.Connection, company_id: str, key: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM llm_calls WHERE stage = ? AND company_id = ? AND "
                        "prompt_version LIKE ?", (STAGE, company_id,
                                                  f"{VERIFY_VERSION}:{key}:%")).fetchone()[0]


def abstain_reason(v: VerifierVerdict) -> str:
    return (f"thin: verifier: {v.not_supported} of {len(v.claims)} claims not_supported; the "
            f"judge named {v.gap_type}")


def _store(conn: sqlite3.Connection, company_id: str, prompt_version: str,
           v: VerifierVerdict) -> None:
    blob = v.model_dump_json()
    with conn:
        if v.outcome == "abstained":
            conn.execute("UPDATE diagnoses SET attempted = 0, gap_type = NULL, abstain_reason = ?, "
                         "verifier_verdict = ? WHERE company_id = ? AND prompt_version = ?",
                         (abstain_reason(v), blob, company_id, prompt_version))
        else:
            conn.execute("UPDATE diagnoses SET verifier_verdict = ? WHERE company_id = ? AND "
                         "prompt_version = ?", (blob, company_id, prompt_version))


def _status(conn: sqlite3.Connection, company_id: str, prompt_version: str,
            attempted: bool) -> str:
    if prompt_version.endswith(J.NO_EXAMPLES_SUFFIX):  # a dry test never moves a status
        return ""
    return J.apply_status(conn, company_id, attempted)


def verify_company(conn: sqlite3.Connection, cache: Cache, company_id: str, *,
                   prompt_version: str = J.PROMPT_VERSION, budget: Budget | None = None,
                   dry_run: bool = False) -> Result:
    row = J.stored(conn, company_id, prompt_version)
    if row is None:
        raise VerifyError(f"{company_id}: no diagnosis at {prompt_version}")
    key = diagnosis_key(prompt_version, row["claims"])
    old = (VerifierVerdict.model_validate_json(row["verifier_verdict"])
           if row["verifier_verdict"] else None)
    if old is not None and old.key == key:
        # Verified already. Re-apply: a faithfulness re-run may have reset the row.
        if not dry_run and old.outcome == "abstained" and row["attempted"]:
            _store(conn, company_id, prompt_version, old)
        status = "" if dry_run else _status(conn, company_id, prompt_version,
                                            old.outcome != "abstained")
        return Result(company_id, "existing", f"{old.outcome}; not_supported "
                      f"{old.not_supported} of {len(old.claims)}", status, verdict=old)
    if not row["attempted"]:
        return Result(company_id, "skipped", "an abstention whose claims changed since it was "
                      "verified; nothing to verify")
    claims = [Claim.model_validate(c) for c in json.loads(row["claims"])]
    gap = row["gap_type"]
    try:
        w = writer(conn, company_id, prompt_version)
        wfam = family(w)
        allowed_targets(wfam)
    except VerifyError as e:
        return Result(company_id, "refused", str(e))
    arts = F.load_arts(conn, cache, company_id)
    before = _calls(conn, company_id, key)
    verdicts: list[ClaimVerdict] = []
    masked = 0
    sizes: list[int] = []
    for i, claim in enumerate(claims):
        version = call_version(key, i)
        try:
            prompt, n, _ = build_prompt(conn, claim, arts, gap)
        except VerifyError as e:
            return Result(company_id, "refused", str(e), masked=masked)
        masked += n
        sizes.append(len(prompt))
        if dry_run:
            continue
        try:
            out = gateway.call(STAGE, company_id, version, ROUTE, prompt, SCHEMA, conn=conn,
                               budget=budget, exclude_family=wfam)
        except (ContactInPrompt, HeldOutInPrompt) as e:
            return Result(company_id, "refused", str(e), masked=masked)
        except LlmError as e:
            return Result(company_id, "error", f"{type(e).__name__}: {e}", masked=masked,
                          calls=_calls(conn, company_id, key) - before)
        by = answered_by(conn, company_id, version)
        if by is None or family(by) == wfam or family(by).startswith("unknown:"):
            return Result(company_id, "refused", f"claim {i}: the verdict on record was not "
                          f"written by another family than {wfam}; it is not used",
                          masked=masked, calls=_calls(conn, company_id, key) - before)
        verdicts.append(ClaimVerdict(index=i, artifact_kind=claim.artifact_kind,
                                     found=claim.found, verdict=out["verdict"],
                                     reason=out["reason"][:MAX_REASON], provider=by.provider,
                                     family=family(by)))
    if dry_run:
        return Result(company_id, "dry_run", f"{len(claims)} claims; writer {w.provider} "
                      f"({wfam})", masked=masked, prompts=sizes)
    calls = _calls(conn, company_id, key) - before
    ns = sum(1 for v in verdicts if v.verdict == "not_supported")
    v = VerifierVerdict(version=VERIFY_VERSION, key=key, gap_type=gap, writer=w.provider,
                        writer_family=wfam, claims=verdicts, not_supported=ns,
                        outcome=outcome_for(ns))
    _store(conn, company_id, prompt_version, v)
    status = _status(conn, company_id, prompt_version, v.outcome != "abstained")
    detail = f"not_supported {ns} of {len(verdicts)}"
    if v.outcome == "abstained":
        detail += f"; {abstain_reason(v)}"
    elif v.outcome == "review":
        detail += "; flagged: the quality gate fails it before human review"
    return Result(company_id, v.outcome, detail, status, calls, masked, v)
