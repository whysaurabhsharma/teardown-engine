"""Shared types for the model gateway and its providers.

A provider is a callable `(prompt, schema, model) -> ProviderResult`. It sends
one request and reports what came back; it never validates against the schema,
retries or falls back. Those belong to engine/llm/gateway.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class LlmError(RuntimeError):
    """A model call could not produce a valid result."""


class ProviderError(LlmError):
    """The provider failed in a way that falling back would not fix."""


class ProviderUnavailable(LlmError):
    """The provider cannot serve this call now; the gateway tries the fallback."""


class QuotaError(ProviderUnavailable):
    """Quota or rate limit reached."""


class NotConfigured(ProviderUnavailable):
    """No API key in the environment, or the executable is not installed."""


class ContactInPrompt(LlmError):
    """The prompt contains a value from the contacts table. Nothing was sent."""


class HeldOutInPrompt(LlmError):
    """The prompt contains a quote from a held_out gold row. Nothing was sent."""


class SchemaInvalid(LlmError):
    """The output failed schema validation twice (the call and its one retry)."""


class CachedOutputInvalid(LlmError):
    """A stored output no longer matches the schema: bump prompt_version."""


@dataclass(frozen=True)
class ProviderResult:
    output: Any | None  # the parsed JSON result; None means no usable output
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    problem: str | None = None  # why output is None, for the retry note


class Provider(Protocol):
    def __call__(self, prompt: str, schema: dict[str, Any], model: str | None) -> ProviderResult: ...
