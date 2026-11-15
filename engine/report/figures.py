"""`te report figures --out DIR`: the three figures of docs/PLAN.md Step 85, drawn
from the database as hand-written SVG (no plotting dependency).

    funnel.svg      qualified -> diagnosed -> sent -> replied -> meeting held,
                    with the drop reasons counted at each step
    reply_rate.svg  reply rate by segment and by offer type, each a dot with
                    its 95% Wilson whisker; values with fewer than MIN_GROUP
                    sent companies merged into "other" (numbers.merge_small)
    minutes.svg     minutes per teardown, hand-made beside engine-made: one dot
                    per teardown, the median marked

Aggregate only: labels are stage names, reason codes and category values; no
company name, id or person is ever drawn. Deterministic: fixed geometry, fixed
number formats, sorted inputs, no timestamp, so the same database gives
byte-identical files. Colours: the dataviz reference palette (blue slot 1 for a
single series; blue and orange for engine and hand), text in neutral ink.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from engine.outreach import metrics as MX
from engine.report import numbers as N

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
FONT = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
WIDTH = 760
FIGURES = ("funnel.svg", "reply_rate.svg", "minutes.svg")


def _f(x: float) -> str:
    """A fixed coordinate format: at most one decimal, no trailing zeros."""
    s = f"{x:.1f}"
    return s[:-2] if s.endswith(".0") else s


class Svg:
    def __init__(self, title: str, desc: str):
        self.title, self.desc = title, desc
        self.parts: list[str] = []

    def text(self, x: float, y: float, s: str, *, size: int = 13, fill: str = INK,
             anchor: str = "start", weight: str = "normal") -> None:
        w = f' font-weight="{weight}"' if weight != "normal" else ""
        self.parts.append(f'<text x="{_f(x)}" y="{_f(y)}" font-size="{size}" fill="{fill}" '
                          f'text-anchor="{anchor}"{w}>{escape(s)}</text>')

    def rect(self, x: float, y: float, w: float, h: float, fill: str, rx: float = 0) -> None:
        r = f' rx="{_f(rx)}"' if rx else ""
        self.parts.append(f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(max(w, 0))}" '
                          f'height="{_f(h)}" fill="{fill}"{r}/>')

    def line(self, x1: float, y1: float, x2: float, y2: float, stroke: str,
             width: float = 1) -> None:
        self.parts.append(f'<line x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}" '
                          f'stroke="{stroke}" stroke-width="{_f(width)}"/>')

    def circle(self, cx: float, cy: float, r: float, fill: str) -> None:
        self.parts.append(f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(r)}" fill="{fill}" '
                          f'stroke="{SURFACE}" stroke-width="2"/>')

    def render(self, height: float) -> str:
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
                f'height="{_f(height)}" viewBox="0 0 {WIDTH} {_f(height)}" role="img" '
                f'font-family="{escape(FONT)}">')
        body = [head, f"<title>{escape(self.title)}</title>", f"<desc>{escape(self.desc)}</desc>",
                f'<rect width="{WIDTH}" height="{_f(height)}" fill="{SURFACE}"/>', *self.parts,
                "</svg>"]
        return "\n".join(body) + "\n"


def _counts(d: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(d.items()))


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


# -- (a) the funnel ------------------------------------------------------------------


STAGE_LABEL = {"qualified": "Qualified", "diagnosed": "Diagnosed", "sent": "Sent",
               "replied": "Replied", "meeting": "Meeting held"}


def funnel_svg(n: dict[str, Any]) -> str:
    stages = n["funnel"]
    top = max([f["reached"] for f in stages] + [1])
    left, bar_w, row_h = 130, 420, 58
    desc = "; ".join(f"{f['stage']} {f['reached']}" for f in stages)
    svg = Svg("Funnel: qualified to meeting held", "Companies reaching each stage: " + desc)
    svg.text(16, 28, "Funnel: companies reaching each stage, and why they dropped",
             size=15, weight="600")
    y = 52
    for i, f in enumerate(stages):
        w = bar_w * f["reached"] / top
        svg.text(left - 10, y + 17, STAGE_LABEL.get(f["stage"], f["stage"]), anchor="end")
        svg.rect(left, y + 2, w, 22, BLUE, rx=4)
        conv = ""
        if f["of_previous"] and f["of_previous"]["of"]:
            conv = f"  ({_pct(f['of_previous']['rate'])} of previous)"
        svg.text(left + w + 8, y + 18, f"{f['reached']}{conv}", fill=INK)
        if f["dropped_here"]:
            svg.text(left, y + 42, "dropped here: " + _counts(f["dropped_here"]), size=11,
                     fill=INK_2)
        y += row_h
    if n["dropped_before_qualified"]:
        svg.text(16, y + 8, "Dropped before qualified: " + _counts(n["dropped_before_qualified"]),
                 size=11, fill=INK_2)
        y += 20
    svg.text(16, y + 8, "Source: te report figures, from the campaign database. Aggregate counts "
             "only.", size=10, fill=INK_2)
    return svg.render(y + 22)


# -- (b) reply rate by segment and by offer type -------------------------------------


def reply_rate_svg(n: dict[str, Any]) -> str:
    panels = (("segment", "By segment"), ("offer_type", "By offer type"))
    left, axis_w = 210, 400
    svg = Svg("Reply rate by segment and offer type",
              "Reply rate with 95% Wilson interval per group; " + "; ".join(
                  f"{d} {v} {b['reply_rate']['n']}/{b['reply_rate']['of']}"
                  for d, _ in panels for v, b in n["by"].get(d, {}).items()))
    svg.text(16, 28, "Reply rate, with 95% Wilson intervals", size=15, weight="600")
    svg.text(16, 46, f"Groups with fewer than {n['min_group']} sent companies are merged into "
             "\"other\". Every rate here is soft.", size=11, fill=INK_2)
    y = 70
    for dim, heading in panels:
        rows = n["by"].get(dim, {})
        svg.text(16, y + 14, heading, size=13, weight="600")
        y += 26
        axis_top = y
        if not rows:
            svg.text(left, y + 14, "no sends yet", size=12, fill=INK_2)
            y += 30
        for value, b in rows.items():
            r = b["reply_rate"]
            svg.text(left - 10, y + 15, f"{value} (n={r['of']})", anchor="end", size=12)
            if r["of"]:
                x_lo = left + axis_w * r["low"]
                x_hi = left + axis_w * r["high"]
                svg.line(x_lo, y + 11, x_hi, y + 11, INK_2, 2)
                svg.line(x_lo, y + 6, x_lo, y + 16, INK_2, 2)
                svg.line(x_hi, y + 6, x_hi, y + 16, INK_2, 2)
                svg.circle(left + axis_w * r["rate"], y + 11, 5, BLUE)
                svg.text(left + axis_w + 12, y + 15, f"{_pct(r['rate'])} ({_pct(r['low'])} "
                         f"to {_pct(r['high'])})", size=11, fill=INK_2)
            else:
                svg.text(left, y + 15, "none delivered", size=11, fill=INK_2)
            y += 28
        for t in (0, 0.25, 0.5, 0.75, 1.0):
            x = left + axis_w * t
            svg.parts.insert(0, f'<line x1="{_f(x)}" y1="{_f(axis_top)}" x2="{_f(x)}" '
                                f'y2="{_f(y)}" stroke="{GRID}" stroke-width="1"/>')
            svg.text(x, y + 14, _pct(t), size=10, fill=INK_2, anchor="middle")
        y += 30
    svg.text(16, y + 4, "Source: te report figures, from the campaign database.", size=10,
             fill=INK_2)
    return svg.render(y + 18)


# -- (c) minutes per teardown, hand beside engine ------------------------------------


def _nice_max(x: float) -> float:
    for step in (40, 60, 120, 240, 480, 960, 1920, 3840):
        if x <= step:
            return float(step)
    return float(int(x // 1000 + 1) * 1000)


def minutes_svg(recs: list[MX.Rec], n: dict[str, Any]) -> str:
    by: dict[str, list[float]] = defaultdict(list)
    for r in recs:
        if r.minutes is not None:
            by["hand" if r.hand else "engine"].append(r.minutes)
    groups = (("hand", "Hand-made", ORANGE), ("engine", "Engine-made", BLUE))
    top = _nice_max(max([m for v in by.values() for m in v] + [1]))
    left, axis_w = 130, 470
    mp = n["minutes_per_teardown"]
    svg = Svg("Minutes per teardown, hand-made and engine-made",
              "; ".join(f"{who}: {mp[who]['teardowns']} teardowns, median {mp[who]['median']}, "
                        f"mean {mp[who]['mean']}" for who, _, _ in groups))
    svg.text(16, 28, "Minutes per teardown: hand-made beside engine-made", size=15, weight="600")
    svg.text(16, 46, "One dot per teardown; the bar marks the median.", size=11, fill=INK_2)
    # legend
    lx = 16
    for _, label, colour in groups:
        svg.circle(lx + 6, 64, 5, colour)
        svg.text(lx + 16, 68, label, size=11, fill=INK_2)
        lx += 110
    y = 86
    axis_top = y
    for who, label, colour in groups:
        vals = sorted(by.get(who, []))
        svg.text(left - 10, y + 24, label, anchor="end")
        if not vals:
            svg.text(left, y + 24, "no minutes recorded", size=11, fill=INK_2)
        for i, v in enumerate(vals):
            svg.circle(left + axis_w * v / top, y + 14 + (i % 3) * 8, 4.5, colour)
        if vals:
            med = mp[who]["median"]
            x = left + axis_w * med / top
            svg.line(x, y + 4, x, y + 40, INK, 2)
            svg.text(left + axis_w + 12, y + 20, f"median {med:g} min", size=11, fill=INK_2)
            svg.text(left + axis_w + 12, y + 34, f"mean {mp[who]['mean']:g}, n={len(vals)}",
                     size=11, fill=INK_2)
        y += 52
    svg.line(left, y, left + axis_w, y, INK_2, 1)
    for i in range(5):
        t = top * i / 4
        x = left + axis_w * i / 4
        svg.parts.insert(0, f'<line x1="{_f(x)}" y1="{_f(axis_top)}" x2="{_f(x)}" '
                            f'y2="{_f(y)}" stroke="{GRID}" stroke-width="1"/>')
        svg.text(x, y + 14, f"{t:g}", size=10, fill=INK_2, anchor="middle")
    svg.text(left + axis_w / 2, y + 30, "minutes", size=10, fill=INK_2, anchor="middle")
    svg.text(16, y + 50, "Hand-made: research + writing + slice minutes as typed; engine-made: "
             "review minutes. Source: te report figures.", size=10, fill=INK_2)
    return svg.render(y + 64)


def render_all(conn: sqlite3.Connection, n: dict[str, Any] | None = None) -> dict[str, str]:
    """{file name: svg text} for the three figures."""
    n = n or N.compute(conn)
    recs = MX.load(conn)
    return {"funnel.svg": funnel_svg(n), "reply_rate.svg": reply_rate_svg(n),
            "minutes.svg": minutes_svg(recs, n)}


def write(out: Path, figures: dict[str, str]) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in sorted(figures):
        p = out / name
        p.write_text(figures[name], encoding="utf-8", newline="\n")
        paths.append(p)
    return paths
