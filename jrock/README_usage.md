# J+ROCK Usage Guide

This document explains how to use the J+ROCK AI trading agent and its features.

## Getting Started

1. Navigate to the J+ROCK directory:
   ```bash
   cd /bitunix-agent/jrock
   ```

2. Create and activate the virtual environment:
   ```bash
   uv venv
   source .venv/bin/activate
   ```

3. Install dependencies and the package itself:
   ```bash
   uv run pip install -e .
   ```

4. Run J+ROCK:
   ```bash
   uv run main.py
   ```

## Commands and Features

### Trading Commands

- **`/engine on|off`** - Start or stop the trading engine
- **`/scan on|off|now`** - Enable/disable or run a scan
- **`/autotrade on|off`** - Enable/disable autotrade
- **`/settings`** - View or update trading settings
- **`/settings trade`** - View real-time trading config
- **`/positions`** - List open positions
- **`/pnl`** - Show PnL report
- **`/close`** - Close a position
- **`/trades`** - View trade history

### Configuration Commands

- **`/set_model`** - Set active LLM model or auto-discover
- **`/models`** - List discovered models
- **`/help`** - Show available commands
- **`/memory`** - Manage agent memory
- **`/soul`** - Switch agent persona
- **`/skill`** - Use or list skills

### Self-Edit Commands

- **`/code_review`** - Review and fix code
- **`/bug_fix`** - Describe and fix bugs
- **`/verify`** - Verify codebase integrity
- **`/simplify`** - Simplify a module
- **`/diagnose`** - Diagnose specific issues
- **`/autopsy`** - Analyze trade journal

### Utility Commands

- **`/report`** - Report control
- **`/reversal`**** - Reversal settings
- **`/max_positions`** - Get or set max open positions
- **`/position_mode`** - Get or set position mode
- **`/margin`** - Get or set margin mode
- **`/leverage`** - Get or set leverage
- **`/symbols`** - Get or set active symbols

## The "Real Agent" System

J+ROCK now implements a pragmatic "real agent" design:

1. **Honesty first** — never claim that the conversational LLM executed anything
2. **Intent routing** — short natural-language config requests route to real slash commands
3. **Command tracing** — all command dispatches are logged for auditability
4. **Fuzzy typo rescue** — /codereveiew → /code_review, etc.

This ensures your commands are actually executed and verified, not hallucinated.

## Example Workflow

```bash
cd /bitunix-agent/jrock
uv venv
source .venv/bin/activate

# Set risk per trade to 20% (actually executed)
/settings risk_per_trade_pct=20

# Enable scanning
/scan on

# Start the engine
/engine on

# Check status
/status
```

## Notes

- All settings are persisted to `jrock/state/settings.json`
- Configuration changes take effect immediately
- Real exchange operations require proper API credentials in `.env`
- Run tests to verify everything works: `uv run python -m pytest jrock/tests -v`
- For issues, use `/diagnose` or `/code_review` commands

## Quick Reference

**Common Commands:**
- `/help` - All available commands
- `/engine on` - Start trading
- `/scan now` - Run one scan
- `/settings trade` - View current config
- `/pnl` - Check profits

**Configuration:**
- `/settings leverage=20` - Set leverage
- `/settings risk_per_trade_pct=1.5` - Set risk per trade
- `/margin CROSS` - Set margin mode
- `/position_mode HEDGE` - Set position mode

**Self-Heal:**
- `/code_review` - Review code changes
- `/bug_fix <description>` - Fix a bug
- `/diagnose <issue>` - Debug problems
- `/verify` - Check system integrity

Enjoy using J+ROCK! 🚀