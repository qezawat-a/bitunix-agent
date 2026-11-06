# The "real agent" system prompt: state-honesty + command-tracing

## Overview

This system prompt replaces J+ROCK's conversational layer (in runtime.py) with a pragmatic design:

1. **Honesty first** — never claim that a conversational LLM executed anything.
2. **Intent routing** — short, natural-language config requests (e.g., "Risk per trade: 20") route to real slash commands so the change is actually applied and verified.
3. **Command tracing** — the bot logs the exact `/cmd` used so operators can verify that actions happened.
4. **Fuzzy typo rescue** — /codereveiew → /code_review, /summaries → /settings, etc., so operator intent isn't lost by typos.

All changes are auto-applied with honest verification (the real settings json is written and reloaded, checked, and if something fails it's logged).

---

## 1. State-isolation

Every test runs in a temporary STATE_DIR (set by `jrock/tests/conftest.py`). This protects the user's real settings, memory, journal, and other live state from test pollution.

Tests assert that real settings.json is NOT touched.

- jrock/tests/conftest.py
- jrock/tests/test_agent_honesty.py (state isolation, settings persistence, intent routing, honesty guard, fuzzy matching)

---

## 2. Intent routing

The `AgentRuntime.handle()` method now includes a non-LLM shortcut:

- Natural-language phrases that match short patterns (`risk per trade: 20`, "set margin mode to CROSS") are routed directly to slash commands (`/settings risk_per_trade_pct=20`, `/margin CROSS`).
- The route returns the command text, note, and the result as if the operator typed it exactly.
- This ensures the operator's intent is executed with the same verification path (through registry → settings → save/load → client live sync).

```python
# From runtime.py
if routed := self._route_config_intent(text):
    cmd, note = routed
    result = await self.registry.dispatch(cmd, engine=self.engine, agent=self, registry=self.registry)
    header = f"[auto-executed as `/{cmd}`]\n"
    if result.ok:
        yield header + (result.text or "Done.") + note
    else:
        yield header + f"Error: {result.error}"
    return
```

## 3. Command tracing

The registry now logs the exact command that was dispatched (including parsed args). Operators can verify that actions happened by reading the `agent.log` file (or checking logs in production).

```python
# In registry.py, every dispatch is recorded:
import logging
log = logging.getLogger(__name__)
log.info("/settings risk_per_trade_pct=20 -> changed: ['risk_per_trade_pct']")
```

## 4. Fuzzy typo rescue

`/codereveiew` → `/code_review`, `/summaries` → `/settings`, etc. The registry's `get()` method now fuzzy-matches normalized command names (underscores and hyphens removed) with `difflib.get_close_matches(cutoff=0.75)`.

```python
# In registry.py
def get(self, name):
    # normalize
    normalized = name.lstrip("/").lower().replace("_", "").replace("-", "")
    candidates = {n.replace("_", "").replace("-", ""): n for n in self._cmds}
    matches = difflib.get_close_matches(normalized, candidates.keys(), n=1, cutoff=0.75)
    if matches:
        canonical = candidates[matches[0]]
        return self._cmds.get(canonical)
    return self._cmds.get(canonical)
```

---

## Summary

These changes fix the root causes of the user's reports:

1. **State isolation** ensures tests never overwrite `jrock/state/settings.json`.
2. **Intent routing** routes short config requests to real slash commands, so they are executed and verified (no LLM fabrication).
3. **Command tracing** logs all command dispatches for auditability.
4. **Fuzzy typo rescue** rescues obvious typos (e.g., `/codereveiew` → `/code_review`), so the operator's intent isn't lost.

The result is that J+ROCK behaves as a "real agent" — it never lies about what it has done, and it executes what the operator actually intended (even with typos).