"""Agent runtime: the autonomous agent that wraps the trading engine.

The runtime:
1. Receives user messages from bots or CLI.
2. Parses commands and dispatches via the registry.
3. Falls back to the LLM for conversational input.
4. Can read/write its own source files (with confirmation gate).
5. Maintains conversation history and memory.

Honesty guarantee: the agent never fabricates exchange data. When it doesn't
know something, say so. Uncertainty is always explicit.
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
2. Trading safety first: Never place live orders without explicit confirmation (unless autotrade is on).
3. Relentless quality: Your analysis and code are production-grade, never half-baked.
4. Proactive debugging: Verify before claiming success. If something might be wrong, check it.
5. Transparency: Explain your reasoning. Don't hide failures or mask errors.

You are an ACTIVE agent — you have full ability to execute commands and change state.
When the user asks you to change a setting, execute the slash command yourself via the
registry and report the real result. Never just tell the user what to do — DO IT.

Execution rules:
- If user says "set max open positions to 1", run "/settings max_open_positions=1"
- If user says "risk per trade: 20", run "/settings risk_per_trade_pct=20"
- If user says "leverage 10x", run "/leverage 10"
- Always report the BEFORE → AFTER state honestly
- If a change fails, report the actual error, not a fabricated success

You have full access to a Bitunix futures trading engine via the command registry.
Be direct, precise, and professional. Take action — don't just suggest it.
"""

# Natural-language config intents routed to real commands.
# Flexible matching: single tokens match anywhere, multi-word phrases match
# as substrings. Singular/plural variants covered by separate entries.
_CONFIG_INTENT_PATTERNS: list[tuple[tuple[str, ...], str]] = [
    # risk per trade
    (("risk per trade", "riesgo por operacion", "riesgo por operación", "riesgo por trade", "risk trade"), "risk_per_trade_pct"),
    # leverage
    (("leverage", "apalancamiento", "lever"), "leverage"),
    # max open positions / max positions / max position
    (("max open positions", "max open position", "max positions", "max position", "maxopenpositions", "max_open_positions"), "max_open_positions"),
    # margin mode
    (("margin mode", "modo margen", "modo de margen", "cross margin", "isolated margin"), "margin_mode"),
    # position mode
    (("position mode", "modo posicion", "modo de posición", "hedge mode", "one way mode"), "position_mode"),
    # min confidence
    (("min confidence", "confianza minima", "confianza mínima", "minimum confidence"), "min_confidence"),
    # scan interval
    (("scan interval", "intervalo de escaneo", "scan interval sec"), "scan_interval_sec"),
    # daily loss
    (("daily loss", "perdida diaria", "pérdida diaria", "max daily loss"), "max_daily_loss_pct"),
]
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

        # Natural-language config requests route to real commands so the
        # change is actually executed instead of hallucinated by the LLM.
        routed = self._route_config_intent(text)
        if routed is not None:
            cmd, note = routed
            result = await self.registry.dispatch(
                cmd, engine=self.engine, agent=self, registry=self.registry
            )
            header = f"[auto-executed as `/{cmd}`]\n"
            if result.ok:
                yield header + (result.text or "Done.") + note
            else:
                yield header + f"Error: {result.error}"
            return

        # Conversation goes to the LLM.
        self._history.append({"role": "user", "content": text})
        system = self._build_system_prompt()
        messages = self._history[-20:]

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

        # Honesty guard: if the LLM claims a state change it cannot have made
        # (it has no execution ability), append a factual correction.
        correction = self._honesty_correction(full_reply)
        if correction:
            self._history.append({"role": "assistant", "content": correction})
            yield "\n\n" + correction

    # ------------------------------------------------------------------ #
    # Intent routing & honesty guard                                      #
    # ------------------------------------------------------------------ #

    def _route_config_intent(self, text: str) -> Optional[tuple[str, str]]:
        """Match short natural-language config requests to a real command.

        Returns (command, note) or None. Deliberately strict: the request must
        name exactly one known setting and contain a number or mode token.
        """
        if len(text) > 120 or "\n" in text:
            return None
        lowered = text.lower().strip().rstrip(".:!")
        settings = self.engine._settings

        for phrases, key in _CONFIG_INTENT_PATTERNS:
            if not any(p in lowered for p in phrases):
                continue
            # Extract the value: trailing number, or mode word.
            tokens = lowered.replace(":", " ").split()
            value: Optional[str] = None
            for t in reversed(tokens):
                try:
                    float(t.rstrip("%x"))
                    value = t.rstrip("%x")
                    break
                except ValueError:
                    continue
            if value is None:
                mode_words = ("cross", "isolated", "cruzado", "aislado", "hedge", "one_way", "oneway")
                for t in reversed(tokens):
                    if t in mode_words:
                        value = {"cruzado": "CROSS", "aislado": "isolated"}.get(t, t.upper())
                        if value == "ISOLATED" or value == "isolated":
                            value = "ISOLATION" if key == "margin_mode" else "ISOLATED"
                        break
            if value is None:
                return None

            if key == "margin_mode":
                cmd = f"/margin {value}"
                note = f"\n\n(Risk note: CROSS trades are capped at {settings.cross_margin_risk_ceiling_pct}% of equity.)"
            elif key == "position_mode":
                cmd = f"/position_mode {value}"
                note = ""
            elif key == "risk_per_trade_pct":
                cmd = f"/settings risk_per_trade_pct={value}"
                note = (
                    f"\n\n(⚠️ At {settings.leverage}x leverage with {value}% risk per trade, a single "
                    f"stop-out can cost ~{value}% of equity — verify your SL sizing.)"
                )
            elif key == "leverage":
                cmd = f"/leverage {value}"
                note = ""
            else:
                cmd = f"/settings {key}={value}"
                note = ""
            return cmd, note

    def _honesty_correction(self, reply: str) -> Optional[str]:
        """Detect LLM claims of state changes it cannot execute, and correct them.

        The conversational LLM has no execution ability — only slash commands
        mutate state. Any claim that a setting was changed/applied/saved is a
        fabrication and gets a factual correction appended.
        """
        lowered = reply.lower()
        claim_markers = (
            "set to", "changed to", "updated to", "has been set", "is now set",
            "i've set", "i have set", "saved", "applied", "configurado",
            "actualizado", "he cambiado", "he establecido",
        )
        if not any(m in lowered for m in claim_markers):
            return None
        # Exception: the reply itself recommends the slash command.
        if "/settings" in reply or "/leverage" in reply or "/margin" in reply:
            return None
        settings = self.engine._settings
        return (
            "⚠️ Correction: no setting has been changed by that reply — this agent's "
            "conversation layer cannot execute anything. Current values: risk per trade "
            f"{settings.risk_per_trade_pct}%, leverage {settings.leverage}x, margin "
            f"{settings.margin_mode}, autotrade "
            f"{'ON' if settings.autotrade_enabled else 'OFF'}. To apply a change, run the "
            "slash command (e.g. /settings risk_per_trade_pct=20); commands are executed "
            "and verified by the runtime."
        )

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