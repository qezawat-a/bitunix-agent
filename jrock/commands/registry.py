"""Command registry: parse /cmd args into structured commands and dispatch them.

Every command returns a CommandResult — a plain dataclass, not a print side-effect.
This keeps the same command surface working for CLI, Telegram, and Slack.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional


@dataclass
class CommandResult:
    ok: bool
    text: str
    data: Any = None
    error: str = ""

    def __bool__(self) -> bool:
        return self.ok


Handler = Callable[..., Awaitable[CommandResult]]


@dataclass
class CommandDef:
    name: str
    handler: Handler
    help_text: str
    aliases: list[str] = field(default_factory=list)
    args_schema: str = ""   # e.g. "[symbol] [leverage]"
    category: str = "general"


class CommandRegistry:
    """Registry of all slash commands."""

    def __init__(self) -> None:
        self._cmds: dict[str, CommandDef] = {}
        self._aliases: dict[str, str] = {}

    def register(self, defn: CommandDef) -> None:
        self._cmds[defn.name] = defn
        for alias in defn.aliases:
            self._aliases[alias] = defn.name

    def get(self, name: str) -> Optional[CommandDef]:
        name = name.lstrip("/").lower()
        canonical = self._aliases.get(name, name)
        return self._cmds.get(canonical)

    def all_defs(self) -> list[CommandDef]:
        return list(self._cmds.values())

    async def dispatch(self, raw: str, **ctx) -> CommandResult:
        """Parse and execute `/cmd arg1 arg2 key=val` input."""
        raw = raw.strip()
        if not raw.startswith("/"):
            return CommandResult(False, "", error="not a command")

        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()

        cmd_name = tokens[0].lstrip("/").lower()
        args = tokens[1:]

        # Parse key=value kwargs.
        positional = []
        kwargs: dict[str, str] = {}
        for t in args:
            if "=" in t and not t.startswith("-"):
                k, _, v = t.partition("=")
                kwargs[k.strip()] = v.strip()
            else:
                positional.append(t)

        defn = self.get(cmd_name)
        if defn is None:
            close = [c for c in self._cmds if c.startswith(cmd_name[:3])]
            suggestion = f"  Did you mean: {', '.join('/' + c for c in close[:3])}?" if close else ""
            return CommandResult(False, "", error=f"Unknown command /{cmd_name}.{suggestion}")

        try:
            result = await defn.handler(*positional, **kwargs, **ctx)
        except TypeError as e:
            return CommandResult(False, "", error=f"/{cmd_name} argument error: {e}")
        except Exception as e:
            return CommandResult(False, "", error=f"/{cmd_name} failed: {e}")

        return result

    def help_text(self, category: Optional[str] = None) -> str:
        cats: dict[str, list[CommandDef]] = {}
        for d in self._cmds.values():
            cats.setdefault(d.category, []).append(d)

        lines = ["**J+ROCK Commands**\n"]
        for cat, defs in sorted(cats.items()):
            if category and cat != category:
                continue
            lines.append(f"**{cat.upper()}**")
            for d in sorted(defs, key=lambda x: x.name):
                aliases = f"  [{', '.join('/' + a for a in d.aliases)}]" if d.aliases else ""
                args = f" {d.args_schema}" if d.args_schema else ""
                lines.append(f"  /{d.name}{args}{aliases}")
                lines.append(f"    {d.help_text}")
            lines.append("")
        return "\n".join(lines)
