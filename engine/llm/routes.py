"""Model routes: which provider and model serve each kind of call, in one place.

From docs/PLAN.md "Models" and "Where the model is allowed":
- title_classifier: a small fast model (Groq), Gemini Flash as fallback (used
  directly when no Groq key is configured).
- judge, drafter: the strongest model (Claude Code headless), Gemini as fallback.
- verifier: Gemini, Groq as fallback; never the family that wrote the claim.
  If the judge itself fell back to Gemini, pass exclude_family="google" to
  gateway.call so the verifier skips Gemini.

Model names are the published ones as of this step; confirm them against your
own dashboards and change them here only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    provider: str  # a key of gateway.PROVIDERS
    model: str | None  # None lets the provider use its default


@dataclass(frozen=True)
class Route:
    name: str
    primary: Target
    fallback: Target | None = None

    @property
    def targets(self) -> tuple[Target, ...]:
        return (self.primary,) if self.fallback is None else (self.primary, self.fallback)


ROUTES: dict[str, Route] = {
    r.name: r
    for r in (
        Route(
            "title_classifier",
            Target("groq", "llama-3.1-8b-instant"),
            Target("gemini", "gemini-2.5-flash"),
        ),
        Route("judge", Target("claude", "opus"), Target("gemini", "gemini-2.5-pro")),
        Route("verifier", Target("gemini", "gemini-2.5-pro"), Target("groq", "llama-3.3-70b-versatile")),
        Route("drafter", Target("claude", "opus"), Target("gemini", "gemini-2.5-pro")),
    )
}

# Model family by provider; providers that host open models go by model name.
_PROVIDER_FAMILY = {"claude": "anthropic", "gemini": "google"}
_MODEL_FAMILY = (
    ("llama", "meta"),
    ("gemma", "google"),
    ("mixtral", "mistral"),
    ("mistral", "mistral"),
    ("qwen", "alibaba"),
    ("deepseek", "deepseek"),
    ("gpt-oss", "openai"),
    ("kimi", "moonshot"),
)


def family(target: Target) -> str:
    """The model family behind a target, e.g. 'anthropic', 'google', 'meta'."""
    if target.provider in _PROVIDER_FAMILY:
        return _PROVIDER_FAMILY[target.provider]
    name = (target.model or "").lower()
    for marker, fam in _MODEL_FAMILY:
        if marker in name:
            return fam
    return f"unknown:{target.provider}:{target.model}"


def get_route(route: str | Route) -> Route:
    if isinstance(route, Route):
        return route
    try:
        return ROUTES[route]
    except KeyError:
        raise KeyError(f"unknown route {route!r}; known: {', '.join(sorted(ROUTES))}") from None
