from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class ProviderError(RuntimeError):
    """A provider call could not be completed or parsed."""


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    model: str
    api_key: str


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class OpenAICompatibleProvider:
    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client
        self._last_response = ProviderResponse("")

    @property
    def last_response(self) -> ProviderResponse:
        return self._last_response

    async def chat_with_usage(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> ProviderResponse:
        if not self.config.api_key:
            raise ProviderError("OpenAI-compatible API key is not configured")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=90)
        try:
            response = await client.post(
                self.config.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            try:
                data = response.json()
                usage = data.get("usage") or {}
                prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
                result = ProviderResponse(
                    content=str(data["choices"][0]["message"]["content"]),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )
                self._last_response = result
                return result
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ProviderError("provider returned invalid JSON or no assistant content") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"provider request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def chat(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
        return (await self.chat_with_usage(messages, json_mode=json_mode)).content


class FakeProvider(OpenAICompatibleProvider):
    """Deterministic adapter for unit tests; production never selects this class."""

    def __init__(self, response: str, *, usage: ProviderResponse | None = None) -> None:
        self.response = response
        self.usage = usage or ProviderResponse(response)
        self._last_response = self.usage

    async def chat_with_usage(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> ProviderResponse:
        return self.usage
