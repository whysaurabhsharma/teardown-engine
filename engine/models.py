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
GapId = Literal["G1", "G2", "G3", "G4", "G5"]  # protocol/GAPS.md


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


class Gold(Row):
    TABLE: ClassVar[str] = "gold"
    JSON_FIELDS: ClassVar[frozenset[str]] = frozenset({"evidence"})

    gold_id: int | None = None
    company_id: NonBlank
    gap_type: GapId | None = None
    abstain: bool
    evidence: list[Any] | dict[str, Any] = Field(default_factory=list)
    split: Split
    gaps_version: NonBlank

    @model_validator(mode="after")
    def _gap_or_abstain(self) -> Gold:
        if self.abstain == (self.gap_type is not None):
            raise ValueError("a gold row has a gap_type or abstains, not both or neither")
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


class Faithfulness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checked: Annotated[int, Field(ge=0)]
    passed: Annotated[int, Field(ge=0)]


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


class Suppression(Row):
    TABLE: ClassVar[str] = "suppressions"

    email_or_domain: NonBlank
    reason: NonBlank
    added_at: str | None = None


ALL_MODELS: tuple[type[Row], ...] = (
    Signal, Company, CompanyFlag, Artifact, Fact, Contact, Gold,
    Diagnosis, Teardown, Touch, LlmCall, Suppression,
)
