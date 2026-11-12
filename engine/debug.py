"""Redacted debug export: `te debug export <company_id>`.

Writes one company's record to debug/<company_id>/ so a session can read it
without touching data/:

    record.json            company row, signals, facts, diagnoses, teardowns,
                           touches and artifact rows
    artifacts/<id>.txt     each artifact's text

Contacts rows are never read into the output. Every value from that company's
contacts rows (name, title, email, source_url, and each name part of 3+ chars)
is replaced with "[redacted]" wherever it appears, case-insensitive and
whole-word, using the same matching as the model gateway's contacts guard.
Touches lose hubspot_deal_id (it links to a person in HubSpot).

Text extraction does not exist yet, so an artifact's text is its raw cached
body decoded as UTF-8 with errors replaced; record.json notes this per artifact.

Output is deterministic: the company folder is rewritten from scratch, keys are
sorted and rows ordered by primary key, so a re-run on unchanged inputs writes
identical files.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from engine.guard.cache import Cache, sha256_hex
from engine.llm.gateway import contact_pattern, contact_values

DEFAULT_DEBUG_DIR = Path("debug")
REDACTED = "[redacted]"
EXPORT_VERSION = "debug-export-v1"
RAW_TEXT_NOTE = "raw body decoded as utf-8 (errors replaced); no text extraction yet"

# table -> ORDER BY
TABLES: dict[str, str] = {
    "signals": "signal_id",
    "facts": "fact_id",
    "diagnoses": "diagnosis_id",
    "teardowns": "teardown_id",
    "touches": "touch_id",
    "artifacts": "artifact_id",
}
JSON_COLUMNS: dict[str, tuple[str, ...]] = {
    "diagnoses": ("claims", "secondary", "faithfulness"),
}
# Columns dropped because they derive from a contact.
CONTACT_DERIVED: dict[str, tuple[str, ...]] = {
    "touches": ("hubspot_deal_id",),
}

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ExportError(RuntimeError):
    """The company is unknown or its id cannot name a folder."""


class Redactor:
    def __init__(self, values: set[str]):
        # Longest first, so a full name goes before its parts.
        ordered = sorted(values, key=lambda v: (-len(v), v))
        self.patterns = [contact_pattern(v) for v in ordered]

    def text(self, s: str) -> str:
        for p in self.patterns:
            s = p.sub(REDACTED, s)
        return s

    def value(self, v: Any) -> Any:
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if isinstance(v, dict):
            return {k: self.value(x) for k, x in v.items()}
        return v


def _rows(conn: sqlite3.Connection, table: str, company_id: str) -> list[dict[str, Any]]:
    order = TABLES[table]
    cur = conn.execute(
        f"SELECT * FROM {table} WHERE company_id = ? ORDER BY {order}", (company_id,)
    )
    drop = set(CONTACT_DERIVED.get(table, ()))
    json_cols = JSON_COLUMNS.get(table, ())
    out = []
    for r in cur:
        row = {k: r[k] for k in r.keys() if k not in drop}
        for col in json_cols:
            if isinstance(row.get(col), str):
                try:
                    row[col] = json.loads(row[col])
                except json.JSONDecodeError:
                    pass
        out.append(row)
    return out


def _body(cache: Cache, sha: str | None, cache_path: str | None) -> tuple[bytes | None, str]:
    if not sha:
        return None, "no body recorded"
    candidates = [cache.body_path(sha)]
    if cache_path and Path(cache_path).name == sha:
        candidates.append(Path(cache_path))
    for p in candidates:
        if p.is_file():
            body = p.read_bytes()
            if sha256_hex(body) != sha:
                return body, RAW_TEXT_NOTE + "; body hash does not match text_sha256"
            return body, RAW_TEXT_NOTE
    return None, "body not in cache"


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def export_company(
    conn: sqlite3.Connection,
    company_id: str,
    out_root: Path | None = None,
    cache: Cache | None = None,
) -> Path:
    """Write company_id's redacted record; returns the company folder."""
    if not _SAFE_ID.match(company_id):
        raise ExportError(f"company id cannot name a folder: {company_id!r}")
    row = conn.execute("SELECT * FROM companies WHERE company_id = ?", (company_id,)).fetchone()
    if row is None:
        raise ExportError(f"no company {company_id!r}")
    cache = cache or Cache()
    redact = Redactor(contact_values(conn, company_id))

    record: dict[str, Any] = {
        "export_version": EXPORT_VERSION,
        "company": {k: row[k] for k in row.keys()},
    }
    for table in TABLES:
        record[table] = _rows(conn, table, company_id)

    texts: dict[str, str] = {}
    for art in record["artifacts"]:
        body, note = _body(cache, art.get("text_sha256"), art.get("cache_path"))
        art.pop("cache_path", None)  # a local path; the text file replaces it
        art["text_note"] = note
        if body is None:
            art["text_file"] = None
            continue
        name = f"artifacts/{art['artifact_id']}.txt"
        art["text_file"] = name
        texts[name] = redact.text(body.decode("utf-8", errors="replace"))

    record = redact.value(record)

    root = out_root if out_root is not None else DEFAULT_DEBUG_DIR
    dest = root / company_id
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "artifacts").mkdir(parents=True)
    (dest / "record.json").write_text(_dump(record), encoding="utf-8", newline="\n")
    for name, text in texts.items():
        (dest / name).write_text(text, encoding="utf-8", newline="")
    return dest
