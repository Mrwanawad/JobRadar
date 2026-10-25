"""Free-tier LLM client: ordered OpenAI-compatible providers with throttling and fallback.

Every provider here speaks the OpenAI `/chat/completions` shape, so one client covers all.
Order comes from LLM_PROVIDERS (default "gemini,openrouter"); a provider with no API key is
skipped. Per-provider RPM/TPM are enforced client-side so we stay under free quotas instead
of burning requests on 429s. When a provider hits its daily cap, it is parked for the rest of
the run and the next one takes over.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from dotenv import load_dotenv

try:  # platform extra; lite mode runs without OpenTelemetry
    from opentelemetry import trace

    _tracer: Any = trace.get_tracer("jobradar.llm")
except ImportError:  # pragma: no cover
    _tracer = None

# Prompts and replies carry CV text, so span content is opt-in (OTEL_CAPTURE_LLM_CONTENT=true),
# and the agent worker switches it off for demo visitors whatever the setting.
CAPTURE_CONTENT: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "capture_llm_content", default=os.getenv("OTEL_CAPTURE_LLM_CONTENT", "").lower() == "true"
)

# name -> defaults. Limits are free-tier values checked 2026-10-15; override via env.
PRESETS: dict[str, dict[str, Any]] = {
    "gemini": {
        # Free limits are per project, shown in AI Studio (aistudio.google.com/rate-limit).
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.8-flash",
        "rpm": 10,
        "tpm": 0,
        "extra": {"reasoning_effort": "medium"},
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "rpm": 30,
        "tpm": 8_000,
        "extra": {"reasoning_effort": "medium", "include_reasoning": False},
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "rpm": 20,
        "tpm": 0,
        "extra": {"reasoning": {"effort": "low", "exclude": True}},
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-medium-latest",
        "rpm": 2,
        "tpm": 0,
        "extra": {},
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "model": "gpt-oss-120b",
        "rpm": 5,
        "tpm": 30_000,
        "extra": {"reasoning_effort": "medium"},
    },
}

# Waits longer than this mean "quota gone for now": switch provider instead of sleeping.
MAX_WAIT_S = 65.0
# A daily-quota 429 parks that key this long before it is tried again.
DAILY_PARK_S = 3600.0

log = logging.getLogger(__name__)


def _is_auth_error(status: int, body: str) -> bool:
    # Gemini answers a bad key with 400 API_KEY_INVALID, others with 401/403.
    return status in (401, 403) or (status == 400 and "api key" in body.lower().replace("_", " "))


class LLMError(RuntimeError):
    """No provider could answer."""


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    rpm: int = 0  # 0 = unlimited
    tpm: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    # runtime state
    calls: deque[float] = field(default_factory=deque)
    tokens: deque[tuple[float, int]] = field(default_factory=deque)
    parked_until: float = 0.0


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    usage: dict[str, Any]


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


def _keys_for(prefix: str) -> list[str]:
    """NAME_API_KEY_1..NAME_API_KEY_9 in order, deduped. Plain NAME_API_KEY is used only
    when no numbered key exists, so a stale OS-level variable can't sneak into the chain."""
    keys: list[str] = []
    for i in range(1, 10):
        k = os.getenv(f"{prefix}_API_KEY_{i}", "").strip()
        if k and k not in keys:
            keys.append(k)
    if not keys and (k := os.getenv(f"{prefix}_API_KEY", "").strip()):
        keys.append(k)
    return keys


def providers_from_env() -> list[Provider]:
    """One Provider per API key, so a second key of the same service is its own fallback."""
    load_dotenv(override=True)  # .env wins over OS-level variables
    order = os.getenv("LLM_PROVIDERS", "gemini,openrouter")
    out: list[Provider] = []
    for name in (n.strip().lower() for n in order.split(",") if n.strip()):
        preset = PRESETS.get(name)
        if preset is None:
            raise ValueError(f"unknown LLM provider {name!r}; known: {', '.join(PRESETS)}")
        p = name.upper()
        keys = _keys_for(p)
        for i, key in enumerate(keys, 1):
            out.append(
                Provider(
                    name=f"{name}#{i}" if len(keys) > 1 else name,
                    base_url=os.getenv(f"{p}_BASE_URL", preset["base_url"]),
                    api_key=key,
                    model=os.getenv(f"{p}_MODEL", preset["model"]),
                    rpm=int(os.getenv(f"{p}_RPM", preset["rpm"])),
                    tpm=int(os.getenv(f"{p}_TPM", preset["tpm"])),
                    extra=dict(preset["extra"]),
                )
            )
    return out


