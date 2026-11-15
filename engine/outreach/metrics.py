"""The campaign, from the database alone (docs/PLAN.md Step 78, Gate 6.2; the
Outcome contract: reply rate, meeting rate and hours per meeting, by segment).

    te metrics [--by segment|offer_type|gap|slice|role|channel|made_by] [--json] [--detail]

Reads data/engine.db (or --db) and nothing else: no network, no model call, no
file under data/ besides the database. The same database gives the same output,
byte for byte: groups and reasons are sorted, numbers have a fixed format.

Definitions (one place; the CLI and the JSON use these names):

  sent            companies with a touch a person sent (status sent, or a
                  sent_at: the Phase 0 hand rows)
  bounced         sent companies with a bounced touch and no reply logged
  delivered       sent minus bounced
  replied         delivered companies with any reply (a replies row, a touch
                  outcome replied_<type>, or a reply_at)
  positive        delivered companies with a reply of type positive, question
                  or referral
  meeting         delivered companies with at least one call held (meetings)
  reply rate      replied / delivered
  positive rate   positive / delivered
  meeting rate    meeting / delivered
  meetings held   calls held (meetings rows), all companies
  hours/meeting   (every teardown's review minutes, teardowns.review_minutes,
                  hand-made ones included when recorded) / 60 / meetings held
  minutes per teardown
                  mean and median of teardowns.review_minutes per company,
                  hand-made (companies.hand = 1) and engine-made apart; a
                  hand-made company with hand_minutes recorded (0020: research
                  + writing + slice, typed with `te company add --hand`) counts
                  those instead of its review minutes, here and in hours per
                  meeting
  model spend     llm_calls.cost_usd summed (what the provider reported; a
                  subscription reports an estimate, not a charge); per meeting
                  held. Spend outside model calls is not in the database.
  drop rate       companies dropped after reaching qualified / companies that
                  reached qualified, by reason; drops before qualified are
                  counted apart
  judge drop rate companies the judge dropped (drop_stage judge) / companies
                  judged (a diagnoses row, or dropped by the judge)
  opt-outs        companies with an opt-out reply; suppressions by reason
  bounces         touches bounced, companies bounced, sending stops

Funnel: qualified -> diagnosed -> sent -> replied -> meeting. A company counts
at every stage it reached: its current status, or for a dropped company the
stage its drop_stage comes after (DROP_STAGE_REACHED), raised by what the
record shows (an attempted diagnosis, a sent touch, a reply, a meeting). Each
stage lists the companies dropped after reaching it and before the next one,
by reason.

Splits (every rate, Step 78): segment, offer_type (the best signal's), gap
(the diagnosis's gap_type: the judge's current prompt version, else the
latest attempted), slice (teardowns.slice_kind, else the selected slice),
role (the recipient's title bucketed by ROLE_BUCKETS; the title itself is
never output), channel (touch 1's), made_by (hand or engine). Hours per
meeting in a split counts the review minutes and meetings of that split's
companies.

The default output is aggregate only: no company name, no id. --detail adds
company ids per funnel stage and drop; the CLI prints it to a terminal only.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from engine.outreach.replies import POSITIVE_TYPES
from engine.states import PIPELINE, Status

METRICS_VERSION = "metrics-1.0"
FUNNEL = (Status.QUALIFIED, Status.DIAGNOSED, Status.SENT, Status.REPLIED, Status.MEETING)
DIMENSIONS = ("segment", "offer_type", "gap", "slice", "role", "channel", "made_by")
NONE = "(none)"
_RANK = {s: i for i, s in enumerate(PIPELINE)}

# The last status a company dropped at each drop_stage had reached (the stages that
# call states.drop; see protocol/ICP.md "dropped" and the engines that drop).
DROP_STAGE_REACHED: dict[str, Status] = {
    "resolve": Status.SOURCED,         # E2 segment (out_of_scope)
    "qualify": Status.RESOLVED,        # E2 qualify
    "review": Status.RESOLVED,         # Step 30 human review, before qualified
    "capture": Status.QUALIFIED,       # E3 (no_site_evidence)
    "people": Status.QUALIFIED,        # Step 43 (no_recipient)
    "judge": Status.EXTRACTED,         # E5 abstained (no_gap)
    "review_queue": Status.BUILT,      # Step 66 kill with an ICP reason
    "send_day": Status.APPROVED,       # Step 70 (stale_signal)
}

# Recipient title -> bucket, first match wins. Only the bucket is ever output.
ROLE_BUCKETS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("founder_ceo", re.compile(r"\b(co-?founder|founder|ceo|chief executive|owner|"
                               r"managing director)\b", re.I)),
    ("gtm_leader", re.compile(r"\b(cro|chief|vp|vice president|svp|evp|head|director|"
                              r"general manager|gm)\b", re.I)),
    ("manager", re.compile(r"\b(manager|lead)\b", re.I)),
)


def role_bucket(title: str | None) -> str:
    if not title or not title.strip():
        return NONE
    for name, pat in ROLE_BUCKETS:
        if pat.search(title):
            return name
    return "other"


@dataclass
class Rec:
    company_id: str
    status: Status
    drop_reason: str | None
    drop_stage: str | None
    hand: bool
    dims: dict[str, str] = field(default_factory=dict)
    reached: Status = Status.SOURCED
    judged: bool = False
    sent: bool = False
    bounced: bool = False
    replied: bool = False
    reply_types: set[str] = field(default_factory=set)
    optout: bool = False
    meetings: int = 0
    minutes: float | None = None
    close: str | None = None

    @property
    def delivered(self) -> bool:
        return self.sent and not self.bounced

    def at(self, stage: Status) -> bool:
        return _RANK[self.reached] >= _RANK[stage]


# -- reading the database ------------------------------------------------------------------


def _current_gap(conn: sqlite3.Connection) -> dict[str, str]:
    from engine.judge.judge import PROMPT_VERSION

    out: dict[str, str] = {}
    for r in conn.execute("SELECT company_id, gap_type FROM diagnoses WHERE attempted = 1 "
                          "ORDER BY diagnosis_id"):
        out[r["company_id"]] = r["gap_type"]  # the latest attempted
    cur = {r["company_id"]: r["gap_type"] for r in conn.execute(
        "SELECT company_id, gap_type FROM diagnoses WHERE attempted = 1 AND prompt_version = ?",
        (PROMPT_VERSION,))}
    out.update(cur)
    return out


def dimensions(conn: sqlite3.Connection, company_id: str | None = None) -> dict[str, dict[str, str]]:
    """company_id -> {dimension: value}, every value a string (NONE when unknown).
    Reads the recipient's title only to bucket it; the title is never returned."""
    where, params = ("WHERE c.company_id = ?", (company_id,)) if company_id else ("", ())
    rows = conn.execute(
        "SELECT c.company_id, c.segment, c.hand, s.offer_type FROM companies c "
        f"LEFT JOIN signals s ON s.signal_id = c.best_signal_id {where} "
        "ORDER BY c.company_id", params).fetchall()
    gaps = _current_gap(conn)
    slice_td: dict[str, str] = {}
    for r in conn.execute("SELECT company_id, slice_kind FROM teardowns "
                          "WHERE slice_kind IS NOT NULL ORDER BY teardown_id"):
        slice_td.setdefault(r["company_id"], r["slice_kind"])
    slice_sel = {r["company_id"]: r["kind"] for r in conn.execute(
        "SELECT company_id, kind FROM slices WHERE selected = 1")}
    roles: dict[str, str] = {}
    for r in conn.execute("SELECT company_id, title FROM contacts ORDER BY contact_id"):
        roles.setdefault(r["company_id"], role_bucket(r["title"]))
    channels = {r["company_id"]: r["channel"] for r in conn.execute(
        "SELECT company_id, channel FROM touches WHERE touch_no = 1")}
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        cid = r["company_id"]
        out[cid] = {
            "segment": r["segment"] or NONE,
            "offer_type": r["offer_type"] or NONE,
            "gap": gaps.get(cid) or NONE,
            "slice": slice_td.get(cid) or slice_sel.get(cid) or NONE,
            "role": roles.get(cid, NONE),
            "channel": channels.get(cid) or NONE,
            "made_by": "hand" if r["hand"] else "engine",
        }
    return out


