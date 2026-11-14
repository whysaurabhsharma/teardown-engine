"""The drafter (docs/PLAN.md Step 64; protocol/TEARDOWN_SPEC.md v1.2).

    te draft <company_id> | --company <id> | --pilot [--limit n] [--dry-run] [--force] [--no-voice]
    te draft init-voice [--force]

One model call per company on the "drafter" route writes sections 3, 4, 5 and
7 of the teardown and the email block, into data/teardowns/<company_id>.yaml
(created with `te teardown init` logic when missing). The output is a schema
(drafter_schema.json) with one field per section; nothing parses prose.

Inputs (engine/build/drafter_prompt.md, PROMPT_VERSION):
- the spec (protocol/TEARDOWN_SPEC.md in full), the word limits, the banned
  phrases and superlatives from engine/build/teardown.py;
- the signal's offer type and its email variant;
- the voice examples: data/voice_examples.yaml ($TE_VOICE_EXAMPLES or
  --voice overrides; gitignored with data/), emails actually sent with names
  removed. `te draft init-voice` writes a skeleton. An example still holding
  TODO does not count. Fewer than VOICE_TARGET: the run goes on with what
  exists and warns. None: refused unless --no-voice. A voice example holding
  an email address or a contact's name (any contacts name or name part) is
  refused; the message never names the value;
- the company: name, role, gap, the judge's need and cannot_see, the passing
  claims only (kept by faithfulness, the verifier's outcome pass, not
  not_supported) with their original indices, the facts, and the proof slice
  `te slice build` selected (up to ten rows).
Every contacts value left in the prompt is masked as the judge masks it
(judge.mask_contacts); the gateway's contacts and held-out guards still run.
The recipient's name never reaches the drafter: the email greets
{first_name}, filled locally by `te queue draft`.

Refused before any call: a dropped company; no diagnosis at the judge's
current prompt version, or an abstention; the verifier not run, stale, or its
outcome not pass (one not_supported fails the quality gate anyway); fewer than
two passing claims; an offer type other than candidate | adjacent; no
selected proof slice.

Cache: gateway key (stage "draft", company_id, "d-1.0:<sha256 of the prompt
and schema>"). The same inputs make no call on a re-run; a changed input makes
a new key.

The file: the drafted sections (need, why_now, build, might_have_wrong, email)
replace those blocks in the YAML text, leaving every other block and comment
as it is, and a `drafted` block records {by: d-1.0, key, sections: a sha256 per
section as drafted}. A section whose current value no longer matches its
recorded hash was edited by a person; a file with no `drafted` block whose
drafted sections hold anything but TODO was written by a person. Either way
the run is refused unless --force. The same key with no edits is unchanged:
no call and no write. need.evidence (the two or three quoted lines) is chosen
here from the passing claims the sentences cite, one artifact kind first.

No network but the gateway's provider call. Quality gates run separately
(`te quality`, engine/build/quality.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from engine.build import teardown as T
from engine.guard.budget import Budget
from engine.llm import gateway
from engine.llm.base import ContactInPrompt, HeldOutInPrompt, LlmError, SchemaInvalid

HERE = Path(__file__).parent
PROMPT_PATH = HERE / "drafter_prompt.md"
SCHEMA_PATH = HERE / "drafter_schema.json"
SPEC_PATH = Path(__file__).resolve().parents[2] / "protocol" / "TEARDOWN_SPEC.md"

PROMPT_VERSION = "d-1.0"
STAGE = "draft"
ROUTE = "drafter"
KEY_CHARS = 16

VOICE_ENV_VAR = "TE_VOICE_EXAMPLES"
DEFAULT_VOICE_PATH = Path("data") / "voice_examples.yaml"
VOICE_VERSION = "voice-1"
VOICE_TARGET = 3
SLICE_ROWS = T.SLICE_MAX_ROWS
SECTIONS = ("need", "why_now", "build", "might_have_wrong", "email")
SECTION_LABELS = {"need": "section 3", "why_now": "section 4", "build": "section 5",
                  "might_have_wrong": "section 7", "email": "the email block"}

_COMMENT = re.compile(r"<!--.*?-->\s*", re.DOTALL)
_VERSION_COMMENT = re.compile(r"<!--\s*prompt_version:\s*(\S+)\s*-->")
_EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_TOP_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")


class DrafterError(RuntimeError):
    """The drafter refuses (bad inputs, voice examples, a human-edited file)."""


# -- the template --------------------------------------------------------------------------


def template_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def template_version(text: str | None = None) -> str | None:
    m = _VERSION_COMMENT.search(text if text is not None else template_text())
    return m.group(1) if m else None


def template() -> str:
    return _COMMENT.sub("", template_text()).strip() + "\n"


@cache
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def spec_text() -> str:
    return SPEC_PATH.read_text(encoding="utf-8").strip()


# -- voice examples ------------------------------------------------------------------------


class VoiceExample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant: str | None = None  # candidate | adjacent
    subject: str = ""
    body: str


class VoiceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = VOICE_VERSION
    examples: list[VoiceExample] = []


def voice_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(VOICE_ENV_VAR)
    return Path(env) if env else DEFAULT_VOICE_PATH


VOICE_SKELETON = f"""# data/voice_examples.yaml: the voice the drafter matches (te draft init-voice).
# Gitignored. Paste up to three emails you actually sent in the pilot, with
# every name, company, address and link replaced by a placeholder such as
# {{first_name}}, {{Company}}, {{role}} or {{link}}. Never leave an email address or
# a person's name here: the drafter refuses the file if it finds one. An example
# that still reads TODO is not used.
version: {VOICE_VERSION}
examples:
  - variant: candidate        # candidate | adjacent
    subject: "TODO: the subject line, names removed"
    body: |
      TODO: the email body as sent, names removed.
  - variant: adjacent
    subject: "TODO"
    body: |
      TODO
  - variant: adjacent
    subject: "TODO"
    body: |
      TODO
