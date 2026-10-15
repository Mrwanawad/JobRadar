"""One real call per configured LLM provider, to check keys and models work.

Run: uv run python scripts/llm_smoke.py
"""

from __future__ import annotations

from jobradar.llm import LLMClient, LLMError, providers_from_env


def main() -> None:
    providers = providers_from_env()
    if not providers:
        print("No provider keys found. Copy .env.example to .env and set GEMINI_API_KEY.")
        return
    for p in providers:
        try:
            data, res = LLMClient([p]).chat_json(
                [{"role": "user", "content": 'Reply with JSON {"ok": true, "sum": 17+25}.'}],
                max_tokens=300,
            )
            print(f"OK   {p.name} {res.model}: {data} usage={res.usage}")
        except LLMError as e:
            print(f"FAIL {p.name}: {e}")


if __name__ == "__main__":
    main()
