"""Swappable model wrapper.

The rest of the project talks only to the `LLM` interface. Which model runs is decided by
environment variables (see `.env.example`), so moving from a local model to a hosted one is a
config change, not a code change.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""


@dataclass
class LLMResponse:
    """A normalized model reply: free text, tool calls, or both."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


# Neutral message format used everywhere in this project. Backends convert it to their own.
#   {"role": "system" | "user", "content": str}
#   {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments"}]}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str}
Message = dict[str, Any]
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


def _openai_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                        }
                        for c in m["tool_calls"]
                    ],
                }
            )
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def _ollama_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("content") or "",
                    "tool_calls": [
                        {"function": {"name": c["name"], "arguments": c["arguments"]}}
                        for c in m["tool_calls"]
                    ],
                }
            )
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_name": m["name"], "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def _tool_wrappers(tools: Sequence[ToolSpec] | None) -> list[dict[str, Any]]:
    return [{"type": "function", "function": t} for t in (tools or [])]


class OllamaLLM:
    """Local model through Ollama's /api/chat endpoint."""

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
        max_output_tokens: int = 1024,
        timeout: float = 300.0,
        num_ctx: int = 8192,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        # Ollama's default context window is small. Tool results are long, so without this the
        # model silently loses the start of the conversation, including its instructions.
        self.num_ctx = num_ctx

    def chat(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None = None
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _ollama_messages(messages),
            "stream": False,
            "options": {
                "temperature": 0,
                "num_predict": self.max_output_tokens,
                "num_ctx": self.num_ctx,
            },
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


class LLMRequestError(RuntimeError):
    """A hosted API call failed for good (not a retryable rate limit)."""

    def __init__(self, message: str, status: int, body: str):
        super().__init__(message)
        self.status = status
        self.body = body


class OpenAICompatLLM:
    """Any hosted API that speaks the OpenAI /chat/completions format."""

    def _post_with_retries(self, payload: dict[str, Any]):
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            self._last_call = time.monotonic()
            r = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
            status = getattr(r, "status_code", 200)
            daily = status == 429 and "per day" in getattr(r, "text", "").lower()
            if status in (429, 500, 502, 503, 504) and attempt < self.max_retries and not daily:
                self._sleep(_retry_after(r, attempt))
                continue
            if status >= 400:
                body = getattr(r, "text", "")
                raise LLMRequestError(
                    f"{self.base_url} returned HTTP {status} for model {self.model!r}: {body[:300]}",
                    status,
                    body,
                )
            return r
        raise RuntimeError("unreachable")

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        max_output_tokens: int = 1024,
        timeout: float = 120.0,
        max_retries: int = 5,
        min_interval: float = 0.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        # Free tiers allow only a few requests a minute. Space calls out and retry on 429.
        self.max_retries = max_retries
        self.min_interval = min_interval
        self._last_call = 0.0
        self._sleep = time.sleep

    def chat(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None = None
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _openai_messages(messages),
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
        }
        if tools:
            payload["tools"] = _tool_wrappers(tools)
        try:
            r = self._post_with_retries(payload)
        except LLMRequestError as e:
            # Some providers reject a malformed tool call with HTTP 400. Hand the raw attempt back
            # as text so the loop can try to recover it or ask the model to try again.
            if e.status == 400 and "tool_use_failed" in e.body:
                try:
                    failed = json.loads(e.body)["error"].get("failed_generation", "")
                except (ValueError, KeyError, AttributeError):
                    failed = ""
                return LLMResponse(text=failed or "")
            raise
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            args = c["function"].get("arguments") or "{}"
            calls.append(ToolCall(c["function"]["name"], json.loads(args), c.get("id", "")))
        usage = data.get("usage", {})
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )


def _retry_after(response: Any, attempt: int) -> float:
    """Seconds to wait after a 429 or 5xx: the server's Retry-After if sent, else back off."""
    header = getattr(response, "headers", {}).get("retry-after")
    try:
        return min(float(header), 120.0)
    except (TypeError, ValueError):
        return min(2.0 * 2**attempt, 60.0)


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
            num_ctx=int(env.get("OLLAMA_NUM_CTX", "8192")),
        )
    if provider == "openai_compat":
        base_url, api_key = env.get("LLM_BASE_URL", ""), env.get("LLM_API_KEY", "")
        if not base_url or not api_key:
            raise ValueError("openai_compat needs LLM_BASE_URL and LLM_API_KEY")
        return OpenAICompatLLM(
            model,
            base_url,
            api_key,
            max_out,
            max_retries=int(env.get("LLM_MAX_RETRIES", "5")),
            min_interval=float(env.get("LLM_MIN_INTERVAL", "0")),
        )
    raise ValueError(f"Unknown LLM_PROVIDER: {provider!r}")
