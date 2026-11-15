"""Phase 7 analysis (docs/PLAN.md Steps 83-85, Gate 7.1): the final numbers with
Wilson intervals, the anonymisation check and the three figures, all from the
database alone. `te report all --out DIR` regenerates every one of them.

    stats.py      the Wilson score interval
    numbers.py    `te report numbers`: rates with intervals, small groups merged
    figures.py    `te report figures`: three dependency-free SVGs
    anonymise.py  `te report anonymise-check`: no company, person or reply in a file

No network, no model call. Outputs are aggregate only by construction: no
company name, id, domain or person ever enters them, and a split value held by
fewer than MIN_GROUP sent companies is merged into "other".
"""
