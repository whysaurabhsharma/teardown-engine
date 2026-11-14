"""Pilot teardown authoring and the page rules (docs/PLAN.md Step 53;
protocol/TEARDOWN_SPEC.md v1.2).

    te teardown init <company_id> [--force]
    te teardown check <company_id> | --pilot

`init` writes data/teardowns/<company_id>.yaml (gitignored; $TE_TEARDOWNS_DIR
or --dir overrides the folder) from the company's stored diagnosis at the
judge's current PROMPT_VERSION: the header, section 2 from the capture (every
artifact found or not found; blocked, dead and robots_denied left out), the
diagnosis claims listed as comments by index, and TODO placeholders for the
sections the human writes: 3 (one sentence per item, each naming the claim
indices it traces to), 4, 5, 6 (the proof slice) and 7 (pre-filled from
cannot_see as a suggestion, still marked TODO). It never overwrites a file
without --force.

`check` validates a file against TEARDOWN_SPEC and prints one line per failure
with the rule name; any failure exits 1. The rules (RULES below) are the
spec's renderer rules and the quality gates that can be decided on this file:

  structure         the YAML parses into the teardown shape
  unfinished        no TODO left (in the file or in data/sender.yaml)
  sender            data/sender.yaml exists and is complete
  diagnosis         the diagnosis the file was written from is still the stored
                    one (same prompt_version and claims key), and names a gap
  header            company, role, date present; offer_type candidate|adjacent
                    and equal to the signal's offer type when it has one
  looked_at         section 2 equals the capture: every ok / not_found artifact,
                    url and capture date as recorded; none blocked or unreachable
  word_limit        section 3 80, section 4 40, section 7 40, section 8 60 words;
                    the header is one line
  build_items       section 5 has exactly three items
  total_words       under 450 words, excluding the proof slice (see total_words)
  banned_phrase     the spec's list, anywhere on the page, quotes included
  superlative       a small lexicon, in every line written by the sender
  company_name      in the header, and at least twice in the body
  one_sentence      each section 3 item is one sentence
  untraced          each section 3 sentence names at least one claim, and every
                    claim it names passes: kept by the faithfulness check, the
                    verifier ran on these claims, its outcome is not abstained
                    and its verdict on that claim is not not_supported
  evidence          section 3 shows two or three evidence lines, each a passing
                    claim; the cited claims span at least two artifact kinds; a
                    "Not found" line rests on an artifact still not_found, a
                    quoted line on one still ok
  quote_length      every quote (evidence and slice) under 25 words
  slice_row         one to ten rows; each row a source URL (http/https) and a
                    quote; cells match the columns; an empty cell reads
                    "not found"
  contact_value     no value from the contacts table (name or name part, title,
                    email, LinkedIn URL) appears in the file; the message never
                    names the value
  placeholder       no {placeholder} left on the page: the page is shared by
                    link and carries no recipient name; {first_name} belongs in
                    the email (Step 54)
  competitor        a competitor name (from the competitors list, or a
                    named_competitors fact) appears only when marked confirmed

No network and no model call.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from engine.build import sender as S
from engine.capture import bundle as B
from engine.models import Claim

TEARDOWN_VERSION = "td-1.0"
TEARDOWNS_ENV_VAR = "TE_TEARDOWNS_DIR"
DEFAULT_TEARDOWNS_DIR = Path("data") / "teardowns"
TODO = "TODO"
NOT_FOUND_CELL = "not found"

LIMITS = {"need": 80, "why_now": 40, "might_have_wrong": 40, "about": 60}
TOTAL_LIMIT = 450  # under this
QUOTE_LIMIT = 25  # under this
BUILD_ITEMS = 3
SLICE_MAX_ROWS = 10
EVIDENCE_MIN, EVIDENCE_MAX = 2, 3
MIN_KINDS = 2
MIN_BODY_NAMES = 2

SHOWN = ("ok", "not_found")  # outcomes shown in section 2; the rest are left out

BANNED_PHRASES: tuple[str, ...] = (
    "I hope this finds you well", "quick question", "game-changing", "synergy",
    "circle back", "pick your brain",
)
# "leverage" as a verb: its verb forms, or after a subject or modal, or before a
# determiner. "Leverage" as a noun ("pricing leverage") passes.
LEVERAGE_VERB = re.compile(
    r"\bleverag(?:ed|es|ing)\b"
    r"|\b(?:to|i|we|you|they|can|could|will|would|should|must|let's|and|then)\s+leverage\b"
    r"|\bleverage\s+(?:your|our|their|its|my|the|this|that|these|those|a|an)\b",
    re.IGNORECASE)
SUPERLATIVES: tuple[str, ...] = (
    "best", "best-in-class", "world-class", "world class", "greatest", "finest",
    "unmatched", "unparalleled", "unrivalled", "unrivaled", "unbeatable",
    "industry-leading", "market-leading", "cutting-edge", "state-of-the-art",
    "top-notch", "rockstar", "ninja", "guru", "exceptional", "outstanding",
    "second to none", "number one", "#1",
)
CONTACT_FIELDS = ("name", "title", "email", "linkedin_url")  # source_url: see _contact_values
MIN_CONTACT_LEN = 3

KIND_LABELS = {
    "homepage": "homepage", "pricing": "pricing page", "product": "product page",
    "customers": "customers page", "about": "about page", "careers": "careers page",
    "contact": "contact page", "demo_form": "demo form", "compare": "comparison page",
    "job_post": "job post", "ats_board": "job board", "dns": "public DNS records",
}

RULES = ("structure", "unfinished", "sender", "diagnosis", "header", "looked_at",
         "word_limit", "build_items", "total_words", "banned_phrase", "superlative",
         "company_name", "one_sentence", "untraced", "evidence", "quote_length",
         "slice_row", "contact_value", "placeholder", "competitor")


class TeardownError(RuntimeError):
    """A teardown cannot be started or loaded."""


# -- the file ------------------------------------------------------------------------------


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagRef(_M):
    prompt_version: str
    key: str
    gap_type: str | None = None


class Header(_M):
    company: str
    role: str
    offer_type: str
    date: date


class LookedAt(_M):
    kind: str
    url: str
    found: bool
    captured: date


class Sentence(_M):
    text: str
    claims: list[int] = []


class Need(_M):
    sentences: list[Sentence]
    evidence: list[int]


class SliceRow(_M):
    cells: list[str]
    url: str | None = None
    quote: str | None = None


class Slice(_M):
    title: str
    columns: list[str]
    rows: list[SliceRow]


class Competitor(_M):
    name: str
    confirmed: bool = False


class Email(_M):
    """The words of the first email that the human writes (TEARDOWN_SPEC "The
    email, two variants"; filled by `te queue draft`, Step 54). Optional in the
    file: the page renders without it, the draft is refused until it is there."""

    observation: str  # one sentence; candidate: the specific thing the public pages
    #                   show; adjacent: what the hire will need and what seems missing
    slice: str  # the proof slice, named concretely: "built {slice} for you"
    claims: list[int] = []  # the passing claims the observation traces to
    subject: str | None = None  # adjacent only: the three-word observation


class TeardownDoc(_M):
    version: str
    company_id: str
    diagnosis: DiagRef
    header: Header
    looked_at: list[LookedAt]
    need: Need
    why_now: str
    build: list[str]
    slice: Slice
    might_have_wrong: str
    competitors: list[Competitor] = []
    email: Email | None = None


def teardowns_dir(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(TEARDOWNS_ENV_VAR)
    return Path(env) if env else DEFAULT_TEARDOWNS_DIR


def file_for(company_id: str, directory: str | os.PathLike[str] | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", company_id) or company_id.startswith("."):
        raise TeardownError(f"not a company id: {company_id!r}")
    return teardowns_dir(directory) / f"{company_id}.yaml"


# -- what the database says ----------------------------------------------------------------


@dataclass
class Context:
    company_id: str
    name: str
    status: str
    role: str | None
    offer_type: str | None
    diagnosis: sqlite3.Row | None
    claims: list[Claim]
    key: str | None
    verdict: Any  # verify.VerifierVerdict | None
    arts: dict[str, sqlite3.Row]  # kind -> artifacts row
    competitors: list[str] = field(default_factory=list)  # named_competitors facts

    @property
    def homepage_url(self) -> str | None:
        h = self.arts.get("homepage")
        return h["url"] if h is not None else None


def load_context(conn: sqlite3.Connection, company_id: str) -> Context:
    from engine.judge import judge as J
    from engine.judge import verify as V

    c = conn.execute("SELECT * FROM companies WHERE company_id = ?", (company_id,)).fetchone()
    if c is None:
        raise TeardownError(f"{company_id}: no such company")
    role = offer = None
    if c["best_signal_id"]:
        s = conn.execute("SELECT title, offer_type FROM signals WHERE signal_id = ?",
                         (c["best_signal_id"],)).fetchone()
        if s is not None:
            role, offer = s["title"], s["offer_type"]
    d = J.stored(conn, company_id, J.PROMPT_VERSION)
    claims: list[Claim] = []
    key = verdict = None
    if d is not None:
        claims = [Claim.model_validate(x) for x in json.loads(d["claims"])]
        key = V.diagnosis_key(d["prompt_version"], d["claims"])
        if d["verifier_verdict"]:
            verdict = V.VerifierVerdict.model_validate_json(d["verifier_verdict"])
    arts = {r["kind"]: r for r in conn.execute(
        "SELECT kind, url, outcome, fetched_at FROM artifacts WHERE company_id = ?",
        (company_id,))}
    comps: list[str] = []
    for r in conn.execute("SELECT value FROM facts WHERE company_id = ? AND "
                          "fact = 'named_competitors' AND found = 1", (company_id,)):
        v = r["value"]
        if v and v != "none" and v not in comps:
            comps.append(v)
    return Context(company_id, c["name"], c["status"], role, offer, d, claims, key, verdict,
                   arts, comps)


def claim_problem(ctx: Context, i: int) -> str | None:
    """None when claim i is a passing claim; else why not."""
    d = ctx.diagnosis
    if d is None:
        return "no diagnosis at the judge's current prompt version"
    if not d["attempted"]:
        return "the diagnosis abstained"
    if not 0 <= i < len(ctx.claims):
        return f"no claim [{i}] in the diagnosis (it has {len(ctx.claims)})"
    if d["faithfulness"] is None:
        return "the faithfulness check has not run on this diagnosis"
    v = ctx.verdict
    if v is None:
        return "the verifier has not run on this diagnosis (te judge verify)"
    if v.key != ctx.key:
        return "the verifier's verdict is for older claims; re-run te judge verify"
    if v.outcome == "abstained":
        return "the verifier abstained on this diagnosis"
    for cv in v.claims:
        if cv.index == i and cv.verdict == "not_supported":
            return f"the verifier found claim [{i}] not_supported"
    return None


def captured(row: sqlite3.Row) -> date:
    v = row["fetched_at"]
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()


def looked_at_rows(ctx: Context) -> list[LookedAt]:
    """Section 2 from the capture, in bundle order: found or not found only."""
    order = {k: i for i, k in enumerate(B.KINDS)}
    rows = sorted(ctx.arts.values(), key=lambda r: (order.get(r["kind"], 99), r["kind"]))
    return [LookedAt(kind=r["kind"], url=r["url"], found=r["outcome"] == "ok",
                     captured=captured(r)) for r in rows if r["outcome"] in SHOWN]


# -- init ----------------------------------------------------------------------------------


def _q(s: Any) -> str:
    return json.dumps(s, ensure_ascii=False)


def _claim_comment(ctx: Context, i: int, c: Claim) -> str:
    what = f"found {_q(c.quote)}" if c.found else "NOT FOUND"
    p = claim_problem(ctx, i)
    state = "passing" if p is None else f"NOT PASSING: {p}"
    text = " ".join(c.text.split())
    return (f"#   [{i}] {c.artifact_kind}, {what}\n#       url: {c.url}\n"
            f"#       judge: {text}\n#       {state}")


def _default_evidence(claims: list[Claim]) -> list[int]:
    picked: list[int] = []
    kinds: set[str] = set()
    for i, c in enumerate(claims):  # one per kind first, then fill
        if c.artifact_kind not in kinds and len(picked) < EVIDENCE_MAX:
            picked.append(i)
            kinds.add(c.artifact_kind)
    for i in range(len(claims)):
        if i not in picked and len(picked) < EVIDENCE_MAX:
            picked.append(i)
    return sorted(picked)


def skeleton(ctx: Context, today: date) -> str:
    d = ctx.diagnosis
    assert d is not None
    looked = "\n".join(
        f"  - {{kind: {_q(r.kind)}, url: {_q(r.url)}, found: {str(r.found).lower()}, "
        f"captured: {_q(r.captured.isoformat())}}}" for r in looked_at_rows(ctx))
    claims = "\n".join(_claim_comment(ctx, i, c) for i, c in enumerate(ctx.claims))
    need = " ".join((d["need"] or "").split())
    cannot = " ".join((d["cannot_see"] or "").split())
    comps = "\n".join(f"  - {{name: {_q(n)}, confirmed: false}}" for n in ctx.competitors)
    offer = ctx.offer_type if ctx.offer_type in ("candidate", "adjacent") else \
        "TODO: candidate or adjacent"
    return f"""# Teardown for {ctx.company_id}: written by `te teardown init`, yours to edit.
# Rules: protocol/TEARDOWN_SPEC.md v1.2. Check with `te teardown check {ctx.company_id}`;
# render with `te teardown render {ctx.company_id}`. Never write a person's name here:
# the page is shared by link and names no one; the email (Step 54) greets the recipient.
version: {TEARDOWN_VERSION}
company_id: {_q(ctx.company_id)}
diagnosis:              # do not edit: ties this file to the diagnosis it was written from
  prompt_version: {_q(d['prompt_version'])}
  key: {_q(ctx.key)}
  gap_type: {_q(d['gap_type'])}

header:                 # section 1: "<company>: notes for your <role> hire", date, your name
  company: {_q(ctx.name)}
  role: {_q(ctx.role or 'TODO: the role')}
  offer_type: {_q(offer)}   # candidate | adjacent (protocol/OFFER.md)
  date: {_q(today.isoformat())}

# Section 2, from the capture: every page found or not found. Blocked and
# unreachable pages are left out. Do not edit; `check` compares it with the capture.
looked_at:
{looked or '  []'}

# The diagnosis claims you may cite, by index. Only passing claims may be cited.
{claims or '#   (none)'}
# The judge's need: {need}

need:                   # section 3, at most 80 words; one sentence per item
  sentences:
    - text: "TODO: what the {ctx.role or 'role'} will need, as the pages show it."
      claims: []        # the claim indices this sentence traces to, e.g. [0, 2]
    - text: "TODO: what could not be found on the public pages."
      claims: []
  evidence: {_q(_default_evidence(ctx.claims))}   # 2 or 3 claim indices shown as quoted / Not found lines

why_now: "TODO: the tie to the open role, at most 40 words."   # section 4

build:                  # section 5: exactly three items, each a system component
  - "TODO: component 1"
  - "TODO: component 2"
  - "TODO: component 3"

slice:                  # section 6, the proof slice: up to ten rows, each with a source
  title: "TODO: what the slice is, named concretely"
  columns: ["TODO: column 1", "TODO: column 2"]
  rows:                 # every row: cells (one per column; "not found" when no source), url, quote
    - cells: ["TODO", "not found"]
      url: "TODO: https://..."
      quote: "TODO: the quoted line, under 25 words"

# Section 7, at most 40 words: name the specific thing that cannot be seen.
# Suggested from the diagnosis's cannot_see; edit it and remove TODO.
might_have_wrong: {_q('TODO: ' + cannot)}

# Competitors named on their pages. A name appears on the page only once confirmed
# in review (set confirmed: true).
competitors:
{comps or '  []'}

# The first email (Step 54, `te queue draft`), filled into the spec's variant for
# this offer type. Uncomment and write it once the page is done. Never a name:
# the greeting's {{first_name}} is filled locally when the draft is made.
# email:
#   observation: "One sentence: what the public pages show."
#   claims: []          # the passing claim indices it traces to
#   slice: "the proof slice, named concretely"   # "...built <slice> for you"
#   subject: "three word observation"            # adjacent only
"""


def init(conn: sqlite3.Connection, company_id: str, *, directory: Path | None = None,
         force: bool = False, today: date | None = None) -> Path:
    p = file_for(company_id, directory)
    if p.exists() and not force:
        raise TeardownError(f"{p} exists; pass --force to overwrite it")
    ctx = load_context(conn, company_id)
    if ctx.status == "dropped":
        raise TeardownError(f"{company_id}: dropped; no teardown")
    if ctx.diagnosis is None:
        raise TeardownError(f"{company_id}: no diagnosis at the judge's current prompt "
                            "version; run te judge first")
    if not ctx.diagnosis["attempted"]:
        raise TeardownError(f"{company_id}: the diagnosis abstained; no teardown")
    text = skeleton(ctx, today or date.today())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def load(path: Path) -> tuple[str, TeardownDoc]:
    """(raw text, parsed file). Raises TeardownError on a missing or malformed file."""
    if not path.exists():
        raise TeardownError(f"no teardown file at {path}; run te teardown init")
    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw) or {}
        return raw, TeardownDoc.model_validate(data)
    except (yaml.YAMLError, ValidationError) as e:
        raise TeardownError(str(e)) from e


# -- text helpers --------------------------------------------------------------------------


def words(text: str) -> int:
    return len(text.split())


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])[\"”’')\]]*\s+(?=[A-Z\"“‘'(\[])", text.strip())
    return [p for p in parts if p.strip()]


def _phrase(p: str) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(w) for w in p.split())
    return re.compile(rf"(?<![\w#]){body}(?![\w-])", re.IGNORECASE)


def banned_in(text: str) -> list[str]:
    hits = [p for p in BANNED_PHRASES if _phrase(p).search(text)]
    if LEVERAGE_VERB.search(text):
        hits.append('"leverage" as a verb')
    return hits


def superlatives_in(text: str) -> list[str]:
    return [s for s in SUPERLATIVES if _phrase(s).search(text)]


def name_count(name: str, text: str) -> int:
    return len(_phrase(name).findall(text)) if name.strip() else 0


def where_label(url: str, ctx_arts: dict[str, Any], homepage: str | None) -> str:
    """Where a not-found page was looked for, in words."""
    if homepage and url == homepage:
        return "the homepage"
    if homepage:
        from engine.judge.faithfulness import sitemap_url

        if url == sitemap_url(homepage):
            return "the sitemap"
    for kind, r in ctx_arts.items():
        if r["url"] == url and r["outcome"] == "ok":
            return f"the {KIND_LABELS.get(kind, kind.replace('_', ' '))}"
    return re.sub(r"^https?://", "", url).rstrip("/")


def _contact_values(conn: sqlite3.Connection) -> set[str]:
    """Contacts values that identify a person: name and its parts, title, email,
    LinkedIn URL. source_url is left out: it is the company page the person was
    found on, which a teardown may legitimately cite."""
    values: set[str] = set()
    cols = ", ".join(CONTACT_FIELDS)
    for row in conn.execute(f"SELECT {cols} FROM contacts"):
        for f in CONTACT_FIELDS:
            v = row[f]
            if not isinstance(v, str):
                continue
            v = " ".join(v.split())
            parts = [v] + (v.split(" ") if f == "name" else [])
            values.update(p for p in parts if len(p) >= MIN_CONTACT_LEN)
    return values


# -- check ---------------------------------------------------------------------------------


def _todos(obj: Any, path: str = "") -> list[str]:
    """The field paths whose value still holds TODO."""
    if isinstance(obj, str):
        return [path or "a value"] if TODO in obj else []
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in _todos(v, f"{path}.{k}" if path else k)]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in _todos(v, f"{path}[{i}]")]
    return []


@dataclass(frozen=True)
class Failure:
    rule: str
    message: str

    def __str__(self) -> str:
        return f"FAIL {self.rule}: {self.message}"


@dataclass
class Checked:
    company_id: str
    path: Path
    failures: list[Failure]
    doc: TeardownDoc | None = None
    ctx: Context | None = None
    sender: S.Sender | None = None

    @property
    def ok(self) -> bool:
        return not self.failures


def offer_line(doc: TeardownDoc, snd: S.Sender) -> str:
    return snd.offer_line(doc.header.offer_type, role=doc.header.role,
                          company=doc.header.company)


def header_line(doc: TeardownDoc) -> str:
    return f"{doc.header.company}: notes for your {doc.header.role} hire"


def total_words(doc: TeardownDoc, ctx: Context, snd: S.Sender | None) -> int:
    """Words counted toward the 450: the header line, section 3 (sentences and the
    quoted evidence lines), 4, 5, 7 and 8 (about, offer, link labels). Left out:
    the proof slice (the spec excludes it), section 2's generated list of links,
    the fixed section headings and the footer."""
    parts = [header_line(doc), *(s.text for s in doc.need.sentences), doc.why_now,
             *doc.build, doc.might_have_wrong]
    for i in doc.need.evidence:
        if 0 <= i < len(ctx.claims) and ctx.claims[i].quote:
            parts.append(ctx.claims[i].quote or "")
    if snd is not None:
        parts += [*snd.about, offer_line(doc, snd), *(lk.label for lk in snd.links)]
    return sum(words(p) for p in parts)


def check(conn: sqlite3.Connection, company_id: str, *, directory: Path | None = None,
          sender_file: Path | None = None) -> Checked:
    path = file_for(company_id, directory)
    out: list[Failure] = []

    def fail(rule: str, msg: str) -> None:
        out.append(Failure(rule, msg))

    try:
        raw, doc = load(path)
    except TeardownError as e:
        fail("structure", " ".join(str(e).split())[:400])
        return Checked(company_id, path, out)
    ctx = load_context(conn, company_id)
    res = Checked(company_id, path, out, doc, ctx)

    # sender
    snd: S.Sender | None = None
    try:
        snd = S.load(sender_file)
        for p in S.problems(snd):
            fail("sender", f"{S.sender_path(sender_file)}: {p}")
    except S.SenderError as e:
        fail("sender", str(e))
    res.sender = snd

    # unfinished
    for where in _todos(doc.model_dump(mode="json")):
        fail("unfinished", f"{where} still reads TODO")

    # structure basics
    if doc.version != TEARDOWN_VERSION:
        fail("structure", f"version {doc.version!r}; expected {TEARDOWN_VERSION}")
    if doc.company_id != company_id:
        fail("structure", f"company_id {doc.company_id!r} does not match {company_id!r}")

    # diagnosis
    d = ctx.diagnosis
    if d is None:
        fail("diagnosis", "no diagnosis at the judge's current prompt version")
    elif not d["attempted"]:
        fail("diagnosis", "the diagnosis abstained; no teardown")
    elif (doc.diagnosis.prompt_version, doc.diagnosis.key) != (d["prompt_version"], ctx.key):
        fail("diagnosis", "the file was written from another diagnosis "
                          f"({doc.diagnosis.prompt_version} {doc.diagnosis.key}; stored "
                          f"{d['prompt_version']} {ctx.key}); claim indices may have moved: "
                          "re-run te teardown init --force into a new file and copy over")

    # header
    h = doc.header
    for fname in ("company", "role"):
        if not getattr(h, fname).strip():
            fail("header", f"header.{fname} is blank")
    if h.offer_type not in ("candidate", "adjacent"):
        fail("header", f"offer_type {h.offer_type!r}; must be candidate or adjacent")
    elif ctx.offer_type in ("candidate", "adjacent") and h.offer_type != ctx.offer_type:
        fail("header", f"offer_type {h.offer_type} does not match the signal's "
                       f"{ctx.offer_type} (protocol/OFFER.md)")
    if "\n" in header_line(doc):
        fail("word_limit", "the header must be one line")

    # section 2
    want = {(r.kind, r.url, r.found, r.captured) for r in looked_at_rows(ctx)}
    have = {(r.kind, r.url, r.found, r.captured) for r in doc.looked_at}
    for kind, url, found, _ in sorted(have - want):
        a = ctx.arts.get(kind)
        if a is not None and a["outcome"] not in SHOWN:
            fail("looked_at", f"{kind} was {a['outcome']}: blocked or unreachable pages are "
                              "left out of the evidence")
        else:
            fail("looked_at", f"{kind} ({'found' if found else 'not found'}, {url}) does not "
                              "match the capture")
    for kind, _, _, _ in sorted(want - have):
        if kind not in {k for k, *_ in have - want}:
            fail("looked_at", f"{kind} is missing; section 2 lists every page looked at")

    # word limits
    need_text = " ".join(s.text for s in doc.need.sentences)
    for label, text, limit in (("section 3", need_text, LIMITS["need"]),
                               ("section 4", doc.why_now, LIMITS["why_now"]),
                               ("section 7", doc.might_have_wrong, LIMITS["might_have_wrong"])):
        if words(text) > limit:
            fail("word_limit", f"{label} has {words(text)} words; the limit is {limit}")
    if snd is not None:
        s8 = [*snd.about, offer_line(doc, snd), *(lk.label for lk in snd.links)]
        n = sum(words(t) for t in s8)
        if n > LIMITS["about"]:
            fail("word_limit", f"section 8 has {n} words; the limit is {LIMITS['about']}")
    if len(doc.build) != BUILD_ITEMS:
        fail("build_items", f"section 5 has {len(doc.build)} items; it takes exactly three")
    tw = total_words(doc, ctx, snd)
    if tw >= TOTAL_LIMIT:
        fail("total_words", f"{tw} words excluding the proof slice; must be under {TOTAL_LIMIT}")

    # the page's words
    prose = [header_line(doc), *(s.text for s in doc.need.sentences), doc.why_now,
             *doc.build, doc.slice.title, doc.might_have_wrong]
    sender_lines = ([snd.name, *snd.about, offer_line(doc, snd), snd.opt_out]
                    if snd is not None else [])
    quotes = [ctx.claims[i].quote or "" for i in doc.need.evidence
              if 0 <= i < len(ctx.claims)]
    slice_text = [*doc.slice.columns, *(c for r in doc.slice.rows for c in r.cells),
                  *(r.quote or "" for r in doc.slice.rows)]
    everything = prose + sender_lines + quotes + slice_text
    for p in sorted({b for t in everything for b in banned_in(t)}):
        fail("banned_phrase", f"banned phrase {p if p.startswith(chr(34)) else repr(p)}")
    for s in sorted({x for t in prose + sender_lines for x in superlatives_in(t)}):
        fail("superlative", f"superlative {s!r}")

    # company name
    body = [*(s.text for s in doc.need.sentences), doc.why_now, *doc.build, doc.slice.title,
            doc.might_have_wrong]
    if not h.company.strip():
        fail("company_name", "no company name in the header")
    else:
        n = sum(name_count(h.company, t) for t in body)
        if n < MIN_BODY_NAMES:
            fail("company_name", f"{h.company!r} appears {n} times in the body; at least "
                                 f"{MIN_BODY_NAMES} (quotes do not count)")

    # section 3 tracing
    if not doc.need.sentences:
        fail("untraced", "section 3 has no sentences")
    for k, s in enumerate(doc.need.sentences):
        if len(sentences(s.text)) > 1:
            fail("one_sentence", f"section 3 item {k + 1} holds {len(sentences(s.text))} "
                                 "sentences; one per item, each with its claims")
        if not s.claims:
            fail("untraced", f"section 3 item {k + 1} names no claim")
        for i in s.claims:
            p = claim_problem(ctx, i)
            if p is not None:
                fail("untraced", f"section 3 item {k + 1} cites claim [{i}]: {p}")

    # evidence
    ev = doc.need.evidence
    if not EVIDENCE_MIN <= len(ev) <= EVIDENCE_MAX:
        fail("evidence", f"section 3 shows {len(ev)} evidence lines; it takes two or three")
    if len(set(ev)) != len(ev):
        fail("evidence", "an evidence line is listed twice")
    for i in ev:
        p = claim_problem(ctx, i)
        if p is not None:
            fail("evidence", f"evidence claim [{i}]: {p}")
            continue
        c = ctx.claims[i]
        a = ctx.arts.get(c.artifact_kind)
        now = a["outcome"] if a is not None else "no artifact row"
        if c.found and now != "ok":
            fail("evidence", f"evidence claim [{i}]: the {c.artifact_kind} capture is now {now}")
        if not c.found and now != "not_found":
            fail("evidence", f"evidence claim [{i}]: a Not found line must rest on a "
                             f"not_found capture; {c.artifact_kind} is {now}")
    cited = {i for s in doc.need.sentences for i in s.claims} | set(ev)
    kinds = {ctx.claims[i].artifact_kind for i in cited if 0 <= i < len(ctx.claims)}
    if len(kinds) < MIN_KINDS:
        fail("evidence", f"section 3 cites {len(kinds)} artifact kind(s); at least {MIN_KINDS}")

    # quotes
    for i in ev:
        if 0 <= i < len(ctx.claims) and ctx.claims[i].quote:
            n = words(ctx.claims[i].quote or "")
            if n >= QUOTE_LIMIT:
                fail("quote_length", f"evidence claim [{i}] quote has {n} words; under "
                                     f"{QUOTE_LIMIT}")
    for k, r in enumerate(doc.slice.rows):
        if r.quote and words(r.quote) >= QUOTE_LIMIT:
            fail("quote_length", f"slice row {k + 1} quote has {words(r.quote)} words; under "
                                 f"{QUOTE_LIMIT}")

    # the slice
    rows = doc.slice.rows
    if not 1 <= len(rows) <= SLICE_MAX_ROWS:
        fail("slice_row", f"the slice has {len(rows)} rows; one to {SLICE_MAX_ROWS}")
    if not doc.slice.columns:
        fail("slice_row", "the slice has no columns")
    for k, r in enumerate(rows):
        if not r.url or not re.match(r"^https?://\S+$", r.url.strip()):
            fail("slice_row", f"slice row {k + 1} has no source URL (http or https)")
        if not r.quote or not r.quote.strip():
            fail("slice_row", f"slice row {k + 1} has no quote")
        if len(r.cells) != len(doc.slice.columns):
            fail("slice_row", f"slice row {k + 1} has {len(r.cells)} cells for "
                              f"{len(doc.slice.columns)} columns")
        if any(not c.strip() for c in r.cells):
            fail("slice_row", f"slice row {k + 1} has an empty cell; write "
                              f"{NOT_FOUND_CELL!r}, never a guess")

    # contacts: never name the value
    values = _contact_values(conn)
    if values:
        from engine.llm.gateway import contact_pattern

        lines = raw.splitlines()
        hit_lines = sorted({n for v in values for n, line in enumerate(lines, start=1)
                            if contact_pattern(v).search(line)})
        for n in hit_lines:
            fail("contact_value", f"line {n} holds a value from the contacts table; the page "
                                  "names no individual")

    # placeholders
    page_text = prose + sender_lines + [*doc.slice.columns,
                                        *(c for r in rows for c in r.cells)]
    for t in page_text:
        for m in sorted(set(re.findall(r"\{[A-Za-z_][A-Za-z0-9_]*\}", t))):
            if m == "{first_name}":
                fail("placeholder", "{first_name} on the page: the page is shared by link "
                                    "and carries no recipient name; the email carries it")
            else:
                fail("placeholder", f"{m} is left unfilled")

    # competitors
    confirmed = {c.name.lower() for c in doc.competitors if c.confirmed}
    names = {c.name for c in doc.competitors} | set(ctx.competitors)
    for name in sorted(names):
        if name.lower() in confirmed or not name.strip():
            continue
        if any(name_count(name, t) for t in everything):
            fail("competitor", f"competitor {name!r} is named but not confirmed in review")

    return res
