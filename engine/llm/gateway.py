"""The model gateway: every model call in the engine goes through `call`.

    output = call(stage, company_id, prompt_version, route, prompt, schema)

In order:
1. Contacts guard. A prompt (or schema) containing any value from the contacts
   table is refused with ContactInPrompt: nothing is sent, nothing recorded.
2. Cache. If llm_calls already holds a valid output for
   (stage, company_id, prompt_version), it is returned; no provider is called
   and nothing is written (a re-run changes nothing).
3. Providers, in route order. Each provider call is charged to the run's
   Budget, then recorded in llm_calls with tokens and estimated cost (output is
   stored only when it validates). Output that fails the JSON schema is retried
   once on the same provider with the validation error appended; a second
   failure raises SchemaInvalid. A quota/rate-limit error or a provider that is
   not configured moves on to the route's fallback.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

import jsonschema

from engine import db
from engine.guard.budget import Budget
from engine.llm import api_providers, claude_headless
from engine.llm.base import (
    CachedOutputInvalid,
    ContactInPrompt,
    LlmError,
    Provider,
    ProviderResult,
    ProviderUnavailable,
    SchemaInvalid,
)
from engine.llm.routes import Route, Target, family, get_route
from engine.models import LlmCall

PROVIDERS: dict[str, Provider] = {
    "claude": claude_headless.run,
    "groq": api_providers.groq,
    "gemini": api_providers.gemini,
    "cloudflare": api_providers.cloudflare,
}

CONTACT_FIELDS = ("name", "title", "email", "source_url")  # channel is an enum, not personal
MIN_CONTACT_LEN = 3
MAX_ERROR_IN_RETRY = 500

_budget = Budget()


class AllProvidersUnavailable(LlmError):
    """Every provider on the route was over quota or not configured."""


def default_budget() -> Budget:
    """The per-run (per-process) model-call budget."""
    return _budget


# --- contacts guard --------------------------------------------------------------

def contact_values(conn: sqlite3.Connection, company_id: str | None = None) -> set[str]:
    """Every non-null text value in contacts, plus each part of each name, >= 3 chars.

    With company_id, only that company's contacts rows."""
    cols = ", ".join(CONTACT_FIELDS)
    values: set[str] = set()
    sql, params = f"SELECT {cols} FROM contacts", ()
    if company_id is not None:
        sql, params = sql + " WHERE company_id = ?", (company_id,)
    for row in conn.execute(sql, params):
        for field in CONTACT_FIELDS:
            v = row[field]
            if not isinstance(v, str):
                continue
            v = " ".join(v.split())
            parts = [v] + (v.split(" ") if field == "name" else [])
            values.update(p for p in parts if len(p) >= MIN_CONTACT_LEN)
    return values


def contact_pattern(value: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in value.split(" ")]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


def check_no_contacts(conn: sqlite3.Connection, *texts: str) -> None:
    """Raise ContactInPrompt if any text contains a contacts value (case-insensitive,
    whole-word). The message never names the value."""
    values = contact_values(conn)
    if not values:
        return
    for text in texts:
        for v in values:
            if contact_pattern(v).search(text):
                raise ContactInPrompt(
                    "prompt refused: it contains a value from the contacts table; "
                    "use a placeholder and let the renderer fill it locally"
                )


# --- schema ----------------------------------------------------------------------

def _validator(schema: Mapping[str, Any]) -> jsonschema.protocols.Validator:
    cls = jsonschema.validators.validator_for(schema)
    try:
        cls.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise LlmError(f"the schema itself is invalid: {exc.message}") from exc
    return cls(schema)


def schema_error(validator: jsonschema.protocols.Validator, output: Any) -> str | None:
    """None if `output` validates; otherwise the most relevant error, located."""
    err = jsonschema.exceptions.best_match(validator.iter_errors(output))
    if err is None:
        return None
    where = "/".join(str(p) for p in err.absolute_path) or "(root)"
    return f"at {where}: {err.message}"[:MAX_ERROR_IN_RETRY]


