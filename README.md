# J+ROCK — AI Trading Agent + Bitunix Futures Engine

A full-stack autonomous trading agent and Bitunix futures engine, controlled via Telegram bot.

## Features

- **16 trading strategies**: Vegas Tunnel, Golden/Death Cross, VWAP, Bollinger Breakout, candlestick patterns, and more
- **Auto-calculated TPSL**: SL placed 50% of way from entry to liquidation; TP scaled by signal confidence
- **Multi-timeframe scanner**: 1m/3m/5m/15m, requiring N agreeing strategies before a trade
- **Full command surface**: `/engine`, `/scan`, `/autotrade`, `/status`, `/pnl`, `/positions`, `/close`, `/settings`, and 30+ more
- **Self-editing agent**: reads/writes its own source with confirmation gate; `/code_review`, `/bug_fix`, `/diagnose`
- **AutoSetModelByKey**: auto-discovers working LLM models for your API key/endpoint (scans `.env` `BASE_URL`/`API_KEY` on boot)
- **Git push automation**: `/git_push` force-pushes verified code assets to GitHub via `GITHUB_PAT` + `GITHUB_REPO`
- **Soul & skills system**: swappable agent personas (`/soul`), injectable skills (`/skill`)
- **Telegram bot**: python-telegram-bot (async, long polling), streaming replies, inline Approve/Reject buttons for live orders and self-edits

## Quick Start

```bash
# Run from the repository root: `jrock` is a package, so imports resolve as
# `jrock.*` and the environment is created at the project root (next to
# pyproject.toml) where ty/uv expect to find it.
cp jrock/.env.example jrock/.env
# Fill in BITUNIX_API_KEY, BITUNIX_SECRET_KEY, TELEGRAM_BOT_TOKEN, LLM_API_KEY

uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -e .
.venv/bin/python jrock/main.py
```

## Type checking

```bash
.venv/bin/python -m ty check        # or: ty check   (works from any subdirectory)
```

## Project Structure

```
pyproject.toml  Package metadata + dependencies (`websockets`, `httpx`, …)
jrock/           The `jrock` package (import as `jrock.<module>`)
  adapters/    Bitunix REST client (battle-tested, corrected against live API)
  engine/      Scanner, executor, TPSL calculator, settings, main engine loop
  strategies/  16 strategy signals + all technical indicators (pure numpy)
  risk/        Liquidation math, position sizing, pre-trade risk checks
  agent/       LLM client, AutoSetModelByKey, agent runtime, memory
  commands/    Slash-command registry + all 35 command handlers
  bots/        Telegram bot (python-telegram-bot: streaming, inline keyboards)
  soul/        Agent persona files (jrock_default, aggressive_trader)
  skills/      Injectable skill files (risk_audit, signal_explain)
  state/       Persistent JSON settings, trade journal, memory
  tests/       54 unit tests (liquidation, sizing, indicators, strategies, engine, telegram bot, ground-truth API compliance)
  main.py      Entrypoint: Telegram bot / CLI / scan / verify
```

## Commands

### Agent
`/help` `/models` `/set_model` `/connect_provider` `/think` `/memory` `/soul` `/skill` `/add_soul` `/add_skill` `/add_mcp` `/mcp`

### Self-heal
`/code_review` `/bug_fix` `/verify` `/simplify` `/check_ai` `/check_system` `/autopsy` `/diagnose` `/team_spawn` `/git_push`

### Engine
`/engine on|off` `/scan on|off|now` `/autotrade on|off` `/status` `/pnl` `/positions` `/close` `/trades` `/tpsl_history` `/settings` `/settings trade` `/leverage` `/symbols` `/margin` `/max_positions` `/reversal` `/mid_management` `/report`

## Tests

```bash
.venv/bin/python -m pytest jrock/tests -v
```

54/54 tests pass.

## Exchange Docs

All Bitunix API docs extracted to `jrock/docs/` — 40+ endpoints covering market data, account, positions, orders, TP/SL, and WebSocket channels.

---
Built with Python 3.14 · Bitunix Futures API · Telegram Bot API