def _sent_touch(t: sqlite3.Row) -> bool:
    return t["status"] == "sent" or t["sent_at"] is not None


def load(conn: sqlite3.Connection) -> list[Rec]:
    dims = dimensions(conn)
    recs: dict[str, Rec] = {}
    for r in conn.execute("SELECT company_id, status, drop_reason, drop_stage, hand "
                          "FROM companies ORDER BY company_id"):
        recs[r["company_id"]] = Rec(r["company_id"], Status(r["status"]), r["drop_reason"],
                                    r["drop_stage"], bool(r["hand"]), dims[r["company_id"]])
    diagnosed = {r[0] for r in conn.execute(
        "SELECT DISTINCT company_id FROM diagnoses WHERE attempted = 1")}
    judged = {r[0] for r in conn.execute("SELECT DISTINCT company_id FROM diagnoses")}
    for t in conn.execute("SELECT * FROM touches ORDER BY company_id, touch_no"):
        rec = recs.get(t["company_id"])
        if rec is None:
            continue
        if _sent_touch(t):
            rec.sent = True
        if t["bounced_on"] is not None or t["outcome"] == "bounced":
            rec.bounced = True
        out = t["outcome"] or ""
        if out.startswith("replied_"):
            rec.replied = True
            rec.reply_types.add(out[len("replied_"):])
        if t["reply_at"] is not None:
            rec.replied = True
    for r in conn.execute("SELECT company_id, type, optout FROM replies"):
        rec = recs.get(r["company_id"])
        if rec is None:
            continue
        rec.replied = True
        rec.reply_types.add(r["type"])
        rec.optout = rec.optout or bool(r["optout"])
    for r in conn.execute("SELECT company_id, COUNT(*) AS n FROM meetings GROUP BY company_id"):
        if r["company_id"] in recs:
            recs[r["company_id"]].meetings = int(r["n"])
    for r in conn.execute("SELECT company_id, SUM(review_minutes) AS m FROM teardowns "
                          "WHERE review_minutes IS NOT NULL GROUP BY company_id"):
        if r["company_id"] in recs:
            recs[r["company_id"]].minutes = float(r["m"])
    # A hand-made teardown's cost is what was typed with `te company add --hand
    # --minutes-*` (hand_minutes, 0020): research + writing + slice. It replaces the
    # review minutes for that company, so the two are never added twice.
    try:
        hand_rows = conn.execute("SELECT company_id, research, writing, slice "
                                 "FROM hand_minutes").fetchall()
    except sqlite3.OperationalError:  # a database not yet migrated to 0020
        hand_rows = []
    for r in hand_rows:
        rec = recs.get(r["company_id"])
        parts = [r[k] for k in ("research", "writing", "slice") if r[k] is not None]
        if rec is not None and rec.hand and parts:
            rec.minutes = float(sum(parts))
    for r in conn.execute("SELECT company_id, outcome FROM closes"):
        if r["company_id"] in recs:
            recs[r["company_id"]].close = r["outcome"]
    for rec in recs.values():
        if "optout" in rec.reply_types:
            rec.optout = True
        if rec.replied:
            rec.bounced = False  # a reply means the message arrived
        if rec.status is Status.DROPPED:
            reached = DROP_STAGE_REACHED.get(rec.drop_stage or "", Status.SOURCED)
        else:
            reached = rec.status
        for cond, stage in ((rec.company_id in diagnosed, Status.DIAGNOSED),
                            (rec.sent, Status.SENT), (rec.replied, Status.REPLIED),
                            (rec.meetings > 0, Status.MEETING)):
            if cond and _RANK[stage] > _RANK[reached]:
                reached = stage
        rec.reached = reached
        rec.judged = rec.company_id in judged or rec.drop_stage == "judge"
    return list(recs.values())


