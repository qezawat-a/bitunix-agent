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


@pytest.mark.parametrize("reply", [
    "Done!",
    "All set, everything is 5x",
    "Margin updated",
    "Your risk is now 20%",
    "Switched to CROSS successfully",
    "Trading is now enabled",
    "Your balance is $5,200",
    "You have 3 positions open",
])
def test_honesty_guard_catches_broadened_blind_spots(reply):
    """The broadened markers must catch the 'done/all set/updated' lies."""
    agent, _ = _make_agent()
    corr = agent._honesty_correction(reply)
    assert corr is not None, f"blind spot not caught: {reply!r}"
    assert "correction" in corr.lower()


def test_router_handles_long_message_no_cap():
    """Requests >120 chars used to be silently dropped to the LLM."""
    agent, eng = _make_agent()
    out = _run_handle(agent, "Please " + "x" * 150 + " risk 5")
    assert "risk_per_trade_pct=5" in agent.registry.dispatched[0]
    assert eng._settings.risk_per_trade_pct == 5.0


def test_router_bare_risk_routes():
    """Bare 'risk 5' used to miss the router entirely."""
    agent, eng = _make_agent()
    _run_handle(agent, "Risk 5")
    assert eng._settings.risk_per_trade_pct == 5.0


def test_router_negation_never_executes():
    agent, eng = _make_agent()
    before = eng._settings.risk_per_trade_pct
    out = _run_handle(agent, "Dont set risk 5")
    assert eng._settings.risk_per_trade_pct == before
    assert "not taken" in out.lower()


def test_unparseable_mutation_clarifies_not_llm():
    """A mutation we can't parse must clarify, never hand off to the LLM."""
    agent, _ = _make_agent()
    out = _run_handle(agent, "Change something to make it better")
    assert agent.registry.dispatched == []
    assert "no state was changed" in out.lower()


def test_dispatch_verification_passes_when_mutated():
    """When the handler actually mutates, verification stays silent."""
    agent, eng = _make_agent()
    before = agent._snapshot_settings()
    result = asyncio.run(agent.registry.dispatch("/settings risk_per_trade_pct=20",
                                                  engine=eng, agent=agent, registry=agent.registry))
    from jrock.commands.registry import CommandResult
    assert agent._verify_dispatch("/settings risk_per_trade_pct=20", result, before) is None


def test_dispatch_verification_warns_on_no_op():
    """Anti-lie: handler says success but field unchanged → warning."""
    agent, eng = _make_agent()
    from jrock.commands.registry import CommandResult
    lying = CommandResult(True, "executed /settings risk_per_trade_pct=20")
    before = agent._snapshot_settings()  # engine still at 1.0, never mutated
    warn = agent._verify_dispatch("/settings risk_per_trade_pct=20", lying, before)
    assert warn is not None and "did not change" in warn


def test_fabricated_balance_corrected():
    agent, _ = _make_agent()
    corr = agent._fabricated_data_correction("Your balance is $5,200")
    assert corr is not None and "cannot verify" in corr.lower()


def test_fabricated_percent_corrected():
    agent, _ = _make_agent()
    assert agent._fabricated_data_correction("You gained 42% today") is not None


def test_fabricated_model_corrected():
    agent, _ = _make_agent()
    assert agent._fabricated_data_correction("Using gpt-4o-mini for this") is not None


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
