"""The faithfulness check (docs/PLAN.md Step 48; protocol/GAPS.md "Checks after
the judge", item 1). Runs after every judge call, before the diagnosis is
stored; `te judge faithfulness` re-runs it.

Each claim is checked against the company's captured artifacts:

- Found claim (found: true). The quote, normalised, must be a substring of the
  artifact's stored text, OR of its nav and footer text (structure
  removed_text), OR of a string in its structure (link text or href, field
  name or label, script src). Normalisation is applied to both sides:
  whitespace runs collapsed to one space (extract_text.normalise_ws), case
  folded, and the curly quotes ‘ ’ “ ” folded to ' and " (gold.guard_norm,
  which the held-out guard also uses). Then the claim's url must equal that
  artifact's URL exactly. A quote that misses but scores 95 or more on
  rapidfuzz partial_ratio against the text or the nav/footer text is a near
  miss: logged in its own category (and counted in `near_miss`), still a
  failure.
- Not-found claim (found: false). Passes only when that artifact kind's
  capture outcome is not_found (blocked, dead, robots_denied or no row mean
  unseen, never absent), and its url is where it was looked for: the
  artifact's URL, the homepage URL or the homepage's /sitemap.xml.
- A claim that breaks the claim contract (found without a quote, not found
  with one, blank text or url) is `malformed`.

Failed claims are removed from the stored claims and kept, with their
category, in faithfulness.failed. A diagnosis that names a gap but keeps
fewer than two passing claims from two artifact kinds, or no passing found
claim, becomes an abstention: attempted false, gap_type null, and an
abstain_reason starting "thin: faithfulness". The judge's original output stays
in llm_calls, so a re-run starts from it.

No network and no model call.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from rapidfuzz import fuzz

from engine.capture import extract_text as X
from engine.extract.base import Art
from engine.guard.cache import Cache
from engine.judge.gold import guard_norm
from engine.models import Claim, Diagnosis, Faithfulness, FaithfulnessFailure

NEAR_MISS = 95.0  # rapidfuzz partial_ratio at or above this is a near miss
MIN_KINDS = 2  # passing claims must come from at least this many artifact kinds
UNSEEN = frozenset({"blocked", "dead", "robots_denied"})


def normalise(s: str) -> str:
    """Whitespace collapsed, smart quotes folded, case folded (both sides)."""
    return guard_norm(s)


# -- the artifacts -----------------------------------------------------------------------


def load_arts(conn: sqlite3.Connection, cache: Cache, company_id: str) -> dict[str, Art]:
    """kind -> Art for the company's artifacts rows. An ok artifact not extracted at
    the current extractor version, or missing from the cache, has text None."""
    arts: dict[str, Art] = {}
    for r in conn.execute("SELECT * FROM artifacts WHERE company_id = ? ORDER BY artifact_id",
                          (company_id,)):
        text = structure = None
        if r["outcome"] == "ok" and r["extractor_version"] == X.EXTRACTOR_VERSION:
            text, structure = X.stored_text(cache, r), X.stored_structure(cache, r)
        arts[r["kind"]] = Art(r["kind"], r["url"], r["outcome"], text, structure)
    return arts


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def sitemap_url(url: str) -> str | None:
    p = urlsplit(url)
    if not p.scheme or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}/sitemap.xml"


def looked_for_urls(art: Art, arts: dict[str, Art]) -> set[str]:
    """Where an absence may say it looked: the artifact's URL, the homepage, its sitemap."""
    out = {art.url}
    home = arts.get("homepage")
    if home is not None:
        out.add(home.url)
        sm = sitemap_url(home.url)
        if sm:
            out.add(sm)
    return out


# -- one claim ---------------------------------------------------------------------------


