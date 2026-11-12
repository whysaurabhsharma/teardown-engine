"""HTTP-API model providers: Groq, Gemini and Cloudflare Workers AI.

Each builds one request and sends it through engine/guard/model_api.py, the
only network path. API keys are read from the environment at call time
(GROQ_API_KEY, GOOGLE_API_KEY, CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID);
a missing key raises NotConfigured, which the gateway treats as unavailable.

None of these APIs is asked to enforce the schema itself: the schema is stated
in the prompt and the gateway validates what comes back.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote

from engine.guard import model_api
from engine.llm.base import NotConfigured, ProviderError, ProviderResult, QuotaError

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CLOUDFLARE_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"


def _key(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise NotConfigured(f"${name} is not set")
    return value


def with_schema(prompt: str, schema: dict[str, Any]) -> str:
    return (
        f"{prompt}\n\nReturn only a JSON object, with no other text, that validates "
        f"against this JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}"
    )


def _post(provider: str, url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
    try:
        return model_api.post_json(provider, url, payload, headers)
    except model_api.ModelApiQuota as exc:
        raise QuotaError(str(exc)) from exc
    except model_api.ModelApiError as exc:
        raise ProviderError(str(exc)) from exc


def _parse(text: Any) -> tuple[Any | None, str | None]:
    if isinstance(text, dict):
        return text, None
    if not isinstance(text, str) or not text.strip():
        return None, "the response had no text"
    t = text.strip()
    if t.startswith("```"):  # tolerate a fenced block
        t = t.strip("`")
        t = t[4:] if t.lower().startswith("json") else t
    try:
        return json.loads(t), None
    except ValueError:
        return None, "the response was not valid JSON"


def _nonneg(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def groq(prompt: str, schema: dict[str, Any], model: str | None) -> ProviderResult:
    if not model:
        raise ProviderError("groq needs a model name")
    key = _key("GROQ_API_KEY")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": with_schema(prompt, schema)}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    data = _post("groq", GROQ_URL, payload, {"Authorization": f"Bearer {key}"})
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        text = None
    output, problem = _parse(text)
    usage = data.get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    return ProviderResult(
        output=output,
        model=str(data.get("model") or model) if isinstance(data, dict) else model,
        input_tokens=_nonneg(usage.get("prompt_tokens")),
        output_tokens=_nonneg(usage.get("completion_tokens")),
        problem=problem,
    )


def gemini(prompt: str, schema: dict[str, Any], model: str | None) -> ProviderResult:
    if not model:
        raise ProviderError("gemini needs a model name")
    key = _key("GOOGLE_API_KEY")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": with_schema(prompt, schema)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    url = GEMINI_URL.format(model=quote(model, safe=""))
    data = _post("gemini", url, payload, {"x-goog-api-key": key})
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    except (KeyError, IndexError, TypeError):
        text = None
    output, problem = _parse(text)
    usage = data.get("usageMetadata") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    return ProviderResult(
        output=output,
        model=str(data.get("modelVersion") or model) if isinstance(data, dict) else model,
        input_tokens=_nonneg(usage.get("promptTokenCount")),
        output_tokens=_nonneg(usage.get("candidatesTokenCount")),
        problem=problem,
    )


def cloudflare(prompt: str, schema: dict[str, Any], model: str | None) -> ProviderResult:
    if not model:
        raise ProviderError("cloudflare needs a model name")
    token = _key("CLOUDFLARE_API_TOKEN")
    account = _key("CLOUDFLARE_ACCOUNT_ID")
    payload = {"messages": [{"role": "user", "content": with_schema(prompt, schema)}]}
    url = CLOUDFLARE_URL.format(account=quote(account, safe=""), model=model)
    data = _post("cloudflare", url, payload, {"Authorization": f"Bearer {token}"})
    result = data.get("result") if isinstance(data, dict) else None
    result = result if isinstance(result, dict) else {}
    output, problem = _parse(result.get("response"))
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    return ProviderResult(
        output=output,
        model=model,
        input_tokens=_nonneg(usage.get("prompt_tokens")),
        output_tokens=_nonneg(usage.get("completion_tokens")),
        problem=problem,
    )
