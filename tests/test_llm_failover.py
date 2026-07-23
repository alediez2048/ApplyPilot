"""Tests for multi-provider LLM round-robin + failover."""

from __future__ import annotations

import httpx
import pytest

from applypilot import llm


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
              "CLAUDE_API_KEY", "LLM_URL", "LLM_PROVIDER_ORDER",
              "OPENAI_MODEL", "GEMINI_MODEL", "ANTHROPIC_MODEL", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)


def _names(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return [n for (n, _b, _m, _k) in llm._detect_providers()]


def test_detect_single_provider(monkeypatch):
    assert _names(monkeypatch, {"OPENAI_API_KEY": "x"}) == ["openai"]


def test_detect_all_three_default_order(monkeypatch):
    # Default order is anthropic-first (the reliable primary), then openai, then gemini.
    assert _names(monkeypatch, {
        "OPENAI_API_KEY": "x", "GEMINI_API_KEY": "y", "ANTHROPIC_API_KEY": "z",
    }) == ["anthropic", "openai", "gemini"]


def test_detect_custom_order(monkeypatch):
    assert _names(monkeypatch, {
        "OPENAI_API_KEY": "x", "GEMINI_API_KEY": "y", "ANTHROPIC_API_KEY": "z",
        "LLM_PROVIDER_ORDER": "gemini,anthropic,openai",
    }) == ["gemini", "anthropic", "openai"]


def test_claude_api_key_alias(monkeypatch):
    assert _names(monkeypatch, {"CLAUDE_API_KEY": "z"}) == ["anthropic"]


def test_no_provider_raises(monkeypatch):
    with pytest.raises(RuntimeError):
        llm._detect_providers()


class _Stub:
    def __init__(self, name, mode):
        self.name, self.model, self.mode, self.calls = name, "m", mode, 0

    def attempt(self, messages, temperature, max_tokens):
        self.calls += 1
        if self.mode == "429":
            raise httpx.HTTPStatusError("rl", request=None, response=httpx.Response(429))
        return f"OK:{self.name}"


def test_round_robin_rotates_across_providers():
    a, b, c = _Stub("a", "ok"), _Stub("b", "ok"), _Stub("c", "ok")
    fc = llm.FailoverClient([a, b, c])
    out = [fc.chat([{"role": "user", "content": "hi"}]) for _ in range(3)]
    assert out == ["OK:a", "OK:b", "OK:c"]
    assert (a.calls, b.calls, c.calls) == (1, 1, 1)


def test_failover_on_rate_limit():
    bad, good = _Stub("bad", "429"), _Stub("good", "ok")
    fc = llm.FailoverClient([bad, good])
    assert fc.chat([{"role": "user", "content": "hi"}]) == "OK:good"
    assert bad.calls == 1 and good.calls == 1


def test_all_providers_fail_raises(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda *_a, **_k: None)  # no real backoff waits
    a, b = _Stub("a", "429"), _Stub("b", "429")
    fc = llm.FailoverClient([a, b])
    with pytest.raises(RuntimeError):
        fc.chat([{"role": "user", "content": "hi"}])
    assert a.calls == 5 and b.calls == 5  # n * _MAX_RETRIES attempts total
