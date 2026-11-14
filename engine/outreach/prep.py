"""The meeting prep sheet (docs/PLAN.md Step 80; Step 81 for the demo that follows).

    te prep <company_id> [--out-dir DIR]

Writes one Markdown page to data/prep/<company_id>.md (gitignored; $TE_PREP_DIR
or --out-dir overrides the folder): the diagnosis and its evidence (each claim
with its quote, link and verifier verdict), the secondary gaps the teardown did
not use, the full proof slice (every row, not only the ten on the page), the
open questions (section 7 of their teardown, else the judge's cannot_see), the
facts with their quotes, the touches timeline, and the replies, meetings and
close logged by hand.

The recipient appears only as {first_name}, with the role bucket metrics use;
no name, title, address or URL from the contacts table is read into the page,
and a last pass replaces any contacts value that reached it through a typed
note with [redacted]. No network, no model call. A re-run on the same day with
unchanged inputs writes the same file.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from engine.build import teardown as T
from engine.outreach import metrics as M

PREP_VERSION = "prep-1.0"
PREP_ENV_VAR = "TE_PREP_DIR"
DEFAULT_PREP_DIR = Path("data") / "prep"
REDACTED = "[redacted]"


class PrepError(RuntimeError):
    pass


def prep_dir(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get(PREP_ENV_VAR)
    return Path(env) if env else DEFAULT_PREP_DIR


def _cell(v: object) -> str:
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")


def _table(columns: list[str], rows: list[list[object]]) -> list[str]:
    out = ["| " + " | ".join(_cell(c) for c in columns) + " |",
           "| " + " | ".join("---" for _ in columns) + " |"]
    out += ["| " + " | ".join(_cell(v) for v in r) + " |" for r in rows]
    return out


def _diagnosis(conn: sqlite3.Connection, company_id: str) -> sqlite3.Row | None:
    from engine.judge.judge import PROMPT_VERSION

    d = conn.execute("SELECT * FROM diagnoses WHERE company_id = ? AND prompt_version = ?",
                     (company_id, PROMPT_VERSION)).fetchone()
    return d or conn.execute("SELECT * FROM diagnoses WHERE company_id = ? "
                             "ORDER BY diagnosis_id DESC LIMIT 1", (company_id,)).fetchone()


def _verdicts(d: sqlite3.Row) -> dict[int, str]:
    if not d["verifier_verdict"]:
        return {}
    try:
        v = json.loads(d["verifier_verdict"])
    except ValueError:
        return {}
    return {int(c["index"]): str(c["verdict"]) for c in v.get("claims") or []
            if isinstance(c, dict) and "index" in c and "verdict" in c}


def _teardown_doc(company_id: str, teardowns: Path | None) -> T.TeardownDoc | None:
    try:
        _, doc = T.load(T.file_for(company_id, teardowns))
    except T.TeardownError:
        return None
    return doc


def render(conn: sqlite3.Connection, company_id: str, *, teardowns: Path | None = None,
           today: date | None = None) -> str:
    c = conn.execute("SELECT * FROM companies WHERE company_id = ?", (company_id,)).fetchone()
    if c is None:
        raise PrepError(f"{company_id}: no such company")
    today = today or datetime.now(UTC).date()
    dims = M.dimensions(conn, company_id)[company_id]
    sig = None
    if c["best_signal_id"]:
        sig = conn.execute("SELECT title, offer_type, url FROM signals WHERE signal_id = ?",
                           (c["best_signal_id"],)).fetchone()
    doc = _teardown_doc(company_id, teardowns)
    L: list[str] = [
        f"# Prep: {c['name']} ({company_id})", "",
        f"Generated {today.isoformat()} by te prep ({PREP_VERSION}). Local only; never shared.",
        "",
        f"- Recipient: {{first_name}} ({dims['role']})",
        f"- Segment: {dims['segment']}; offer: {dims['offer_type']}; made by: {dims['made_by']}",
        f"- Role: {sig['title'] if sig else 'unknown'}" + (f" ({sig['url']})"
                                                            if sig and sig['url'] else ""),
        f"- Status: {c['status']}" + (f" ({c['drop_reason']}, {c['drop_stage']})"
                                      if c["drop_reason"] else ""),
        "",
    ]

    # the diagnosis and its evidence
    d = _diagnosis(conn, company_id)
    L.append("## The diagnosis and its evidence")
    if d is None:
        L += ["", "No diagnosis on record.", ""]
    elif not d["attempted"]:
        L += ["", f"The judge abstained ({d['prompt_version']}): {d['abstain_reason']}", ""]
    else:
        L += ["", f"Gap {d['gap_type']}, confidence {d['confidence']}, prompt "
                  f"{d['prompt_version']}, verifier "
                  f"{json.loads(d['verifier_verdict']).get('outcome', 'n/a') if d['verifier_verdict'] else 'not run'}.",
              "", f"Need: {d['need']}", ""]
        verdicts = _verdicts(d)
        for i, cl in enumerate(json.loads(d["claims"])):
            what = f"\"{cl.get('quote')}\"" if cl.get("found") else "Not found"
            L.append(f"{i}. {cl.get('text')}  ")
            L.append(f"   {cl.get('artifact_kind')}: {what} ({cl.get('url')}); verifier: "
                     f"{verdicts.get(i, 'n/a')}")
        L.append("")

    # secondary gaps
    L.append("## Secondary gaps not used")
    sec = json.loads(d["secondary"]) if d is not None and d["secondary"] else []
    L += ["", *(f"- {g}" for g in sec)] if sec else ["", "None recorded."]
    L.append("")

    # the full proof slice
    from engine.build.slices import store as SS

    sl = SS.selected(conn, company_id)
    L.append("## The full proof slice")
    L.append("")
    if sl is not None:
        L.append(f"{sl.title} ({sl.kind}, {len(sl.rows)} rows; {len(sl.dropped)} dropped "
                 "before storage for want of a source)")
        L.append("")
        cols = list(sl.columns)
        L += _table(cols + ["source", "quote"],
                    [[r.get(col, T.NOT_FOUND_CELL) if isinstance(r, dict) else "" for col in cols]
                     + [r.get("url"), r.get("quote")] for r in sl.rows])
    elif doc is not None:
        L.append(f"{doc.slice.title} (from the teardown file)")
        L.append("")
        L += _table(list(doc.slice.columns) + ["source", "quote"],
                    [list(r.cells) + [r.url, r.quote] for r in doc.slice.rows])
    else:
        L.append("No proof slice on record.")
    L.append("")

    # open questions
    L.append("## Open questions (section 7)")
    L.append("")
    if doc is not None:
        L.append(doc.might_have_wrong)
    elif d is not None and d["cannot_see"]:
        L.append(f"{d['cannot_see']} (the judge's cannot_see; no teardown file)")
    else:
        L.append("None recorded.")
    L.append("")

    # facts
    facts = conn.execute("SELECT fact, value, artifact_kind, quote, found FROM facts WHERE "
                         "company_id = ? ORDER BY fact, artifact_kind, fact_id",
                         (company_id,)).fetchall()
    L += ["## The facts", ""]
    if facts:
        L += _table(["fact", "value", "kind", "found", "quote"],
                    [[f["fact"], f["value"], f["artifact_kind"], "yes" if f["found"] else "no",
                      f["quote"]] for f in facts])
    else:
        L.append("No facts on record.")
    L.append("")

    # the timeline
    L += ["## Touches and replies", ""]
    touches = conn.execute("SELECT touch_no, channel, drafted_at, sent_at, outcome, reply_at, "
                           "bounced_on, applied_on FROM touches WHERE company_id = ? "
                           "ORDER BY touch_no", (company_id,)).fetchall()
    if touches:
        L += _table(["touch", "channel", "drafted", "sent", "outcome", "reply", "bounced",
                     "applied"], [list(t) for t in touches])
    else:
        L.append("No touches.")
    replies = conn.execute("SELECT replied_on, touch_no, type, optout, note FROM replies WHERE "
                           "company_id = ? ORDER BY replied_on, reply_id",
                           (company_id,)).fetchall()
    if replies:
        L += ["", "Replies:", ""]
        for r in replies:
            L.append(f"- {r['replied_on']} (touch {r['touch_no']}): {r['type']}"
                     f"{' (opted out)' if r['optout'] else ''}"
                     f"{': ' + r['note'] if r['note'] else ''}")
    meetings = conn.execute("SELECT held_on, minutes, note FROM meetings WHERE company_id = ? "
                            "ORDER BY held_on", (company_id,)).fetchall()
    if meetings:
        L += ["", "Meetings held:", ""]
        for m in meetings:
            L.append(f"- {m['held_on']}" + (f", {m['minutes']:g} min" if m["minutes"] else "")
                     + (f": {m['note']}" if m["note"] else ""))
    cl = conn.execute("SELECT * FROM closes WHERE company_id = ?", (company_id,)).fetchone()
    if cl is not None:
        L += ["", f"Closed {cl['closed_on']}: {cl['outcome']}; next: {cl['next_step']}"]
    L.append("")
    return _redact(conn, company_id, "\n".join(L))


def _redact(conn: sqlite3.Connection, company_id: str, text: str) -> str:
    from engine.llm.gateway import contact_pattern, contact_values

    for v in sorted(contact_values(conn, company_id), key=lambda v: (-len(v), v)):
        text = contact_pattern(v).sub(REDACTED, text)
    return text


def write(conn: sqlite3.Connection, company_id: str, *, out_dir: Path | None = None,
          teardowns: Path | None = None, today: date | None = None) -> Path:
    T.file_for(company_id)  # refuses an id that cannot name a file
    text = render(conn, company_id, teardowns=teardowns, today=today)
    d = prep_dir(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{company_id}.md"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path
