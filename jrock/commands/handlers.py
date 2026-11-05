"""All slash-command handler implementations.

Every handler is an async function that returns a CommandResult.
Handlers are pure: they receive the engine/agent as injected context, never
as globals, so tests can inject mocks.

Registration is done in build_registry() at the bottom of this module.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from .registry import CommandRegistry, CommandDef, CommandResult

if TYPE_CHECKING:
    from jrock.engine.engine import Engine
    from jrock.agent.runtime import AgentRuntime


def _ok(text: str, data: Any = None) -> CommandResult:
    return CommandResult(ok=True, text=text, data=data)


def _err(msg: str) -> CommandResult:
    return CommandResult(ok=False, text="", error=msg)


# ------------------------------------------------------------------ #
# Agent meta commands                                                 #
# ------------------------------------------------------------------ #

async def cmd_help(category: str = "", *, registry: CommandRegistry, **_) -> CommandResult:
    return _ok(registry.help_text(category or None))


async def cmd_models(*, engine: "Engine", agent: "AgentRuntime", **_) -> CommandResult:
    discovered = agent.discovered_models
    if not discovered:
        return _ok("No models discovered yet. Run /set_model base_url=... api_key=... to scan.")
    lines = ["**Discovered models:**"]
    for m in discovered[:20]:
        status = "✅" if m.working else "❌"
        lines.append(f"  {status} `{m.id}` latency={m.latency_ms:.0f}ms ctx={m.context_window}")
    current = engine._settings.model_id
    if current:
        lines.append(f"\nActive model: `{current}`")
    return _ok("\n".join(lines), data=discovered)


async def cmd_set_model(
    model_id: str = "",
    base_url: str = "",
    api_key: str = "",
    *,
    engine: "Engine",
    agent: "AgentRuntime",
    **_,
) -> CommandResult:
    if base_url or api_key:
        # Run AutoSetModelByKey discovery.
        from jrock.agent.autoset import discover_models
        b = base_url or agent.llm_config.base_url
        k = api_key or agent.llm_config.api_key
        import asyncio
        discovered = await discover_models(b, k)
        agent.discovered_models = discovered
        if not discovered:
            return _err("No working models found for the given key/endpoint.")
        if not model_id:
            model_id = discovered[0].id
        agent.llm_config.base_url = b
        agent.llm_config.api_key = k
        agent.llm_config.model_id = model_id
        await engine.update_settings(model_id=model_id, base_url=b)
        return _ok(f"Discovered {len([m for m in discovered if m.working])} working models. Active: `{model_id}`")
    if model_id:
        agent.llm_config.model_id = model_id
        await engine.update_settings(model_id=model_id)
        return _ok(f"Model set to `{model_id}`")
    return _err("Usage: /set_model [model_id] [base_url=...] [api_key=...]")


async def cmd_connect_provider(
    provider: str = "",
    base_url: str = "",
    api_key: str = "",
    *,
    agent: "AgentRuntime",
    **_,
) -> CommandResult:
    if not (base_url and api_key):
        return _err("Usage: /connect_provider provider=openai base_url=... api_key=...")
    agent.llm_config.base_url = base_url
    agent.llm_config.api_key = api_key
    agent.llm_config.provider = provider or "openai"
    return _ok(f"Provider `{provider or 'openai'}` configured at `{base_url}`")


async def cmd_think(
    *args,
    agent: "AgentRuntime",
    engine: "Engine",
    **_,
) -> CommandResult:
    """Deep-think mode: agent reasons step-by-step before acting."""
    topic = " ".join(args) if args else "current market conditions and open positions"
    prompt = f"Think deeply and reason step-by-step about: {topic}\nBe explicit about uncertainty. State what you know vs what you infer."
    reply = await agent.think(prompt)
    return _ok(reply)


async def cmd_memory(
    action: str = "list",
    *args,
    agent: "AgentRuntime",
    **_,
) -> CommandResult:
    if action == "list":
        memories = agent.memory.list()
        if not memories:
            return _ok("Memory is empty.")
        lines = [f"  [{i+1}] {m['key']}: {str(m['value'])[:80]}" for i, m in enumerate(memories)]
        return _ok("**Memory:**\n" + "\n".join(lines), data=memories)
    if action == "add" and args:
        key = args[0]
        val = " ".join(args[1:]) if len(args) > 1 else ""
        agent.memory.set(key, val)
        return _ok(f"Remembered: `{key}` = `{val}`")
    if action == "clear":
        agent.memory.clear()
        return _ok("Memory cleared.")
    return _err("Usage: /memory [list|add key value|clear]")


async def cmd_soul(
    action: str = "show",
    *args,
    agent: "AgentRuntime",
    **_,
) -> CommandResult:
    if action == "show":
        return _ok(f"**Active soul:** `{agent.soul_name}`\n\n{agent.soul_text[:800]}")
    if action == "list":
        souls = agent.list_souls()
        return _ok("**Available souls:**\n" + "\n".join(f"  - {s}" for s in souls))
    if action == "switch" and args:
        ok = agent.switch_soul(args[0])
        if ok:
            return _ok(f"Soul switched to `{args[0]}`")
        return _err(f"Soul `{args[0]}` not found. Use /soul list to see available souls.")
    return _err("Usage: /soul [show|list|switch <name>]")


async def cmd_add_soul(name: str = "", *, agent: "AgentRuntime", **_) -> CommandResult:
    if not name:
        return _err("Usage: /add_soul <name>  (then paste soul content on the next line)")
    return _ok(f"Ready to receive soul `{name}`. Paste the soul content now.")


async def cmd_skill(action: str = "list", *args, agent: "AgentRuntime", **_) -> CommandResult:
    if action == "list":
        skills = agent.list_skills()
        if not skills:
            return _ok("No skills loaded.")
        return _ok("**Skills:**\n" + "\n".join(f"  - `{s['name']}`: {s['description']}" for s in skills))
    if action == "use" and args:
        result = await agent.use_skill(args[0])
        return _ok(result)
    return _err("Usage: /skill [list|use <name>]")


async def cmd_add_skill(name: str = "", *, agent: "AgentRuntime", **_) -> CommandResult:
    if not name:
        return _err("Usage: /add_skill <name>")
    return _ok(f"Ready to receive skill `{name}`. Paste the skill markdown now.")


async def cmd_add_mcp(url: str = "", name: str = "", *, agent: "AgentRuntime", **_) -> CommandResult:
    if not url:
        return _err("Usage: /add_mcp url=<mcp-server-url> [name=<alias>]")
    agent.mcp_servers[name or url] = url
    return _ok(f"MCP server registered: `{name or url}` at `{url}`")


async def cmd_mcp(action: str = "list", *args, agent: "AgentRuntime", **_) -> CommandResult:
    if action == "list":
        if not agent.mcp_servers:
            return _ok("No MCP servers registered.")
        return _ok("**MCP servers:**\n" + "\n".join(f"  - `{k}`: {v}" for k, v in agent.mcp_servers.items()))
    return _err("Usage: /mcp [list]")


# ------------------------------------------------------------------ #
# Self-edit / diagnosis                                               #
# ------------------------------------------------------------------ #

async def cmd_code_review(
    *args,
    agent: "AgentRuntime",
    engine: "Engine",
    **_,
) -> CommandResult:
    """Ask agent to review and optionally fix code in the project."""
    target = " ".join(args) if args else "the engine directory"
    reply = await agent.code_review(target)
    return _ok(reply)


async def cmd_bug_fix(
    *args,
    agent: "AgentRuntime",
    **_,
) -> CommandResult:
    description = " ".join(args) if args else ""
    if not description:
        return _err("Usage: /bug_fix <describe the bug>")
    reply = await agent.bug_fix(description)
    return _ok(reply)


async def cmd_verify(
    *args,
    agent: "AgentRuntime",
    **_,
) -> CommandResult:
    target = " ".join(args) if args else "all"
    reply = await agent.verify(target)
    return _ok(reply)


async def cmd_simplify(
    *args,
    agent: "AgentRuntime",
    **_,
) -> CommandResult:
    target = " ".join(args) if args else ""
    if not target:
        return _err("Usage: /simplify <file or module>")
    reply = await agent.simplify(target)
    return _ok(reply)


async def cmd_check_ai(*, agent: "AgentRuntime", engine: "Engine", **_) -> CommandResult:
    lines = [
        "**Agent self-check:**",
        f"  Model: `{agent.llm_config.model_id or 'not set'}`",
        f"  Provider: `{agent.llm_config.provider}`",
        f"  Base URL: `{agent.llm_config.base_url or 'default'}`",
        f"  Soul: `{agent.soul_name}`",
        f"  Skills loaded: {len(agent.list_skills())}",
        f"  Memory entries: {len(agent.memory.list())}",
        f"  Auto-accept: {'ON' if agent.auto_accept else 'OFF'}",
        f"  MCP servers: {len(agent.mcp_servers)}",
    ]
    return _ok("\n".join(lines))


async def cmd_check_system(*, agent: "AgentRuntime", engine: "Engine", **_) -> CommandResult:
    """Check for bugs/issues in the engine and agent codebase."""
    import subprocess
    results = []
    proj = Path(__file__).resolve().parents[2]
    for pkg in ["engine", "adapters", "strategies", "risk", "agent", "commands"]:
        d = proj / pkg
        if not d.exists():
            continue
        for f in sorted(d.glob("*.py")):
            r = subprocess.run(
                [sys.executable, "-m", "py_compile", str(f)],
                capture_output=True, text=True
            )
            if r.returncode != 0:
                results.append(f"  ❌ `{f.name}`: {r.stderr.strip()[:200]}")
            else:
                results.append(f"  ✅ `{f.name}`")
    if not results:
        return _ok("No Python files found to check.")
    ok_count = sum(1 for r in results if "✅" in r)
    err_count = len(results) - ok_count
    summary = f"**System check:** {ok_count} OK, {err_count} errors\n"
    return _ok(summary + "\n".join(results))


async def cmd_autopsy(
    *args,
    agent: "AgentRuntime",
    engine: "Engine",
    **_,
) -> CommandResult:
    """Analyze the trade journal for patterns and failures."""
    from jrock.engine.executor import JOURNAL
    if not JOURNAL.exists():
        return _ok("No trade journal found yet.")
    lines = JOURNAL.read_text(encoding="utf-8").splitlines()[-200:]
    text = "\n".join(lines)
    prompt = f"Analyze this trade journal. Identify failure patterns, missed opportunities, and recurring errors. Be specific:\n\n{text}"
    reply = await agent.think(prompt)
    return _ok(reply)


async def cmd_diagnose(
    *args,
    agent: "AgentRuntime",
    engine: "Engine",
    **_,
) -> CommandResult:
    """Diagnose a specific issue in the engine or agent."""
    description = " ".join(args) if args else "recent errors"
    from jrock.engine.executor import JOURNAL
    recent = ""
    if JOURNAL.exists():
        lines = JOURNAL.read_text(encoding="utf-8").splitlines()[-50:]
        recent = "\n".join(lines)
    prompt = f"Diagnose this issue: {description}\n\nRecent journal:\n{recent}\n\nProvide root cause analysis and a concrete fix."
    reply = await agent.think(prompt)
    return _ok(reply)


async def cmd_team_spawn(
    *args,
    agent: "AgentRuntime",
    **_,
) -> CommandResult:
    """Spawn sub-agents for parallel tasks (experimental)."""
    tasks = list(args)
    if not tasks:
        return _err("Usage: /team_spawn task1 task2 ...")
    results = []
    for task in tasks:
        r = await agent.think(f"Complete this sub-task and return a concise result: {task}")
        results.append(f"  [{task[:30]}]: {r[:200]}")
    return _ok("**Team results:**\n" + "\n".join(results))


# ------------------------------------------------------------------ #
# Engine trading commands                                             #
# ------------------------------------------------------------------ #

async def cmd_engine(
    action: str = "status",
    *,
    engine: "Engine",
    **_,
) -> CommandResult:
    action = action.lower()
    if action == "on":
        msg = await engine.start()
        return _ok(msg)
    if action == "off":
        msg = await engine.stop()
        return _ok(msg)
    if action == "status":
        s = await engine.status()
        return _ok(s.render(), data=s)
    return _err("Usage: /engine [on|off|status]")


async def cmd_status(*, engine: "Engine", **_) -> CommandResult:
    s = await engine.status()
    return _ok(s.render(), data=s)


async def cmd_autotrade(
    action: str = "status",
    *,
    engine: "Engine",
    **_,
) -> CommandResult:
    action = action.lower()
    if action in ("on", "start"):
        return _ok(await engine.autotrade_on())
    if action in ("off", "stop"):
        return _ok(await engine.autotrade_off())
    s = await engine.status()
    return _ok(f"Autotrade: {'ON' if s.autotrade else 'OFF'}")


async def cmd_scan(
    action: str = "status",
    *,
    engine: "Engine",
    **_,
) -> CommandResult:
    action = action.lower()
    if action == "on":
        return _ok(await engine.scan_on())
    if action == "off":
        return _ok(await engine.scan_off())
    if action == "now":
        signals = await engine.scan_once()
        if not signals:
            return _ok("Scan complete. No qualifying signals.")
        lines = [f"  {s}" for s in signals[:10]]
        return _ok(f"**Scan results ({len(signals)} signals):**\n" + "\n".join(lines), data=signals)
    return _err("Usage: /scan [on|off|now]")


async def cmd_report(
    action: str = "status",
    interval: str = "",
    *,
    engine: "Engine",
    **_,
) -> CommandResult:
    action = action.lower()
    if action == "on":
        engine._settings.report_enabled = True
        engine._settings.save()
        return _ok("Report ON.")
    if action == "off":
        engine._settings.report_enabled = False
        engine._settings.save()
        return _ok("Report OFF.")
    if action == "interval" and interval:
        await engine.update_settings(report_interval_sec=interval)
        return _ok(f"Report interval set to {interval}s.")
    pnl = await engine.pnl_report()
    return _ok(
        f"**Report:**\n"
        f"  Equity: {pnl.get('equity', '?')}\n"
        f"  Available: {pnl.get('available', '?')}\n"
        f"  Unrealized PnL: {pnl.get('unrealized_pnl', '?')}\n"
        f"  Open positions: {pnl.get('open_positions', '?')}",
        data=pnl,
    )


async def cmd_pnl(*, engine: "Engine", **_) -> CommandResult:
    data = await engine.pnl_report()
    if "error" in data:
        return _err(data["error"])
    return _ok(
        f"Equity: {data['equity']:.2f}  Available: {data['available']:.2f}\n"
        f"Unrealized PnL: {data['unrealized_pnl']:.2f}  Positions: {data['open_positions']}",
        data=data,
    )


async def cmd_trades_history(limit: str = "50", *, engine: "Engine", **_) -> CommandResult:
    trades = await engine.get_orders()
    if not trades:
        return _ok("No trade history found.")
    lines = []
    for t in trades[:int(limit)]:
        sym = t.get("symbol", "?")
        side = t.get("side", "?")
        qty = t.get("qty", "?")
        price = t.get("avgPrice") or t.get("price") or "?"
        status = t.get("status", "?")
        lines.append(f"  {sym} {side} {qty} @ {price} [{status}]")
    return _ok(f"**Trades ({len(lines)}):**\n" + "\n".join(lines), data=trades)


async def cmd_tpsl_history(limit: str = "50", *, engine: "Engine", **_) -> CommandResult:
    data = await engine.get_tpsl()
    if not data:
        return _ok("No TP/SL history found.")
    lines = []
    for t in data[:int(limit)]:
        sym = t.get("symbol", "?")
        tp = t.get("tpPrice", "—")
        sl = t.get("slPrice", "—")
        status = t.get("status", "?")
        lines.append(f"  {sym} TP={tp} SL={sl} [{status}]")
    return _ok(f"**TPSL history ({len(lines)}):**\n" + "\n".join(lines))


async def cmd_positions(*, engine: "Engine", **_) -> CommandResult:
    positions = await engine.get_positions()
    if not positions:
        return _ok("No open positions.")
    lines = []
    for p in positions:
        sym = p.get("symbol", "?")
        side = p.get("side", "?")
        qty = p.get("qty") or p.get("size") or "?"
        entry = p.get("avgOpenPrice") or p.get("entryPrice") or "?"
        pnl = p.get("unrealizedPnl") or p.get("pnl") or "?"
        pid = p.get("positionId", "")
        lines.append(f"  {sym} {side} qty={qty} entry={entry} pnl={pnl} id={pid}")
    return _ok(f"**Open positions ({len(lines)}):**\n" + "\n".join(lines), data=positions)


async def cmd_close(
    symbol: str = "",
    position_id: str = "",
    *,
    engine: "Engine",
    **_,
) -> CommandResult:
    if not symbol:
        return _err("Usage: /close <symbol> [position_id=...]")
    result = await engine.close_position(symbol, position_id)
    if isinstance(result, dict) and "error" in result:
        return _err(result["error"])
    return _ok(f"Position closed: {symbol}")


async def cmd_settings(
    *args,
    engine: "Engine",
    **_,
) -> CommandResult:
    """Show or update engine settings."""
    if not args:
        return _ok(engine._settings.summary())
    # Parse key=value pairs from args.
    kwargs = {}
    for a in args:
        if "=" in a:
            k, _, v = a.partition("=")
            kwargs[k.strip()] = v.strip()
    if not kwargs:
        return _err("Usage: /settings [key=value ...]")
    changed = await engine.update_settings(**kwargs)
    if not changed:
        return _ok("No settings changed (values were already set).")
    return _ok(f"Updated: {', '.join(changed)}")


async def cmd_leverage(
    value: str = "",
    symbol: str = "",
    *,
    engine: "Engine",
    **_,
) -> CommandResult:
    if not value:
        return _ok(f"Current leverage: {engine._settings.leverage}x")
    await engine.update_settings(leverage=value)
    if symbol:
        try:
            await engine._client.change_leverage(symbol, int(value))
        except Exception as e:
            return _ok(f"Settings updated but live set failed: {e}")
    return _ok(f"Leverage set to {value}x")


async def cmd_symbols(
    *syms,
    engine: "Engine",
    **_,
) -> CommandResult:
    if not syms:
        return _ok(f"Active symbols: {', '.join(engine._settings.symbols)}")
    await engine.update_settings(symbols=",".join(syms))
    return _ok(f"Symbols set to: {', '.join(syms)}")


async def cmd_margin(
    mode: str = "",
    symbol: str = "",
    *,
    engine: "Engine",
    **_,
) -> CommandResult:
    if not mode:
        return _ok(f"Current margin mode: {engine._settings.margin_mode}")
    mode = mode.upper()
    if mode not in ("ISOLATED", "CROSS"):
        return _err("Mode must be ISOLATED or CROSS")
    await engine.update_settings(margin_mode=mode)
    if symbol:
        try:
            await engine._client.change_margin_mode(symbol, mode)
        except Exception as e:
            return _ok(f"Settings updated but live set failed: {e}")
    return _ok(f"Margin mode set to {mode}")


async def cmd_reversal(
    value: str = "",
    confidence: str = "",
    *,
    engine: "Engine",
    **_,
) -> CommandResult:
    kwargs = {}
    if value:
        kwargs["reversal_enabled"] = value
    if confidence:
        kwargs["reversal_confidence"] = confidence
    if kwargs:
        await engine.update_settings(**kwargs)
        return _ok(f"Reversal updated: {kwargs}")
    return _ok(
        f"Reversal: {'ON' if engine._settings.reversal_enabled else 'OFF'}  "
        f"Confidence: {engine._settings.reversal_confidence}"
    )


async def cmd_mid_management(symbol: str = "", *, engine: "Engine", **_) -> CommandResult:
    if not symbol:
        syms = engine._settings.symbols
        msgs = []
        for s in syms:
            msgs.append(await engine.trigger_mid_management(s))
        return _ok("\n".join(msgs))
    return _ok(await engine.trigger_mid_management(symbol))


# ------------------------------------------------------------------ #
# Registry builder                                                    #
# ------------------------------------------------------------------ #

def build_registry() -> CommandRegistry:
    reg = CommandRegistry()

    cmds = [
        # Agent
        CommandDef("help", cmd_help, "Show this help", aliases=["h", "?"], category="agent"),
        CommandDef("models", cmd_models, "List discovered models", category="agent"),
        CommandDef("set_model", cmd_set_model, "Set active model or auto-discover",
                   args_schema="[model_id] [base_url=...] [api_key=...]", category="agent"),
        CommandDef("connect_provider", cmd_connect_provider, "Configure an LLM provider",
                   args_schema="provider=openai base_url=... api_key=...", category="agent"),
        CommandDef("think", cmd_think, "Deep reasoning on a topic", args_schema="[topic]", category="agent"),
        CommandDef("memory", cmd_memory, "Manage agent memory", args_schema="[list|add|clear]", category="agent"),
        CommandDef("soul", cmd_soul, "Show/switch agent soul/persona", args_schema="[show|list|switch <name>]", category="agent"),
        CommandDef("add_soul", cmd_add_soul, "Install a new soul", args_schema="<name>", category="agent"),
        CommandDef("skill", cmd_skill, "List/use skills", args_schema="[list|use <name>]", category="agent"),
        CommandDef("add_skill", cmd_add_skill, "Add a new skill", args_schema="<name>", category="agent"),
        CommandDef("add_mcp", cmd_add_mcp, "Register an MCP server", args_schema="url=... [name=...]", category="agent"),
        CommandDef("mcp", cmd_mcp, "List MCP servers", category="agent"),
        # Self-edit / diagnosis
        CommandDef("code_review", cmd_code_review, "Review and optionally fix code", args_schema="[target]", category="selfheal",
                   aliases=["cr", "fixcodes"]),
        CommandDef("bug_fix", cmd_bug_fix, "Describe a bug to fix", args_schema="<description>", category="selfheal"),
        CommandDef("verify", cmd_verify, "Verify code correctness", args_schema="[target]", category="selfheal"),
        CommandDef("simplify", cmd_simplify, "Simplify a module", args_schema="<file>", category="selfheal"),
        CommandDef("check_ai", cmd_check_ai, "Agent self-check", category="selfheal"),
        CommandDef("check_system", cmd_check_system, "Check engine code for syntax errors", category="selfheal"),
        CommandDef("autopsy", cmd_autopsy, "Analyze trade journal for patterns", category="selfheal"),
        CommandDef("diagnose", cmd_diagnose, "Diagnose a specific issue", args_schema="[description]", category="selfheal"),
        CommandDef("team_spawn", cmd_team_spawn, "Spawn parallel sub-agents", args_schema="task1 task2 ...", category="selfheal"),
        # Engine
        CommandDef("engine", cmd_engine, "Engine control", args_schema="[on|off|status]", category="engine"),
        CommandDef("status", cmd_status, "Engine status", aliases=["s"], category="engine"),
        CommandDef("autotrade", cmd_autotrade, "Toggle autotrade", args_schema="[on|off]", category="engine"),
        CommandDef("scan", cmd_scan, "Scanner control", args_schema="[on|off|now]", category="engine"),
        CommandDef("report", cmd_report, "Reporting control", args_schema="[on|off|interval N]", category="engine"),
        CommandDef("pnl", cmd_pnl, "Show current PnL", aliases=["p"], category="engine"),
        CommandDef("trades", cmd_trades_history, "Trade history", args_schema="[limit]", aliases=["trades_history"], category="engine"),
        CommandDef("tpsl_history", cmd_tpsl_history, "TPSL history", args_schema="[limit]", category="engine"),
        CommandDef("positions", cmd_positions, "Open positions", aliases=["pos", "positions_history"], category="engine"),
        CommandDef("close", cmd_close, "Close a position", args_schema="<symbol> [position_id=...]", category="engine"),
        CommandDef("settings", cmd_settings, "Show/update settings", args_schema="[key=value ...]", category="engine"),
        CommandDef("leverage", cmd_leverage, "Get/set leverage", args_schema="[value] [symbol=...]", category="engine"),
        CommandDef("symbols", cmd_symbols, "Get/set active symbols", args_schema="[SYM1 SYM2 ...]", category="engine"),
        CommandDef("margin", cmd_margin, "Get/set margin mode", args_schema="[ISOLATED|CROSS] [symbol=...]", category="engine"),
        CommandDef("reversal", cmd_reversal, "Reversal settings", args_schema="[true|false] [confidence=N]", category="engine"),
        CommandDef("mid_management", cmd_mid_management, "Trigger mid-position management", args_schema="[symbol]", category="engine"),
    ]

    for c in cmds:
        reg.register(c)

    return reg
