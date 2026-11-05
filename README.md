# J+ROCK — AI Trading Agent + Bitunix Futures Engine

A full-stack autonomous trading agent and Bitunix futures engine, controlled via Telegram bot.

## Features

- **16 trading strategies**: Vegas Tunnel, Golden/Death Cross, VWAP, Bollinger Breakout, candlestick patterns, and more
- **Auto-calculated TPSL**: SL placed 50% of way from entry to liquidation; TP scaled by signal confidence
- **Multi-timeframe scanner**: 1m/3m/5m/15m, requiring N agreeing strategies before a trade
- **Full command surface**: `/engine`, `/scan`, `/autotrade`, `/status`, `/pnl`, `/positions`, `/close`, `/settings`, and 30+ more
- **Self-editing agent**: reads/writes its own source with confirmation gate; `/code_review`, `/bug_fix`, `/diagnose`
- **AutoSetModelByKey**: auto-discovers working LLM models for your API key/endpoint
- **Soul & skills system**: swappable agent personas (`/soul`), injectable skills (`/skill`)
- **Telegram bot**: streaming replies, inline Approve/Reject buttons for live orders and self-edits

## Quick Start

```bash
cd jrock
cp .env.example .env
# Fill in BITUNIX_API_KEY, BITUNIX_SECRET_KEY, TELEGRAM_BOT_TOKEN, LLM_API_KEY

uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python main.py
```

## Project Structure

```
jrock/
  adapters/    Bitunix REST client (battle-tested, corrected against live API)
  engine/      Scanner, executor, TPSL calculator, settings, main engine loop
  strategies/  16 strategy signals + all technical indicators (pure numpy)
  risk/        Liquidation math, position sizing, pre-trade risk checks
  agent/       LLM client, AutoSetModelByKey, agent runtime, memory
  commands/    Slash-command registry + all 35 command handlers
  bots/        Telegram bot (raw httpx, streaming, inline keyboards)
  soul/        Agent persona files (jrock_default, aggressive_trader)
  skills/      Injectable skill files (risk_audit, signal_explain)
  state/       Persistent JSON settings, trade journal, memory
  tests/       22 unit tests (liquidation, sizing, indicators, strategies, safety)
  main.py      Entrypoint: Telegram bot / CLI / scan / verify
```

## Commands

### Agent
`/help` `/models` `/set_model` `/connect_provider` `/think` `/memory` `/soul` `/skill` `/add_soul` `/add_skill` `/add_mcp` `/mcp`

### Self-heal
`/code_review` `/bug_fix` `/verify` `/simplify` `/check_ai` `/check_system` `/autopsy` `/diagnose` `/team_spawn`

### Engine
`/engine on|off` `/scan on|off|now` `/autotrade on|off` `/status` `/pnl` `/positions` `/close` `/trades` `/tpsl_history` `/settings` `/leverage` `/symbols` `/margin` `/reversal` `/mid_management` `/report`

## Tests

```bash
cd jrock && ../.venv/bin/python -m pytest tests/ -v
```

22/22 tests pass.

## Exchange Docs

All Bitunix API docs extracted to `jrock/docs/` — 40+ endpoints covering market data, account, positions, orders, TP/SL, and WebSocket channels.

---
Built with Python 3.14 · Bitunix Futures API · Telegram Bot API