"""


def init_voice(path: str | os.PathLike[str] | None = None, *, force: bool = False) -> Path:
    p = voice_path(path)
    if p.exists() and not force:
        raise DrafterError(f"{p} exists; pass --force to overwrite it")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(VOICE_SKELETON, encoding="utf-8")
    return p


def load_voice(path: str | os.PathLike[str] | None = None) -> list[VoiceExample]:
    """The filled examples (no TODO, a body). A missing file is no examples."""
    p = voice_path(path)
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        vf = VoiceFile.model_validate(data)
    except (yaml.YAMLError, ValidationError) as e:
        raise DrafterError(f"{p}: not a voice examples file: "
                           f"{' '.join(str(e).split())[:300]}") from e
    return [e for e in vf.examples
            if e.body.strip() and T.TODO not in e.body and T.TODO not in e.subject]


def _contact_names(conn: sqlite3.Connection) -> set[str]:
    """Every contacts name and each part of it (gateway.MIN_CONTACT_LEN or more)."""
    out: set[str] = set()
    for r in conn.execute("SELECT name FROM contacts"):
        v = " ".join((r["name"] or "").split())
        for p in [v, *v.split(" ")]:
            if len(p) >= gateway.MIN_CONTACT_LEN:
                out.add(p)
    return out


def voice_problems(conn: sqlite3.Connection, examples: list[VoiceExample]) -> list[str]:
    """Why these examples may not enter a prompt. Never names the value."""
    names = _contact_names(conn)
    out = []
    for n, e in enumerate(examples, start=1):
        text = f"{e.subject}\n{e.body}"
        if _EMAIL_ADDRESS.search(text):
            out.append(f"voice example {n} holds an email address; replace it with a "
                       "placeholder")
        if any(gateway.contact_pattern(v).search(text) for v in names):
            out.append(f"voice example {n} holds a name from the contacts table; replace it "
                       "with {first_name}")
    return out


def voice_block(examples: list[VoiceExample]) -> str:
    if not examples:
        return "(No voice examples in this run. Write plainly and briefly, as the spec's " \
               "email variants read.)"
    out = []
    for n, e in enumerate(examples, start=1):
        head = f"### Voice example {n}" + (f" ({e.variant})" if e.variant else "")
        subj = f"Subject: {' '.join(e.subject.split())}\n" if e.subject.strip() else ""
        out.append(f"{head}\n{subj}\n{e.body.strip()}")
    return "\n\n".join(out)


# -- the company block ---------------------------------------------------------------------


def passing_claims(ctx: T.Context) -> list[int]:
    return [i for i in range(len(ctx.claims)) if T.claim_problem(ctx, i) is None]


def _q(s: Any) -> str:
    return json.dumps(s, ensure_ascii=False)


def _facts(conn: sqlite3.Connection, company_id: str) -> str:
    rows = conn.execute("SELECT fact, value, artifact_kind, found, quote FROM facts WHERE "
                        "company_id = ? ORDER BY artifact_kind, fact, fact_id",
                        (company_id,)).fetchall()
    if not rows:
        return "(no facts)"
    out = []
    for r in rows:
        if r["found"]:
            out.append(f"- {r['fact']} = {r['value']} [{r['artifact_kind']}] quote: "
                       f"{_q(r['quote'])}")
        else:
            out.append(f"- {r['fact']} = {r['value']} [{r['artifact_kind']}] not found")
    return "\n".join(out)


def _gap_row(gap: str | None) -> str:
    """The gap's row of the gap table in protocol/GAPS.md, or nothing."""
    if not gap:
        return ""
    from engine.judge import gold as G
    from engine.judge import judge as J

    for line in J.gap_table(G.GAPS_PATH.read_text(encoding="utf-8")):
        if line.startswith(f"| {gap} |") or line.startswith(f"| {gap} ("):
            return line
    return ""


