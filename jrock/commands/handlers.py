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
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from .registry import CommandRegistry, CommandDef, CommandResult

if TYPE_CHECKING:
    from jrock.engine.engine import Engine
    from jrock.agent.runtime import AgentRuntime

from jrock.agent.git_push import GitPusher


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


async def cmd_automodelsetbykey(
    base_url: str = "",
    api_key: str = "",
    max_probe: str = "20",
    *,
    engine: "Engine",
    agent: "AgentRuntime",
    **_,
) -> CommandResult:
    """AutoSetModelByKey: probe all models for the given key and set the best one.

    Usage: /automodelsetbykey [base_url=...] [api_key=...] [max_probe=20]
    Falls back to the agent's current LLM config if base_url/api_key omitted.
    """
    b = base_url or agent.llm_config.base_url
    k = api_key or agent.llm_config.api_key
    if not b or not k:
        return _err(
            "No base_url or api_key provided, and none configured in LLM config. "
            "Usage: /automodelsetbykey base_url=... api_key=... [max_probe=20]"
        )

    try:
        mp = int(max_probe)
    except ValueError:
        mp = 20

    from jrock.agent.autoset import discover_models

    discovered = await discover_models(b, k, max_probe=mp)
    agent.discovered_models = discovered

    working = [m for m in discovered if m.working]
    if not working:
        return _err(
            f"No working models found for key at {b}. "
            f"Probed {len(discovered)} candidates, all failed."
        )

    best = working[0]
    agent.llm_config.base_url = b
    agent.llm_config.api_key = k
    agent.llm_config.model_id = best.id
    await engine.update_settings(model_id=best.id, base_url=b)

    lines = [
        f"✅ AutoSetModelByKey complete: `{best.id}`",
        f"   Latency: {best.latency_ms:.0f}ms  Context: {best.context_window}  Score: {best.score:.1f}",
        f"   Working models: {len(working)}/{len(discovered)}",
    ]
    for m in working[1:6]:
        lines.append(f"   ✅ `{m.id}` latency={m.latency_ms:.0f}ms score={m.score:.1f}")
    if len(working) > 6:
        lines.append(f"   ... and {len(working) - 6} more")
    return _ok("\n".join(lines), data=discovered)


async def cmd_connect_provider(
    provider: str = "",
    base_url: str = "",
    api_key: str = "",
    *,
    engine: "Engine",
    agent: "AgentRuntime",
    **_,
) -> CommandResult:
    if not (base_url and api_key):
        return _err("Usage: /connect_provider provider=openai base_url=... api_key=...")
    agent.llm_config.base_url = base_url
    agent.llm_config.api_key = api_key
    agent.llm_config.provider = provider or "openai"
    # Auto-discover models for the new provider.
    try:
        from jrock.agent.autoset import discover_models
        discovered = await discover_models(base_url, api_key)
        agent.discovered_models = discovered
        working = [m for m in discovered if m.working]
        if working:
            agent.llm_config.model_id = working[0].id
            await engine.update_settings(model_id=working[0].id, base_url=base_url)
            return _ok(
                f"Provider `{provider or 'openai'}` configured at `{base_url}`. "
                f"Auto-discovered {len(working)} working models. Active: `{working[0].id}`"
            )
    except Exception as e:
        pass
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


async def cmd_add_soul(name: str = "", content: str = "", *, agent: "AgentRuntime", **_) -> CommandResult:
    if not name:
        return _err("Usage: /add_soul <name> content=<markdown>")
    if not content:
        return _err("No content provided (content=...). Paste the soul markdown in content=.")
    try:
        from jrock.agent.runtime import SOULS_DIR
        SOULS_DIR.mkdir(parents=True, exist_ok=True)
        (SOULS_DIR / f"{name}.md").write_text(content, encoding="utf-8")
        agent._load_souls()
        return _ok(f"Soul `{name}` installed ({len(content)} chars).")
    except Exception as e:
        return _err(f"Failed to install soul: {e}")


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