# -- computing -----------------------------------------------------------------------------


def ratio(n: int | float, d: int | float) -> dict[str, Any]:
    return {"n": n, "of": d, "rate": round(n / d, 4) if d else None}


def _round(x: float | None, places: int = 2) -> float | None:
    return None if x is None else round(x, places)


def rates(recs: list[Rec], all_recs: list[Rec] | None = None) -> dict[str, Any]:
    """The rate block for a set of companies. all_recs: the companies whose review
    minutes count towards hours per meeting (default: recs)."""
    sent = [r for r in recs if r.sent]
    delivered = [r for r in sent if r.delivered]
    replied = [r for r in delivered if r.replied]
    positive = [r for r in delivered if r.reply_types & set(POSITIVE_TYPES)]
    met = [r for r in delivered if r.meetings > 0]
    held = sum(r.meetings for r in (all_recs if all_recs is not None else recs))
    minutes = sum(r.minutes or 0.0 for r in (all_recs if all_recs is not None else recs))
    return {
        "sent": len(sent),
        "bounced": len(sent) - len(delivered),
        "delivered": len(delivered),
        "reply_rate": ratio(len(replied), len(delivered)),
        "positive_reply_rate": ratio(len(positive), len(delivered)),
        "meeting_rate": ratio(len(met), len(delivered)),
        "meetings_held": held,
        "review_hours": _round(minutes / 60),
        "hours_per_meeting": _round(minutes / 60 / held) if held else None,
    }