def company_block(conn: sqlite3.Connection, ctx: T.Context) -> str:
    d = ctx.diagnosis
    assert d is not None
    claims = []
    for i in passing_claims(ctx):
        c = ctx.claims[i]
        what = f"found, quote: {_q(c.quote)}" if c.found else "not found (no quote)"
        claims.append(f"[{i}] {c.artifact_kind}, {what}\n    url: {c.url}\n    says: "
                      f"{' '.join(c.text.split())}")
    st = ctx.slice
    rows = "\n".join(f"- {' | '.join(r['cells'])} (source {r['url']}, quote: {_q(r['quote'])})"
                     for r in st.rows[:SLICE_ROWS])
    gap = d["gap_type"]
    lines = [
        f"Company: {ctx.name}",
        f"Role: {ctx.role or 'unknown'}",
        f"Offer type: {ctx.offer_type}",
        f"Gap: {gap}",
        f"Gap definition: {_gap_row(gap) or '(not in the table)'}",
        f"What the hire will need (the judge): {' '.join((d['need'] or '').split())}",
        f"What the pages cannot show (the judge): {' '.join((d['cannot_see'] or '').split())}",
        "",
        "### Claims you may cite, by index",
        "\n".join(claims),
        "",
        "### Facts",
        _facts(conn, ctx.company_id),
        "",
        f"### The proof slice (section 6, shown by code): {st.kind}",
        f"Title: {st.title}",
        f"Columns: {' | '.join(st.columns)}",
        rows or "(no rows)",
    ]
    return "\n".join(lines)


def limits_block() -> str:
    L = T.LIMITS
    return "\n".join([
        f"- section 3: at most {L['need']} words in all its sentences",
        f"- section 4: at most {L['why_now']} words",
        "- section 5: exactly three items",
        f"- section 7: at most {L['might_have_wrong']} words",
        f"- the whole page: under {T.TOTAL_LIMIT} words without the proof slice",
        "- the email: under 120 words in all, with the fixed text of the variant",
        "- email observation: one sentence; subject_observation: exactly three words",
    ])


def banned_block() -> str:
    lines = [f'- "{p}"' for p in T.BANNED_PHRASES]
    lines.append('- "leverage" as a verb')
    lines.append("- superlatives, anywhere: " + ", ".join(f'"{s}"' for s in T.SUPERLATIVES))
    return "\n".join(lines)


def offer_block(offer_type: str) -> str:
    from engine.outreach import draft as D

    subject, body = D.TEMPLATES[offer_type]
    if offer_type == "candidate":
        how = ("The candidate variant: the sender has applied for the role. `observation` is "
               "the {One sentence: ...} line: the specific thing the public pages show. "
               "`subject_observation` is null: the subject is fixed.")
    else:
        how = ("The adjacent variant: the sender is not applying for this role. Never say "
               "the sender applied. `observation` is the {One sentence: ...} line: what that "
               "hire will need, and what the public pages suggest is missing. "
               "`subject_observation` is the {three-word observation}: exactly three words.")
    return (f"Offer type: {offer_type}\n\n{how}\n\nThe email, with your parts in braces:\n\n"
            f"Subject: {subject}\n\n{body}")


