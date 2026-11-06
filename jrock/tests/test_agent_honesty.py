"""Tests for agent honesty: state isolation, fuzzy command matching, intent routing.

Root cause these lock down: the bot once claimed "Risk per trade set to 20%"
without executing anything (natural-language input fell through to the LLM,
which has no execution ability), and test runs overwrote the user's real
settings.json. These tests pin the fixes.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from jrock.commands.registry import CommandDef, CommandRegistry, CommandResult
from jrock.engine.settings import EngineSettings, STATE_DIR


# ------------------------------------------------------------------ #
# Test-state isolation (conftest.py must protect real state)          #
# ------------------------------------------------------------------ #

def test_state_dir_isolated_from_real_state():
    """STATE_DIR must resolve to a temp dir, never the real jrock/state."""
    assert "jrock-test-state" in str(STATE_DIR)
    assert not str(STATE_DIR).endswith(str(Path("jrock/state")))


def test_settings_save_does_not_touch_real_state_file():
    """Saving settings in tests must not overwrite the real settings.json."""
    real = Path(__file__).resolve().parents[1] / "state" / "settings.json"
    before = real.read_text(encoding="utf-8") if real.exists() else None
    s = EngineSettings()
    s.update(risk_per_trade_pct=99)  # triggers save() into temp STATE_DIR
    after = real.read_text(encoding="utf-8") if real.exists() else None
    assert before == after, "test run overwrote the real settings.json"


# ------------------------------------------------------------------ #
# Natural-language intent routing → real command execution            #
# ------------------------------------------------------------------ #

class _FakeLLM:
    async def stream(self, messages, system=None):
        yield "ok"

    async def complete(self, messages, system=None, temperature=0.3):
        return "ok"


class _FakeEngine:
    def __init__(self):
        self._settings = EngineSettings()


def _make_agent():
    from jrock.agent.runtime import AgentRuntime
    eng = _FakeEngine()
    reg = CommandRegistryStub()
    return AgentRuntime(engine=eng, registry=reg, llm_config=None), eng


class CommandRegistryStub:
    def __init__(self):
        self.dispatched = []

    async def dispatch(self, raw, **ctx):
        self.dispatched.append(raw)
        # Simulate the real command handlers mutating settings.
        if "risk_per_trade_pct=" in raw:
            ctx["engine"]._settings.update(risk_per_trade_pct=raw.split("=")[1])
        return CommandResult(True, f"executed {raw}")


def test_intent_router_routes_risk_request_to_real_command():
    """'Risk per trade: 20' must execute a real command, not reach the LLM."""
    agent, eng = _make_agent()
    out = _run_handle(agent, "Risk per trade: 20")
    assert "risk_per_trade_pct=20" in agent.registry.dispatched[0]
    assert eng._settings.risk_per_trade_pct == 20.0
    assert "auto-executed" in out


def test_intent_router_ignores_general_conversation():
    agent, _ = _make_agent()
    _run_handle(agent, "What is the weather like?")
    assert agent.registry.dispatched == []  # went to LLM, not command dispatcher


def test_intent_router_margin_mode():
    agent, _ = _make_agent()
    _run_handle(agent, "set margin mode to CROSS")
    assert agent.registry.dispatched[0] == "/margin CROSS"


def _run_handle(agent, text):
    async def _one():
        chunks = []
        async for c in agent.handle(text):
            chunks.append(c)
        return "".join(chunks)
    return asyncio.run(_one())


# ------------------------------------------------------------------ #
# Honesty guard: LLM claims of state changes get corrected            #
# ------------------------------------------------------------------ #

def test_honesty_guard_corrects_false_change_claims():
    agent, _ = _make_agent()
    reply = "Great! Risk per trade has been set to 20% and saved."
    corr = agent._honesty_correction(reply)
    assert corr is not None
    assert "no setting has been changed" in corr.lower()


def test_honesty_guard_allows_command_recommendations():
    agent, _ = _make_agent()
    reply = "To set it, run: /settings risk_per_trade_pct=20"
    assert agent._honesty_correction(reply) is None


# ------------------------------------------------------------------ #
# Fuzzy command matching for typo'd commands                          #
# ------------------------------------------------------------------ #

def test_fuzzy_match_rescues_typo_command():
    reg = CommandRegistry()

    async def _noop(*a, **k):
        return CommandResult(True, "ok")

    reg.register(CommandDef("code_review", _noop, "review code"))
    res = asyncio.run(reg.dispatch("/codereveiew"))
    assert res.ok, f"typo'd command not rescued: {res.error}"
