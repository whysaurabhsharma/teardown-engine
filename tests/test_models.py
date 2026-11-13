import json
import sqlite3

import pytest
from pydantic import ValidationError

from engine import db
from engine.models import ALL_MODELS, Claim, Company, Diagnosis, Fact, Gold
from tests.conftest import add_company

FACT = {
    "company_id": "zentrico-io",
    "fact": "pricing_model",
    "value": "contact_sales",
    "artifact_kind": "pricing",
    "quote": "Talk to our team for a custom quote",
    "char_start": 1840,
    "extractor_version": "ex-1.0",
}

CLAIM = {
    "text": "The SDR role asks the hire to build target lists from scratch.",
    "artifact_kind": "job_post",
    "found": True,
    "url": "https://jobs.zentrico.example/sdr",
    "quote": "build and own target account lists from scratch",
}

ABSENT = {
    "text": "The site shows no pricing page.",
    "artifact_kind": "pricing",
    "found": False,
    "url": "https://zentrico.example/",
    "quote": None,
}

DIAG = {
    "company_id": "zentrico-io",
    "prompt_version": "j-1.2",
    "attempted": True,
    "gap_type": "G1",
    "confidence": 0.7,
    "claims": [CLAIM],
    "need": "A target account list built from a defined segment.",
    "cannot_see": "Which segments the current pipeline actually closes.",
    "secondary": ["G3"],
}


def test_every_table_has_a_model(conn: sqlite3.Connection) -> None:
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert tables == {m.TABLE for m in ALL_MODELS}
    assert len(tables) == 12
    for model in ALL_MODELS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({model.TABLE})")}
        assert set(model.model_fields) == cols, model.TABLE


# --- facts: no quote, no row -------------------------------------------------

@pytest.mark.parametrize("quote", [None, "", "   "])
def test_fact_without_quote_fails_validation(quote: str | None) -> None:
    data = dict(FACT, quote=quote)
    with pytest.raises(ValidationError):
        Fact(**data)


def test_fact_missing_quote_fails_validation() -> None:
    data = {k: v for k, v in FACT.items() if k != "quote"}
    with pytest.raises(ValidationError):
        Fact(**data)


@pytest.mark.parametrize("quote", [None, "", "  "])
def test_database_rejects_fact_without_quote(conn: sqlite3.Connection, quote: str | None) -> None:
    add_company(conn)
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(
            "INSERT INTO facts (company_id, fact, value, artifact_kind, quote, char_start, "
            "extractor_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("zentrico-io", "pricing_model", "contact_sales", "pricing", quote, 0, "ex-1.0"),
        )


def test_fact_with_quote_round_trips(conn: sqlite3.Connection) -> None:
    add_company(conn)
    with conn:
        db.insert(conn, Fact(**FACT))
    row = conn.execute("SELECT * FROM facts").fetchone()
    assert Fact(**dict(row)).quote == FACT["quote"]


# --- diagnoses: attempted is explicit; every claim quoted ----------------------

def test_diagnosis_without_attempted_fails_validation() -> None:
    data = {k: v for k, v in DIAG.items() if k != "attempted"}
    with pytest.raises(ValidationError, match="attempted"):
        Diagnosis(**data)


def test_diagnosis_attempted_none_fails_validation() -> None:
    with pytest.raises(ValidationError):
        Diagnosis(**dict(DIAG, attempted=None))


def test_attempted_has_no_default() -> None:
    assert Diagnosis.model_fields["attempted"].is_required()


def test_abstention_needs_reason_and_no_gap() -> None:
    with pytest.raises(ValidationError):
        Diagnosis(company_id="zentrico-io", prompt_version="j-1.2", attempted=False)
    with pytest.raises(ValidationError):
        Diagnosis(**dict(DIAG, attempted=False, abstain_reason="thin evidence"))
    ok = Diagnosis(
        company_id="zentrico-io", prompt_version="j-1.2", attempted=False,
        abstain_reason="thin evidence",
    )
    assert ok.gap_type is None


def test_claim_without_quote_fails_validation() -> None:
    with pytest.raises(ValidationError):
        Claim(**{k: v for k, v in CLAIM.items() if k != "quote"})
    with pytest.raises(ValidationError):
        Diagnosis(**dict(DIAG, claims=[dict(CLAIM, quote="")]))


def test_found_claim_needs_quote_and_url() -> None:
    with pytest.raises(ValidationError):
        Claim(**dict(CLAIM, quote=None))
    with pytest.raises(ValidationError):
        Claim(**{k: v for k, v in CLAIM.items() if k != "url"})
    with pytest.raises(ValidationError):
        Claim(**dict(CLAIM, url=" "))