def build_prompt(conn: sqlite3.Connection, ctx: T.Context,
                 examples: list[VoiceExample]) -> tuple[str, int]:
    """(the masked prompt, contacts values masked)."""
    from engine.judge import judge as J

    assert ctx.offer_type is not None
    filled = (template()
              .replace("{{spec}}", spec_text())
              .replace("{{limits}}", limits_block())
              .replace("{{banned}}", banned_block())
              .replace("{{offer}}", offer_block(ctx.offer_type))
              .replace("{{voice}}", voice_block(examples))
              .replace("{{company}}", company_block(conn, ctx)))
    return J.mask_contacts(conn, filled)


def version_for(prompt: str) -> str:
    h = hashlib.sha256((prompt + "\n" + json.dumps(schema(), sort_keys=True)).encode("utf-8"))
    return f"{PROMPT_VERSION}:{h.hexdigest()[:KEY_CHARS]}"


# -- the output into the file --------------------------------------------------------------


def evidence_for(ctx: T.Context, cited: list[int]) -> list[int]:
    """Two or three passing claims to show as quoted / Not found lines: the cited
    ones first, one artifact kind each, then any passing claim."""
    ok = passing_claims(ctx)
    pool = [i for i in dict.fromkeys(cited) if i in ok] + [i for i in ok if i not in cited]
    picked: list[int] = []
    kinds: set[str] = set()
    for i in pool:
        if len(picked) < T.EVIDENCE_MAX and ctx.claims[i].artifact_kind not in kinds:
            picked.append(i)
            kinds.add(ctx.claims[i].artifact_kind)
    for i in pool:
        if len(picked) < T.EVIDENCE_MAX and i not in picked:
            picked.append(i)
    return sorted(picked)


def sections_from(ctx: T.Context, output: dict[str, Any]) -> dict[str, Any]:
    """The drafter's output as the teardown file's blocks."""
    s3 = [{"text": " ".join(x["sentence"].split()), "claims": list(x["claim_indices"])}
          for x in output["section3"]]
    cited = [i for x in s3 for i in x["claims"]]
    e = output["email"]
    subject = e.get("subject_observation")
    email = {
        "observation": " ".join(e["observation"].split()),
        "slice": " ".join(e["slice_name"].split()),
        "claims": list(e["observation_claims"]),
        "subject": (" ".join(subject.split()) if subject and ctx.offer_type == "adjacent"
                    else None),
    }
    return {
        "need": {"sentences": s3, "evidence": evidence_for(ctx, cited)},
        "why_now": " ".join(output["section4"].split()),
        "build": [" ".join(b.split()) for b in output["section5"]],
        "might_have_wrong": " ".join(output["section7"].split()),
        "email": email,
    }


def section_hash(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dump(key: str, value: Any) -> list[str]:
    return yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True,
                          width=100_000, default_flow_style=False).rstrip("\n").split("\n")


DRAFTED_COMMENT = (f"# Written by te draft ({PROMPT_VERSION}): the hash of each drafted section. "
                   "Edit any section freely;\n# te draft never overwrites an edited section "
                   "without --force. Do not edit this block.")


def replace_blocks(text: str, blocks: dict[str, Any]) -> str:
    """text with each top-level key's block replaced by blocks[key] (dumped as YAML),
    every other line and comment kept. A key not in the text is appended. The
    commented email stub `te teardown init` writes is removed once email is set."""
    lines = text.rstrip("\n").split("\n")
    if "email" in blocks:
        out, skipping = [], False
        for ln in lines:
            if ln.startswith("# email:"):
                skipping = True
                continue
            if skipping and ln.startswith("#   "):
                continue
            skipping = False
            out.append(ln)
        lines = out
    keys = [(i, m.group(1)) for i, ln in enumerate(lines) if (m := _TOP_KEY.match(ln))]
    spans: dict[str, tuple[int, int]] = {}
    for n, (i, k) in enumerate(keys):
        end = keys[n + 1][0] if n + 1 < len(keys) else len(lines)
        while end - 1 > i and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
            end -= 1
        spans[k] = (i, end)
    for k in sorted((k for k in blocks if k in spans), key=lambda k: -spans[k][0]):
        a, b = spans[k]
        lines[a:b] = _dump(k, blocks[k])
    for k in blocks:
        if k not in spans:
            extra = [DRAFTED_COMMENT] if k == "drafted" else []
            lines += ["", *extra, *_dump(k, blocks[k])]
    return "\n".join(lines) + "\n"


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return []


