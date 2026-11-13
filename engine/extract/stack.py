"""stack_signals (docs/PLAN.md Steps 37-38): tools the bundle shows, matched
against patterns.yaml.

One fact per tool, value {"tool": id, "category": ..., "via": ...}, quoting
the first evidence in a fixed order: the dns artifact first (root TXT
verification records, SPF includes, MX hosts, CNAME targets of the checked
sending subdomains), then the web kinds in bundle order (script src, iframe
src, link href, form action, as stored in the structure).

No absence fact: most tools load through a tag manager or sit behind a login,
so "no tool detected" is not evidence that none is used.
"""

from __future__ import annotations

from typing import Any

from engine.capture import bundle as B
from engine.extract import dns as D
from engine.extract.base import Bundle, FactOut, host_matches, patterns, url_matches

FACT = "stack_signals"


def _match(tool: dict[str, Any], key: str, value: str, as_url: bool) -> bool:
    for p in tool.get(key) or []:
        if as_url and url_matches(value, p):
            return True
        if not as_url and host_matches(value, p):
            return True
    return False


def _dns_evidence(b: Bundle) -> list[tuple[str, str, D.Line]]:
    """(key, value-to-match, line) in text order."""
    if b.state(D.KIND) != "ok":
        return []
    art = b.arts[D.KIND]
    d = D.domain_of(b, art)
    subs = {f"{s}.{d}" for s in B.SENDING_SUBDOMAINS}
    out = []
    for x in D.parse(art.text or ""):
        if x.status != "answer":
            continue
        if x.name == d and x.rtype == "TXT":
            if x.value.lower().startswith("v=spf1"):
                for tok in x.value.split()[1:]:
                    t = tok.lstrip("+-~?")
                    if t.lower().startswith("include:"):
                        out.append(("spf_includes", t.split(":", 1)[1], x))
            else:
                out.append(("txt_prefixes", x.value, x))
        elif x.name == d and x.rtype == "MX":
            host = x.value.split()[-1] if x.value.split() else ""
            out.append(("mx_hosts", host, x))
        elif x.name in subs and x.rtype in ("CNAME", "MX"):
            host = x.value.split()[-1] if x.value.split() else ""
            out.append(("cname_targets", host, x))
    return out


def _web_evidence(b: Bundle) -> list[tuple[str, str, str, str]]:
    """(kind, structure key, stored string, absolute url) in bundle and page order."""
    out = []
    for kind in B.WEB_KINDS:
        if b.state(kind) != "ok":
            continue
        s = b.arts[kind].structure or {}
        for sc in s.get("scripts") or []:
            if sc.get("src"):
                out.append((kind, "script", sc["src"], sc.get("url") or sc["src"]))
        for fr in s.get("iframes") or []:
            if fr.get("src"):
                out.append((kind, "iframe", fr["src"], fr.get("url") or fr["src"]))
        for a in s.get("links") or []:
            if a.get("href"):
                out.append((kind, "link", a["href"], a.get("url") or a["href"]))
        for f in s.get("forms") or []:
            if f.get("action"):
                out.append((kind, "form", f["action"], f.get("url") or f["action"]))
    return out


_WEB_KEYS = {"script": ("script_hosts",), "iframe": ("script_hosts", "link_hosts"),
             "link": ("link_hosts",), "form": ("link_hosts",)}


def stack_signals(b: Bundle) -> list[FactOut]:
    tools = patterns()["tools"]
    seen: set[str] = set()
    out: list[FactOut] = []
    dns_ev = _dns_evidence(b)
    web_ev = _web_evidence(b)
    for t in tools:
        for key, value, line in dns_ev:
            if key == "txt_prefixes":
                hit = any(value.lower().startswith(p.lower()) for p in t.get(key) or [])
            else:
                hit = _match(t, key, value, as_url=False)
            if hit:
                out.append(FactOut(FACT, {"tool": t["id"], "category": t["category"],
                                          "via": key}, D.KIND, line.text, "text", True,
                                   line.start))
                seen.add(t["id"])
                break
        if t["id"] in seen:
            continue
        for kind, what, stored, url in web_ev:
            if any(_match(t, k, url, as_url=True) for k in _WEB_KEYS[what]):
                out.append(FactOut(FACT, {"tool": t["id"], "category": t["category"],
                                          "via": what}, kind, stored, "structure", True, 0))
                seen.add(t["id"])
                break
    return out


def tools_in(b: Bundle, kind: str, category: str) -> list[tuple[str, str]]:
    """(tool id, stored string) for web evidence of one category on one kind."""
    out = []
    for k, what, stored, url in _web_evidence(b):
        if k != kind:
            continue
        for t in patterns()["tools"]:
            if t["category"] == category and any(
                    _match(t, key, url, as_url=True) for key in _WEB_KEYS[what]):
                out.append((t["id"], stored))
    return out


EXTRACTORS = (
    ("stack_signals", stack_signals, ("dns", *B.WEB_KINDS)),
)
