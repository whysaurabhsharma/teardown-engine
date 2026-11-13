"""Scoring the judge on the held-out gold rows (docs/PLAN.md Step 50, Gate 4.2;
protocol/GAPS.md "Gold set").

    te judge score [--dry-run] [--reveal-held-out]

1. The judge runs on every gold company (each company's gold row at its highest
   gaps_version), with the teach examples, through judge.judge_company: a
   company with a diagnosis at the prompt version, or a cached judge output,
   makes no call. A company not yet extracted, and one whose judging fails,
   is "not judged". `--dry-run` makes no call and writes nothing: it scores
   only diagnoses already stored.
2. For the held-out rows only:
   - same call: the stored diagnosis names the human's gap, or both abstain
     (target: TARGET_SAME of HELD_OUT);
   - false gap: the judge names a gap where the human abstained (at most
     TARGET_FALSE_GAP);
   - faithfulness failures that would reach a draft: the stored claims of every
     held-out diagnosis that names a gap, checked again against the captured
     artifacts (faithfulness.check_claims); target zero.
   A not-judged row is not a same call. The gate needs exactly HELD_OUT
   held-out rows.
   The stored diagnosis is the judge's call after the faithfulness check, and
   after the verifier when `te judge verify` has run on it.
3. The result is recorded in judge_scores (counts only). Each prompt_version
   scored is one round; GAPS.md allows MAX_ROUNDS, and a later version is
   warned about. A re-run with the same counts at the same version writes
   nothing.

Per-row detail never prints a held-out quote. The human's call (and the
judge's, which with "agree" would give it away) are printed only with
--reveal-held-out to a terminal, as `te gold list --reveal-held-out` does;
otherwise each held-out row shows only agree, disagree or not judged.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from engine import db
from engine.guard.budget import Budget
from engine.guard.cache import Cache
from engine.judge import faithfulness as F
from engine.judge import gold as G
from engine.judge import judge as J
from engine.models import JudgeScore
from engine.states import Status

HELD_OUT = G.GATE_ROWS - G.TEACH_SIZE  # 13
TARGET_SAME = 10
TARGET_FALSE_GAP = 1
TARGET_FAITHFULNESS = 0
MAX_ROUNDS = 3


class ScoreError(RuntimeError):
    """Scoring refuses to run (no split, no gold rows)."""


@dataclass
class Row:
    company_id: str
    human_gap: str | None  # None: the human abstained
    judged: bool
    judge_gap: str | None = None  # None: the judge abstained (when judged)
    faithfulness_failures: int = 0
    note: str = ""  # why not judged

    @property
    def same(self) -> bool:
        return self.judged and self.human_gap == self.judge_gap

    @property
    def false_gap(self) -> bool:
        return self.judged and self.human_gap is None and self.judge_gap is not None


@dataclass
class Score:
    prompt_version: str
    rows: list[Row] = field(default_factory=list)  # held-out rows only
    calls: int = 0
    judged_now: int = 0
    teach_problems: list[str] = field(default_factory=list)  # teach rows not judged

    @property
    def held_out(self) -> int:
        return len(self.rows)

    @property
    def same_call(self) -> int:
        return sum(r.same for r in self.rows)

    @property
    def false_gap(self) -> int:
        return sum(r.false_gap for r in self.rows)

    @property
    def faithfulness_failures(self) -> int:
        return sum(r.faithfulness_failures for r in self.rows)

    @property
    def not_judged(self) -> int:
        return sum(not r.judged for r in self.rows)

    def checks(self) -> list[tuple[bool, str]]:
        return [
            (self.held_out == HELD_OUT,
             f"held-out rows: {self.held_out}; need {HELD_OUT}"),
            (self.same_call >= TARGET_SAME,
             f"same call as the human: {self.same_call} of {self.held_out}; target "
             f"{TARGET_SAME} of {HELD_OUT} or better"),
            (self.false_gap <= TARGET_FALSE_GAP,
             f"judge names a gap where the human abstained: {self.false_gap}; target "
             f"{TARGET_FALSE_GAP} at most"),
            (self.faithfulness_failures <= TARGET_FAITHFULNESS,
             f"claims failing the faithfulness check that would reach a draft: "
             f"{self.faithfulness_failures}; target {TARGET_FAITHFULNESS}"),
        ]

    @property
    def passed(self) -> bool:
        return all(ok for ok, _ in self.checks())


def _faithfulness_failures(conn: sqlite3.Connection, cache: Cache, company_id: str,
                           diag: sqlite3.Row) -> int:
    """Failing claims of a diagnosis that names a gap; an abstention reaches no draft."""
    if not diag["attempted"]:
        return 0
    claims = json.loads(diag["claims"])
    _, f = F.check_claims(claims, F.load_arts(conn, cache, company_id))
    return len(f.failed)


def _judgeable(conn: sqlite3.Connection, company_id: str) -> bool:
    r = conn.execute("SELECT status FROM companies WHERE company_id = ?",
                     (company_id,)).fetchone()
    return r is not None and Status(r["status"]) in J._FROM_EXTRACTED


def run(conn: sqlite3.Connection, cache: Cache, *, dry_run: bool = False,
        budget: Budget | None = None) -> Score:
    rows = G.current_rows(conn)
    if not rows:
        raise ScoreError("no gold rows; `te gold add` them first")
    problem = J.table_problem()
    if problem:
        raise ScoreError(problem)
    try:
        examples = J.teach_examples(conn)
    except J.JudgeError as e:
        raise ScoreError(str(e)) from e
    version = J.version_for(False)
    score = Score(version)
    for cid in sorted(rows):
        g = rows[cid]
        note = ""
        if J.stored(conn, cid, version) is None and not dry_run:
            if _judgeable(conn, cid):
                r = J.judge_company(conn, cache, cid, examples=examples, budget=budget)
                score.calls += r.calls
                if r.outcome in ("diagnosed", "abstained"):
                    score.judged_now += 1
                else:
                    note = r.outcome
            else:
                note = "not extracted"
        diag = J.stored(conn, cid, version)
        if diag is None and not note:
            note = "no diagnosis stored" if dry_run else "not judged"
        if g["split"] != "held_out":
            if diag is None:
                score.teach_problems.append(f"{cid}: {note}")
            continue
        if diag is None:
            score.rows.append(Row(cid, g["gap_type"], False, note=note))
            continue
        score.rows.append(Row(cid, g["gap_type"], True, diag["gap_type"],
                              _faithfulness_failures(conn, cache, cid, diag)))
    return score


# -- rounds ------------------------------------------------------------------------------


def rounds(conn: sqlite3.Connection) -> list[str]:
    """Prompt versions scored so far, in the order first scored."""
    return [r[0] for r in conn.execute(
        "SELECT prompt_version FROM judge_scores GROUP BY prompt_version "
        "ORDER BY MIN(score_id)")]


def round_number(conn: sqlite3.Connection, prompt_version: str) -> int:
    done = rounds(conn)
    return done.index(prompt_version) + 1 if prompt_version in done else len(done) + 1


def round_warning(conn: sqlite3.Connection, prompt_version: str) -> str | None:
    n = round_number(conn, prompt_version)
    if n > MAX_ROUNDS:
        return (f"round {n} against the held-out set: GAPS.md allows {MAX_ROUNDS} at most. "
                "Every round tuned against it makes it less held out; report the score as "
                "what it is")
    return None


def record(conn: sqlite3.Connection, score: Score) -> bool:
    """Insert the counts unless the last row for this version holds the same ones."""
    s = JudgeScore(prompt_version=score.prompt_version, held_out=score.held_out,
                   same_call=score.same_call, false_gap=score.false_gap,
                   faithfulness_failures=score.faithfulness_failures,
                   not_judged=score.not_judged, passed=score.passed)
    last = conn.execute("SELECT held_out, same_call, false_gap, faithfulness_failures, "
                        "not_judged, passed FROM judge_scores WHERE prompt_version = ? "
                        "ORDER BY score_id DESC LIMIT 1", (score.prompt_version,)).fetchone()
    now = (s.held_out, s.same_call, s.false_gap, s.faithfulness_failures, s.not_judged,
           int(s.passed))
    if last is not None and tuple(last) == now:
        return False
    with conn:
        db.insert(conn, s)
    return True


def history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM judge_scores ORDER BY score_id").fetchall()

