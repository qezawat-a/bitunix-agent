"""Agent runtime: the autonomous agent that wraps the trading engine.

The runtime:
1. Receives user messages from bots or CLI.
2. Parses commands and dispatches via the registry.
3. Falls back to the LLM for conversational input.
4. Can read/write its own source files (with confirmation gate).
5. Maintains conversation history and memory.

Honesty guarantee: the agent never fabricates exchange data. When it doesn't
know something, it says so. Uncertainty is always explicit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional, Callable, Awaitable, Dict, List

from .autoset import ModelInfo, discover_models
from .llm import LLMClient, LLMConfig
from jrock.commands.registry import CommandRegistry, CommandResult
from jrock.engine.engine import Engine
from jrock.engine.settings import STATE_DIR

log = logging.getLogger(__name__)

MEMORY_FILE = STATE_DIR / "memory.json"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOULS_DIR = PROJECT_ROOT / "jrock" / "soul"
SKILLS_DIR = PROJECT_ROOT / "jrock" / "skills"

BASE_SOUL = """You are J+ROCK, an elite AI trading agent with a battle-hardened soul.

CORE PRINCIPLES:
1. Radical honesty: Never fabricate data. State uncertainty explicitly. If you don't know, say so.
2. Trading safety first: Never place orders without explicit confirmation (unless autotrade is on).
3. Relentless quality: Your analysis and code are production-grade, never half-baked.
4. Proactive debugging: Verify before claiming success. If something might be wrong, check it.
5. Transparency: Explain your reasoning. Don't hide failures or mask errors.