def parse_json(text: str) -> Any:
    """Pull JSON out of a reply that may have fences, chatter or visible reasoning around it.
    Prefers the last fenced block, then the last (outermost) JSON value that parses."""
    s = text.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    fences = re.findall(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    for block in reversed(fences):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue
    dec = json.JSONDecoder()
    found: Any = None
    end_of_found = -1
    for i, ch in enumerate(s):
        if ch not in "{[" or i < end_of_found:
            continue
        try:
            val, end = dec.raw_decode(s, i)
        except json.JSONDecodeError:
            continue
        if isinstance(val, (dict, list)) and val:
            found, end_of_found = val, end
    if found is None:
        raise ValueError("no JSON in reply")
    return found


class LLMClient:
    def __init__(
        self,
        providers: list[Provider] | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.providers = providers if providers is not None else providers_from_env()
        if not self.providers:
            raise LLMError(
                "no LLM provider configured: set GEMINI_API_KEY (or another *_API_KEY) in .env"
            )
        self.http = http or httpx.Client(timeout=120)
        self.sleep = sleep
        self.clock = clock

    # --- throttling -------------------------------------------------------------------------

    def _wait_needed(self, p: Provider, est: int) -> float:
        now = self.clock()
        while p.calls and now - p.calls[0] >= 60:
            p.calls.popleft()
        while p.tokens and now - p.tokens[0][0] >= 60:
            p.tokens.popleft()
        wait = 0.0
        if p.rpm and len(p.calls) >= p.rpm:
            wait = max(wait, 60 - (now - p.calls[0]))
        if p.tpm:
            used = sum(n for _, n in p.tokens)
            if used + est > p.tpm and p.tokens:
                # wait until enough old usage expires
                need = used + est - p.tpm
                freed = 0
                for ts, n in p.tokens:
                    freed += n
                    if freed >= need:
                        wait = max(wait, 60 - (now - ts))
                        break
        return wait

    def _record(self, p: Provider, tokens: int) -> None:
        now = self.clock()
        p.calls.append(now)
        p.tokens.append((now, tokens))

    # --- requests ---------------------------------------------------------------------------

    def _post(self, p: Provider, body: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {p.api_key}"}
        if p.name.startswith("openrouter"):
            headers["X-Title"] = "jobradar"
        return self.http.post(f"{p.base_url}/chat/completions", json=body, headers=headers)

    @staticmethod
    def _retry_after(r: httpx.Response) -> float:
        for h in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
            v = r.headers.get(h)
            if not v:
                continue
            try:
                return float(v)
            except ValueError:
                pass
            # Groq style: "2m59.56s", "7.66s", "120ms"
            total = 0.0
            for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", v):
                total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
            if total:
                return total
        m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', r.text)
        if m:
            return float(m.group(1))
        return 30.0

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 1500,
        temperature: float | None = None,
    ) -> LLMResult:
        """Try providers in order. Auth failure or rate limit on one moves to the next;
        only the last available provider waits out a short rate limit.

        Traced as one OpenTelemetry GenAI span (Langfuse shows it as a generation)."""
        if _tracer is None:
            return self._chat(messages, max_tokens=max_tokens, temperature=temperature)
        with _tracer.start_as_current_span("chat") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("langfuse.observation.type", "generation")
            span.set_attribute("gen_ai.request.max_tokens", max_tokens)
            if CAPTURE_CONTENT.get():
                span.set_attribute("langfuse.observation.input", json.dumps(messages))
            res = self._chat(messages, max_tokens=max_tokens, temperature=temperature)
            span.update_name(f"chat {res.model}")
            span.set_attribute("gen_ai.system", res.provider.split("#")[0])
            span.set_attribute("gen_ai.request.model", res.model)
            span.set_attribute("gen_ai.response.model", res.model)
            with contextlib.suppress(KeyError, TypeError, ValueError):
                span.set_attribute("gen_ai.usage.input_tokens", int(res.usage["prompt_tokens"]))
                span.set_attribute(
                    "gen_ai.usage.output_tokens", int(res.usage["completion_tokens"])
                )
            if CAPTURE_CONTENT.get():
                span.set_attribute("langfuse.observation.output", res.text)
            return res

    def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float | None,
    ) -> LLMResult:
        prompt_text = "".join(m["content"] for m in messages)
        est = estimate_tokens(prompt_text) + max_tokens
        errors: list[str] = []

        for idx, p in enumerate(self.providers):
            if p.parked_until > self.clock():
                errors.append(f"{p.name}: parked")
                continue
            has_next = any(q.parked_until <= self.clock() for q in self.providers[idx + 1 :])
            body: dict[str, Any] = {
                "model": p.model,
                "messages": messages,
                "max_tokens": max_tokens,
                **p.extra,
            }
            if temperature is not None:
                body["temperature"] = temperature

            for attempt in range(3):
                wait = self._wait_needed(p, est)
                if wait > 0 and (has_next or wait > MAX_WAIT_S):
                    errors.append(f"{p.name}: local limit, {wait:.0f}s to free")
                    break
                if wait > 0:
                    self.sleep(wait)
                try:
                    r = self._post(p, body)
                except httpx.HTTPError as e:
                    errors.append(f"{p.name}: {type(e).__name__}")
                    self.sleep(2**attempt)
                    continue

                if r.status_code == 200:
                    data = r.json()
                    usage = data.get("usage") or {}
                    self._record(p, usage.get("total_tokens") or est)
                    text = (data["choices"][0]["message"].get("content") or "").strip()
                    if not text:
                        errors.append(f"{p.name}: empty reply")
                        break
                    if errors:
                        log.info("LLM answered by %s after: %s", p.name, "; ".join(errors))
                    return LLMResult(text, p.name, data.get("model", p.model), usage)

                self._record(p, 0)
                body_text = r.text[:300]
                if r.status_code == 429:
                    ra = self._retry_after(r)
                    if "perday" in body_text.lower().replace(" ", "").replace("_", ""):
                        ra = max(ra, DAILY_PARK_S)
                    if has_next or ra > MAX_WAIT_S:
                        p.parked_until = self.clock() + ra
                        errors.append(f"{p.name}: 429, parked {ra:.0f}s")
                        break
                    self.sleep(ra)
                    continue
                if r.status_code >= 500:
                    errors.append(f"{p.name}: {r.status_code}")
                    self.sleep(2**attempt)
                    continue
                if _is_auth_error(r.status_code, body_text):
                    # bad/revoked key: won't fix itself during this run
                    p.parked_until = float("inf")
                    errors.append(f"{p.name}: {r.status_code} auth, disabled for this run")
                    break
                errors.append(f"{p.name}: {r.status_code} {body_text}")
                break

        raise LLMError("all LLM providers failed: " + "; ".join(errors))

    def chat_json(
        self, messages: list[dict[str, str]], *, max_tokens: int = 1500
    ) -> tuple[Any, LLMResult]:
        """Ask for JSON; on unparseable reply, retry once with the error fed back."""
        res = self.chat(messages, max_tokens=max_tokens, temperature=0)
        try:
            return parse_json(res.text), res
        except (ValueError, json.JSONDecodeError) as e:
            retry = f"That was not valid JSON ({e}). Reply with only the JSON."
            fix = messages + [
                {"role": "assistant", "content": res.text},
                {"role": "user", "content": retry},
            ]
            res = self.chat(fix, max_tokens=max_tokens, temperature=0)
            return parse_json(res.text), res
