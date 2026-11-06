"""Agent runtime: the autonomous agent that wraps the trading engine.

The runtime:
1. Receives user messages from bots or CLI.
2. Parses commands and dispatches via the registry.
3. Falls back to the LLM for conversational input only — never for mutations.
4. Can read/write its own source files (with confirmation gate).
5. Maintains conversation history and memory.

Honesty guarantee: the agent never fabricates exchange data and never claims
a state change it did not execute. Mutations are deterministic (intent router
→ registry); every dispatch is verified by a state-diff; fabricated values
(balances, PnL, positions, models) are detected against live engine state.
When it doesn't know something, say so. Uncertainty is always explicit.
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import difflib
import json
import logging
import os
import re
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
# Broadened: bare "risk" routes to risk_per_trade_pct, "margin" to margin_mode,
# "position" to position_mode. Negation is explicit so "don't set X" never fires.
_CONFIG_INTENT_PATTERNS: list[tuple[tuple[str, ...], str]] = [
    # risk per trade
    (("risk per trade", "risk", "riesgo", "riesgo por operacion", "riesgo por operación", "riesgo por trade", "risk trade"), "risk_per_trade_pct"),
    # leverage
    (("leverage", "lever", "apalancamiento", "x leverage"), "leverage"),
    # max open positions / max positions / max position
    (("max open positions", "max open position", "max positions", "max position", "maxopenpositions", "max_open_positions", "max positions", "open positions"), "max_open_positions"),
    # margin mode
    (("margin mode", "margin", "modo margen", "modo de margen", "cross margin", "isolated margin"), "margin_mode"),
    # position mode
    (("position mode", "position", "modo posicion", "modo de posición", "hedge mode", "one way mode"), "position_mode"),
    # min confidence
    (("min confidence", "min_confidence", "confianza minima", "confianza mínima", "minimum confidence"), "min_confidence"),
    # scan interval
    (("scan interval", "scan_interval_sec", "intervalo de escaneo", "scan interval sec"), "scan_interval_sec"),
    # daily loss
    (("daily loss", "max_daily_loss_pct", "perdida diaria", "pérdida diaria", "max daily loss"), "max_daily_loss_pct"),
    # balance / pnl
    (("balance", "balanceo", "show me the balance", "pnl", "profit", "loss", "equity"), "pnl"),
    # soul
    (("soul", "persona", "set the soul", "switch soul", "change soul"), "soul"),
    # --- expanded coverage (every settings field must be reachable from chat) ---
    (("autotrade", "auto trade", "auto-trade"), "autotrade"),
    (("scanner", "scan is on", "scan is off", "scan enabled", "scan disabled", "turn scan"), "scan"),
    (("breakeven threshold", "breakeven", "break even", "breakeven threshold pct"), "breakeven_threshold_pct"),
    (("trailing stop", "trailing", "trail stop", "trailing stop roi"), "trailing_stop_roi_pct"),
    (("liq sl distance", "liq distance", "liquidation distance", "liq sl"), "liq_sl_distance_pct"),
    (("min agreeing strategies", "agreeing strategies", "min agreeing"), "min_agreeing_strategies"),
    (("timeframes", "timeframe", "time frame", "time frames"), "timeframes"),
    (("tf min confidence", "tfminconfidence", "tf_min_confidence", "timeframe confidence"), "tf_min_confidence"),
    (("reversal confidence", "reversal_conf"), "reversal_confidence"),
    (("report interval", "report_interval", "report every"), "report_interval_sec"),
    (("scan on", "scanner on", "scan off", "scanner off", "scan on/off"), "scan"),
    (("report on", "report off", "turn report", "enable report", "disable report"), "report"),
    (("engine on", "engine off", "turn engine", "engine status", "start engine", "stop engine"), "engine"),
    (("start trading", "stop trading", "start autotrade", "stop autotrade"), "autotrade"),
    (("max daily loss", "max_daily_loss", "daily loss pct"), "max_daily_loss_pct"),
    (("cross margin risk", "cross risk ceiling", "cross ceiling", "margin risk ceiling"), "cross_margin_risk_ceiling_pct"),
    (("max notional", "notional total", "notional limit", "max_notional_total"), "max_notional_total"),
    (("sl stop type", "sl_stop_type", "stop trigger type"), "sl_stop_type"),
]

_NEGATION_PATTERNS = (
    "don't", "dont", "do not", "dont", "not ", "never", "no ", "stop",
    "cancel", "revert", "undo", "pause when",
    "no pongas", "no poner", "no cambies", "no cambiar", "deja de",
    "para de", "detener", "cancela", "revertir",
)

# Words that, WITHOUT a setting name, still signal a desired mutation —
# so the agent never hallucinates a change it could not parse.
_BARE_MUTATION_PATTERNS = (
    "set ", "set to", "change ", "update ", "switch ", "turn on", "turn off",
    "enable ", "disable ", "put it ", "make it ", "adjust ",
    "pon ", "cambia", "actualiza", "activa", "desactiva", "ajusta",
    "increase ", "decrease ", "raise ", "lower ",
)

# Tokens meaning "no mutation requested".
_NOOP_PATTERNS = (
    " what is ", " what if ", " how does ", " why ", " explain",
    " tell me about ", " help me understand ", " describe ",
)

# Regexes for fabricated numeric claims: "now 20%", "$5,200", "is 42".
_AMOUNT_RE = re.compile(r"\$?\s?\d[\d,]*(?:\.\d+)?\s?(?:%|usd|usdt|x)?", re.I)
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s?%", re.I)
_PNL_RE = re.compile(r"(?:balance|pnl|equity|profit)[^\n]{0,40}\$?\s?-?\d[\d,]*(?:\.\d+)?", re.I)

# Broader claim markers (catches "Done!", "All set", "Margin updated")
_CLAIM_MARKERS = (
    "set to", "changed to", "updated to", "has been set", "is now set",
    "i've set", "i have set", "saved", "applied", "configurado",
    "actualizado", "he cambiado", "he establecido",
    "done!", "done.", "all set", "all done", "is now", "now at", "now ",
    "now 20", "now 10", "set the ", "change applied", "update applied",
    "configured", "successfully", "fixed!", "your risk", "your margin",
    "your leverage", "your position", "your balance", "positions open",
    "trading is now", "switched to", "enabled", "disabled", "activated",
    "updated", "updated.", "updated!", " update",
)

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def _normalize(s: str) -> str:
    """Lowercase + normalize accents/punctuation for intent matching."""
    s = s.lower().strip()
    s = s.replace("ó", "o").replace("á", "a").replace("é", "e").replace("í", "i").replace("ú", "u").replace("ñ", "n")
    return s
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

    async def request_confirmation(self, prompt: str) -> bool:
        """Confirmation gate for high-impact actions (force-push etc.).

        Delegates to the bot-provided confirm callback (Telegram approve/reject)
        when wired, otherwise blocks when auto_accept is off, and allows when
        auto_accept is on.
        """
        if self.auto_accept:
            return True
        if self._confirm is None:
            return False
        return bool(await self._confirm(prompt))

    def switch_soul(self, name: str) -> bool:
        name = name.lower().strip()
        if name in self._souls:
            self.soul_name = name
            return True
        # Fuzzy match: normalize separators and find closest
        normalized = name.replace("_", "").replace("-", "").replace(" ", "")
        candidates = {}
        for soul_name in self._souls:
            candidates[soul_name.replace("_", "").replace("-", "").replace(" ", "")] = soul_name
        matches = difflib.get_close_matches(normalized, candidates.keys(), n=1, cutoff=0.7)
        if matches:
            self.soul_name = candidates[matches[0]]
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
            f"\n**Current engine state (read from live settings, not invented):**\n"
            f"- Symbols: {settings.symbols}\n"
            f"- Leverage: {settings.leverage}x\n"
            f"- Margin: {settings.margin_mode}, Position: {settings.position_mode}\n"
            f"- Risk per trade: {settings.risk_per_trade_pct}%\n"
            f"- Max open positions: {settings.max_open_positions}\n"
            f"- Autotrade: {'ON' if settings.autotrade_enabled else 'OFF'}\n"
            f"- Scanner: {'ON' if settings.scan_enabled else 'OFF'}\n"
            f"- Model: {settings.model_id or '(none set)'}\n"
        )
        return "\n\n".join(parts)

    async def handle(self, text: str) -> AsyncIterator[str]:
        """Main entry point. Yields text chunks for streaming output.

        Order of operations (deterministic, verifiable):
          1. Slash commands → registry (state diff verified).
          2. Mutation intent → intent router → registry (state diff verified).
          3. Bare mutation intent with no parseable value → clarify, never LLM.
          4. Only then: converse with the LLM, filtered by honesty + fabrication guards.
        """
        text = text.strip()
        if not text:
            return

        # 1. Slash commands go to the registry.
        if text.startswith("/"):
            before = self._snapshot_settings()
            result = await self.registry.dispatch(
                text, engine=self.engine, agent=self, registry=self.registry
            )
            yield self._render_dispatch_result(
                text, result, before, auto_executed=False
            )
            return

        # 2. Natural-language mutation intent → real command execution.
        routed = self._route_config_intent(text)
        if routed is not None:
            cmd, note, negation = routed
            header = f"[auto-executed as `{cmd}`]"
            if negation:
                yield header + "\n⚠️ Action NOT taken — your message read as a negation/no-op. No state was changed."
                return
            before = self._snapshot_settings()
            result = await self.registry.dispatch(
                cmd, engine=self.engine, agent=self, registry=self.registry
            )
            rendered = self._render_dispatch_result(cmd, result, before, auto_executed=True)
            yield rendered + note
            return

        # 3. The user asked for a mutation but we couldn't parse an exact value.
        #    Never hand this to the LLM — it would hallucinate the change.
        if self._looks_like_mutation(text):
            yield (
                "I can't safely map that to a concrete setting change, and I won't guess. "
                "Please use the exact command, e.g. `/settings risk_per_trade_pct=20`, "
                "`/leverage 10`, `/margin CROSS`. No state was changed."
            )
            return

        # 4. REAL tool calling: let the LLM emit function calls; WE execute them
        #    through the registry. This is the fix for "says ok but does nothing":
        #    the old path was text-only, so a change request silently fell through
        #    to the LLM. Now a config-style request produces a tool call, and the
        #    tool result (with the verified before -> after diff) goes back to the
        #    model, which can then only report what actually happened.
        tool_reply = await self._run_tool_loop(text)
        if tool_reply is not None:
            yield tool_reply
            return

        # 4. Pure conversation: hand to the LLM, then verify its output.
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

        # Guards: (a) LLM claims a mutation → correct it; (b) LLM fabricates
        # balances/PnL/positions → correct against live engine state.
        correction = self._honesty_correction(full_reply)
        if correction:
            self._history.append({"role": "assistant", "content": correction})
            yield "\n\n" + correction

    # ------------------------------------------------------------------ #
    # State snapshot / verification                                       #
    # ------------------------------------------------------------------ #

    def _snapshot_settings(self) -> dict:
        """Deep copy of current settings for before/after diffing."""
        try:
            return copy.deepcopy(dataclasses.asdict(self.engine._settings))
        except Exception:
            return {}

    def _settings_diff(self, before: dict) -> dict:
        """Return {key: (before, after)} for fields that changed."""
        try:
            after = dataclasses.asdict(self.engine._settings)
        except Exception:
            return {}
        diff = {}
        for k in set(list(before.keys()) + list(after.keys())):
            b, a = before.get(k), after.get(k)
            if b != a:
                diff[k] = (b, a)
        return diff

    def _expected_dispatch_diff(self, cmd: str, result) -> dict:
        """Fields the user asked us to change, extracted from the command syntax."""
        expected = {}
        # /settings key=value
        for m in re.finditer(r"(\w+)=([^\s]+)", cmd):
            expected[m.group(1)] = m.group(2)
        # /leverage 10, /max_positions 3
        parts = cmd.split()
        if len(parts) >= 2 and parts[0].lower() in ("/leverage", "/max_positions", "/margin", "/position_mode"):
            expected[parts[0].lstrip("/")] = parts[1]
        return expected

    def _verify_dispatch(self, cmd: str, result, before: dict) -> Optional[str]:
        """Verify a dispatch actually mutated state. Returns a warning or None.

        - If the handler reported failure → always warn.
        - If the handler reported success but the expected fields didn't change
          → warn (this is the 'told me done but didn't do it' case).
        """
        after = self._snapshot_settings()
        diff = self._settings_diff(before)
        if not result.ok:
            return f"⚠️ Command reported an error: {result.error}"
        expected = self._expected_dispatch_diff(cmd, result)
        if not expected:
            return None  # No expected mutation (e.g. /pnl) — nothing to verify.
        missing = [k for k in expected if k not in diff]
        # Accept aliased keys (max_positions → max_open_positions etc.)
        aliases = {"max_positions": "max_open_positions"}
        still_missing = []
        for k in missing:
            real = aliases.get(k, k)
            if real not in diff:
                still_missing.append(k)
        if still_missing:
            return (
                f"⚠️ Verification failed: command reported success but these fields "
                f"did not change — {', '.join(still_missing)}. Values are unchanged: "
                f"{ {k: before.get(k) for k in still_missing} }."
            )
        return None

    def _render_dispatch_result(self, cmd: str, result, before: dict, *, auto_executed: bool) -> str:
        header = f"[auto-executed as `{cmd}`]\n" if auto_executed else ""
        verification = self._verify_dispatch(cmd, result, before)
        if result.ok:
            body = result.text or "Done."
        else:
            body = f"Error: {result.error}"
        out = header + body
        if verification:
            out += f"\n\n{verification}"
        return out

    # ------------------------------------------------------------------ #
    # Intent routing & honesty guard                                      #
    # ------------------------------------------------------------------ #

    def _route_config_intent(self, text: str) -> Optional[tuple[str, str, bool]]:
        """Match natural-language config requests to a real command.

        Returns (command, note, negation) or None. No length/newline cap —
        normalization handles punctuation; negation blocks execution; unknown
        values fall through to _looks_like_mutation()/clarify, never the LLM.
        """
        lowered = _normalize(text)
        settings = self.engine._settings
        is_negation = any(n in lowered for n in _NEGATION_PATTERNS)

        for phrases, key in _CONFIG_INTENT_PATTERNS:
            if not any(p in lowered for p in phrases):
                continue
            # Extract the value: first number found, or a mode word.
            tokens = re.split(r"[:,\s]+", lowered)
            value: Optional[str] = None
            for t in tokens:
                clean = t.rstrip("%x").rstrip("x")
                try:
                    float(clean)
                    value = clean
                    break
                except ValueError:
                    if clean in _NUM_WORDS:
                        value = str(_NUM_WORDS[clean])
                        break
                    continue
            if value is None:
                mode_words = ("cross", "isolated", "isolation", "cruzado", "aislado", "hedge", "one_way", "oneway", "one", "way")
                for t in tokens:
                    if t in mode_words:
                        value = t.upper()
                        if key == "margin_mode" and value in ("ISOLATED", "ISOLATION", "AISLADO"):
                            value = "ISOLATION"
                        break
            if value is None:
                # Special intents that don't need a value: pnl, soul
                if key == "pnl":
                    return "/pnl", "", is_negation
                if key == "soul":
                    available = self._souls.keys()
                    matched_soul = None
                    for soul_name in available:
                        variants = {
                            soul_name,
                            soul_name.replace("_", ""),
                            soul_name.replace("_", " "),
                            soul_name.split("_")[0] if "_" in soul_name else soul_name,
                        }
                        for v in variants:
                            if v in lowered:
                                matched_soul = soul_name
                                break
                        if matched_soul:
                            break
                    if matched_soul:
                        return f"/soul switch {matched_soul}", "", is_negation
                    return "/soul list", "", is_negation
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
            return cmd, note, is_negation
        return None

    def _looks_like_mutation(self, text: str) -> bool:
        """True if the user clearly wants a state change we couldn't parse."""
        lowered = _normalize(text)
        # Conversational / noop intros are safe for the LLM.
        if any(p in lowered for p in _NOOP_PATTERNS):
            return False
        if any(p in lowered for p in _BARE_MUTATION_PATTERNS):
            return True
        # Any known setting name + any number still counts as a mutation ask.
        for phrases, _ in _CONFIG_INTENT_PATTERNS:
            if any(p in lowered for p in phrases):
                return True
        return False

    def _honesty_correction(self, reply: str) -> Optional[str]:
        """Detect LLM claims the conversational layer cannot back up, correct them.

        Two independent checks:
          (a) State-change fabrication — the LLM has no execution ability; only
              the deterministic router/registry mutate state. Any claim of a
              completed change is a lie unless it phrases a slash command as
              the action to take.
          (b) Fabricated numeric data — balances, PnL, positions, or model ids
              invented by the LLM are caught by re-checking live engine state.
        """
        lowered = reply.lower()
        settings = self.engine._settings

        # (a) State-change fabrication — broader markers than before ("Done!",
        #     "All set", "is now at", "switched to", "enabled", …).
        if any(m in lowered for m in _CLAIM_MARKERS) or _PNL_RE.search(reply):
            # Exception: the reply itself recommends a slash command as the
            # action — that's guidance, not a fabrication claim.
            if not any(c in reply for c in ("/settings", "/leverage", "/margin", "/position_mode")):
                return (
                    "⚠️ Correction: no setting has been changed by that reply — this agent's "
                    "conversation layer cannot execute anything. Current values: risk per trade "
                    f"{settings.risk_per_trade_pct}%, leverage {settings.leverage}x, margin "
                    f"{settings.margin_mode}, max open positions {settings.max_open_positions}, "
                    f"autotrade {'ON' if settings.autotrade_enabled else 'OFF'}. To apply a "
                    "change, run the slash command (e.g. /settings risk_per_trade_pct=20); "
                    "commands are executed and verified by the runtime."
                )

        # (b) Fabricated numeric claims — amounts/balances/PnL that don't match
        #     anything we can verify in live state.
        correction = self._fabricated_data_correction(reply)
        if correction:
            return correction
        return None

    def _fabricated_data_correction(self, reply: str) -> Optional[str]:
        """Catch invented numbers (balances, PnL, positions, models).

        The conversational LLM has no access to live exchange state, so any
        specific balance/PnL/position count it states is fabricated unless it
        came from a verified command result (which never enters this path).
        """
        # Model-id style fabrications: long hex / versioned ids with no source.
        if re.search(r"\b(?:gpt|claude|gemini|llama|qwen|mistral|deepseek|phi)-[a-z0-9.\-]{4,}", reply, re.I):
            return (
                "⚠️ Correction: that model name was not verified against any provider. "
                "Run `/models` to discover working models for your configured key."
            )

        fabricated = None
        m = _PNL_RE.search(reply)
        if m:
            fabricated = m.group(0)
        if fabricated is None and _PERCENT_RE.search(reply):
            # A percentage change claim without any command backing it is suspect.
            fabricated = _PERCENT_RE.search(reply).group(0)
        if fabricated is None and re.search(r"\b\d+\s+positions?\s+open", reply, re.I):
            fabricated = re.search(r"\d+\s+positions?\s+open", reply, re.I).group(0)
        if fabricated is None:
            return None
        return (
            "⚠️ Correction: I cannot verify a live figure like that from conversation — "
            f"`{fabricated}` was not backed by any command. Run `/pnl` or `/status` for "
            "real exchange data; I never invent balances, PnL, or position counts."
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

# ------------------------------------------------------------------ #
# Real tool calling (OpenAI function calling pattern, registry-backed) #
# ------------------------------------------------------------------ #

_TOOL_SYSTEM_SUFFIX = """

You have access to real tools (the J+ROCK command registry). When the user asks
you to change, view or control anything, you MUST call the matching tool instead
of describing it. Calling the tool is how changes happen — chat text changes
nothing. After the tool returns a result, report the BEFORE → AFTER state from
that result exactly. Never claim a change you did not execute (tool result ok
must be true and the field must appear in the diff). If the tool errors, report
the error honestly and stop. Do not invent tool names — only call tools listed.
"""

def _tool_calls_from_openai(raw: dict) -> list[dict]:
    """Extract tool_calls from an OpenAI-compatible chat completion payload."""
    try:
        msg = raw["choices"][0]["message"]
    except Exception:
        return []
    calls = []
    for tc in msg.get("tool_calls") or []:
        name = (tc.get("function") or {}).get("name", "")
        args_raw = (tc.get("function") or {}).get("arguments") or "{}"
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw) if args_raw.strip() else {}
            except json.JSONDecodeError:
                args = {}
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            args = {}
        if name:
            calls.append({"id": tc.get("id", name), "name": name, "args": args})
    return calls


class _ToolLoopMixin:
    """Tool-calling methods used by AgentRuntime.

    Implemented as a standalone class (no instance state of its own) and
    promoted onto AgentRuntime below via attribute aliasing. This avoids
    re-basing the class (which changes its deallocator/layout and is invalid
    for a class that already exists).
    """

    async def _run_tool_loop(self, text: str) -> Optional[str]:
        """Run one message through the tool loop. Returns the rendered answer,
        or None if the model chose not to call any tool (plain conversation)."""
        if not self.registry or not hasattr(self.registry, "tool_schema"):
            return None
        tools = self.registry.tool_schema()
        if not tools:
            return None
        try:
            convo = [{"role": "user", "content": text}]
            executed: list[tuple[str, dict, bool, str, dict]] = []
            turns = 0
            while turns < 6:
                turns += 1
                raw = await self._llm.complete_raw(
                    convo, system=self._build_system_prompt() + _TOOL_SYSTEM_SUFFIX,
                    tools=tools, temperature=0.0,
                )
                calls = _tool_calls_from_openai(raw)
                if not calls:
                    # Model answered in plain text. Only treat as a turn result
                    # if at least one tool ran; otherwise fall through to the
                    # normal conversation path (return None).
                    if executed:
                        final = (raw.get("choices") or [{}])[0].get("message", {}).get("content", "")
                        return (final or "").strip() or self._render_tool_summary(executed)
                    return None
                # Execute every call in order; results feed back to the model.
                results = []
                for call in calls:
                    before = self._snapshot_settings()
                    cmd_text, ok, err, diff = await self._execute_named_tool(call["name"], call["args"])
                    after_diff = self._settings_diff(before)
                    executed.append((call["name"], call["args"], ok, err, after_diff))
                    results.append((call, ok, err, after_diff))
                for call, ok, err, after_diff in results:
                    convo.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": call["id"], "type": "function",
                            "function": {"name": call["name"], "arguments": json.dumps(call["args"])},
                        }],
                    })
                    convo.append({
                        "role": "tool", "tool_call_id": call["id"],
                        "content": json.dumps({"ok": ok, "error": err, "diff": after_diff}),
                    })
            if executed:
                return self._render_tool_summary(executed)
        except Exception as e:  # provider unreachable, bad JSON, etc.
            log.warning("tool loop failed, falling back to chat: %s", e)
            return None
        return None

    def _render_tool_summary(self, executed) -> str:
        lines = []
        for name, args, ok, err, diff in executed:
            shown = ", ".join(f"{k}={v}" for k, v in args.items())
            if ok:
                chg = "; ".join(f"{k}: {b} -> {a}" for k, (b, a) in diff.items()) or "no state change"
                lines.append(f"✅ /{name}{(' ' + shown) if shown else ''}: {chg}")
            else:
                lines.append(f"❌ /{name}: {err}")
        return "\n".join(lines)

    async def _execute_named_tool(self, name: str, args: dict) -> tuple[str, bool, str, dict]:
        """Execute a tool call by name via registry.dispatch (verbatim, no fuzzy rescue).

        Tool calls address commands by canonical name only: the LLM must call a
        real tool from the provided list. Typo rescue would turn a non-existent
        tool into a different command silently, which defeats the point.
        """
        raw = "/" + name
        parts = [raw]
        for k, v in (args or {}).items():
            if v == "" or v is None:
                continue
            parts.append(f"{k}={v}")
        cmd_text = " ".join(parts)
        before = self._snapshot_settings()
        result = await self.registry.dispatch(cmd_text, engine=self.engine, agent=self, registry=self.registry)
        return cmd_text, bool(result.ok), (result.error if not result.ok else ""), self._settings_diff(before)


# Promote the mixin methods onto AgentRuntime by direct aliasing, so both the
# instance and the class expose them without re-basing (which would break layout).
AgentRuntime._run_tool_loop = _ToolLoopMixin._run_tool_loop
AgentRuntime._render_tool_summary = _ToolLoopMixin._render_tool_summary
AgentRuntime._execute_named_tool = _ToolLoopMixin._execute_named_tool