async def cmd_add_skill(name: str = "", content: str = "", *, agent: "AgentRuntime", **_) -> CommandResult:
    if not name:
        return _err("Usage: /add_skill <name> content=<markdown>")
    if not content:
        return _err("No content provided (content=...). Paste the skill markdown in content=.")
    try:
        from jrock.agent.runtime import SKILLS_DIR
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        (SKILLS_DIR / f"{name}.md").write_text(content, encoding="utf-8")
        agent._load_skills()
        return _ok(f"Skill `{name}` installed ({len(content)} chars).")
    except Exception as e:
        return _err(f"Failed to install skill: {e}")


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
    # proj dir is the jrock package root (this file lives two levels up).
    proj = Path(__file__).resolve().parents[1]
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


async def cmd_git_push(
    *args,
    agent: "AgentRuntime",
    **_,
) -> CommandResult:
    """Force-push verified code assets to GitHub via PAT + repo link.

    Usage: /git_push [message="..."] [branch=main]
    Requires GITHUB_PAT and GITHUB_REPO env vars (set via /env or .env).
    """
    message = "chore(jrock): auto-push verified code assets"
    branch = "main"
    for a in args:
        if a.startswith("message="):
            message = a.split("=", 1)[1]
        elif a.startswith("branch="):
            branch = a.split("=", 1)[1]

    pusher = GitPusher()
    if not pusher.is_configured():
        return _err(
            "Git push not configured. Set GITHUB_PAT and GITHUB_REPO "
            "(or GIT_REPO_LINK) in .env / environment."
        )

    # Confirmation gate: a force-push must never run silently (unless auto-accept).
    if not agent.auto_accept:
        allowed = await agent.request_confirmation(f"Force-push to {branch} ({message})?")
        if not allowed:
            return _err("Force-push cancelled by user.")

    result = await pusher.push_verified_assets(message=message, branch=branch)
    if result.ok:
        return _ok(result.message, data=asdict(result))
    return _err(result.message)


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


async def cmd_cancels(
    symbol: str = "",
    *args,
    engine: "Engine",
    **_,
) -> CommandResult:
    """Cancel pending orders / TPSL orders for a symbol (or all if omitted)."""
    try:
        if symbol:
            res = await engine._client.cancel_all_orders(symbol)
            return _ok(f"All pending orders cancelled for {symbol}: {res}")
        res = await engine._client.cancel_all_orders()
        return _ok(f"All pending orders cancelled globally: {res}")
    except Exception as e:
        return _err(f"cancel failed: {e}")


async def cmd_positions_history(
    symbol: str = "",
    *args,
    engine: "Engine",
    **_,
) -> CommandResult:
    """Closed/history positions (distinct from /positions which shows open)."""
    try:
        rows = await engine._client.get_history_positions(symbol or None)
        if not rows:
            return _ok("No closed positions in history.")
        lines = ["**Closed positions:**"]
        for p in rows[:20]:
            sym = p.get("symbol", "?")
            side = p.get("side", "?")
            qty = p.get("qty", p.get("volume", "?"))
            pnl = p.get("realizedPnl", p.get("pnl", "?"))
            lines.append(f"  - {sym} {side} qty={qty} realizedPnl={pnl}")
        if len(rows) > 20:
            lines.append(f"  ... and {len(rows)-20} more")
        return _ok("\n".join(lines))
    except Exception as e:
        return _err(f"history fetch failed: {e}")


async def cmd_guard(
    value: str = "",
    engine: "Engine" = None,
    **kw,
) -> CommandResult:
    """Get/set the trailing/breakeven guard cadence (guard_interval_sec)."""
    if not value:
        return _ok(f"Guard interval: {getattr(engine._settings, 'guard_interval_sec', '?')}s")
    changed = await engine.update_settings(guard_interval_sec=value)
    if "guard_interval_sec" not in changed:
        return _ok(f"Guard interval already {engine._settings.guard_interval_sec}s.")
    return _ok(f"Guard interval set to {engine._settings.guard_interval_sec}s.")


