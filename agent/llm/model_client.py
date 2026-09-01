"""ModelClient — thin wrapper around the OpenRouter (OpenAI-compatible) API.

Portability: this is the only file that knows about OpenRouter. To switch to
Bedrock/OpenAI/Azure, implement the same ``chat`` interface and swap the
factory.

Also exports a ``StubModelClient`` used by offline tests and playbooks so we
don't burn credits on every unit test.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

import httpx

from agent.core.config import get_settings


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict]  # OpenAI-style tool call dicts
    tokens_in: int
    tokens_out: int
    model: str
    raw: dict


class ModelClient:
    """Interface: implement ``chat`` and you're portable."""

    def chat(
        self,
        model: str,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: str | dict = "auto",
        temperature: float = 0.0,
        max_tokens: int = 2000,
        timeout_seconds: Optional[float] = None,
    ) -> ChatResult:
        raise NotImplementedError


class OpenRouterClient(ModelClient):
    def __init__(self, api_key: str, base_url: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat(
        self,
        model: str,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: str | dict = "auto",
        temperature: float = 0.0,
        max_tokens: int = 2000,
        timeout_seconds: Optional[float] = None,
    ) -> ChatResult:
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": "AccessInvestigationAgent",
            "HTTP-Referer": "https://example.local/access-agent",
        }
        # simple retry with backoff on 429/5xx
        last_err: Optional[Exception] = None
        deadline = (
            time.monotonic() + timeout_seconds
            if timeout_seconds is not None
            else None
        )

        def remaining_timeout() -> float:
            if deadline is None:
                return self.timeout
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("model-call wall-clock budget exhausted")
            return max(0.001, min(self.timeout, remaining))

        def bounded_sleep(delay: float) -> None:
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise TimeoutError("model-call wall-clock budget exhausted during retry")
            time.sleep(delay)

        for attempt in range(3):
            try:
                with httpx.Client(timeout=remaining_timeout()) as c:
                    resp = c.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_err = RuntimeError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                    if attempt < 2:
                        bounded_sleep(1.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                msg = choice["message"]
                content = msg.get("content") or ""
                tool_calls = msg.get("tool_calls") or []
                usage = data.get("usage") or {}
                return ChatResult(
                    content=content,
                    tool_calls=tool_calls,
                    tokens_in=int(usage.get("prompt_tokens") or 0),
                    tokens_out=int(usage.get("completion_tokens") or 0),
                    model=data.get("model") or model,
                    raw=data,
                )
            except Exception as e:
                last_err = e
                if attempt < 2:
                    bounded_sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"OpenRouter call failed after retries: {last_err}")


class StubModelClient(ModelClient):
    """For tests and offline mode. Returns pre-canned responses keyed by role/model."""

    def __init__(self, responder: Callable[[str, list[dict], Optional[list[dict]]], ChatResult]) -> None:
        self.responder = responder

    def chat(
        self,
        model: str,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: str | dict = "auto",
        temperature: float = 0.0,
        max_tokens: int = 2000,
        timeout_seconds: Optional[float] = None,
    ) -> ChatResult:
        return self.responder(model, messages, tools)


def default_client() -> ModelClient:
    s = get_settings()
    if not s.openrouter_api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Either set it in the environment / .env, "
            "put it in token.txt, or use --offline."
        )
    return OpenRouterClient(s.openrouter_api_key, s.openrouter_base_url)
