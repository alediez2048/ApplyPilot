"""
Unified LLM client for ApplyPilot — multi-provider with round-robin + failover.

Configure any combination of providers via environment variables. When more than
one is set, requests are **round-robined** across them (to spread load and dodge
per-key rate limits) and **fail over** to the next provider on 429/5xx/timeout.

  OPENAI_API_KEY      -> OpenAI            (default model: gpt-4o-mini)
  GEMINI_API_KEY      -> Google Gemini     (default model: gemini-2.0-flash)
  ANTHROPIC_API_KEY   -> Anthropic Claude  (default model: claude-3-5-haiku-latest)
  LLM_URL             -> local OpenAI-compatible endpoint (llama.cpp / Ollama)

Per-provider model overrides: OPENAI_MODEL, GEMINI_MODEL, ANTHROPIC_MODEL, LLM_MODEL.
Failover/rotation order: LLM_PROVIDER_ORDER (comma list, e.g. "gemini,openai,anthropic").
"""

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5          # per provider, across the round-robin
_TIMEOUT = 120            # seconds
_RATE_LIMIT_BASE_WAIT = 10  # base backoff once ALL providers are rate-limited

_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"

_DEFAULT_ORDER = ["anthropic", "openai", "gemini", "local"]

# ---------------------------------------------------------------------------
# Model tiers — route task DIFFICULTY to model capability, not one model for all.
#   "light" : quick generation / edits (outreach draft, message edit, cover letter)
#             -> cheap, fast models. Don't burn a big model to reword a sentence.
#   "heavy" : structured reasoning where quality matters (résumé tailoring, fit
#             scoring, JD extraction, fabrication judge) -> stronger models.
# Per-provider defaults are the model IDs verified working against each API. Override any of
# them via env (OPENAI_MODEL_HEAVY, ANTHROPIC_MODEL_LIGHT, …); a legacy OPENAI_MODEL /
# ANTHROPIC_MODEL / GEMINI_MODEL still applies to BOTH tiers for back-compat.
# ---------------------------------------------------------------------------
_TIERS = ("light", "heavy")
_TIER_DEFAULTS = {
    "openai":    {"light": "gpt-4o-mini",      "heavy": "gpt-4o"},
    "anthropic": {"light": "claude-haiku-4-5", "heavy": "claude-sonnet-4-5"},
    "gemini":    {"light": "gemini-2.0-flash", "heavy": "gemini-2.0-flash"},
    "local":     {"light": "local-model",      "heavy": "local-model"},
}


def _model_for(provider: str, tier: str) -> str:
    """Resolve the model id for a provider at a difficulty tier (env override → legacy → default)."""
    tier = tier if tier in _TIERS else "heavy"
    up = provider.upper()
    per_tier = os.environ.get(f"{up}_MODEL_{tier.upper()}")
    legacy = os.environ.get(f"{up}_MODEL")  # e.g. OPENAI_MODEL / ANTHROPIC_MODEL / GEMINI_MODEL
    if provider == "local":
        legacy = legacy or os.environ.get("LLM_MODEL")
    return per_tier or legacy or _TIER_DEFAULTS.get(provider, {}).get(tier, "")


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_providers(tier: str = "heavy") -> list[tuple[str, str, str, str]]:
    """Return a list of (name, base_url, model, api_key) for every configured provider, in
    failover/round-robin order, using the model appropriate for the given difficulty `tier`.
    Reads env at call time.
    """
    builders = {
        "openai": lambda: (
            "openai", "https://api.openai.com/v1",
            _model_for("openai", tier),
            os.environ.get("OPENAI_API_KEY", ""),
        ),
        "gemini": lambda: (
            "gemini", _GEMINI_COMPAT_BASE,
            _model_for("gemini", tier),
            os.environ.get("GEMINI_API_KEY", ""),
        ),
        "anthropic": lambda: (
            "anthropic", "https://api.anthropic.com/v1",
            _model_for("anthropic", tier),
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY", ""),
        ),
        "local": lambda: (
            "local", os.environ.get("LLM_URL", "").rstrip("/"),
            _model_for("local", tier),
            os.environ.get("LLM_API_KEY", ""),
        ),
    }

    order_env = os.environ.get("LLM_PROVIDER_ORDER", "")
    order = [x.strip().lower() for x in order_env.split(",") if x.strip()] or _DEFAULT_ORDER

    specs: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for name in order:
        if name in seen or name not in builders:
            continue
        seen.add(name)
        n, base, model, key = builders[name]()
        if name == "local":
            if base:
                specs.append((n, base, model, key))
        elif key:
            specs.append((n, base, model, key))

    if not specs:
        raise RuntimeError(
            "No LLM provider configured. Set one of OPENAI_API_KEY, GEMINI_API_KEY, "
            "ANTHROPIC_API_KEY, or LLM_URL in your environment."
        )
    return specs