def human_edits(raw_data: dict[str, Any]) -> list[str]:
    """The drafted sections a person has written or edited."""
    mark = raw_data.get("drafted")
    if not isinstance(mark, dict) or not isinstance(mark.get("sections"), dict):
        # never drafted: any text but the init skeleton's TODO is a person's work
        return [s for s in SECTIONS
                if any(t.strip() and T.TODO not in t for t in _strings(raw_data.get(s)))]
    hashes = mark["sections"]
    return [s for s in SECTIONS if hashes.get(s) != section_hash(raw_data.get(s))]


# -- one company ---------------------------------------------------------------------------


@dataclass
class Result:
    company_id: str
    outcome: str  # drafted | unchanged | dry_run | refused | error
    detail: str = ""
    path: Path | None = None
    calls: int = 0
    masked: int = 0
    voice: int = 0
    prompt_chars: int = 0
    warnings: list[str] = field(default_factory=list)


def _calls(conn: sqlite3.Connection, company_id: str, version: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM llm_calls WHERE stage = ? AND company_id = ? AND "
                        "prompt_version = ?", (STAGE, company_id, version)).fetchone()[0]


def input_problem(ctx: T.Context) -> str | None:
    """Why this company cannot be drafted, or None."""
    d = ctx.diagnosis
    if ctx.status == "dropped":
        return "dropped; no teardown"
    if d is None:
        return "no diagnosis at the judge's current prompt version; run te judge"
    if not d["attempted"]:
        return "the diagnosis abstained; no teardown"
    v = ctx.verdict
    if v is None:
        return "the verifier has not run; run te judge verify"
    if v.key != ctx.key:
        return "the verifier's verdict is for older claims; re-run te judge verify"
    if v.outcome != "pass":
        return (f"the verifier's outcome is {v.outcome} ({v.not_supported} not_supported); "
                "the quality gate fails it before review, so it is not drafted")
    ok = passing_claims(ctx)
    if len(ok) < 2:
        return f"{len(ok)} passing claim(s); a teardown needs at least two"
    if ctx.offer_type not in ("candidate", "adjacent"):
        return (f"the signal's offer type is {ctx.offer_type!r}; it must be candidate or "
                "adjacent (protocol/OFFER.md)")
    if ctx.slice is None or not ctx.slice.ok or not ctx.slice.rows:
        return "no proof slice selected; run te slice build"
    return None