async def cmd_breakeven(
    value: str = "",
    engine: "Engine" = None,
    **kw,
) -> CommandResult:
    """Get/set breakeven trigger (ROI %)."""
    if not value:
        return _ok(f"Breakeven threshold: {engine._settings.breakeven_threshold_pct}% ROI")
    changed = await engine.update_settings(breakeven_threshold_pct=value)
    if "breakeven_threshold_pct" not in changed:
        return _ok(f"Breakeven already {engine._settings.breakeven_threshold_pct}% ROI.")
    return _ok(f"Breakeven threshold set to {engine._settings.breakeven_threshold_pct}% ROI.")


async def cmd_trailing(
    trigger: str = "",
    distance: str = "",
    *,
    engine: "Engine",
    **_,
) -> CommandResult:
    """Get/set trailing: /trailing [trigger%] [distance%]"""
    if not trigger:
        return _ok(
            f"Trailing trigger: {engine._settings.trailing_stop_roi_pct}% ROI, "
            f"distance: {getattr(engine._settings, 'trailing_distance_pct', 2.0)}% (ROI%)"
        )
    upd = {}
    if trigger:
        upd["trailing_stop_roi_pct"] = trigger
    if distance:
        upd["trailing_distance_pct"] = distance
    changed = await engine.update_settings(**upd)
    return _ok(f"Trailing updated: {', '.join(changed) or 'no change'}")


async def cmd_lqdistance(
    value: str = "",
    engine: "Engine" = None,
    **kw,
) -> CommandResult:
    if not value:
        return _ok(f"Liquidation SL distance: {engine._settings.liq_sl_distance_pct}% of entry->liq")
    changed = await engine.update_settings(liq_sl_distance_pct=value)
    if "liq_sl_distance_pct" not in changed:
        return _ok(f"Liq SL distance already {engine._settings.liq_sl_distance_pct}%.")
    return _ok(f"Liq SL distance set to {engine._settings.liq_sl_distance_pct}%")


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
    **kwargs,
) -> CommandResult:
    """Show or update engine settings. Use /settings trade for the full snapshot."""
    # Handle /settings trade subcommand (registry only sees first token as cmd name).
    if args and args[0].lower() == "trade":
        return await cmd_settings_trade(engine=engine)
    if not args and not kwargs:
        return _ok(engine._settings.summary())
    if not kwargs:
        return _err("Usage: /settings [key=value ...]")
    changed = await engine.update_settings(**kwargs)
    # Honest verification: only keys that actually differ are reported as changed.
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
    before = engine._settings.leverage
    changed = await engine.update_settings(leverage=value)
    if "leverage" not in changed:
        return _ok(f"Leverage already {engine._settings.leverage}x (no change).")
    if symbol:
        try:
            await engine._client.change_leverage(symbol, int(value))
        except Exception as e:
            return _ok(f"Settings updated ({before}x → {value}x) but live set failed: {e}")
    return _ok(f"Leverage set to {value}x (was {before}x).")


async def cmd_symbols(
    *syms,
    engine: "Engine",
    **_,
) -> CommandResult:
    if not syms:
        return _ok(f"Active symbols: {', '.join(engine._settings.symbols)}")
    before = list(engine._settings.symbols)
    changed = await engine.update_settings(symbols=",".join(syms))
    if "symbols" not in changed:
        return _ok(f"Symbols already: {', '.join(engine._settings.symbols)} (no change).")
    return _ok(f"Symbols set to: {', '.join(engine._settings.symbols)} (was {', '.join(before)}).")


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
    # Spec uses ISOLATION/CROSS; settings stores ISOLATED/CROSS — accept both.
    alias = {"ISOLATION": "ISOLATED", "ISOLATED": "ISOLATED", "CROSS": "CROSS"}
    if mode not in alias:
        return _err("Mode must be ISOLATION/ISOLATED or CROSS")
    normalized = alias[mode]
    before = engine._settings.margin_mode
    changed = await engine.update_settings(margin_mode=normalized)
    if "margin_mode" not in changed:
        return _ok(f"Margin mode already {normalized} (no change).")
    if symbol:
        try:
            await engine._client.change_margin_mode(symbol, normalized)
        except Exception as e:
            return _ok(f"Settings updated ({before} → {normalized}) but live set failed: {e}")
    return _ok(f"Margin mode set to {normalized} (was {before}).")


