"""AutoSetModelByKey: discover which LLM models work with a given API key.

Given a base_url and api_key, probes the models endpoint, then validates each
candidate with a minimal completion. Returns a ranked list of working models.

Handles: OpenAI-compatible, Anthropic, Ollama, and custom endpoints.
Never raises — returns empty list with error details if all probes fail.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 10.0
COMPLETION_TIMEOUT = 20.0
PROBE_MSG = [{"role": "user", "content": "Say OK"}]
TOOLS = [{
    "type": "function",
    "function": {
        "name": "probe_ok",
        "description": "Probe tool call",
        "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": []},
    },
}]
PREFERRED_CHAT_MARKERS = ("chat", "gpt", "claude", "llama", "gemini", "qwen", "moonshot", "kimi", "deepseek", "mistral", "mixtral", "phi", "gemma", "command", "o1", "o3")
_SKIP_MARKERS = ("embed", "rerank", "bge", "clip", "vision", "whisper", "tts", "stt", "image", "audio")


@dataclass
class ModelInfo:
    id: str
    context_window: int = 0
    latency_ms: float = 0.0
    provider: str = ""
    working: bool = False
    error: str = ""
    score: float = 0.0  # higher = better


async def _list_models(base_url: str, api_key: str) -> tuple[list[str], str]:
    """Try various /models endpoints. Returns (model_ids, error)."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    client = httpx.AsyncClient(timeout=PROBE_TIMEOUT)

    paths = ["/v1/models", "/models", "/v1/model/list", "/api/tags"]  # OpenAI / Ollama
    for path in paths:
        url = base_url.rstrip("/") + path
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code in (401, 403):
                await client.aclose()
                return [], f"auth error {resp.status_code}"
            if resp.status_code != 200:
                continue
            data = resp.json()
            # OpenAI format: {"data": [{"id": ...}]}
            if "data" in data:
                ids = [m.get("id") or m.get("name") for m in data["data"] if isinstance(m, dict)]
            # Ollama: {"models": [{"name": ...}]}
            elif "models" in data:
                ids = [m.get("name") or m.get("id") for m in data["models"] if isinstance(m, dict)]
            elif isinstance(data, list):
                ids = [m.get("id") or m.get("name") for m in data if isinstance(m, dict)]
            else:
                continue
            ids = [i for i in ids if i]
            if ids:
                await client.aclose()
                return ids, ""
        except Exception as e:
            log.debug("model list failed %s: %s", url, e)
            continue

    await client.aclose()
    return [], "no models endpoint responded"


async def _probe_model(base_url: str, api_key: str, model_id: str) -> ModelInfo:
    """Attempt a minimal completion with this model."""
    info = ModelInfo(id=model_id)
    url = base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # The probe must look like the agent's real usage: a tool-call-capable
    # completion, not a plain text chat. Providers that fail on tools will show
    # up as broken before the user ever relies on them.
    body = {"model": model_id, "messages": PROBE_MSG, "max_tokens": 5, "stream": False,
            "tools": TOOLS, "tool_choice": "auto"}

    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=COMPLETION_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)
        dt = (time.monotonic() - t0) * 1000

        if resp.status_code in (401, 403):
            info.error = f"auth error {resp.status_code}"
            return info
        if resp.status_code == 404:
            info.error = "model not found"
            return info
        if resp.status_code != 200:
            info.error = f"HTTP {resp.status_code}"
            return info

        data = resp.json()
        content = ""
        try:
            content = data["choices"][0]["message"]["content"]
            ctx = data.get("model") or ""
        except (KeyError, IndexError):
            info.error = "unexpected response shape"
            return info

        tool_calls = []
        try:
            tool_calls = data["choices"][0]["message"].get("tool_calls") or []
        except Exception:
            tool_calls = []
        info.working = True
        info.latency_ms = dt
        # Context window was previously fake (prompt_tokens * 100); without a
        # real model-metadata endpoint we say 0 and rely on latency only.
        info.context_window = 0
        # Score: lower latency = higher score (context term removed).
        info.score = 1000.0 / max(dt, 1) + (2.0 if tool_calls else 0.0)

    except httpx.TimeoutException:
        info.error = "timeout"
    except Exception as e:
        info.error = str(e)[:80]

    return info


async def discover_models(
    base_url: str,
    api_key: str,
    *,
    max_probe: int = 20,
    concurrency: int = 5,
) -> list[ModelInfo]:
    """Discover and validate all working models for this key.

    Returns a list sorted by score descending (best first).
    Never raises.
    """
    if not base_url or not api_key:
        return []

    model_ids, err = await _list_models(base_url, api_key)
    if not model_ids:
        log.warning("model discovery: %s", err or "no models found")
        return []

    # Filter to chat models only; embeddings/rerankers probe as failures and
    # waste the probe budget.
    def _is_chat(mid: str) -> bool:
        m = mid.lower()
        if any(s in m for s in _SKIP_MARKERS):
            return False
        return any(p in m for p in PREFERRED_CHAT_MARKERS)

    chat_ids = [m for m in model_ids if _is_chat(m)]
    if chat_ids:
        model_ids = chat_ids
    else:
        log.debug("no chat-model names found; keeping all candidates")

    log.info("found %d model candidates, probing up to %d", len(model_ids), max_probe)
    model_ids = model_ids[:max_probe]

    sem = asyncio.Semaphore(concurrency)

    async def guarded(mid: str) -> ModelInfo:
        async with sem:
            return await _probe_model(base_url, api_key, mid)

    results = await asyncio.gather(*[guarded(m) for m in model_ids], return_exceptions=True)

    infos = []
    for r in results:
        if isinstance(r, ModelInfo):
            infos.append(r)
        elif isinstance(r, Exception):
            log.debug("probe exception: %s", r)

    working = sorted([i for i in infos if i.working], key=lambda x: x.score, reverse=True)
    failed = [i for i in infos if not i.working]
    log.info("discovery: %d working, %d failed", len(working), len(failed))
    return working + failed