def draft_company(conn: sqlite3.Connection, company_id: str, *, directory: Path | None = None,
                  voice_file: Path | None = None, no_voice: bool = False, force: bool = False,
                  dry_run: bool = False, budget: Budget | None = None,
                  today: date | None = None) -> Result:
    if template_version() != PROMPT_VERSION:
        raise DrafterError(f"drafter_prompt.md says version {template_version()!r}; "
                           f"drafter.py PROMPT_VERSION is {PROMPT_VERSION!r}")
    try:
        ctx = T.load_context(conn, company_id)
        path = T.file_for(company_id, directory)
    except T.TeardownError as e:
        return Result(company_id, "refused", str(e))
    problem = input_problem(ctx)
    if problem:
        return Result(company_id, "refused", problem, path)

    warnings: list[str] = []
    if no_voice:
        examples: list[VoiceExample] = []
    else:
        try:
            examples = load_voice(voice_file)
        except DrafterError as e:
            return Result(company_id, "refused", str(e), path)
        if not examples:
            return Result(company_id, "refused",
                          f"no voice examples in {voice_path(voice_file)}; fill it (te draft "
                          "init-voice) or pass --no-voice", path)
        bad = voice_problems(conn, examples)
        if bad:
            return Result(company_id, "refused", "; ".join(bad), path)
        if len(examples) < VOICE_TARGET:
            warnings.append(f"{len(examples)} voice example(s); the drafter is meant to see "
                            f"{VOICE_TARGET}")

    prompt, masked = build_prompt(conn, ctx, examples)
    version = version_for(prompt)
    res = Result(company_id, "", "", path, 0, masked, len(examples), len(prompt), warnings)

    raw_text: str | None = None
    if path.exists():
        raw_text = path.read_text(encoding="utf-8")
        try:
            raw_data = yaml.safe_load(raw_text) or {}
        except yaml.YAMLError as e:
            res.outcome, res.detail = "refused", f"{path} does not parse: {str(e)[:200]}"
            return res
        if not isinstance(raw_data, dict):
            res.outcome, res.detail = "refused", f"{path} is not a teardown file"
            return res
        edited = human_edits(raw_data)
        if edited and not force:
            res.outcome = "refused"
            res.detail = ("edited by a person since it was drafted: "
                          + ", ".join(SECTION_LABELS[s] for s in edited)
                          + "; pass --force to overwrite")
            return res
        mark = raw_data.get("drafted")
        if not edited and isinstance(mark, dict) and mark.get("key") == version:
            res.outcome, res.detail = "unchanged", f"drafted already at {version}"
            return res

    if dry_run:
        hit = gateway.cached_output(conn, STAGE, company_id, version)[0]
        try:
            gateway.check_no_contacts(conn, prompt)
            gateway.check_no_held_out(conn, company_id, prompt)
            what = "cached output, no call" if hit else "would call"
        except (ContactInPrompt, HeldOutInPrompt) as e:
            what = f"would be refused: {e}"
        res.outcome, res.detail = "dry_run", f"{version}: {what}"
        return res

    before = _calls(conn, company_id, version)
    try:
        output = gateway.call(STAGE, company_id, version, ROUTE, prompt, schema(), conn=conn,
                              budget=budget)
    except (ContactInPrompt, HeldOutInPrompt) as e:
        res.outcome, res.detail = "refused", str(e)
        return res
    except SchemaInvalid as e:
        res.outcome, res.detail = "error", f"SchemaInvalid: {e}"
        res.calls = _calls(conn, company_id, version) - before
        return res
    except LlmError as e:
        res.outcome, res.detail = "error", f"{type(e).__name__}: {e}"
        res.calls = _calls(conn, company_id, version) - before
        return res
    res.calls = _calls(conn, company_id, version) - before

    blocks = sections_from(ctx, output)
    blocks["drafted"] = {"by": PROMPT_VERSION, "key": version,
                         "sections": {s: section_hash(blocks[s]) for s in SECTIONS}}
    if raw_text is None:
        try:
            T.init(conn, company_id, directory=directory, today=today)
        except T.TeardownError as e:
            res.outcome, res.detail = "refused", str(e)
            return res
        raw_text = path.read_text(encoding="utf-8")
    new_text = replace_blocks(raw_text, blocks)
    try:
        T.TeardownDoc.model_validate(yaml.safe_load(new_text))
    except (yaml.YAMLError, ValidationError) as e:
        res.outcome = "error"
        res.detail = f"the drafted file does not parse: {' '.join(str(e).split())[:300]}"
        return res
    path.write_text(new_text, encoding="utf-8")
    res.outcome, res.detail = "drafted", version
    return res


# -- selection -----------------------------------------------------------------------------


def select(conn: sqlite3.Connection, *, company_id: str | None = None, pilot: bool = False,
           limit: int | None = None) -> list[str]:
    """One company as asked, or every company (pilot = 1 with --pilot), not dropped,
    with a named gap at the judge's current prompt version."""
    if company_id is not None:
        return [company_id]
    from engine.judge import judge as J

    sql = ("SELECT c.company_id FROM companies c JOIN diagnoses d ON d.company_id = "
           "c.company_id AND d.prompt_version = ? WHERE c.status != 'dropped' AND "
           "d.attempted = 1")
    if pilot:
        sql += " AND c.pilot = 1"
    ids = [r[0] for r in conn.execute(sql + " ORDER BY c.company_id", (J.PROMPT_VERSION,))]
    return ids[:limit] if limit is not None else ids