async def cmd_max_positions(
    value: str = "",
    *,
    engine: "Engine",
    **_,
) -> CommandResult:
    """Get or set max open positions (concurrent position cap)."""
    if not value:
        return _ok(f"Max open positions: {engine._settings.max_open_positions}")
    try:
        new_val = int(value)
    except ValueError:
        return _err("Usage: /max_positions <number>")
    before = engine._settings.max_open_positions
    changed = await engine.update_settings(max_open_positions=new_val)
    if "max_open_positions" not in changed:
        return _ok(f"Max open positions already {new_val} (no change).")
    # Also keep executor risk limit in sync live.
    engine._executor._risk.max_open_positions = new_val
    return _ok(f"Max open positions set to {new_val} (was {before}).")


async def cmd_position_mode(
    mode: str = "",
    *,
    engine: "Engine",
    **_,
) -> CommandResult:
    """Get or set account-wide position mode (HEDGE / ONE_WAY)."""
    if not mode:
        return _ok(f"Position mode: {engine._settings.position_mode}")
    mode = mode.upper()
    if mode not in ("HEDGE", "ONE_WAY"):
        return _err("Mode must be HEDGE or ONE_WAY")
    before = engine._settings.position_mode
    changed = await engine.update_settings(position_mode=mode)
    if "position_mode" not in changed:
        return _ok(f"Position mode already {mode} (no change).")
    try:
        await engine._client.change_position_mode(mode)
    except Exception as e:
        return _ok(f"Settings updated ({before} → {mode}) but live set failed: {e}")
    return _ok(f"Position mode set to {mode} (was {before}).")


