"""role_funnel: each open GTM role beside what it will need and what the site
shows today (docs/PLAN.md Step 62). The fallback slice for any gap
(protocol/GAPS.md).

Pure assembly from the facts table (Step 37): no network, no model call, no
new extraction. Every quote is re-checked against the company's bundle with
extract.base.verify before it is kept.

Rows:
- the signal's post: one row per scope phrase it carries (jd_scope_flags),
  quoting that phrase; with none, one row quoting the post's title.
- each GTM role family open on the job board (gtm_roles_by_family; not_gtm and
  unclassified left out), quoting the first title. Only the signal's own post
  is captured, so these rows read "post not captured" in that column.

Columns beside each role, the same on every row (what the site shows today):
segment pages (segment_pages), the customers page (customer_regions),
the pricing page (pricing_model) and a booking step (has_booking_step).
An absence on a page that was looked at reads "not found". A page that was not
seen (blocked, dead, robots_denied: its fact is missing) drops that whole
COLUMN, never written as "not found"; meta.dropped_columns says which and why.
The rows stand on their own url and quote (the role or the post). With fewer
than MIN_COLUMNS columns left besides the role, the slice is thin.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from engine.build.slices import common as C
from engine.extract.base import Art, Bundle, exact_text_quote

KIND = "role_funnel"
VERSION = "sl-role-1.1"
MIN_ROWS = 3
MIN_COLUMNS = 2  # besides "Open role"
COLUMNS = ["Open role", "What the post asks for", "Segment pages", "Customers page",
           "Pricing page", "Booking step"]

# The page each site column reads (the booking step reads the demo path).
SITE_KINDS = {"Segment pages": "homepage", "Customers page": "customers",
              "Pricing page": "pricing", "Booking step": "demo_form"}
SITE_START = 2  # index of the first site column

FAMILY_LABELS = {
    "sdr": "SDR", "ae": "Account Executive", "sales_lead": "Sales lead", "revops": "RevOps",
    "gtm_engineer": "GTM engineer", "growth": "Growth", "demand_gen": "Demand generation",
}
FLAG_LABELS = {
    "list_building": "build prospect lists", "icp_definition": "define the ICP",
    "sequencing": "run outbound sequences", "crm_setup": "set up or run the CRM",
    "closing": "close deals", "quota": "carry a quota", "territory": "own a territory",
}
PRICING_LABELS = {
    "numbers": "found; lists prices", "free_or_trial": "found; free plan or trial",
    "contact_sales": "found; contact sales", "unclassified": "found; no price wording",
}


def title(name: str) -> str:
    return f"{name}'s open GTM roles beside what its site shows today"


@dataclass(frozen=True)
class FactRow:
    fact: str
    value: Any
    kind: str
    quote: str
    char_start: int
    quote_source: str
    found: bool


def load_facts(conn: sqlite3.Connection, company_id: str) -> list[FactRow]:
    out = []
    for r in conn.execute("SELECT * FROM facts WHERE company_id = ? ORDER BY fact_id",
                          (company_id,)):
        v: Any = r["value"]
        try:
            v = json.loads(v) if v and v[:1] in "{[" else v
        except ValueError:
            pass
        out.append(FactRow(r["fact"], v, r["artifact_kind"], r["quote"], r["char_start"],
                           r["quote_source"] or "text", bool(r["found"])))
    return out


def _src(f: FactRow, b: Bundle) -> C.Src | None:
    art = b.arts.get(f.kind)
    if art is None:
        return None
    return C.Src("company", (b.domain or "").lower(), f.kind, art.url, f.quote, f.quote_source,
                 f.char_start, f.found)


def _first(facts: list[FactRow], name: str) -> list[FactRow]:
    return [f for f in facts if f.fact == name]


def _absence(f: FactRow) -> bool:
    return (not f.found) or f.quote_source in C.ABSENCE_SOURCES


def site_cells(facts: list[FactRow], b: Bundle) -> list[C.Cell]:
    """Segment pages, customers page, pricing page, booking step."""
    out: list[C.Cell] = []

    seg = _first(facts, "segment_pages")
    if not seg or (s := _src(seg[0], b)) is None:
        out.append(C.UNSEEN)
    elif _absence(seg[0]):
        out.append(C.not_found(s))
    else:
        out.append(C.cell(f"{seg[0].value} linked from the homepage", s))

    cust = _first(facts, "customer_regions")
    if not cust or (s := _src(cust[0], b)) is None:
        out.append(C.UNSEEN)
    elif cust[0].quote_source == "not_found":
        out.append(C.not_found(s))
    elif _absence(cust[0]):
        out.append(C.cell("found; names no region", s))
    else:
        regions = [str(f.value).replace("_", " ") for f in cust if f.found]
        out.append(C.cell("found; names " + ", ".join(regions), s))

    pr = _first(facts, "pricing_model")
    if not pr or (s := _src(pr[0], b)) is None:
        out.append(C.UNSEEN)
    elif pr[0].quote_source == "not_found":
        out.append(C.not_found(s))
    else:
        out.append(C.cell(PRICING_LABELS.get(str(pr[0].value), str(pr[0].value)), s))

    bk = _first(facts, "has_booking_step")
    if not bk or (s := _src(bk[0], b)) is None:
        out.append(C.UNSEEN)
    elif _absence(bk[0]):
        out.append(C.not_found(s))
    else:
        out.append(C.cell("found on the demo path", s))
    return out


def drafts(facts: list[FactRow], b: Bundle) -> list[C.Draft]:
    site = site_cells(facts, b)
    out: list[C.Draft] = []
    post: Art | None = b.arts.get("job_post")
    role = b.signal_title or "the signal role"
    flags = _first(facts, "jd_scope_flags")
    if post is not None and b.state("job_post") == "ok" and flags:
        found = [f for f in flags if not _absence(f)]
        for f in found:
            out.append(C.Draft([C.cell(role), C.cell(FLAG_LABELS.get(str(f.value),
                                                                      str(f.value))), *site],
                               _src(f, b)))
        if not found:
            q = exact_text_quote("slice", None, post, b.signal_title or "")
            src = (C.Src("company", (b.domain or "").lower(), "job_post", post.url, q.quote,
                         q.quote_source, q.char_start, True) if q is not None else None)
            out.append(C.Draft([C.cell(role), C.cell("none of the scope phrases looked for",
                                                     _src(flags[0], b)), *site], src))
    for f in _first(facts, "gtm_roles_by_family"):
        v = f.value if isinstance(f.value, dict) else {}
        fam = v.get("family")
        if fam not in FAMILY_LABELS or not f.found:
            continue
        out.append(C.Draft([C.cell(f"{FAMILY_LABELS[fam]} ({v.get('count')} open)"),
                            C.cell("post not captured"), *site], _src(f, b)))
    return out


def drop_unseen_columns(drafts_: list[C.Draft], site: list[C.Cell],
                        b: Bundle) -> tuple[list[str], list[C.Draft], list[dict[str, str]]]:
    """Remove each site column whose source page was not seen, from the columns and
    every row. Returns (columns, drafts, dropped columns with why)."""
    gone: list[int] = []
    dropped: list[dict[str, str]] = []
    for j, c in enumerate(site):
        if c.text is not None:
            continue
        col = COLUMNS[SITE_START + j]
        kind = SITE_KINDS[col]
        a = b.arts.get(kind)
        state = a.outcome if a is not None else "no artifact row"
        if a is not None and a.outcome == "ok":
            state = "seen but its fact is missing"
        gone.append(SITE_START + j)
        dropped.append({"column": col, "kind": kind, "why": f"source not seen: {kind} {state}"})
    keep = [i for i in range(len(COLUMNS)) if i not in gone]
    columns = [COLUMNS[i] for i in keep]
    out = [C.Draft([d.cells[i] for i in keep], d.src) for d in drafts_]
    return columns, out, dropped


def build(conn: sqlite3.Connection, b: Bundle, name: str) -> C.Slice:
    facts = load_facts(conn, b.company_id)
    if not facts:
        return C.failed(KIND, VERSION, title(name), COLUMNS, "no facts; run te extract facts")
    columns, rows, dropped_cols = drop_unseen_columns(drafts(facts, b), site_cells(facts, b), b)
    meta = {"dropped_columns": dropped_cols}
    if len(columns) - 1 < MIN_COLUMNS:
        return C.failed(KIND, VERSION, title(name), columns,
                        f"thin: {len(columns) - 1} column(s) besides the role, needs "
                        f"{MIN_COLUMNS}", meta=meta)

    def resolve(s: C.Src) -> Art | None:
        return b.arts.get(s.kind) if s.scope == "company" else None

    return C.finalize(KIND, VERSION, title(name), columns, rows, resolve, min_rows=MIN_ROWS,
                      meta=meta)
