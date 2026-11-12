"""Model providers: claude_headless (fake subprocess, fake executable) and the
HTTP-API providers through guard.model_api (httpx.MockTransport). No real model
or network calls."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from engine.guard import model_api
from engine.llm import api_providers, claude_headless
from engine.llm.base import NotConfigured, ProviderError, QuotaError

SCHEMA = {
    "type": "object",
    "description": "A gap diagnosis with spaces and \"quotes\" in it.",
    "properties": {"gap": {"type": "string"}},
    "required": ["gap"],
}
_REAL_PACE = model_api._pace
PROMPT = "Zentrico's careers page lists a Founding Account Executive role. Café ✓"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url}")

    monkeypatch.setattr(model_api, "transport", httpx.MockTransport(refuse))
    monkeypatch.setattr(model_api, "_pace", lambda *a: None)


# --- claude_headless with a faked subprocess.run ----------------------------------

class FakeRun:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0, exc: Exception | None = None):
        self.stdout, self.stderr, self.returncode, self.exc = stdout, stderr, returncode, exc
        self.args: Any = None
        self.kwargs: dict[str, Any] = {}

    def __call__(self, args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        self.args, self.kwargs = args, kwargs
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


def _payload(**kw: Any) -> str:
    base = {
        "type": "result", "subtype": "success", "is_error": False, "result": "",
        "total_cost_usd": 0.0431,
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 200,
                  "cache_read_input_tokens": 1000, "output_tokens": 55},
        "modelUsage": {"claude-small": {"outputTokens": 3}, "claude-big": {"outputTokens": 52}},
        "structured_output": {"gap": "G2"},
    }
    base.update(kw)
    return json.dumps({k: v for k, v in base.items() if v is not ...})


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fr = FakeRun(stdout=_payload())
    monkeypatch.setattr(claude_headless.subprocess, "run", fr)
    monkeypatch.setattr(claude_headless.shutil, "which", lambda name: "/opt/fake/claude")
    return fr


def test_claude_is_called_with_an_argument_list_and_prompt_on_stdin(fake_run: FakeRun) -> None:
    claude_headless.run(PROMPT, SCHEMA, "opus")
    args, kw = fake_run.args, fake_run.kwargs
    assert isinstance(args, list) and all(isinstance(a, str) for a in args)
    assert args[:6] == ["/opt/fake/claude", "-p", "--output-format", "json", "--json-schema",
                        claude_headless.schema_text(SCHEMA)]
    assert json.loads(args[5]) == SCHEMA
    assert args[6:] == ["--model", "opus"]
    assert "--bare" not in args and PROMPT not in args
    assert kw["input"] == PROMPT
    assert kw["encoding"] == "utf-8" and kw["text"] is True and kw["capture_output"] is True
    assert kw["timeout"] > 0
    assert not kw.get("shell")


def test_claude_without_a_model_passes_no_model_flag(fake_run: FakeRun) -> None:
    claude_headless.run(PROMPT, SCHEMA, None)
    assert "--model" not in fake_run.args


def test_claude_reads_structured_output_tokens_cost_and_model(fake_run: FakeRun) -> None:
    r = claude_headless.run(PROMPT, SCHEMA, "opus")
    assert r.output == {"gap": "G2"} and r.problem is None
    assert (r.input_tokens, r.output_tokens) == (1210, 55)
    assert r.cost_usd == pytest.approx(0.0431)
    assert r.model == "claude-big"


@pytest.mark.parametrize("value", [..., None])
def test_missing_structured_output_is_no_valid_result(fake_run: FakeRun, value: Any) -> None:
    fake_run.stdout = _payload(structured_output=value, result="Here is my answer in prose.")
    r = claude_headless.run(PROMPT, SCHEMA, "opus")
    assert r.output is None and "structured_output" in (r.problem or "")
    assert r.cost_usd == pytest.approx(0.0431)  # the attempt still cost something


def test_structured_output_retries_exhausted_is_no_valid_result(fake_run: FakeRun) -> None:
    fake_run.stdout = _payload(is_error=True, subtype="error_max_structured_output_retries",
                               structured_output=...)
    fake_run.returncode = 1
    r = claude_headless.run(PROMPT, SCHEMA, None)
    assert r.output is None


@pytest.mark.parametrize("stdout,stderr", [
    (_payload(is_error=True, subtype="success", result="Claude AI usage limit reached|1767225600",
              structured_output=...), ""),
    ("", "Error: 429 rate limit exceeded"),
])
def test_claude_usage_limit_is_a_quota_error(fake_run: FakeRun, stdout: str, stderr: str) -> None:
    fake_run.stdout, fake_run.stderr, fake_run.returncode = stdout, stderr, 1
    with pytest.raises(QuotaError):
        claude_headless.run(PROMPT, SCHEMA, None)


def test_claude_other_errors_are_provider_errors(fake_run: FakeRun) -> None:
    fake_run.stdout, fake_run.returncode = "not json", 2
    with pytest.raises(ProviderError):
        claude_headless.run(PROMPT, SCHEMA, None)
    fake_run.stdout = _payload(is_error=True, subtype="error_during_execution", result="boom",
                               structured_output=...)
    with pytest.raises(ProviderError):
        claude_headless.run(PROMPT, SCHEMA, None)


def test_claude_timeout_is_a_provider_error(fake_run: FakeRun) -> None:
    fake_run.exc = subprocess.TimeoutExpired(["claude"], 1)
    with pytest.raises(ProviderError, match="timed out"):
        claude_headless.run(PROMPT, SCHEMA, None, timeout=1)


def test_claude_not_on_path_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_headless.shutil, "which", lambda name: None)
    with pytest.raises(NotConfigured):
        claude_headless.run(PROMPT, SCHEMA, None)


def test_cmd_shim_refuses_characters_cmd_would_interpret() -> None:
    with pytest.raises(ProviderError, match="cmd.exe"):
        claude_headless.build_args(r"C:\npm\claude.cmd", {"pattern": "a|b"})
    assert claude_headless.build_args("/usr/bin/claude", {"pattern": "a|b"})[0] == "/usr/bin/claude"


# --- claude_headless against a fake executable (real subprocess) ------------------

FAKE_CLAUDE = r'''
import json, sys
args = sys.argv[1:]
prompt = sys.stdin.buffer.read().decode("utf-8")
schema = json.loads(args[args.index("--json-schema") + 1])
out = {"type": "result", "subtype": "success", "is_error": False, "result": "",
       "total_cost_usd": 0.01, "usage": {"input_tokens": 5, "output_tokens": 7},
       "structured_output": {"gap": "G1", "argv": args, "schema": schema, "prompt": prompt}}
sys.stdout.buffer.write(json.dumps(out).encode("utf-8"))
'''


def _fake_executable(tmp_path: Path) -> str:
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    if os.name == "nt":  # mirrors npm's claude.cmd shim
        exe = tmp_path / "claude.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "claude"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    return str(exe)


def test_fake_claude_executable_round_trip(tmp_path: Path) -> None:
    r = claude_headless.run(PROMPT, SCHEMA, "opus", executable=_fake_executable(tmp_path), timeout=60)
    assert r.output is not None
    assert r.output["schema"] == SCHEMA  # quotes and spaces survive the argument list
    assert r.output["prompt"] == PROMPT  # prompt arrives on stdin, utf-8 intact
    assert r.output["argv"][:3] == ["-p", "--output-format", "json"]
    assert r.output["argv"][-2:] == ["--model", "opus"]
    assert (r.input_tokens, r.output_tokens, r.cost_usd) == (5, 7, 0.01)


# --- HTTP-API providers through guard.model_api ------------------------------------

def _mock(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(model_api, "transport", httpx.MockTransport(wrapped))
    return seen


def test_groq_parses_output_and_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    seen = _mock(monkeypatch, lambda req: httpx.Response(200, json={
        "model": "llama-3.1-8b-instant",
        "choices": [{"message": {"content": '{"gap": "G3"}'}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 6},
    }))
    r = api_providers.groq(PROMPT, SCHEMA, "llama-3.1-8b-instant")
    assert r.output == {"gap": "G3"} and (r.input_tokens, r.output_tokens) == (40, 6)
    (req,) = seen
    assert req.url == api_providers.GROQ_URL
    assert req.headers["authorization"] == "Bearer test-key-not-real"
    body = json.loads(req.content)
    assert body["model"] == "llama-3.1-8b-instant" and PROMPT in body["messages"][0]["content"]


def test_groq_429_is_a_quota_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    _mock(monkeypatch, lambda req: httpx.Response(429, json={"error": "slow down"}))
    with pytest.raises(QuotaError):
        api_providers.groq(PROMPT, SCHEMA, "m")


def test_quota_body_on_4xx_is_a_quota_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-not-real")
    _mock(monkeypatch, lambda req: httpx.Response(403, text="Quota exceeded for this project"))
    with pytest.raises(QuotaError):
        api_providers.gemini(PROMPT, SCHEMA, "gemini-2.5-pro")


def test_server_error_is_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    _mock(monkeypatch, lambda req: httpx.Response(500, text="oops"))
    with pytest.raises(ProviderError, match="HTTP 500"):
        api_providers.groq(PROMPT, SCHEMA, "m")


def test_gemini_parses_output_and_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-not-real")
    seen = _mock(monkeypatch, lambda req: httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": '```json\n{"gap": "G4"}\n```'}]}}],
        "usageMetadata": {"promptTokenCount": 90, "candidatesTokenCount": 9},
        "modelVersion": "gemini-2.5-pro",
    }))
    r = api_providers.gemini(PROMPT, SCHEMA, "gemini-2.5-pro")
    assert r.output == {"gap": "G4"} and (r.input_tokens, r.output_tokens) == (90, 9)
    assert seen[0].headers["x-goog-api-key"] == "test-key-not-real"
    assert "gemini-2.5-pro:generateContent" in str(seen[0].url)


def test_unparseable_text_is_no_valid_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    _mock(monkeypatch, lambda req: httpx.Response(200, json={
        "choices": [{"message": {"content": "Sure! The gap is G3."}}]}))
    r = api_providers.groq(PROMPT, SCHEMA, "m")
    assert r.output is None and r.problem


def test_cloudflare_parses_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token-not-real")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct123")
    seen = _mock(monkeypatch, lambda req: httpx.Response(200, json={
        "success": True, "result": {"response": '{"gap": "G5"}',
                                    "usage": {"prompt_tokens": 3, "completion_tokens": 4}}}))
    r = api_providers.cloudflare(PROMPT, SCHEMA, "@cf/meta/llama-3.1-8b-instruct")
    assert r.output == {"gap": "G5"} and (r.input_tokens, r.output_tokens) == (3, 4)
    assert "/accounts/acct123/ai/run/@cf/meta/llama-3.1-8b-instruct" in str(seen[0].url)


@pytest.mark.parametrize("fn,env", [
    (api_providers.groq, ["GROQ_API_KEY"]),
    (api_providers.gemini, ["GOOGLE_API_KEY"]),
    (api_providers.cloudflare, ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"]),
])
def test_missing_key_is_not_configured_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, fn: Any, env: list[str]
) -> None:
    for name in env:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(NotConfigured, match=env[0]):  # the autouse transport refuses any request
        fn(PROMPT, SCHEMA, "m")


def test_model_api_paces_calls_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_api, "_last_call", {})
    now = [100.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    clock = lambda: now[0]
    _REAL_PACE("groq", clock, sleep)
    now[0] += 0.25
    _REAL_PACE("groq", clock, sleep)
    _REAL_PACE("gemini", clock, sleep)
    assert slept == [pytest.approx(0.75)]
