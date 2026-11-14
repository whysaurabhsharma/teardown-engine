"""One pydantic model per table row (docs/PLAN.md, "Data contracts").

Every record that crosses an engine boundary is one of these. Each model names
its table in TABLE and its JSON-encoded columns in JSON_FIELDS, which is what
engine.db.insert uses. Database-assigned ids and timestamps are optional here.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from engine.states import Status


def _not_blank(v: str) -> str:
    if not v.strip():
        raise ValueError("must not be blank")
    return v


NonBlank = Annotated[str, Field(min_length=1), AfterValidator(_not_blank)]
Quote = NonBlank  # verbatim text from a captured artifact; no quote, no row
Probability = Annotated[float, Field(ge=0, le=1)]

Outcome = Literal["ok", "not_found", "blocked", "dead", "robots_denied"]
Split = Literal["teach", "held_out"]
QuoteSource = Literal["text", "removed_text", "structure", "not_found", "absent"]  # 0011
GapId = Literal["G1", "G2", "G3", "G4", "G5", "G6"]  # protocol/GAPS.md v1.2 (G6 provisional; 0013)
# abstain_reason starts with one of these (protocol/GAPS.md v1.2, "Abstain by default")
ABSTAIN_CODES = ("no_evidence:", "contradicted:", "thin:")


class Row(BaseModel):
    model_config = ConfigDict(extra="forbid")

    TABLE: ClassVar[str]
    JSON_FIELDS: ClassVar[frozenset[str]] = frozenset()


class Signal(Row):
    TABLE: ClassVar[str] = "signals"

    signal_id: NonBlank
    source: NonBlank
    board_slug: str | None = None
    company_name_raw: NonBlank
    title: NonBlank
    role_family: str | None = None
    offer_type: str | None = None
    location: str | None = None
    posted_at: date | None = None
    url: str | None = None
    first_seen: date
    company_id: str | None = None
    description_text: str | None = None  # plain text from the board's JSON (0003)
    fresh: bool | None = None  # set by `te signal filter` (0004); None = not yet filtered
    india_located: bool | None = None  # set by `te signal filter` (0004)


class HqMarker(BaseModel):
    """One piece of head-office evidence (Step 28): what was matched, on which
    page, with the verbatim text around it."""

    model_config = ConfigDict(extra="forbid")

    marker: NonBlank  # e.g. "entity:pvt_ltd", "cin", "address:in", "phone:+1"
    side: Literal["india", "abroad"]
    kind: Literal["entity", "incorporation", "address", "phone"]
    quote: Quote
    url: NonBlank
    char_start: Annotated[int, Field(ge=0)]
    entity: str | None = None  # the entity name as written, for entity markers
    role: Literal["parent", "subsidiary"] | None = None  # when the page says which


class Company(Row):
    TABLE: ClassVar[str] = "companies"
    JSON_FIELDS: ClassVar[frozenset[str]] = frozenset({"hq_evidence"})

    company_id: NonBlank
    domain: str | None = None
    name: NonBlank
    segment: str | None = None
    status: Status = Status.SOURCED
    drop_reason: str | None = None
    drop_stage: str | None = None
    best_signal_id: str | None = None
    pilot: bool = False
    hand: bool = False
    created_at: str | None = None
    updated_at: str | None = None
    review_reason: str | None = None  # "<code>: <detail>" when a person must look (0005)
    hq: Literal["india", "abroad"] | None = None  # head office, set by Step 28 (0007)
    hq_basis: str | None = None  # how hq was decided (0007)
    hq_evidence: list[HqMarker] = Field(default_factory=list)  # quoted markers (0007)
    headcount: Annotated[int, Field(gt=0)] | None = None  # typed in the review (0008)
    reviewed_at: str | None = None  # when a person accepted or dropped it (0008)
    review_note: str | None = None  # free text typed with the decision (0008)

    @model_validator(mode="after")
    def _drop_fields(self) -> Company:
        dropped = self.status is Status.DROPPED
        has_drop = bool(self.drop_reason) and bool(self.drop_stage)
        if dropped != has_drop:
            raise ValueError("drop_reason and drop_stage are set exactly when status is dropped")
        return self



FlagKind = Literal["same_stem", "similar_name", "placeholder_name"]


class CompanyFlag(Row):
    """Two companies that may be one (Step 27, 0006). Never merged automatically."""

    TABLE: ClassVar[str] = "company_flags"

    flag_id: int | None = None
    company_id: NonBlank  # the lower of the two ids
    other_company_id: NonBlank
    kind: FlagKind
    score: Annotated[float, Field(ge=0, le=100)]
    detail: NonBlank
    created_at: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> CompanyFlag:
        if not self.company_id < self.other_company_id:
            raise ValueError("company_id must sort before other_company_id")
        return self

class Artifact(Row):
    TABLE: ClassVar[str] = "artifacts"

    artifact_id: int | None = None
    company_id: NonBlank
    kind: NonBlank
    url: NonBlank
    outcome: Outcome
    fetched_at: datetime
    text_sha256: str | None = None  # extracted text under cache/text/ (Step 35)
    cache_path: str | None = None
    status: int | None = None  # final HTTP status (0009)
    body_sha256: str | None = None  # raw body under data/cache/<sha256> (0009)
    fetcher: Literal["http", "playwright", "dns", "none"] | None = None  # (0009)
    detail: str | None = None  # why, in words (0009)
    bundle_version: str | None = None  # capture/bundle.py BUNDLE_VERSION (0009)
    structure_sha256: str | None = None  # links/forms JSON under cache/structure/ (0010)
    extractor_version: str | None = None  # capture/extract_text.py EXTRACTOR_VERSION (0010)


class Fact(Row):
    TABLE: ClassVar[str] = "facts"

    fact_id: int | None = None
    company_id: NonBlank
    fact: NonBlank
    value: str
    artifact_kind: NonBlank
    quote: Quote
    char_start: Annotated[int, Field(ge=0)]
    extractor_version: NonBlank
    found: bool = True  # False records an absence (0011)
    quote_source: QuoteSource | None = None  # where the quote can be checked (0011)


class Contact(Row):
    """Personal data. Never placed in a prompt, a log or a test fixture."""

    TABLE: ClassVar[str] = "contacts"

    contact_id: int | None = None
    company_id: NonBlank
    name: str | None = None
    title: str | None = None
    source_url: str | None = None
    email: str | None = None
    email_confidence: Probability | None = None
    channel: str | None = None
    confidence: Literal["high", "medium", "low"] | None = None  # address confidence (0012)
    email_source: Literal["hunter", "apollo", "pattern", "public", "manual"] | None = None  # (0012)
    linkedin_url: str | None = None  # typed by a person, never fetched (0012)


def abstain_reason_problem(reason: str | None) -> str | None:
    """None when reason starts with an ABSTAIN_CODES code followed by text; else why not."""
    if reason is None or not reason.strip():
        return "an abstention needs an abstain_reason"
    code = next((c for c in ABSTAIN_CODES if reason.startswith(c)), None)
    if code is None:
        return f"abstain_reason must start with one of {', '.join(ABSTAIN_CODES)}"
    if not reason[len(code):].strip():
        return f"abstain_reason gives no reason after {code!r}"
    return None


class GoldEvidence(BaseModel):
    """One piece of gold evidence (protocol/GAPS.md, "Evidence rules"; 0013).

    found=True: a verbatim quote from the artifact of that kind, and its url.
    found=False: the artifact's capture outcome is not_found; quote is None.
    """

    model_config = ConfigDict(extra="forbid")

    kind: NonBlank
    found: bool
    url: NonBlank
    quote: Quote | None = None

    @model_validator(mode="after")
    def _quote_matches_found(self) -> GoldEvidence:
        if self.found and self.quote is None:
            raise ValueError("found evidence needs a verbatim quote")
        if not self.found and self.quote is not None:
            raise ValueError("not-found evidence has quote null")
        return self


class Gold(Row):
    TABLE: ClassVar[str] = "gold"
    JSON_FIELDS: ClassVar[frozenset[str]] = frozenset({"evidence"})

    gold_id: int | None = None
    company_id: NonBlank
    gap_type: GapId | None = None
    abstain: bool
    abstain_reason: str | None = None  # required when abstain; ABSTAIN_CODES (0013)
    evidence: list[GoldEvidence] = Field(default_factory=list)
    split: Split
    gaps_version: NonBlank
    notes: str | None = None  # typed by a person with the diagnosis (0013)

    @model_validator(mode="after")
    def _gap_or_abstain(self) -> Gold:
        if self.abstain == (self.gap_type is not None):
            raise ValueError("a gold row has a gap_type or abstains, not both or neither")
        if self.abstain:
            problem = abstain_reason_problem(self.abstain_reason)
            if problem:
                raise ValueError(problem)
        elif self.abstain_reason is not None:
            raise ValueError("a gold row that names a gap has no abstain_reason")
        return self


class Claim(BaseModel):
    """One diagnosis claim (protocol/GAPS.md, "Evidence rules").

    found=True: quotes the artifact verbatim; quote and url both required.
    found=False: an absence; quote is None and url is where it was looked for.
    """

    model_config = ConfigDict(extra="forbid")

    text: NonBlank
    artifact_kind: NonBlank
    found: bool  # explicit; never inferred from a missing quote
    url: NonBlank
    quote: Quote | None = None

    @model_validator(mode="after")
    def _quote_matches_found(self) -> Claim:
        if self.found and self.quote is None:
            raise ValueError("a found claim needs a verbatim quote")
        if not self.found and self.quote is not None:
            raise ValueError("a not-found claim has quote null")
        return self


FaithfulnessCategory = Literal[
    "near_miss",       # rapidfuzz partial ratio >= 95: logged on its own, still a failure
    "miss",            # the quote is nowhere in the artifact's text, nav/footer or structure
    "wrong_url",       # a found claim's url is not its artifact's URL
    "unseen",          # the artifact has no row, or was not seen (blocked, dead, robots_denied)
    "not_found_invalid",  # a not-found claim whose artifact outcome is not not_found
    "malformed",       # breaks the claim contract (found without a quote, or the reverse)
]


class FaithfulnessFailure(BaseModel):
    """One claim the faithfulness check removed (Step 48), kept for the audit."""

    model_config = ConfigDict(extra="forbid")

    index: Annotated[int, Field(ge=0)]  # position in the judge's claims
    category: FaithfulnessCategory
    artifact_kind: str
    found: bool
    url: str
    quote: str | None = None
    text: str
    score: float | None = None  # best partial ratio, for near_miss and miss


class Faithfulness(BaseModel):
    """diagnoses.faithfulness (engine/judge/faithfulness.py)."""

    model_config = ConfigDict(extra="forbid")

    checked: Annotated[int, Field(ge=0)]
    passed: Annotated[int, Field(ge=0)]
    near_miss: Annotated[int, Field(ge=0)] = 0
    failed: list[FaithfulnessFailure] = Field(default_factory=list)
    converted: bool = False  # True when the check turned a named gap into an abstention


class Diagnosis(Row):
    TABLE: ClassVar[str] = "diagnoses"
    JSON_FIELDS: ClassVar[frozenset[str]] = frozenset({"claims", "secondary", "faithfulness"})

    diagnosis_id: int | None = None
    company_id: NonBlank
    prompt_version: NonBlank
    attempted: bool  # explicit, set by the judge; required with no default
    gap_type: GapId | None = None
    confidence: Probability | None = None
    claims: list[Claim] = Field(default_factory=list)
    abstain_reason: str | None = None
    need: str | None = None  # what the hire will need; required when attempted
    secondary: list[GapId] = Field(default_factory=list)
    cannot_see: str | None = None  # what public pages cannot show; required when attempted
    faithfulness: Faithfulness | None = None
    verifier_verdict: str | None = None
    created_at: str | None = None

    @model_validator(mode="after")
    def _attempted_consistent(self) -> Diagnosis:
        if self.attempted and self.gap_type is None:
            raise ValueError("an attempted diagnosis names a gap_type")
        if not self.attempted and (self.gap_type is not None or not self.abstain_reason):
            raise ValueError("an abstention has no gap_type and gives an abstain_reason")
        if self.attempted:
            for name in ("need", "cannot_see"):
                value = getattr(self, name)
                if value is None or not value.strip():
                    raise ValueError(f"an attempted diagnosis gives a non-blank {name}")
        if self.gap_type is not None:
            if self.gap_type in self.secondary:
                raise ValueError("secondary must not contain the diagnosis's own gap_type")
        return self


class Teardown(Row):
    TABLE: ClassVar[str] = "teardowns"

    teardown_id: int | None = None
    company_id: NonBlank
    slice_kind: str | None = None
    page_slug: str | None = None
    email_subject: str | None = None
    email_body: str | None = None
    review_state: str = "pending"
    review_minutes: Annotated[float, Field(ge=0)] | None = None


class Touch(Row):
    TABLE: ClassVar[str] = "touches"

    touch_id: int | None = None
    company_id: NonBlank
    touch_no: Annotated[int, Field(ge=1)]
    channel: NonBlank
    sent_at: datetime | None = None
    outcome: str | None = None
    reply_at: datetime | None = None
    applied: bool | None = None
    hubspot_deal_id: str | None = None
    draft_id: str | None = None  # Gmail draft id (0015); None for a LinkedIn touch
    drafted_at: datetime | None = None  # when the engine drafted it (0015)
    status: Literal["drafted", "sent"] | None = None  # 0015; a person sends
    applied_on: date | None = None  # 0018: te queue applied (Step 75)
    applied_note: str | None = None  # 0018; never a person's name or contact
    bounced_on: date | None = None  # 0018: te queue bounce (Step 74)


class LlmCall(Row):
    TABLE: ClassVar[str] = "llm_calls"
    JSON_FIELDS: ClassVar[frozenset[str]] = frozenset({"output"})

    call_id: int | None = None
    stage: NonBlank
    company_id: str | None = None
    prompt_version: NonBlank
    provider: NonBlank
    model: NonBlank
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_hit: bool = False
    output: Any = None
    created_at: str | None = None
    cost_usd: Annotated[float, Field(ge=0)] | None = None


class JudgeScore(Row):
    """One `te judge score` result on the held-out gold rows (Step 50; 0014).
    Counts only: never a quote, a gap or a company id."""

    TABLE: ClassVar[str] = "judge_scores"

    score_id: int | None = None
    prompt_version: NonBlank
    held_out: Annotated[int, Field(ge=0)]
    same_call: Annotated[int, Field(ge=0)]
    false_gap: Annotated[int, Field(ge=0)]
    faithfulness_failures: Annotated[int, Field(ge=0)]
    not_judged: Annotated[int, Field(ge=0)]
    passed: bool
    created_at: str | None = None


class Suppression(Row):
    TABLE: ClassVar[str] = "suppressions"

    email_or_domain: NonBlank
    reason: NonBlank
    added_at: str | None = None


class SliceRow(Row):
    """A proof slice (0016; engine/build/slices/). Every row in `rows` carries a
    url and a quote; the builders drop any row without both before storage."""

    TABLE: ClassVar[str] = "slices"
    JSON_FIELDS: ClassVar[frozenset[str]] = frozenset({"columns", "rows", "dropped", "meta"})

    slice_id: int | None = None
    company_id: NonBlank
    kind: Literal["icp_accounts", "funnel_compare", "role_funnel", "dns_inventory"]
    builder_version: NonBlank
    outcome: Literal["ok", "failed"]
    reason: str | None = None
    title: NonBlank
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    dropped: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    inputs_key: str | None = None
    selected: bool = False
    fetches: Annotated[int, Field(ge=0)] = 0
    searches: Annotated[int, Field(ge=0)] = 0
    built_at: str | None = None


class SliceCompetitor(Row):
    """A competitor funnel_compare compares against (0016); confirmed only after a
    person said yes in `te slice competitors`."""

    TABLE: ClassVar[str] = "slice_competitors"

    company_id: NonBlank
    name: NonBlank
    domain: str | None = None
    source: Literal["named_competitors", "alternatives_search", "manual"]
    evidence_url: str | None = None
    evidence_quote: str | None = None
    rank: Annotated[int, Field(ge=1)]
    confirmed: bool = False
    confirmed_at: str | None = None


class SliceArtifact(Row):
    """A page fetched for a slice (0016), kept apart from the company's own bundle."""

    TABLE: ClassVar[str] = "slice_artifacts"

    slice_artifact_id: int | None = None
    company_id: NonBlank
    slice_kind: NonBlank
    subject_domain: NonBlank
    kind: NonBlank
    url: NonBlank
    outcome: Outcome
    fetched_at: str
    status: int | None = None
    body_sha256: str | None = None
    fetcher: Literal["http", "none"] | None = None
    detail: str | None = None
    text_sha256: str | None = None
    structure_sha256: str | None = None
    extractor_version: str | None = None