def test_not_found_claim_has_null_quote_and_a_url() -> None:
    assert Claim(**ABSENT).quote is None
    with pytest.raises(ValidationError):
        Claim(**dict(ABSENT, quote="Pricing"))
    with pytest.raises(ValidationError):
        Claim(**{k: v for k, v in ABSENT.items() if k != "url"})


def test_claim_found_is_required() -> None:
    with pytest.raises(ValidationError, match="found"):
        Claim(**{k: v for k, v in CLAIM.items() if k != "found"})


@pytest.mark.parametrize(
    "claims",
    [
        [{"text": "x", "artifact_kind": "homepage", "found": True, "url": "https://zentrico.example/"}],
        [{"text": "x", "artifact_kind": "homepage", "found": True, "url": "https://zentrico.example/", "quote": None}],
        [{"text": "x", "artifact_kind": "homepage", "found": True, "url": "https://zentrico.example/", "quote": " "}],
        [{"text": "x", "artifact_kind": "homepage", "found": True, "quote": "Built for teams"}],
        [{"text": "x", "artifact_kind": "homepage", "found": True, "url": "", "quote": "q"}],
        [{"text": "x", "artifact_kind": "pricing", "found": False, "url": "https://zentrico.example/", "quote": "q"}],
        [{"text": "x", "artifact_kind": "pricing", "found": False, "quote": None}],
        [{"text": "x", "artifact_kind": "homepage", "url": "https://zentrico.example/", "quote": "q"}],
        [{"text": "x", "artifact_kind": "homepage", "found": 1, "url": "https://zentrico.example/", "quote": "q"}],
        [CLAIM, {"text": "y", "artifact_kind": "homepage", "found": True, "url": "https://zentrico.example/", "quote": 3}],
        ["not an object"],
    ],
)
def test_database_rejects_bad_claim(conn: sqlite3.Connection, claims: list) -> None:
    add_company(conn)
    with pytest.raises(sqlite3.IntegrityError, match="quote"), conn:
        conn.execute(
            "INSERT INTO diagnoses (company_id, prompt_version, attempted, gap_type, claims, "
            "need, cannot_see) VALUES ('zentrico-io', 'j-1.2', 1, 'G1', ?, 'n', 'c')",
            (json.dumps(claims),),
        )


def test_database_accepts_found_and_not_found_claims(conn: sqlite3.Connection) -> None:
    add_company(conn)
    absent_no_key = {k: v for k, v in ABSENT.items() if k != "quote"}
    with conn:
        conn.execute(
            "INSERT INTO diagnoses (company_id, prompt_version, attempted, gap_type, claims, "
            "need, cannot_see) VALUES ('zentrico-io', 'j-1.2', 1, 'G1', ?, 'n', 'c')",
            (json.dumps([CLAIM, ABSENT, absent_no_key]),),
        )


def test_database_rejects_claim_update_without_quote(conn: sqlite3.Connection) -> None:
    add_company(conn)
    with conn:
        db.insert(conn, Diagnosis(**DIAG))
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute("UPDATE diagnoses SET claims = ?", (json.dumps([dict(CLAIM, quote=None)]),))


def test_database_rejects_null_attempted(conn: sqlite3.Connection) -> None:
    add_company(conn)
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(
            "INSERT INTO diagnoses (company_id, prompt_version, gap_type, claims) "
            "VALUES ('zentrico-io', 'j-1.2', 'G1', '[]')"
        )


def test_diagnosis_round_trips(conn: sqlite3.Connection) -> None:
    add_company(conn)
    with conn:
        db.insert(conn, Diagnosis(**dict(DIAG, claims=[CLAIM, ABSENT]),
                                  faithfulness={"checked": 2, "passed": 2}))
    row = conn.execute("SELECT attempted, claims, faithfulness FROM diagnoses").fetchone()
    assert row["attempted"] == 1
    assert json.loads(row["claims"]) == [CLAIM, ABSENT]
    assert json.loads(row["faithfulness"]) == {"checked": 2, "passed": 2}


# --- other contracts -----------------------------------------------------------

def test_company_drop_fields_consistent() -> None:
    with pytest.raises(ValidationError):
        Company(company_id="c", name="C", status="dropped")
    with pytest.raises(ValidationError):
        Company(company_id="c", name="C", drop_reason="dup", drop_stage="resolve")
    Company(company_id="c", name="C", status="dropped", drop_reason="dup", drop_stage="resolve")


def test_gold_gap_or_abstain() -> None:
    base = {"company_id": "c", "split": "teach", "gaps_version": "gaps-1"}
    with pytest.raises(ValidationError):
        Gold(**base, abstain=True, gap_type="G2")
    with pytest.raises(ValidationError):
        Gold(**base, abstain=False)
    with pytest.raises(ValidationError):
        Gold(**dict(base, split="test"), abstain=True, abstain_reason="thin: one page")
    Gold(**base, abstain=True, abstain_reason="thin: one page")
    Gold(**base, abstain=False, gap_type="G6")  # provisional G6 (GAPS.md v1.2, 0013)


