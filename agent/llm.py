"""Swappable model wrapper.

The rest of the project talks only to the `LLM` interface. Which model runs is decided by
environment variables (see `.env.example`), so moving from a local model to a hosted one is a
config change, not a code change.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """A normalized model reply: free text, tool calls, or both."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


Message = dict[str, Any]  # {"role": "system"|"user"|"assistant"|"tool", "content": str, ...}
ToolSpec = dict[str, Any]  # JSON-schema style: {"name", "description", "parameters"}


class LLM(Protocol):
    def chat(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None = None
    ) -> LLMResponse: ...


class MockLLM:
    """Returns scripted replies in order. Used by tests so they never need a real model."""

    def __init__(self, script: Sequence[LLMResponse]):
        self._script = list(script)
        self.calls: list[list[Message]] = []

    def chat(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None = None
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if not self._script:
            raise RuntimeError("MockLLM script exhausted")
        return self._script.pop(0)


def _tool_wrappers(tools: Sequence[ToolSpec] | None) -> list[dict[str, Any]]:
    return [{"type": "function", "function": t} for t in (tools or [])]


class OllamaLLM:
    """Local model through Ollama's /api/chat endpoint."""

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
        max_output_tokens: int = 1024,
        timeout: float = 120.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout

    def chat(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None = None
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "stream": False,
            "options": {"temperature": 0, "num_predict": self.max_output_tokens},
        }
        if tools:
            payload["tools"] = _tool_wrappers(tools)
        r = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        msg = data.get("message", {})
        calls = [
            ToolCall(c["function"]["name"], c["function"].get("arguments") or {})
            for c in msg.get("tool_calls", [])
        ]
        return LLMResponse(
            text=msg.get("content", "") or "",
            tool_calls=calls,
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
        )


class OpenAICompatLLM:
    """Any hosted API that speaks the OpenAI /chat/completions format."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        max_output_tokens: int = 1024,
        timeout: float = 120.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout

    def chat(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None = None
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
        }
        if tools:
            payload["tools"] = _tool_wrappers(tools)
        r = httpx.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            args = c["function"].get("arguments") or "{}"
            calls.append(ToolCall(c["function"]["name"], json.loads(args)))
        usage = data.get("usage", {})
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )


def make_llm(env: dict[str, str] | None = None) -> LLM:
    """Build the model selected by environment variables."""
    env = dict(os.environ) if env is None else env
    provider = env.get("LLM_PROVIDER", "mock").lower()
    model = env.get("LLM_MODEL", "")
    max_out = int(env.get("MAX_OUTPUT_TOKENS", "1024"))

    if provider == "mock":
        return MockLLM([])
    if provider == "ollama":
        return OllamaLLM(
            model=model,
            base_url=env.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            max_output_tokens=max_out,
        )
    if provider == "openai_compat":
        base_url, api_key = env.get("LLM_BASE_URL", ""), env.get("LLM_API_KEY", "")
        if not base_url or not api_key:
            raise ValueError("openai_compat needs LLM_BASE_URL and LLM_API_KEY")
        return OpenAICompatLLM(model, base_url, api_key, max_out)
    raise ValueError(f"Unknown LLM_PROVIDER: {provider!r}")