def _minutes_stats(values: list[float]) -> dict[str, Any]:
    return {"teardowns": len(values),
            "mean": _round(statistics.fmean(values), 1) if values else None,
            "median": _round(statistics.median(values), 1) if values else None}


def compute(conn: sqlite3.Connection, *, by: str | None = None,
            detail: bool = False) -> dict[str, Any]:
    if by is not None and by not in DIMENSIONS:
        raise ValueError(f"--by must be one of {', '.join(DIMENSIONS)}")
    recs = load(conn)
    out: dict[str, Any] = {"version": METRICS_VERSION, "companies": len(recs)}

    # funnel
    funnel = []
    for i, stage in enumerate(FUNNEL):
        here = [r for r in recs if r.at(stage)]
        nxt = FUNNEL[i + 1] if i + 1 < len(FUNNEL) else None
        dropped = [r for r in here if r.status is Status.DROPPED
                   and (nxt is None or not r.at(nxt))]
        row: dict[str, Any] = {
            "stage": stage.value, "reached": len(here),
            "of_previous": ratio(len(here), funnel[-1]["reached"]) if funnel else None,
            "dropped_here": dict(sorted(Counter(r.drop_reason or NONE
                                                for r in dropped).items())),
        }
        if detail:
            row["company_ids"] = sorted(r.company_id for r in here)
            row["dropped_ids"] = sorted(r.company_id for r in dropped)
        funnel.append(row)
    out["funnel"] = funnel
    before = [r for r in recs if r.status is Status.DROPPED and not r.at(Status.QUALIFIED)]
    out["dropped_before_qualified"] = dict(sorted(Counter(r.drop_reason or NONE
                                                          for r in before).items()))
    if detail:
        out["dropped_before_qualified_ids"] = sorted(r.company_id for r in before)

    # drop rate
    qualified = [r for r in recs if r.at(Status.QUALIFIED)]
    post = [r for r in qualified if r.status is Status.DROPPED]
    out["drop_rate"] = {"overall": ratio(len(post), len(qualified)),
                        "by_reason": {k: ratio(v, len(qualified)) for k, v in
                                      sorted(Counter(r.drop_reason or NONE
                                                     for r in post).items())}}
    judged = [r for r in recs if r.judged]
    out["judge_drop_rate"] = ratio(sum(1 for r in judged if r.drop_stage == "judge"),
                                   len(judged))

    # the campaign
    out["overall"] = rates(recs)
    sent = [r for r in recs if r.sent]
    out["sends_by_made_by"] = {"engine": sum(1 for r in sent if not r.hand),
                               "hand": sum(1 for r in sent if r.hand)}
    out["touches_sent"] = {f"touch_{r['touch_no']}": r["n"] for r in conn.execute(
        "SELECT touch_no, COUNT(*) AS n FROM touches WHERE status = 'sent' OR sent_at IS NOT "
        "NULL GROUP BY touch_no ORDER BY touch_no")}
    types = Counter(t for r in recs if r.delivered for t in r.reply_types)
    out["replies_by_type"] = {t: types.get(t, 0) for t in
                              ("positive", "question", "referral", "not_now", "negative",
                               "optout")}
    out["closes_by_outcome"] = dict(sorted(Counter(r.close for r in recs if r.close).items()))
    out["minutes_per_teardown"] = {
        "engine": _minutes_stats(sorted(r.minutes for r in recs
                                        if r.minutes is not None and not r.hand)),
        "hand": _minutes_stats(sorted(r.minutes for r in recs
                                      if r.minutes is not None and r.hand)),
        "hand_not_recorded": sum(1 for r in recs if r.hand and r.minutes is None),
    }
    spend = conn.execute("SELECT coalesce(SUM(cost_usd), 0) FROM llm_calls").fetchone()[0]
    held = out["overall"]["meetings_held"]
    out["model_spend_usd"] = {"total": _round(float(spend), 4),
                              "per_meeting": _round(float(spend) / held, 4) if held else None}
    out["bounces"] = {
        "touches": int(conn.execute("SELECT COUNT(*) FROM touches WHERE bounced_on IS NOT NULL "
                                    "OR outcome = 'bounced'").fetchone()[0]),
        "companies": out["overall"]["bounced"],
        "sending_stops": int(conn.execute("SELECT COUNT(*) FROM send_stops").fetchone()[0]),
    }
    out["optouts"] = {
        "companies": sum(1 for r in recs if r.optout),
        "suppressions_by_reason": {r["reason"]: r["n"] for r in conn.execute(
            "SELECT reason, COUNT(*) AS n FROM suppressions GROUP BY reason ORDER BY reason")},
    }

    # splits
    dims = (by,) if by else DIMENSIONS
    splits: dict[str, dict[str, Any]] = {}
    for d in dims:
        groups: dict[str, list[Rec]] = defaultdict(list)
        for r in recs:
            groups[r.dims[d]].append(r)
        rows = {}
        for value in sorted(groups, key=lambda v: (v == NONE, v)):
            block = rates(groups[value])
            if block["sent"] or block["meetings_held"]:
                rows[value] = block
        splits[d] = rows
    out["by"] = splits
    return out


