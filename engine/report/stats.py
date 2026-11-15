"""The 95% Wilson score interval (docs/PLAN.md Step 83).

At this sample size every rate is soft: a 20 percent reply rate on 70 delivered
carries an interval of roughly 12 to 31 percent. Wilson, not the normal
approximation, because it stays inside [0, 1] and behaves at 0 and n.
"""

from __future__ import annotations

import math
from typing import Any

Z95 = 1.959963984540054  # the 97.5th percentile of the standard normal


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """(low, high) for k successes in n trials; None when n is 0."""
    if n <= 0:
        return None
    if not 0 <= k <= n:
        raise ValueError(f"successes {k} outside 0..{n}")
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    lo, hi = max(0.0, centre - half), min(1.0, centre + half)
    if k == 0:
        lo = 0.0
    if k == n:
        hi = 1.0
    return lo, hi


def rate(k: int, n: int) -> dict[str, Any]:
    """{n, of, rate, low, high}, rounded to 4 places; rate and bounds None when of is 0."""
    ci = wilson(k, n)
    return {"n": k, "of": n,
            "rate": round(k / n, 4) if n else None,
            "low": round(ci[0], 4) if ci else None,
            "high": round(ci[1], 4) if ci else None}


def fmt(r: dict[str, Any] | None) -> str:
    """'14/70  20.0%  (95% CI 12.4 to 31.0%)'."""
    if r is None:
        return "-"
    if not r["of"]:
        return f"{r['n']}/{r['of']}  n/a"
    return (f"{r['n']}/{r['of']}  {r['rate'] * 100:.1f}%  "
            f"(95% CI {r['low'] * 100:.1f} to {r['high'] * 100:.1f}%)")