async def cmd_settings_trade(*, engine: "Engine", **_) -> CommandResult:
    """Real-time trading configuration snapshot (SECTION 4: Settings trade).

    Outputs: leverage, symbols, active PnL, price data, scanner parameters,
    and current active TPSL zones.
    """
    s = engine._settings
    try:
        account = await engine._client.get_account()
        equity = float(
            account.get("equity") or account.get("totalEquity") or
            account.get("available") or account.get("balance") or 0
        )
        available = float(account.get("available") or account.get("availableMargin") or equity)
        cross_upnl = float(account.get("crossUnrealizedPNL") or 0)
        iso_upnl = float(account.get("isolationUnrealizedPNL") or 0)
    except Exception as e:
        equity = available = cross_upnl = iso_upnl = 0.0

    try:
        positions = await engine._client.get_pending_positions()
        pos_pnl = sum(float(p.get("unrealizedPnl") or p.get("pnl") or 0) for p in positions)
    except Exception:
        positions = []
        pos_pnl = 0.0

    # Last known prices from the WS scanner (primary price feed).
    prices: dict[str, float] = {}
    try:
        prices = dict(engine._ws_scanner.prices)
    except Exception:
        prices = {}

    # Active TPSL zones from the last executed trades / TPSL history.
    tpsl_zones: list[dict] = []
    try:
        tpsl_hist = await engine._client.get_history_tpsl_orders(limit=10)
        items = tpsl_hist if isinstance(tpsl_hist, list) else tpsl_hist.get("orderList", [])
        for t in items[:5]:
            tpsl_zones.append({
                "symbol": t.get("symbol", "?"),
                "tp": t.get("tpPrice", "—"),
                "sl": t.get("slPrice", "—"),
                "status": t.get("status", "?"),
            })
    except Exception:
        tpsl_zones = []

    scanner = {
        "scan_enabled": s.scan_enabled,
        "scan_interval_sec": s.scan_interval_sec,
        "timeframes": s.timeframes,
        "min_confidence": s.min_confidence,
        "tf_min_confidence": s.tf_min_confidence,
        "min_agreeing_strategies": s.min_agreeing_strategies,
    }

    tpsl_cfg = {
        "breakeven_threshold_pct": s.breakeven_threshold_pct,
        "trailing_stop_roi_pct": s.trailing_stop_roi_pct,
        "liq_sl_distance_pct": s.liq_sl_distance_pct,
        "sl_stop_type": s.sl_stop_type,
    }

    lines = ["**⚙️ Settings Trade — Real-Time Config**"]
    lines.append(f"  Leverage: {s.leverage}x")
    lines.append(f"  Margin Mode: {s.margin_mode}  |  Position Mode: {s.position_mode}")
    lines.append(f"  Symbols: {', '.join(s.symbols)}")
    lines.append(f"  Max Open Positions: {s.max_open_positions}")
    lines.append(f"  Risk Per Trade: {s.risk_per_trade_pct}%")
    lines.append("")
    lines.append("**💰 Active PnL**")
    lines.append(f"  Equity: {equity:.2f}  |  Available: {available:.2f}")
    lines.append(f"  Unrealized PnL (positions): {pos_pnl:.2f}")
    lines.append(f"  Cross UPNL: {cross_upnl:.2f}  |  Isolation UPNL: {iso_upnl:.2f}")
    lines.append("")
    lines.append("**📈 Price Data (last from WS feed)**")
    if prices:
        for sym, px in prices.items():
            lines.append(f"  {sym}: {px:.4f}")
    else:
        lines.append("  (no live price feed yet — start scanner)")
    lines.append("")
    lines.append("**🔍 Scanner Parameters**")
    for k, v in scanner.items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("**🛡️ Active TPSL Zones**")
    if tpsl_zones:
        for z in tpsl_zones:
            lines.append(f"  {z['symbol']}  TP={z['tp']}  SL={z['sl']}  [{z['status']}]")
    else:
        lines.append("  (no TPSL history yet)")
    lines.append("")
    lines.append("**⚙️ TPSL Configuration**")
    for k, v in tpsl_cfg.items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append(f"  Autotrade: {'ON' if s.autotrade_enabled else 'OFF'}")
    lines.append(f"  Reversal: {'ON' if s.reversal_enabled else 'OFF'} (conf {s.reversal_confidence})")

    return _ok("\n".join(lines), data={
        "leverage": s.leverage,
        "margin_mode": s.margin_mode,
        "position_mode": s.position_mode,
        "symbols": s.symbols,
        "max_open_positions": s.max_open_positions,
        "risk_per_trade_pct": s.risk_per_trade_pct,
        "equity": equity,
        "available": available,
        "unrealized_pnl": pos_pnl,
        "prices": prices,
        "scanner": scanner,
        "tpsl_zones": tpsl_zones,
        "tpsl_config": tpsl_cfg,
        "autotrade_enabled": s.autotrade_enabled,
        "reversal_enabled": s.reversal_enabled,
        "reversal_confidence": s.reversal_confidence,
    })


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
        CommandDef("help", cmd_help, "Show this help", aliases=["h", "?"], category="agent", args_schema_props={}, args_schema_required=[]),
        CommandDef("models", cmd_models, "List discovered models", category="agent", args_schema_props={}, args_schema_required=[]),
        CommandDef("set_model", cmd_set_model, "Set active model or auto-discover",
                   args_schema="[model_id] [base_url=...] [api_key=...]", category="agent", args_schema_props={"model_id": "string"}, args_schema_required=[]),
        CommandDef("automodelsetbykey", cmd_automodelsetbykey,
                   "Auto-probe models by API key and set the best one",
                   args_schema="[base_url=...] [api_key=...] [max_probe=20]",
                   aliases=["autoset", "probe_models", "autodiscover"],
                   category="agent", args_schema_props={}, args_schema_required=[]),
        CommandDef("connect_provider", cmd_connect_provider, "Configure an LLM provider",
                   args_schema="provider=openai base_url=... api_key=...", category="agent", args_schema_props={}, args_schema_required=[]),
        CommandDef("think", cmd_think, "Deep reasoning on a topic", args_schema="[topic]", category="agent", args_schema_props={"topic": "string"}, args_schema_required=[]),
        CommandDef("memory", cmd_memory, "Manage agent memory", args_schema="[list|add|clear]", category="agent", args_schema_props={}, args_schema_required=[]),
        CommandDef("soul", cmd_soul, "Show/switch agent soul/persona", args_schema="[show|list|switch <name>]", category="agent", args_schema_props={"<name>": "string"}, args_schema_required=[]),
        CommandDef("add_soul", cmd_add_soul, "Install a new soul (name + content=...)", args_schema="<name>", category="agent", args_schema_props={}, args_schema_required=[]),
        CommandDef("skill", cmd_skill, "List/use skills", args_schema="[list|use <name>]", category="agent", args_schema_props={"<name>": "string"}, args_schema_required=[]),
        CommandDef("add_skill", cmd_add_skill, "Add a new skill (name + content=...)", args_schema="<name>", category="agent", args_schema_props={}, args_schema_required=[]),
        CommandDef("add_mcp", cmd_add_mcp, "Register an MCP server", args_schema="url=... [name=...]", category="agent", args_schema_props={}, args_schema_required=[]),
        CommandDef("mcp", cmd_mcp, "List MCP servers", category="agent", args_schema_props={}, args_schema_required=[]),
        # Self-edit / diagnosis
        CommandDef("code_review", cmd_code_review, "Review and optionally fix code", args_schema="[target]", category="selfheal",
                   aliases=["cr", "fixcodes"], args_schema_props={"target": "string"}, args_schema_required=[]),
        CommandDef("bug_fix", cmd_bug_fix, "Describe a bug to fix", args_schema="<description>", category="selfheal", args_schema_props={}, args_schema_required=[]),
        CommandDef("verify", cmd_verify, "Verify code correctness", args_schema="[target]", category="selfheal", args_schema_props={"target": "string"}, args_schema_required=[]),
        CommandDef("simplify", cmd_simplify, "Simplify a module", args_schema="<file>", category="selfheal", args_schema_props={}, args_schema_required=[]),
        CommandDef("check_ai", cmd_check_ai, "Agent self-check", category="selfheal", args_schema_props={}, args_schema_required=[]),
        CommandDef("check_system", cmd_check_system, "Check engine code for syntax errors", category="selfheal", args_schema_props={}, args_schema_required=[]),
        CommandDef("autopsy", cmd_autopsy, "Analyze trade journal for patterns", category="selfheal", args_schema_props={}, args_schema_required=[]),
        CommandDef("diagnose", cmd_diagnose, "Diagnose a specific issue", args_schema="[description]", category="selfheal", args_schema_props={"description": "string"}, args_schema_required=[]),
        CommandDef("team_spawn", cmd_team_spawn, "Spawn parallel sub-agents", args_schema="task1 task2 ...", category="selfheal", args_schema_props={}, args_schema_required=[]),
        CommandDef("git_push", cmd_git_push, "Force-push verified code assets to GitHub (PAT + repo link, args_schema_props={}, args_schema_required=[])", args_schema="[message=...] [branch=main]", category="selfheal"),
        # Engine
        CommandDef("engine", cmd_engine, "Engine control", args_schema="[on|off|status]", category="engine", args_schema_props={}, args_schema_required=[]),
        CommandDef("status", cmd_status, "Engine status", aliases=["s"], category="engine", args_schema_props={}, args_schema_required=[]),
        CommandDef("autotrade", cmd_autotrade, "Toggle autotrade", args_schema="[on|off]", category="engine", args_schema_props={}, args_schema_required=[]),
        CommandDef("scan", cmd_scan, "Scanner control", args_schema="[on|off|now]", category="engine", args_schema_props={}, args_schema_required=[]),
        CommandDef("report", cmd_report, "Reporting control", args_schema="[on|off|interval N]", category="engine", args_schema_props={"N": "string"}, args_schema_required=[]),
        CommandDef("pnl", cmd_pnl, "Show current PnL", aliases=["p"], category="engine", args_schema_props={}, args_schema_required=[]),
        CommandDef("trades", cmd_trades_history, "Trade history", args_schema="[limit]", aliases=["trades_history"], category="engine", args_schema_props={"limit": "string"}, args_schema_required=[]),
        CommandDef("tpsl_history", cmd_tpsl_history, "TPSL history", args_schema="[limit]", category="engine", args_schema_props={"limit": "string"}, args_schema_required=[]),
        CommandDef("positions", cmd_positions, "Open positions", aliases=["pos", "positions_history"], category="engine", args_schema_props={}, args_schema_required=[]),
        CommandDef("close", cmd_close, "Close a position", args_schema="<symbol> [position_id=...]", category="engine", args_schema_props={}, args_schema_required=[]),
        CommandDef("settings", cmd_settings, "Show/update settings", args_schema="[key=value ...]", category="engine", args_schema_props={"...": "string"}, args_schema_required=[]),
        CommandDef("settings trade", cmd_settings_trade, "Real-time trading config snapshot (leverage, symbols, PnL, price, scanner, TPSL zones, args_schema_props={}, args_schema_required=[])", category="engine", aliases=["settings_trade", "trade_settings"]),
        CommandDef("leverage", cmd_leverage, "Get/set leverage", args_schema="[value] [symbol=...]", category="engine", args_schema_props={"value": "string"}, args_schema_required=[]),
        CommandDef("symbols", cmd_symbols, "Get/set active symbols", args_schema="[SYM1 SYM2 ...]", category="engine", args_schema_props={"SYM1": "string", "SYM2": "string", "...": "string"}, args_schema_required=[]),
        CommandDef("margin", cmd_margin, "Get/set margin mode", args_schema="[ISOLATION|ISOLATED|CROSS] [symbol=...]", category="engine", args_schema_props={}, args_schema_required=[]),
        CommandDef("max_positions", cmd_max_positions, "Get/set max open positions cap", args_schema="<number>", category="engine", args_schema_props={}, args_schema_required=[]),
        CommandDef("position_mode", cmd_position_mode, "Get/set account position mode (HEDGE/ONE_WAY, args_schema_props={}, args_schema_required=[])", args_schema="[HEDGE|ONE_WAY]", category="engine"),
        CommandDef("reversal", cmd_reversal, "Reversal settings", args_schema="[true|false] [confidence=N]", category="engine", args_schema_props={}, args_schema_required=[]),
        CommandDef("mid_management", cmd_mid_management, "Trigger mid-position management", args_schema="[symbol]", category="engine", args_schema_props={"symbol": "string"}, args_schema_required=[]),
        CommandDef("cancels", cmd_cancels, "Cancel pending orders", args_schema="[symbol]", category="engine", args_schema_props={"symbol": "string"}, args_schema_required=[]),
        CommandDef("positions_history", cmd_positions_history, "Closed position history", args_schema="[symbol]", category="engine", args_schema_props={"symbol": "string"}, args_schema_required=[]),
        CommandDef("guard_interval", cmd_guard, "Guard interval (s)", args_schema="[seconds]", category="engine", args_schema_props={"seconds": "string"}, args_schema_required=[]),
        CommandDef("breakeven", cmd_breakeven, "Breakeven threshold (ROI %)", args_schema="[percent]", category="engine", args_schema_props={"percent": "string"}, args_schema_required=[]),
        CommandDef("trailing", cmd_trailing, "Trailing trigger/distance (ROI %)", args_schema="[trigger%] [distance%]", category="engine", args_schema_props={"trigger%": "string", "distance%": "string"}, args_schema_required=[]),
        CommandDef("liqdistance", cmd_lqdistance, "Liquidation SL distance %", args_schema="[percent]", category="engine", args_schema_props={"percent": "string"}, args_schema_required=[]),
    ]

    for c in cmds:
        reg.register(c)

    return reg