# -- formatting ----------------------------------------------------------------------------


def fmt_ratio(r: dict[str, Any] | None) -> str:
    if r is None:
        return "-"
    if not r["of"]:
        return f"{r['n']}/{r['of']} (n/a)"
    return f"{r['n']}/{r['of']} ({r['rate'] * 100:.1f}%)"


def _num(x: float | None, unit: str = "") -> str:
    return "n/a" if x is None else f"{x:g}{unit}"


def _counts(d: dict[str, Any]) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items()) or "none"


def format_text(m: dict[str, Any]) -> list[str]:
    o = m["overall"]
    lines = [f"te metrics ({m['version']}): {m['companies']} companies, from the database "
             "alone (no network)", "", "funnel"]
    for f in m["funnel"]:
        conv = f"  {fmt_ratio(f['of_previous'])} of previous" if f["of_previous"] else ""
        lines.append(f"  {f['stage']:<10} {f['reached']:>4}{conv}")
        if f["dropped_here"]:
            lines.append(f"    dropped after {f['stage']}: {_counts(f['dropped_here'])}")
        if "company_ids" in f:
            lines.append(f"    ids: {', '.join(f['company_ids']) or '-'}")
            if f["dropped_ids"]:
                lines.append(f"    dropped ids: {', '.join(f['dropped_ids'])}")
    lines.append(f"  dropped before qualified: {_counts(m['dropped_before_qualified'])}")
    if "dropped_before_qualified_ids" in m:
        lines.append(f"    ids: {', '.join(m['dropped_before_qualified_ids']) or '-'}")
    lines += ["", "campaign",
              f"  sent                 {o['sent']}  (engine {m['sends_by_made_by']['engine']}, "
              f"hand {m['sends_by_made_by']['hand']})",
              f"  touches sent         {_counts(m['touches_sent'])}",
              f"  bounced              {o['bounced']}  (touches {m['bounces']['touches']}, "
              f"sending stops {m['bounces']['sending_stops']})",
              f"  delivered            {o['delivered']}",
              f"  reply rate           {fmt_ratio(o['reply_rate'])}",
              f"  positive reply rate  {fmt_ratio(o['positive_reply_rate'])}",
              f"  meeting rate         {fmt_ratio(o['meeting_rate'])}",
              f"  meetings held        {o['meetings_held']}",
              f"  review hours         {_num(o['review_hours'])}",
              f"  hours per meeting    {_num(o['hours_per_meeting'])}",
              f"  model spend (USD)    {_num(m['model_spend_usd']['total'])}, per meeting "
              f"{_num(m['model_spend_usd']['per_meeting'])} (provider-reported; other spend "
              "is not in the database)",
              f"  replies by type      {_counts(m['replies_by_type'])}",
              f"  closes by outcome    {_counts(m['closes_by_outcome'])}",
              f"  opt-outs             {m['optouts']['companies']} companies; suppressions: "
              f"{_counts(m['optouts']['suppressions_by_reason'])}",
              f"  drop rate            {fmt_ratio(m['drop_rate']['overall'])} of qualified",
              ]
    for reason, r in m["drop_rate"]["by_reason"].items():
        lines.append(f"    {reason:<20} {fmt_ratio(r)}")
    lines.append(f"  judge drop rate      {fmt_ratio(m['judge_drop_rate'])} of judged")
    mp = m["minutes_per_teardown"]
    for who in ("engine", "hand"):
        s = mp[who]
        lines.append(f"  minutes/teardown {who:<6} mean {_num(s['mean'])}, median "
                     f"{_num(s['median'])} over {s['teardowns']}")
    lines.append(f"    hand-made with no minutes recorded: {mp['hand_not_recorded']}")
    for dim, rows in m["by"].items():
        lines += ["", f"by {dim}",
                  f"  {'value':<22} {'sent':>4} {'deliv':>5}  {'reply rate':<16} "
                  f"{'positive':<16} {'meeting rate':<16} {'h/meeting':>9}"]
        if not rows:
            lines.append("  (no sends)")
        for value, b in rows.items():
            lines.append(f"  {value:<22} {b['sent']:>4} {b['delivered']:>5}  "
                         f"{fmt_ratio(b['reply_rate']):<16} "
                         f"{fmt_ratio(b['positive_reply_rate']):<16} "
                         f"{fmt_ratio(b['meeting_rate']):<16} "
                         f"{_num(b['hours_per_meeting']):>9}")
    return lines


def format_json(m: dict[str, Any]) -> str:
    return json.dumps(m, indent=2, sort_keys=True, ensure_ascii=False)
