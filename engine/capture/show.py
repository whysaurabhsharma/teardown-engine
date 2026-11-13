"""The viewer: `te show <company_id>` (docs/PLAN.md Step 36).

Prints one company's evidence bundle: the company line, a bundle summary,
each artifact's kind, outcome, fetcher and url, the first lines of its
extracted text (Step 35), and a DNS summary block. Reads only the database
and the cache: no request, no model call.

Contacts are never printed. The contacts table is read only for its values,
which are replaced with "[redacted]" wherever they appear in the output (page
text can name a person who is also a contact), with the same matching as
`te debug export`, but over every company's contacts, not only this one's.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any

from engine.capture import bundle as B
from engine.capture import extract_text as X
from engine.guard.cache import Cache

OUTCOMES = ("ok", "not_found", "blocked", "dead", "robots_denied")
DEFAULT_LINES = 5
WIDTH = 160  # a text line longer than this is cut, marked with "..."


class ShowError(RuntimeError):
    """No such company."""


def _cut(s: str) -> str:
    return s if len(s) <= WIDTH else s[: WIDTH - 3] + "..."


def _order(kind: str) -> tuple[int, str]:
    return (B.KINDS.index(kind) if kind in B.KINDS else len(B.KINDS), kind)


def _dns_block(cache: Cache, row: sqlite3.Row) -> list[str]:
    structure = X.stored_structure(cache, row)
    summary = (structure or {}).get("dns")
    if summary is None:  # not extracted yet: read the raw JSON
        body = cache.get_body(row["body_sha256"]) if row["body_sha256"] else None
        try:
            doc = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            doc = None
        if not isinstance(doc, dict):
            return ["DNS", "  (no DNS JSON in the cache)"]
        summary = X.dns_summary(doc)
    s: dict[str, Any] = summary
    none = "none"
    subs = s.get("sending_subdomains") or {}
    out = [
        f"DNS {s.get('domain') or ''}".rstrip(),
        f"  SPF:   {'; '.join(s.get('spf') or []) or none}",
        f"  DMARC: policy {s.get('dmarc_policy') or none}"
        + (f"  ({s['dmarc'][0]})" if s.get("dmarc") else ""),
        f"  MX:    {', '.join(s.get('mx') or []) or none}",
        f"  DKIM selectors resolving: {', '.join(s.get('dkim_selectors') or []) or none}",
        f"  sending subdomains resolving: "
        + ("; ".join(f"{k} ({', '.join(v)})" for k, v in subs.items()) or none),
    ]
    if s.get("verification_txt"):
        out.append(f"  other root TXT: {len(s['verification_txt'])}")
        out += [f"    {_cut(v)}" for v in s["verification_txt"]]
    if s.get("failed"):
        out.append(f"  lookups failed: {', '.join(s['failed'])}")
    return out


def show_lines(conn: sqlite3.Connection, company_id: str, cache: Cache,
               lines: int = DEFAULT_LINES) -> list[str]:
    from engine.debug import Redactor
    from engine.llm.gateway import contact_values

    c = conn.execute("SELECT company_id, name, domain, status, segment, pilot, hand "
                     "FROM companies WHERE company_id = ?", (company_id,)).fetchone()
    if c is None:
        raise ShowError(f"no company {company_id!r}")
    rows = sorted(conn.execute("SELECT * FROM artifacts WHERE company_id = ?", (company_id,)),
                  key=lambda r: _order(r["kind"]))
    sets = [n for n in ("pilot", "hand") if c[n]]
    out = [f"{c['company_id']}  {c['name'] or ''}  {c['domain'] or ''}  status {c['status']}"
           f"  segment {c['segment'] or '-'}" + (f"  [{', '.join(sets)}]" if sets else "")]

    outcomes = Counter(r["outcome"] for r in rows)
    versions = sorted({r["bundle_version"] or "unversioned" for r in rows})
    have = {r["kind"] for r in rows}
    ok = [r for r in rows if r["outcome"] == "ok"]
    extracted = [r for r in ok if r["extractor_version"] == X.EXTRACTOR_VERSION]
    out.append(f"bundle {', '.join(versions) or 'none'}: {len(rows)} artifacts; "
               + ", ".join(f"{k} {outcomes[k]}" for k in OUTCOMES))
    missing = [k for k in B.KINDS if k not in have]
    if missing:
        out.append(f"kinds with no row: {', '.join(missing)}")
    out.append(f"extracted at {X.EXTRACTOR_VERSION}: {len(extracted)} of {len(ok)} ok")

    dns_row = None
    for r in rows:
        out.append("")
        out.append(f"{r['kind']:<10} {r['outcome']:<13} {r['fetcher'] or '-':<10} {r['url']}")
        if r["detail"]:
            out.append(f"  {_cut(r['detail'])}")
        if r["kind"] == "dns" and r["outcome"] == "ok":
            dns_row = r
        if r["outcome"] != "ok":
            continue
        text = X.stored_text(cache, r)
        if text is None:
            out.append("  (not extracted at this version; run `te extract text`)"
                       if not r["extractor_version"] or r["extractor_version"] != X.EXTRACTOR_VERSION
                       else "  (extracted text missing from the cache; run `te extract text`)")
            continue
        st = X.stored_structure(cache, r) or {}
        all_lines = text.split("\n") if text else []
        info = [f"text {len(text)} chars, {len(all_lines)} lines, {r['extractor_version']}"]
        if st.get("format") == "html":
            forms = st.get("forms") or []
            info.append(f"links {len(st.get('links') or [])}, buttons "
                        f"{len(st.get('buttons') or [])}, forms {len(forms)}"
                        + (f" (fields {', '.join(str(f['field_count']) for f in forms)})"
                           if forms else ""))
        elif st.get("format") == "json_jobs":
            info.append(f"jobs {st.get('job_count', 0)}")
        out.append("  " + "; ".join(info))
        for line in all_lines[:lines]:
            out.append(f"  | {_cut(line)}")
        if len(all_lines) > lines:
            out.append(f"  | ... {len(all_lines) - lines} more lines")

    if dns_row is not None:
        out.append("")
        out += _dns_block(cache, dns_row)

    # Every company's contact values, not only this one's: a page can name anyone.
    redact = Redactor(contact_values(conn))
    return [redact.text(line) for line in out]