You have full access to a Bitunix futures trading engine. Be direct, precise, and professional.
When uncertain about market conditions, say "I don't have enough data" rather than guessing.
"""


class SimpleMemory:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._load()

    def set(self, key: str, value: str) -> None:
        self._store[key] = value
        self._save()

    def get(self, key: str, default: str = "") -> str:
        return self._store.get(key, default)

    def list(self) -> List[Dict[str, str]]:
        return [{"key": k, "value": v} for k, v in self._store.items()]

    def clear(self) -> None:
        self._store.clear()
        self._save()

    def _save(self) -> None:
        try:
            MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            MEMORY_FILE.write_text(json.dumps(self._store, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load(self) -> None:
        try:
            if MEMORY_FILE.exists():
                self._store = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            self._store = {}


class AgentRuntime:
    def __init__(
        self,
        engine: Engine,
        registry: CommandRegistry,
        llm_config: Optional[LLMConfig] = None,
        confirm_fn: Optional[Callable[[str], Awaitable[bool]]] = None,
    ) -> None:
        self.engine = engine
        self.registry = registry
        self.llm_config = llm_config or LLMConfig.from_env()
        # Auto-discover models if no model is set but credentials are available.
        if not self.llm_config.model_id and self.llm_config.base_url and self.llm_config.api_key:
            try:
                import asyncio
                try:
                    discovered = asyncio.run(
                        discover_models(self.llm_config.base_url, self.llm_config.api_key)
                    )
                except RuntimeError:
                    loop = asyncio.get_event_loop()
                    discovered = loop.run_until_complete(
                        discover_models(self.llm_config.base_url, self.llm_config.api_key)
                    )
                working = [m for m in discovered if m.working]
                if working:
                    self.llm_config.model_id = working[0].id
                    self.discovered_models = discovered
                    log.info("AutoSetModelByKey: selected %s (%d working)", working[0].id, len(working))
            except Exception as e:
                log.warning("Auto model discovery failed: %s", e)
        self._llm = LLMClient(self.llm_config)
        self._confirm = confirm_fn
        self._history: list[dict] = []
        self.memory = SimpleMemory()
        self.auto_accept = False
        self.discovered_models: list[ModelInfo] = []
        self.mcp_servers: dict[str, str] = {}
        self._souls: dict[str, str] = {}
        self._skills: dict[str, dict] = {}
        self.soul_name = "default"
        self._load_souls()
        self._load_skills()

    @property
    def soul_text(self) -> str:
        return self._souls.get(self.soul_name, BASE_SOUL)

    def list_souls(self) -> list[str]:
        return list(self._souls.keys())

    def switch_soul(self, name: str) -> bool:
        if name in self._souls:
            self.soul_name = name
            return True
        return False

    def list_skills(self) -> list[dict]:
        return [{"name": k, "description": v.get("description", "")} for k, v in self._skills.items()]

    async def use_skill(self, name: str) -> str:
        skill = self._skills.get(name)
        if not skill:
            return f"Skill `{name}` not found."
        instructions = skill.get("body", "")
        return await self.think(f"Apply this skill:\n{instructions}")

    def _load_souls(self) -> None:
        self._souls["default"] = BASE_SOUL
        if SOULS_DIR.exists():
            for f in SOULS_DIR.glob("*.md"):
                try:
                    content = f.read_text(encoding="utf-8")
                    self._souls[f.stem] = content
                except Exception:
                    pass

    def _load_skills(self) -> None:
        if SKILLS_DIR.exists():
            for f in SKILLS_DIR.glob("*.md"):
                try:
                    content = f.read_text(encoding="utf-8")
                    # Parse simple frontmatter.
                    meta: dict = {}
                    body = content
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            for line in parts[1].splitlines():
                                if ":" in line:
                                    k, _, v = line.partition(":")
                                    meta[k.strip()] = v.strip()
                            body = parts[2].strip()
                    meta["body"] = body
                    self._skills[f.stem] = meta
                except Exception:
                    pass

    def _build_system_prompt(self) -> str:
        parts = [self.soul_text]
        mem = self.memory.list()
        if mem:
            parts.append("\n**Your memory:**\n" + "\n".join(f"- {m['key']}: {m['value']}" for m in mem[:20]))
        settings = self.engine._settings
        parts.append(
            f"\n**Current engine state:**\n"
            f"- Symbols: {settings.symbols}\n"
            f"- Leverage: {settings.leverage}x\n"
            f"- Autotrade: {'ON' if settings.autotrade_enabled else 'OFF'}\n"
            f"- Scanner: {'ON' if settings.scan_enabled else 'OFF'}\n"
        )
        return "\n\n".join(parts)

    async def handle(self, text: str) -> AsyncIterator[str]:
        """Main entry point. Yields text chunks for streaming output."""
        text = text.strip()
        if not text:
            return

        # Commands go to the registry.
        if text.startswith("/"):
            result = await self.registry.dispatch(
                text, engine=self.engine, agent=self, registry=self.registry
            )
            if result.ok:
                yield result.text or "Done."
            else:
                yield f"Error: {result.error}"
            return

        # Conversation goes to the LLM.
        self._history.append({"role": "user", "content": text})
        system = self._build_system_prompt()
        messages = self._history[-20:]  # Keep last 20 turns.

        reply_chunks = []
        try:
            async for chunk in self._llm.stream(messages, system=system):
                reply_chunks.append(chunk)
                yield chunk
        except Exception as e:
            error_msg = f"\n[Agent error: {e}]"
            yield error_msg
            reply_chunks.append(error_msg)

        full_reply = "".join(reply_chunks)
        self._history.append({"role": "assistant", "content": full_reply})

    async def think(self, prompt: str) -> str:
        """Single-shot LLM call for reasoning tasks."""
        system = self._build_system_prompt() + "\n\nThink carefully and reason step-by-step."
        try:
            return await self._llm.complete(
                [{"role": "user", "content": prompt}],
                system=system,
                temperature=0.3,
            )
        except Exception as e:
            return f"[Think error: {e}]"

    async def code_review(self, target: str) -> str:
        proj = PROJECT_ROOT / "jrock"
        files = []
        if target == "all" or not target:
            for d in ("engine", "adapters", "strategies", "risk", "agent", "commands"):
                files.extend((proj / d).glob("*.py"))
        else:
            p = Path(target)
            if p.exists():
                files = [p]
            else:
                p2 = proj / target
                files = list(p2.glob("*.py")) if p2.is_dir() else [p2] if p2.exists() else []

        if not files:
            return f"No files found for target: {target}"

        content_parts = []
        for f in files[:5]:
            try:
                content_parts.append(f"## {f.name}\n```python\n{f.read_text(encoding='utf-8')[:3000]}\n```")
            except Exception:
                pass

        prompt = (
            "Review this code for bugs, security issues, and correctness.\n"
            "Be specific: file:line, exact issue, concrete fix.\n"
            "Check especially: error handling, async correctness, exchange API calls.\n\n"
            + "\n\n".join(content_parts)
        )
        return await self.think(prompt)

    async def bug_fix(self, description: str) -> str:
        prompt = (
            f"Bug report: {description}\n\n"
            "1. Identify the most likely root cause.\n"
            "2. Find the relevant code (name the file and function).\n"
            "3. Write the exact fix.\n"
            "4. State what you'd verify after applying it.\n"
            "Be honest: if you can't determine the cause without more info, say so."
        )
        return await self.think(prompt)

    async def verify(self, target: str) -> str:
        results = []
        proj = PROJECT_ROOT / "jrock"
        dirs = [target] if target != "all" else ["engine", "adapters", "strategies", "risk", "agent", "commands"]
        for d in dirs:
            path = proj / d
            if not path.exists():
                continue
            for f in path.glob("*.py"):
                r = subprocess.run(
                    [sys.executable, "-m", "py_compile", str(f)],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    results.append(f"❌ {f.name}: {r.stderr.strip()[:150]}")
                else:
                    results.append(f"✅ {f.name}")
        if not results:
            return "No files to verify."
        ok = sum(1 for r in results if r.startswith("✅"))
        err = len(results) - ok
        return f"Verification: {ok} OK, {err} errors\n" + "\n".join(results)

    async def simplify(self, target: str) -> str:
        f = Path(target)
        if not f.exists():
            f = PROJECT_ROOT / "jrock" / target
        if not f.exists():
            return f"File not found: {target}"
        content = f.read_text(encoding="utf-8")
        prompt = f"Simplify this Python module. Reduce complexity, remove duplication, improve clarity. Show the simplified version:\n\n```python\n{content[:4000]}\n```"
        return await self.think(prompt)

    async def request_confirmation(self, action: str) -> bool:
        """Ask the user to confirm a potentially dangerous action."""
        if self.auto_accept:
            # Even in auto-accept, block high-risk actions.
            HIGH_RISK_KEYWORDS = ["rm ", "rmdir", "shutil.rmtree", "drop table",
                                  "delete from", ".env", "credentials", "git push",
                                  "sudo", "os.remove"]
            for kw in HIGH_RISK_KEYWORDS:
                if kw.lower() in action.lower():
                    log.warning("HIGH-RISK action blocked even in auto-accept: %s", action[:100])
                    return False
            return True
        if self._confirm:
            return await self._confirm(action)
        return False