class QualityReason(BaseModel):
    """One failed gate: the gate's name and what failed. Never a contact value."""

    model_config = ConfigDict(extra="forbid")

    gate: NonBlank
    message: NonBlank


class QualityCheck(Row):
    """The latest automated quality-gate result for a company's teardown (0017;
    engine/build/quality.py, Step 65). reasons is empty exactly when passed."""

    TABLE: ClassVar[str] = "quality_checks"
    JSON_FIELDS: ClassVar[frozenset[str]] = frozenset({"reasons"})

    company_id: NonBlank
    version: NonBlank
    passed: bool
    reasons: list[QualityReason] = Field(default_factory=list)
    file_sha256: str | None = None
    checked_at: str | None = None

    @model_validator(mode="after")
    def _passed_means_no_reasons(self) -> QualityCheck:
        if self.passed == bool(self.reasons):
            raise ValueError("passed is true exactly when there are no reasons")
        return self


class Review(Row):
    """One decision a person took in the review queue (0018; engine/outreach/review.py,
    Step 66). reason is the kill reason code, set exactly when decision is kill;
    edited maps a drafted section to the fraction of its text changed (None when the
    drafted text is not available to compare)."""

    TABLE: ClassVar[str] = "reviews"
    JSON_FIELDS: ClassVar[frozenset[str]] = frozenset({"edited"})

    review_id: int | None = None
    company_id: NonBlank
    decision: Literal["approve", "kill", "edited"]
    reason: str | None = None
    note: str | None = None
    edited: dict[str, Annotated[float, Field(ge=0, le=1)] | None] = Field(default_factory=dict)
    minutes: Annotated[float, Field(gt=0)] | None = None
    file_sha256: str | None = None
    version: NonBlank
    decided_at: str | None = None

    @model_validator(mode="after")
    def _reason_with_kill(self) -> Review:
        has = self.reason is not None and bool(self.reason.strip())
        if (self.decision == "kill") != has:
            raise ValueError("a kill gives a reason; approve and edited give none")
        return self


class SendStop(Row):
    """The global stop on drafting after two bounces in one day (0018; Step 74)."""

    TABLE: ClassVar[str] = "send_stops"

    stop_id: int | None = None
    reason: NonBlank
    stopped_on: date
    stopped_at: str | None = None
    resumed_at: str | None = None
    resume_reason: str | None = None


ALL_MODELS: tuple[type[Row], ...] = (
    Signal, Company, CompanyFlag, Artifact, Fact, Contact, Gold,
    Diagnosis, Teardown, Touch, LlmCall, Suppression, JudgeScore,
    SliceRow, SliceCompetitor, SliceArtifact, QualityCheck, Review, SendStop,
)