def _quote_found(quote: str, art: Art) -> tuple[bool, float]:
    """(found, best partial ratio against the text and the nav/footer text)."""
    q = normalise(quote)
    if not q:
        return False, 0.0
    hays = [normalise(art.text or ""), normalise(art.removed)]
    if any(q in h for h in hays if h):
        return True, 100.0
    if any(q in normalise(s) for s in _strings(art.structure)):
        return True, 100.0
    best = 0.0
    for h in hays:
        if len(h) >= len(q):  # a shorter haystack would align inside the quote
            best = max(best, fuzz.partial_ratio(q, h))
    return False, round(best, 1)


def check_claim(raw: dict[str, Any], arts: dict[str, Art]) -> tuple[Claim | None,
                                                                    str | None, float | None]:
    """(the claim if it passes, else None; the failure category; the score)."""
    try:
        claim = Claim.model_validate(raw)
    except ValidationError:
        return None, "malformed", None
    art = arts.get(claim.artifact_kind)
    if not claim.found:
        if art is None or art.outcome != "not_found":
            return None, "not_found_invalid", None
        if claim.url not in looked_for_urls(art, arts):
            return None, "wrong_url", None
        return claim, None, None
    if art is None or art.outcome in UNSEEN or not art.ok:
        return None, "unseen", None
    found, score = _quote_found(claim.quote or "", art)
    if not found:
        return None, ("near_miss" if score >= NEAR_MISS else "miss"), score
    if claim.url != art.url:
        return None, "wrong_url", None
    return claim, None, None


def _failure(i: int, raw: Any, category: str, score: float | None) -> FaithfulnessFailure:
    d = raw if isinstance(raw, dict) else {}

    def s(key: str) -> str:
        v = d.get(key)
        return v if isinstance(v, str) else ""

    q = d.get("quote")
    return FaithfulnessFailure(index=i, category=category, artifact_kind=s("artifact_kind"),
                               found=d.get("found") is True, url=s("url"),
                               quote=q if isinstance(q, str) else None, text=s("text"),
                               score=score)


def check_claims(raws: list[Any], arts: dict[str, Art]) -> tuple[list[Claim], Faithfulness]:
    kept: list[Claim] = []
    failed: list[FaithfulnessFailure] = []
    for i, raw in enumerate(raws):
        claim, category, score = (check_claim(raw, arts) if isinstance(raw, dict)
                                  else (None, "malformed", None))
        if claim is not None:
            kept.append(claim)
        else:
            failed.append(_failure(i, raw, category or "malformed", score))
    f = Faithfulness(checked=len(raws), passed=len(kept),
                     near_miss=sum(1 for x in failed if x.category == "near_miss"),
                     failed=failed)
    return kept, f


# -- one diagnosis -----------------------------------------------------------------------


def evidence_problem(claims: list[Claim]) -> str | None:
    """None when the claims can carry a gap (GAPS.md Evidence rules); else why not."""
    kinds = {c.artifact_kind for c in claims}
    if len(kinds) < MIN_KINDS:
        return (f"{len(claims)} passing claim(s) from {len(kinds)} artifact kind(s); "
                f"a gap needs {MIN_KINDS} kinds")
    if not any(c.found for c in claims):
        return "no passing found claim; absence alone never names a gap"
    return None


def apply(diag: Diagnosis, raw_claims: list[Any], arts: dict[str, Art]) -> Diagnosis:
    """The diagnosis with only passing claims, its faithfulness record, and turned
    into an abstention when what passes cannot carry the gap."""
    kept, f = check_claims(raw_claims, arts)
    data = diag.model_dump()
    data.update(claims=[c.model_dump() for c in kept], faithfulness=None)
    if diag.attempted:
        problem = evidence_problem(kept)
        if problem:
            f.converted = True
            data.update(attempted=False, gap_type=None,
                        abstain_reason=(f"thin: faithfulness: {f.passed} of {f.checked} claims "
                                        f"passed; {problem}; the judge named {diag.gap_type}"))
    data["faithfulness"] = f.model_dump()
    return Diagnosis.model_validate(data)
