"""Unified async LLM client — OpenAI-compatible + Anthropic.

Supports streaming and single-shot completions. Retries on transient errors
with exponential backoff. Tracks token usage for context management.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import httpx

# Hard ceiling on tool turns per message so a misbehaving model can't loop.
MAX_TOOL_TURNS = 12

log = logging.getLogger(__name__)

ANTHROPIC_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMConfig:
    base_url: str = ""
    api_key: str = ""
    model_id: str = ""
    provider: str = "openai"   # "openai" | "anthropic"
    max_tokens: int = 4096
    temperature: float = 0.7
    system_prompt: str = ""
    timeout: float = 120.0
    max_retries: int = 3
    tool_timeout: float = 30.0  # single raw completion for a tool turn

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            base_url=os.environ.get("LLM_BASE_URL", ""),
            api_key=os.environ.get("LLM_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "")),
            model_id=os.environ.get("LLM_MODEL", ""),
            provider=os.environ.get("LLM_PROVIDER", "openai"),
        )


class LLMClient:
    """Chat completion client. Auto-detects Anthropic vs OpenAI-compatible."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._usage = Usage()
        self._http = httpx.AsyncClient(timeout=config.timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def usage(self) -> Usage:
        return self._usage

    def update_config(self, **kwargs) -> None:
        for k, v in kwargs.items():
            if hasattr(self._config, k):
                setattr(self._config, k, v)

    async def complete(
        self,
        messages: list[dict],
        *,
        system: str = "",
        stream: bool = False,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Single-shot completion. Returns the assistant message content."""
        chunks = []
        async for chunk in self.stream(messages, system=system,
                                       max_tokens=max_tokens, temperature=temperature):
            chunks.append(chunk)
        return "".join(chunks)

    async def complete_raw(
        self,
        messages: list[dict],
        *,
        system: str = "",
        tools: Optional[list[dict]] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> dict:
        """Raw completion returning the full provider JSON (non-streaming).

        Unlike stream(), this never hides tool_calls inside delta chunks: the
        OpenAI-compatible response keeps ``choices[0].message.tool_calls``
        (id + function.name + function.arguments) verbatim, and the Anthropic
        response keeps its ``content`` blocks (incl. ``tool_use``). The runtime
        uses this for real tool calling.
        """
        cfg = self._config
        is_anthropic = (
            cfg.provider == "anthropic"
        )
        if is_anthropic:
            return await self._complete_raw_anthropic(
                messages, system=system,
                max_tokens=max_tokens or cfg.max_tokens,
                temperature=temperature if temperature is not None else cfg.temperature,
            )
        else:
            return await self._complete_raw_openai(
                messages, system=system, tools=tools,
                max_tokens=max_tokens or cfg.max_tokens,
                temperature=temperature if temperature is not None else cfg.temperature,
            )

    async def _complete_raw_openai(
        self,
        messages: list[dict],
        *,
        system: str,
        tools: Optional[list[dict]],
        max_tokens: int,
        temperature: float,
    ) -> dict:
        base = self._config.base_url or "https://api.openai.com"
        url = base.rstrip("/") + "/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)
        body: dict = {
            "model": self._config.model_id,
            "messages": all_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        http = httpx.AsyncClient(timeout=self._config.tool_timeout)
        try:
            resp = await http.post(url, headers=headers, json=body)
            resp.raise_for_status()
            return resp.json()
        finally:
            await http.aclose()

    async def _complete_raw_anthropic(
        self,
        messages: list[dict],
        *,
        system: str,
        max_tokens: int,
        temperature: float,
    ) -> dict:
        base = self._config.base_url or ANTHROPIC_BASE
        url = base.rstrip("/") + "/v1/messages"
        headers = {
            "x-api-key": self._config.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        body: dict = {
            "model": self._config.model_id or "claude-sonnet-4-5",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
            "stream": False,
        }
        if system:
            body["system"] = system
        http = httpx.AsyncClient(timeout=self._config.tool_timeout)
        try:
            resp = await http.post(url, headers=headers, json=body)
            resp.raise_for_status()
            return resp.json()
        finally:
            await http.aclose()

    async def stream(
        self,
        messages: list[dict],
        *,
        system: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[str]:
        """Streaming completion. Yields text chunks as they arrive."""
        cfg = self._config
        is_anthropic = (
            cfg.provider == "anthropic"
            or "anthropic" in cfg.base_url
            or cfg.base_url == ""
        )

        for attempt in range(cfg.max_retries):
            try:
                if is_anthropic:
                    gen = self._stream_anthropic(
                        messages, system=system or cfg.system_prompt,
                        max_tokens=max_tokens or cfg.max_tokens,
                        temperature=temperature if temperature is not None else cfg.temperature,
                    )
                else:
                    gen = self._stream_openai(
                        messages, system=system or cfg.system_prompt,
                        max_tokens=max_tokens or cfg.max_tokens,
                        temperature=temperature if temperature is not None else cfg.temperature,
                    )
                async for chunk in gen:
                    yield chunk
                return
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt < cfg.max_retries - 1:
                    wait = 1.5 ** attempt
                    log.warning("LLM transient error (attempt %d): %s, retrying in %.1fs", attempt + 1, e, wait)
                    await asyncio.sleep(wait)
                    continue
                raise

    async def _stream_anthropic(
        self,
        messages: list[dict],
        *,
        system: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        base = self._config.base_url or ANTHROPIC_BASE
        url = base.rstrip("/") + "/v1/messages"
        headers = {
            "x-api-key": self._config.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        body = {
            "model": self._config.model_id or "claude-sonnet-5-5",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
            "stream": True,
        }
        if system:
            body["system"] = system

        async with self._http.stream("POST", url, headers=headers, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw == "[DONE]":
                    break
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event = d.get("type", "")
                if event == "content_block_delta":
                    delta = d.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield delta.get("text", "")
                elif event == "message_delta":
                    usage = d.get("usage", {})
                    self._usage.completion_tokens += usage.get("output_tokens", 0)
                elif event == "message_start":
                    usage = d.get("message", {}).get("usage", {})
                    self._usage.prompt_tokens += usage.get("input_tokens", 0)

    async def _stream_openai(
        self,
        messages: list[dict],
        *,
        system: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        base = self._config.base_url or "https://api.openai.com"
        url = base.rstrip("/") + "/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        body = {
            "model": self._config.model_id,
            "messages": all_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }

        async with self._http.stream("POST", url, headers=headers, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw == "[DONE]":
                    break
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                delta = d.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    yield content
                usage = d.get("usage") or {}
                if usage:
                    self._usage.prompt_tokens = usage.get("prompt_tokens", self._usage.prompt_tokens)
                    self._usage.completion_tokens = usage.get("completion_tokens", self._usage.completion_tokens)