def retry_prompt(prompt: str, problem: str) -> str:
    return (
        f"{prompt}\n\nYour previous answer was rejected: {problem}\n"
        "Answer again with output that matches the JSON schema exactly."
    )


# --- ledger ----------------------------------------------------------------------

def cached_output(
    conn: sqlite3.Connection, stage: str, company_id: str | None, prompt_version: str
) -> tuple[bool, Any]:
    row = conn.execute(
        "SELECT output FROM llm_calls WHERE stage = ? AND company_id IS ? AND prompt_version = ?"
        " AND output IS NOT NULL ORDER BY call_id DESC LIMIT 1",
        (stage, company_id, prompt_version),
    ).fetchone()
    if row is None:
        return False, None
    return True, json.loads(row["output"])


def _record(
    conn: sqlite3.Connection,
    stage: str,
    company_id: str | None,
    prompt_version: str,
    target: Target,
    result: ProviderResult,
    valid: bool,
) -> None:
    with conn:
        db.insert(
            conn,
            LlmCall(
                stage=stage,
                company_id=company_id,
                prompt_version=prompt_version,
                provider=target.provider,
                model=result.model or target.model or target.provider,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
                output=result.output if valid else None,
            ),
        )


# --- call ------------------------------------------------------------------------

def call(
    stage: str,
    company_id: str | None,
    prompt_version: str,
    route: str | Route,
    prompt: str,
    schema: Mapping[str, Any],
    *,
    conn: sqlite3.Connection | None = None,
    budget: Budget | None = None,
    exclude_family: str | None = None,
) -> Any:
    """Return a schema-valid output for (stage, company_id, prompt_version).

    Raises ContactInPrompt, CachedOutputInvalid, SchemaInvalid,
    AllProvidersUnavailable, ProviderError or BudgetExceeded.
    """
    r = get_route(route)
    schema = dict(schema)
    validator = _validator(schema)
    own = conn is None
    conn = conn or db.connect()
    try:
        check_no_contacts(conn, prompt, json.dumps(schema, ensure_ascii=False))

        hit, output = cached_output(conn, stage, company_id, prompt_version)
        if hit:
            problem = schema_error(validator, output)
            if problem:
                raise CachedOutputInvalid(
                    f"stored output for ({stage}, {company_id}, {prompt_version}) fails the "
                    f"schema {problem}; bump prompt_version"
                )
            return output

        targets = [t for t in r.targets if exclude_family is None or family(t) != exclude_family]
        if not targets:
            raise AllProvidersUnavailable(f"route {r.name}: every target is excluded")
        budget = budget or default_budget()
        reasons: list[str] = []
        for target in targets:
            provider = PROVIDERS.get(target.provider)
            if provider is None:
                raise LlmError(f"route {r.name}: no provider named {target.provider!r}")
            try:
                return _attempt(
                    conn, stage, company_id, prompt_version, target, provider,
                    prompt, schema, validator, budget,
                )
            except ProviderUnavailable as exc:
                reasons.append(f"{target.provider}: {exc}")
        raise AllProvidersUnavailable(f"route {r.name}: " + "; ".join(reasons))
    finally:
        if own:
            conn.close()


def _attempt(
    conn: sqlite3.Connection,
    stage: str,
    company_id: str | None,
    prompt_version: str,
    target: Target,
    provider: Provider,
    prompt: str,
    schema: dict[str, Any],
    validator: jsonschema.protocols.Validator,
    budget: Budget,
) -> Any:
    """One provider: the call, and at most one retry after a schema failure."""
    text = prompt
    for attempt in (1, 2):
        if attempt == 2:
            check_no_contacts(conn, text)
        budget.charge_model_call()
        result = provider(text, schema, target.model)
        if result.output is None:
            problem = result.problem or "the response had no output"
        else:
            problem = schema_error(validator, result.output)
        _record(conn, stage, company_id, prompt_version, target, result, problem is None)
        if problem is None:
            return result.output
        if attempt == 2:
            raise SchemaInvalid(
                f"{target.provider} output failed the schema twice; last problem: {problem}"
            )
        text = retry_prompt(prompt, problem)
    raise AssertionError("unreachable")
