from __future__ import annotations

import asyncio
import os
import sys

from app.provider import OpenAICompatibleProvider, ProviderConfig, ProviderError


async def main() -> int:
    key = os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL", "")
    model = os.getenv("OPENAI_MODEL", "")
    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", key),
            ("OPENAI_BASE_URL", base_url),
            ("OPENAI_MODEL", model),
        )
        if not value
    ]
    if missing:
        print(f"missing live provider configuration: {', '.join(missing)}", file=sys.stderr)
        return 2

    provider = OpenAICompatibleProvider(ProviderConfig(base_url, model, key))
    try:
        health = await provider.chat(
            [
                {"role": "system", "content": "Reply with exactly LIVE_PROVIDER_OK."},
                {"role": "user", "content": "Health check."},
            ]
        )
        if "LIVE_PROVIDER_OK" not in health:
            print(f"provider smoke returned unexpected response: {health[:200]}", file=sys.stderr)
            return 1
        architect = await provider.chat(
            [
                {"role": "system", "content": "You are an Architect. Return JSON only."},
                {
                    "role": "user",
                    "content": (
                        'Return {"spaces":[{"name":"Smoke"}],'
                        '"agents":[{"name":"Smoke Agent"}],'
                        '"goals":[{"title":"Smoke","criteria":["smoke"]}]}'
                    ),
                },
            ],
            json_mode=True,
        )
        agent = await provider.chat(
            [
                {"role": "system", "content": "You are a factory agent."},
                {"role": "user", "content": "Reply with exactly LIVE_AGENT_OK."},
            ]
        )
    except ProviderError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if "Smoke" not in architect or "LIVE_AGENT_OK" not in agent:
        print("architect/agent smoke response did not contain expected markers", file=sys.stderr)
        return 1
    print(f"LIVE_PROVIDER_OK architect={len(architect)} agent={agent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
