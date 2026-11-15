"""`te report numbers [--json]`: the final numbers (docs/PLAN.md Steps 83-84).

Every rate carries a 95% Wilson interval (stats.wilson) and its counts, n of
denominator. The definitions are Step 78's, from engine/outreach/metrics.py;
nothing is recounted here except the splits:

    reply rate             companies with any reply / delivered
    positive reply rate    positive, question or referral / delivered
    meeting rate           companies with a call held / delivered
    drop rate              dropped after qualified / reached qualified
    judge drop rate        dropped by the judge (no_gap) / judged
    held-out agreement     the judge's latest score (judge_scores) at the current
                           judge prompt version, else the latest at any version:
                           same call / held-out rows (Step 83: "a count out of
                           thirteen")

Anonymised by construction (Step 84): the output holds counts, rates and
category labels (segment, offer type, gap type, slice kind, recipient role
bucket, hand or engine), never a company name, id, domain or person, and no
reply text. In every split, a value held by fewer than MIN_GROUP sent
companies is merged with the other small values into "other", so no row can
point at one or two companies. ("other" can still be small; it names no value.)
Funnel drop reasons are counts per reason code, which name no company.

Deterministic: the same database gives the same bytes.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any

from engine.outreach import metrics as MX
from engine.outreach.replies import POSITIVE_TYPES
from engine.report.stats import fmt, rate

REPORT_VERSION = "report-1.0"
MIN_GROUP = 3
OTHER = "other"
SPLITS = ("segment", "offer_type", "gap", "slice", "role", "made_by")
SOFT = ("Every rate is soft at this sample size: each is shown with its 95% Wilson "
        "interval and its counts; read the interval, not the point.")


def _block(recs: list[MX.Rec]) -> dict[str, Any]:
    sent = [r for r in recs if r.sent]
    delivered = [r for r in sent if r.delivered]
    return {
        "sent": len(sent),
        "delivered": len(delivered),
        "reply_rate": rate(sum(1 for r in delivered if r.replied), len(delivered)),
        "positive_reply_rate": rate(sum(1 for r in delivered
                                        if r.reply_types & set(POSITIVE_TYPES)), len(delivered)),
        "meeting_rate": rate(sum(1 for r in delivered if r.meetings > 0), len(delivered)),
    }


def merge_small(groups: dict[str, list[MX.Rec]], min_group: int = MIN_GROUP
                ) -> list[tuple[str, list[MX.Rec]]]:
    """[(label, companies)] sorted by label with "other" last: groups with no sent
    company are left out; groups with fewer than min_group sent companies are
    pooled into "other" (a value literally named "other" joins the pool)."""
    kept: list[tuple[str, list[MX.Rec]]] = []
    pool: list[MX.Rec] = []
    for label in sorted(groups):
        recs = groups[label]
        n_sent = sum(1 for r in recs if r.sent)
        if n_sent == 0:
            continue
        if n_sent < min_group or label == OTHER:
            pool.extend(recs)
        else:
            kept.append((label, recs))
    if pool:
        kept.append((OTHER, pool))
    return kept


def splits(recs: list[MX.Rec], dims: tuple[str, ...] = SPLITS,
           min_group: int = MIN_GROUP) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in dims:
        groups: dict[str, list[MX.Rec]] = defaultdict(list)
        for r in recs:
            groups[r.dims[d]].append(r)
        out[d] = {label: _block(g) for label, g in merge_small(groups, min_group)}
    return out


def held_out(conn: sqlite3.Connection) -> dict[str, Any] | None:
    from engine.judge.judge import PROMPT_VERSION

    try:
        row = conn.execute(
            "SELECT * FROM judge_scores ORDER BY (prompt_version = ?) DESC, score_id DESC "
            "LIMIT 1", (PROMPT_VERSION,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return {"prompt_version": row["prompt_version"], "held_out": row["held_out"],
            "same_call": row["same_call"], "false_gap": row["false_gap"],
            "faithfulness_failures": row["faithfulness_failures"],
            "not_judged": row["not_judged"], "passed": bool(row["passed"])}


def compute(conn: sqlite3.Connection, min_group: int = MIN_GROUP) -> dict[str, Any]:
    m = MX.compute(conn)
    recs = MX.load(conn)
    o = m["overall"]
    funnel = []
    for f in m["funnel"]:
        prev = f["of_previous"]
        funnel.append({"stage": f["stage"], "reached": f["reached"],
                       "of_previous": rate(prev["n"], prev["of"]) if prev else None,
                       "dropped_here": f["dropped_here"]})
    ho = held_out(conn)
    dr, jd = m["drop_rate"]["overall"], m["judge_drop_rate"]
    rates = {
        "reply_rate": rate(o["reply_rate"]["n"], o["reply_rate"]["of"]),
        "positive_reply_rate": rate(o["positive_reply_rate"]["n"],
                                    o["positive_reply_rate"]["of"]),
        "meeting_rate": rate(o["meeting_rate"]["n"], o["meeting_rate"]["of"]),
        "drop_rate": rate(dr["n"], dr["of"]),
        "judge_drop_rate": rate(jd["n"], jd["of"]),
        "held_out_agreement": rate(ho["same_call"], ho["held_out"]) if ho else None,
    }
    return {
        "version": REPORT_VERSION,
        "soft": SOFT,
        "min_group": min_group,
        "companies": m["companies"],
        "funnel": funnel,
        "dropped_before_qualified": m["dropped_before_qualified"],
        "drop_reasons_after_qualified": {k: v["n"] for k, v in
                                         m["drop_rate"]["by_reason"].items()},
        "sent": o["sent"], "bounced": o["bounced"], "delivered": o["delivered"],
        "sends_by_made_by": m["sends_by_made_by"],
        "meetings_held": o["meetings_held"],
        "rates": rates,
        "held_out": ho,
        "replies_by_type": m["replies_by_type"],
        "minutes_per_teardown": m["minutes_per_teardown"],
        "review_hours": o["review_hours"],
        "hours_per_meeting": o["hours_per_meeting"],
        "model_spend_usd": m["model_spend_usd"],
        "by": splits(recs, SPLITS, min_group),
    }


def _num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:g}"


def _counts(d: dict[str, Any]) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items()) or "none"


RATE_LABELS = (("reply_rate", "reply rate"), ("positive_reply_rate", "positive reply rate"),
               ("meeting_rate", "meeting rate"), ("drop_rate", "drop rate (of qualified)"),
               ("judge_drop_rate", "judge drop rate (of judged)"),
               ("held_out_agreement", "held-out agreement"))


def format_text(n: dict[str, Any]) -> list[str]:
    lines = [f"te report numbers ({n['version']}): {n['companies']} companies, from the "
             "database alone (no network); aggregate only", "", n["soft"], "", "funnel"]
    for f in n["funnel"]:
        conv = f"  {fmt(f['of_previous'])} of previous" if f["of_previous"] else ""
        lines.append(f"  {f['stage']:<10} {f['reached']:>4}{conv}")
        if f["dropped_here"]:
            lines.append(f"    dropped after {f['stage']}: {_counts(f['dropped_here'])}")
    lines.append(f"  dropped before qualified: {_counts(n['dropped_before_qualified'])}")
    lines += ["", "counts",
              f"  sent {n['sent']} (engine {n['sends_by_made_by']['engine']}, hand "
              f"{n['sends_by_made_by']['hand']}), bounced {n['bounced']}, delivered "
              f"{n['delivered']}, meetings held {n['meetings_held']}",
              f"  replies by type: {_counts(n['replies_by_type'])}",
              "", "rates (n/denominator, rate, 95% Wilson interval)"]
    for key, label in RATE_LABELS:
        lines.append(f"  {label:<28} {fmt(n['rates'][key])}")
    ho = n["held_out"]
    if ho:
        lines.append(f"    judge {ho['prompt_version']}: same call on {ho['same_call']} of "
                     f"{ho['held_out']} held-out rows; false gaps {ho['false_gap']}, "
                     f"faithfulness failures {ho['faithfulness_failures']}, not judged "
                     f"{ho['not_judged']}")
    else:
        lines.append("    no judge score recorded (te judge score)")
    mp = n["minutes_per_teardown"]
    lines += ["", "time and cost"]
    for who in ("hand", "engine"):
        s = mp[who]
        lines.append(f"  minutes per teardown, {who + '-made':<11} mean {_num(s['mean'])}, "
                     f"median {_num(s['median'])} over {s['teardowns']}")
    lines += [f"  hand-made with no minutes recorded: {mp['hand_not_recorded']}",
              f"  review hours {_num(n['review_hours'])}; hours per meeting "
              f"{_num(n['hours_per_meeting'])}",
              f"  model spend (USD, provider-reported) {_num(n['model_spend_usd']['total'])}; "
              f"per meeting {_num(n['model_spend_usd']['per_meeting'])}"]
    for dim, rows in n["by"].items():
        lines += ["", f"by {dim} (values with fewer than {n['min_group']} sent companies "
                      f"merged into \"{OTHER}\")"]
        if not rows:
            lines.append("  (no sends)")
        for value, b in rows.items():
            lines.append(f"  {value:<16} sent {b['sent']:>3}, delivered {b['delivered']:>3}")
            lines.append(f"    reply    {fmt(b['reply_rate'])}")
            lines.append(f"    positive {fmt(b['positive_reply_rate'])}")
            lines.append(f"    meeting  {fmt(b['meeting_rate'])}")
    return lines


def format_json(n: dict[str, Any]) -> str:
    return json.dumps(n, indent=2, sort_keys=True, ensure_ascii=False)