# ---------------------------------------------------------------------------
# Single-provider client (one attempt per call; retries live in FailoverClient)
# ---------------------------------------------------------------------------

class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


class LLMClient:
    """One provider. `attempt()` makes a single request and raises on failure."""

    def __init__(self, name: str, base_url: str, model: str, api_key: str) -> None:
        self.name = name
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        self._use_native_gemini = False
        self._is_gemini = base_url.startswith(_GEMINI_COMPAT_BASE)

    # -- native Gemini (for preview models not on the compat layer) ----------

    def _chat_native_gemini(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        contents: list[dict] = []
        system_parts: list[dict] = []
        for msg in messages:
            role, text = msg["role"], msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": text}]})
            else:
                contents.append({"role": "user", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        resp = self._client.post(
            f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent",
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    # -- OpenAI-compatible (OpenAI, Gemini-compat, Anthropic-compat, local) --

    def _chat_compat(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            headers=headers,
        )
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    # -- public: one attempt -------------------------------------------------

    def attempt(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        """Single request. Raises httpx errors (429/5xx/timeout) for the caller
        to handle; transparently switches Gemini compat->native on a 403."""
        if self._use_native_gemini:
            return self._chat_native_gemini(messages, temperature, max_tokens)
        try:
            return self._chat_compat(messages, temperature, max_tokens)
        except _GeminiCompatForbidden:
            log.warning("Gemini compat 403 for model '%s'; switching to native API.", self.model)
            self._use_native_gemini = True
            return self._chat_native_gemini(messages, temperature, max_tokens)

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# Failover client: round-robin across providers, retry with backoff
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS = {429, 500, 502, 503, 529}


def _no_think(client: LLMClient, messages: list[dict]) -> list[dict]:
    """Qwen optimization: prepend /no_think to skip chain-of-thought."""
    if "qwen" in client.model.lower() and messages and messages[0].get("role") == "user" \
            and not messages[0]["content"].startswith("/no_think"):
        return [{"role": "user", "content": f"/no_think\n{messages[0]['content']}"}] + messages[1:]
    return messages


class FailoverClient:
    """Presents the LLMClient API but spreads calls across providers and fails over."""

    def __init__(self, clients: list[LLMClient]) -> None:
        self.clients = clients
        self._rr = 0

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 4096) -> str:
        n = len(self.clients)
        max_attempts = n * _MAX_RETRIES
        last_exc: Exception | None = None
        fail_streak = 0

        for _ in range(max_attempts):
            client = self.clients[self._rr % n]
            self._rr += 1
            try:
                return client.attempt(_no_think(client, messages), temperature, max_tokens)
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.RequestError, RuntimeError) as e:
                last_exc = e
                code = getattr(getattr(e, "response", None), "status_code", None)
                retryable = code in _RETRYABLE_STATUS or code is None  # None => timeout/network
                label = code or type(e).__name__
                if n > 1:
                    log.warning("LLM provider '%s' failed (%s) — failing over to next.", client.name, label)
                else:
                    log.warning("LLM provider '%s' failed (%s).", client.name, label)

                fail_streak += 1
                # Once we've cycled through every provider without success, back off.
                if retryable and fail_streak % n == 0:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** ((fail_streak // n) - 1)), 60)
                    log.warning("All %d provider(s) unavailable; waiting %ds before retrying.", n, wait)
                    time.sleep(wait)
                continue

        raise RuntimeError(f"All LLM providers failed after {max_attempts} attempts") from last_exc

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        for c in self.clients:
            c.close()


# ---------------------------------------------------------------------------
# Singletons — one cached FailoverClient per difficulty tier.
# ---------------------------------------------------------------------------

_instances: dict[str, FailoverClient] = {}


def get_client(tier: str = "heavy") -> FailoverClient:
    """Return (or create) the failover client for a difficulty tier.

    tier="light" for quick generation/edits (outreach, message edits, cover letter);
    tier="heavy" (default) for structured reasoning (résumé tailoring, scoring, extraction).
    Each tier round-robins + fails over across the SAME configured providers, just with the
    tier-appropriate model per provider. Defaults to "heavy" so any un-migrated caller stays safe.
    """
    tier = tier if tier in _TIERS else "heavy"
    if tier not in _instances:
        specs = _detect_providers(tier)
        clients = [LLMClient(name, base, model, key) for (name, base, model, key) in specs]
        log.info(
            "LLM providers [%s tier] (round-robin + failover): %s",
            tier, ", ".join(f"{c.name}:{c.model}" for c in clients),
        )
        _instances[tier] = FailoverClient(clients)
    return _instances[tier]


def reset_client() -> None:
    """Drop all cached clients (e.g. after changing env vars)."""
    global _instances
    for c in _instances.values():
        c.close()
    _instances = {}
