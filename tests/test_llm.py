import httpx
import pytest
import respx

from jobradar.llm import LLMClient, LLMError, Provider, parse_json, providers_from_env

GEM_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
GEM = f"{GEM_BASE}/chat/completions"
OR = "https://openrouter.ai/api/v1/chat/completions"
MSG = [{"role": "user", "content": "x"}]


def ok(text: str, tokens: int = 100) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "m",
            "choices": [{"message": {"content": text}}],
            "usage": {"total_tokens": tokens},
        },
    )


def key1():
    return respx.post(GEM, headers={"Authorization": "Bearer g1"})


def key2():
    return respx.post(GEM, headers={"Authorization": "Bearer g2"})


class FakeTime:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def make(ft: FakeTime, rpm: int = 0, with_or: bool = True) -> LLMClient:
    providers = [
        Provider("gemini#1", GEM_BASE, "g1", "gemini-3.8-flash", rpm=rpm),
        Provider("gemini#2", GEM_BASE, "g2", "gemini-3.8-flash", rpm=rpm),
    ]
    if with_or:
        providers.append(
            Provider("openrouter", "https://openrouter.ai/api/v1", "o", "openai/gpt-oss-120b:free")
        )
    return LLMClient(providers, http=httpx.Client(), sleep=ft.sleep, clock=ft.clock)


@respx.mock
def test_key1_answers():
    key1().mock(return_value=ok("hi"))
    res = make(FakeTime()).chat(MSG)
    assert (res.text, res.provider) == ("hi", "gemini#1")


@respx.mock
def test_key1_rate_limited_goes_to_key2_without_waiting():
    k1 = key1().mock(return_value=httpx.Response(429, headers={"retry-after": "10"}))
    key2().mock(return_value=ok("k2"))
    ft = FakeTime()
    c = make(ft)
    assert c.chat(MSG).provider == "gemini#2"
    assert ft.slept == []
    assert c.chat(MSG).provider == "gemini#2"  # key1 parked
    assert k1.call_count == 1
    ft.t += 11
    k1.mock(return_value=ok("back"))
    assert c.chat(MSG).provider == "gemini#1"  # park expired


@respx.mock
def test_key1_bad_key_goes_to_key2_and_stays_disabled():
    k1 = key1().mock(
        return_value=httpx.Response(400, json=[{"error": {"message": "API key not valid"}}])
    )
    key2().mock(return_value=ok("k2"))
    ft = FakeTime()
    c = make(ft)
    assert c.chat(MSG).provider == "gemini#2"
    ft.t += 100_000
    assert c.chat(MSG).provider == "gemini#2"
    assert k1.call_count == 1


@respx.mock
def test_both_gemini_keys_exhausted_falls_to_openrouter():
    key1().mock(return_value=httpx.Response(401))
    key2().mock(
        return_value=httpx.Response(
            429, text='{"error": {"message": "Quota exceeded GenerateRequestsPerDay"}}'
        )
    )
    respx.post(OR).mock(return_value=ok("or"))
    c = make(FakeTime())
    assert c.chat(MSG).provider == "openrouter"


@respx.mock
def test_gemini_retry_delay_in_body_is_used():
    key1().mock(
        return_value=httpx.Response(429, text='{"error": {"details": [{"retryDelay": "37s"}]}}')
    )
    key2().mock(return_value=ok("k2"))
    ft = FakeTime()
    c = make(ft)
    c.chat(MSG)
    assert c.providers[0].parked_until == pytest.approx(ft.t + 37)


@respx.mock
def test_daily_quota_parks_at_least_an_hour():
    key1().mock(
        return_value=httpx.Response(
            429,
            text='{"error": {"message": "quota_id GenerateRequestsPerDay", "retryDelay": "5s"}}',
        )
    )
    key2().mock(return_value=ok("k2"))
    ft = FakeTime()
    c = make(ft)
    c.chat(MSG)
    assert c.providers[0].parked_until >= ft.t + 3600


@respx.mock
def test_local_rpm_cap_rotates_to_next_key():
    key1().mock(return_value=ok("k1"))
    key2().mock(return_value=ok("k2"))
    ft = FakeTime()
    c = make(ft, rpm=1)
    assert c.chat(MSG).provider == "gemini#1"
    assert c.chat(MSG).provider == "gemini#2"
    assert ft.slept == []


@respx.mock
def test_last_provider_waits_out_short_429():
    respx.post(OR).mock(
        side_effect=[httpx.Response(429, headers={"retry-after": "7.5s"}), ok("ok")]
    )
    c = LLMClient(
        [Provider("openrouter", "https://openrouter.ai/api/v1", "o", "m")],
        http=httpx.Client(),
        sleep=(ft := FakeTime()).sleep,
        clock=ft.clock,
    )
    assert c.chat(MSG).provider == "openrouter"
    assert 7.5 in ft.slept


@respx.mock
def test_all_fail_raises():
    key1().mock(return_value=httpx.Response(401))
    key2().mock(return_value=httpx.Response(403))
    respx.post(OR).mock(return_value=httpx.Response(429, headers={"retry-after": "3600"}))
    with pytest.raises(LLMError, match="gemini#1: 401.*gemini#2: 403.*openrouter: 429"):
        make(FakeTime()).chat(MSG)


@respx.mock
def test_chat_json_repairs_once():
    key1().mock(side_effect=[ok("sure! score is high"), ok('{"score": 8}')])
    data, _ = make(FakeTime()).chat_json(MSG)
    assert data == {"score": 8}


def test_parse_json_variants():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here: {"a": [1, 2]} done') == {"a": [1, 2]}
    assert parse_json("[1]") == [1]
    # visible reasoning with braces before the real answer
    reasoning = 'I think {score} matters. {"score": 8, "r": ["a"]}'
    assert parse_json(reasoning) == {"score": 8, "r": ["a"]}
    two_fences = 'draft ```json\n{"a": 1}\n``` final ```json\n{"a": 2}\n```'
    assert parse_json(two_fences) == {"a": 2}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr("jobradar.llm.load_dotenv", lambda **kw: None)
    for p in ("GEMINI", "OPENROUTER", "GROQ"):
        monkeypatch.delenv(f"{p}_API_KEY", raising=False)
        for i in range(1, 10):
            monkeypatch.delenv(f"{p}_API_KEY_{i}", raising=False)
    monkeypatch.delenv("LLM_PROVIDERS", raising=False)
    return monkeypatch


def test_env_numbered_gemini_keys_then_openrouter(env):
    env.setenv("GEMINI_API_KEY_1", "g1")
    env.setenv("GEMINI_API_KEY_2", "g2")
    env.setenv("OPENROUTER_API_KEY", "o")
    ps = providers_from_env()
    assert [(p.name, p.api_key) for p in ps] == [
        ("gemini#1", "g1"),
        ("gemini#2", "g2"),
        ("openrouter", "o"),
    ]
    assert ps[0].base_url == GEM_BASE


def test_env_single_key_and_missing_skipped(env):
    env.setenv("LLM_PROVIDERS", "groq,openrouter")
    env.setenv("GROQ_API_KEY", "gk")
    ps = providers_from_env()
    assert [p.name for p in ps] == ["groq"]
    assert ps[0].model == "openai/gpt-oss-120b"


def test_env_plain_key_ignored_when_numbered_keys_exist(env):
    env.setenv("GEMINI_API_KEY", "stale")
    env.setenv("GEMINI_API_KEY_1", "g1")
    assert [p.api_key for p in providers_from_env()] == ["g1"]
