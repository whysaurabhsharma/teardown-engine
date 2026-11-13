"""What every E4 fact extractor shares (docs/PLAN.md Step 37).

An extractor is a pure function from a Bundle to a list of FactOut. It reads
only the bundle: the artifacts' extracted text and structure (Step 35), the
company's domain and name, the signal's title, and a title classifier that
never calls a model. No network, no database, no model.

Quotes (protocol/GAPS.md "Evidence rules"; CLAUDE.md "No quote, no row")
-------------------------------------------------------------------------
Every FactOut carries a quote and says where it can be checked
(quote_source):

- text          a verbatim slice of the artifact's extracted text;
                char_start is its exact offset in that text.
- removed_text  a verbatim slice of the nav and footer text the text dropped
                (structure["removed_text"], joined with "\\n"); char_start is
                its offset in that join.
- structure     a string stored in the structure JSON: a link's text or href,
                a field's name or label, a script or iframe src. char_start 0.
- not_found     an absence that rests on the artifact's capture outcome being
                not_found. quote is "not_found: <kind>", found False.
- absent        an absence on an artifact that was seen (ok) where nothing
                matched. quote is "absent: <what> in <kind>", found False.

Absence is never written from an artifact whose outcome is blocked, dead or
robots_denied (or that has no row): unseen is not absent. Such an extractor
returns nothing, and the runner counts it.

`verify` checks a FactOut against its artifact the way the faithfulness check
(Step 48) will: after `normalise_ws` on both sides. The runner drops any fact
that fails it, so a bug in a rule can lose a fact but never invent a quote.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from engine.capture.extract_text import normalise_ws

HERE = Path(__file__).parent
PATTERNS_PATH = HERE / "patterns.yaml"
LEXICONS_PATH = HERE / "lexicons.yaml"

UNSEEN = frozenset({"blocked", "dead", "robots_denied"})
MAX_QUOTE = 200  # characters; a longer line is cut to a window around the match
QUOTE_SOURCES = ("text", "removed_text", "structure", "not_found", "absent")


# -- the bundle --------------------------------------------------------------------------


@dataclass(frozen=True)
class Art:
    """One artifact as an extractor sees it."""

    kind: str
    url: str
    outcome: str
    text: str | None = None  # extracted text; None when not ok or not extracted
    structure: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok" and self.text is not None and self.structure is not None

    @property
    def removed(self) -> str:
        if not self.structure:
            return ""
        return "\n".join(r.get("text", "") for r in self.structure.get("removed_text") or [])


@dataclass(frozen=True)
class Bundle:
    company_id: str
    domain: str | None
    name: str
    arts: dict[str, Art]
    signal_title: str | None = None
    # title -> role family, or None when neither the rules nor the cache place it
    classify: Callable[[str], str | None] = field(default=lambda _t: None)

    def art(self, kind: str) -> Art | None:
        return self.arts.get(kind)

    def state(self, kind: str) -> str:
        """ok | not_found | unseen (blocked, dead, robots_denied, no row, or ok
        but not extracted)."""
        a = self.arts.get(kind)
        if a is None:
            return "unseen"
        if a.outcome == "not_found":
            return "not_found"
        if a.ok:
            return "ok"
        return "unseen"


@dataclass(frozen=True)
class FactOut:
    fact: str
    value: Any
    artifact_kind: str
    quote: str
    quote_source: str
    found: bool = True
    char_start: int = 0

    def value_text(self) -> str:
        """Strings as they are; everything else as compact, sorted JSON."""
        if isinstance(self.value, str):
            return self.value
        return json.dumps(self.value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))


# -- quotes ------------------------------------------------------------------------------


def _line_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    return ls, (len(text) if le == -1 else le)


def window(text: str, start: int, end: int, max_len: int = MAX_QUOTE) -> tuple[str, int]:
    """The line holding text[start:end], or, when that line is long, a window of
    whole words around the match within it. Returns (quote, offset)."""
    ls, le = _line_bounds(text, start, end)
    if le - ls <= max_len:
        return text[ls:le], ls
    pad = max(0, (max_len - (end - start)) // 2)
    ws = max(ls, start - pad)
    we = min(le, end + pad)
    if ws > ls:  # start on a word boundary
        nxt = text.find(" ", ws, start)
        ws = nxt + 1 if nxt != -1 else start
    if we < le:
        prv = text.rfind(" ", end, we)
        we = prv if prv != -1 else end
    return text[ws:we], ws


def text_quote(fact: str, value: Any, art: Art, start: int, end: int, *,
               found: bool = True) -> FactOut:
    q, off = window(art.text or "", start, end)
    return FactOut(fact, value, art.kind, q, "text", found, off)


def exact_text_quote(fact: str, value: Any, art: Art, needle: str, *,
                     found: bool = True) -> FactOut | None:
    """needle as it stands in the text (first occurrence), or None."""
    i = (art.text or "").find(needle) if needle else -1
    if i < 0:
        return None
    return FactOut(fact, value, art.kind, needle, "text", found, i)


def removed_quote(fact: str, value: Any, art: Art, needle: str, *,
                  found: bool = True) -> FactOut | None:
    i = art.removed.find(needle) if needle else -1
    if i < 0:
        return None
    return FactOut(fact, value, art.kind, needle, "removed_text", found, i)


def structure_quote(fact: str, value: Any, art: Art, stored: str, *,
                    found: bool = True) -> FactOut:
    return FactOut(fact, value, art.kind, stored, "structure", found, 0)


def best_quote(fact: str, value: Any, art: Art, s: str, *, found: bool = True) -> FactOut:
    """A string the structure stores (link text, label): from the text if it is
    there, else from the nav or footer text, else from the structure itself."""
    return (exact_text_quote(fact, value, art, s, found=found)
            or removed_quote(fact, value, art, s, found=found)
            or structure_quote(fact, value, art, s, found=found))


def not_found(fact: str, value: Any, kind: str) -> FactOut:
    return FactOut(fact, value, kind, f"not_found: {kind}", "not_found", False, 0)


def absent(fact: str, value: Any, kind: str, what: str) -> FactOut:
    return FactOut(fact, value, kind, f"absent: {what} in {kind}", "absent", False, 0)


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def verify(f: FactOut, art: Art | None) -> bool:
    """True when the quote can be found where quote_source says."""
    if art is None or art.kind != f.artifact_kind or not f.quote.strip():
        return False
    q = normalise_ws(f.quote)
    if f.quote_source == "not_found":
        return art.outcome == "not_found" and not f.found and f.quote == f"not_found: {art.kind}"
    if f.quote_source == "absent":
        return art.ok and not f.found and f.quote.startswith("absent: ") \
            and f.quote.endswith(f" in {art.kind}")
    if not art.ok:
        return False
    if f.quote_source == "text":
        text = art.text or ""
        return (text[f.char_start:f.char_start + len(f.quote)] == f.quote
                and q in normalise_ws(text))
    if f.quote_source == "removed_text":
        return q in normalise_ws(art.removed)
    if f.quote_source == "structure":
        return any(q in normalise_ws(s) for s in _strings(art.structure))
    return False


# -- matching ----------------------------------------------------------------------------


def host_path(url: str | None) -> tuple[str, str]:
    if not url:
        return "", ""
    try:
        p = urlsplit(url if "//" in url else "//" + url)
    except ValueError:
        return "", ""
    return (p.hostname or "").lower().rstrip("."), p.path or ""


def host_matches(host: str, pattern: str) -> bool:
    """pattern is a domain: the host itself or any subdomain of it."""
    pattern = pattern.lower().rstrip(".")
    host = host.lower().rstrip(".")
    return bool(host) and (host == pattern or host.endswith("." + pattern))


def url_matches(url: str | None, pattern: str) -> bool:
    """pattern is "host" or "host/path-prefix"."""
    host, path = host_path(url)
    phost, _, ppath = pattern.partition("/")
    if not host_matches(host, phost):
        return False
    return not ppath or path.lstrip("/").startswith(ppath)


@dataclass(frozen=True)
class Lex:
    id: str
    rxs: tuple[re.Pattern[str], ...]

    def finditer(self, text: str) -> Iterator[re.Match[str]]:
        hits = [m for rx in self.rxs for m in rx.finditer(text) if m.end() > m.start()]
        hits.sort(key=lambda m: (m.start(), -m.end()))
        yield from hits

    def first(self, text: str) -> re.Match[str] | None:
        return next(self.finditer(text), None)


def _compile(entries: list[dict[str, Any]]) -> tuple[Lex, ...]:
    out = []
    for e in entries:
        flags = 0 if e.get("case_sensitive") else re.IGNORECASE
        out.append(Lex(str(e["id"]), tuple(re.compile(p, flags) for p in e["patterns"])))
    return tuple(out)


@cache
def patterns() -> dict[str, Any]:
    doc = yaml.safe_load(PATTERNS_PATH.read_text(encoding="utf-8"))
    ids = [t["id"] for t in doc["tools"]]
    if len(ids) != len(set(ids)):
        raise ValueError("patterns.yaml: duplicate tool id")
    return doc


@cache
def lexicons() -> dict[str, Any]:
    doc = yaml.safe_load(LEXICONS_PATH.read_text(encoding="utf-8"))
    return {"version": doc["version"],
            **{k: _compile(doc[k]) for k in ("jd_tools", "regions", "scope_flags")}}


def lexicon_hits(fact: str, art: Art, lex: tuple[Lex, ...]) -> list[FactOut]:
    """One fact per lexicon entry found in the text, quoting its first match."""
    text = art.text or ""
    out = []
    for e in lex:
        m = e.first(text)
        if m is not None:
            out.append(text_quote(fact, e.id, art, m.start(), m.end()))
    return out