@pytest.mark.parametrize("reason", [None, "", "no reason code", "thin:", "thin:   ",
                                    "Thin: upper case", "maybe: unknown code"])
def test_gold_abstain_reason_needs_a_code_and_text(reason: str | None) -> None:
    base = {"company_id": "c", "split": "teach", "gaps_version": "v1.2", "abstain": True}
    with pytest.raises(ValidationError):
        Gold(**base, abstain_reason=reason)


def test_gold_gap_row_has_no_abstain_reason() -> None:
    with pytest.raises(ValidationError):
        Gold(company_id="c", split="teach", gaps_version="v1.2", abstain=False,
             gap_type="G1", abstain_reason="thin: x")


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError), conn:
        db.insert(conn, Fact(**dict(FACT, company_id="ghost-example")))


# --- diagnoses: need, secondary, cannot_see (protocol/GAPS.md contract) -------

@pytest.mark.parametrize("field", ["need", "cannot_see"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_attempted_diagnosis_needs_need_and_cannot_see(field: str, value: str | None) -> None:
    with pytest.raises(ValidationError, match=field):
        Diagnosis(**dict(DIAG, **{field: value}))


def test_abstention_may_omit_need_and_cannot_see() -> None:
    d = Diagnosis(
        company_id="zentrico-io", prompt_version="j-1.2", attempted=False,
        abstain_reason="thin evidence",
    )
    assert d.need is None and d.cannot_see is None and d.secondary == []


def test_secondary_excludes_own_gap() -> None:
    with pytest.raises(ValidationError, match="secondary"):
        Diagnosis(**dict(DIAG, gap_type="G1", secondary=["G2", "G1"]))


@pytest.mark.parametrize("gap_type", ["G7", "G1_hiring_ahead_of_targeting", "g1"])
def test_gap_type_only_gap_ids(gap_type: str) -> None:
    with pytest.raises(ValidationError):
        Diagnosis(**dict(DIAG, gap_type=gap_type))


def test_database_rejects_unknown_gap_type(conn: sqlite3.Connection) -> None:
    add_company(conn)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), conn:
        conn.execute(
            "INSERT INTO gold (company_id, gap_type, abstain, evidence, split, gaps_version)"
            " VALUES (?, 'G9', 0, '[]', 'teach', 'v1')",
            ("zentrico-io",),
        )


def test_secondary_only_gap_ids() -> None:
    with pytest.raises(ValidationError):
        Diagnosis(**dict(DIAG, secondary=["G7"]))
    assert Diagnosis(**dict(DIAG, secondary=["G2", "G5"])).secondary == ["G2", "G5"]


@pytest.mark.parametrize(
    "need, cannot_see",
    [(None, "c"), ("n", None), ("  ", "c"), ("n", "")],
)
def test_database_requires_need_and_cannot_see_when_attempted(
    conn: sqlite3.Connection, need: str | None, cannot_see: str | None
) -> None:
    add_company(conn)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), conn:
        conn.execute(
            "INSERT INTO diagnoses (company_id, prompt_version, attempted, gap_type, "
            "need, cannot_see) VALUES ('zentrico-io', 'j-1.2', 1, 'G1', ?, ?)",
            (need, cannot_see),
        )


def test_database_allows_null_need_on_abstention(conn: sqlite3.Connection) -> None:
    add_company(conn)
    with conn:
        conn.execute(
            "INSERT INTO diagnoses (company_id, prompt_version, attempted, abstain_reason) "
            "VALUES ('zentrico-io', 'j-1.2', 0, 'thin evidence')"
        )
    row = conn.execute("SELECT need, cannot_see, secondary FROM diagnoses").fetchone()
    assert (row["need"], row["cannot_see"], row["secondary"]) == (None, None, "[]")


@pytest.mark.parametrize("secondary", ["not json", '{"G2": 1}', '"G2"'])
def test_database_requires_secondary_json_array(
    conn: sqlite3.Connection, secondary: str
) -> None:
    add_company(conn)
    with pytest.raises(sqlite3.IntegrityError), conn:
        conn.execute(
            "INSERT INTO diagnoses (company_id, prompt_version, attempted, gap_type, "
            "need, cannot_see, secondary) VALUES ('zentrico-io', 'j-1.2', 1, 'G1', 'n', 'c', ?)",
            (secondary,),
        )


def test_new_diagnosis_fields_round_trip(conn: sqlite3.Connection) -> None:
    add_company(conn)
    with conn:
        db.insert(conn, Diagnosis(**DIAG))
    row = conn.execute("SELECT need, cannot_see, secondary FROM diagnoses").fetchone()
    assert row["need"] == DIAG["need"] and row["cannot_see"] == DIAG["cannot_see"]
    assert json.loads(row["secondary"]) == ["G3"]
