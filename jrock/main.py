"""J+ROCK main entrypoint.

Usage:
    python main.py                    # Telegram bot (default)
    python main.py --cli              # Interactive CLI
    python main.py --scan             # One-shot scan and report
    python main.py --verify           # Verify codebase integrity

Environment: set TELEGRAM_BOT_TOKEN (and optionally LLM_* vars) in .env
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Load .env before anything else.
_env = Path(__file__).resolve().parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            Path(os.environ.get("JRROCK_STATE_DIR", "./state")) / "agent.log",
            mode="a", encoding="utf-8", delay=True
        ),
    ],
)
log = logging.getLogger("jrock")


def build_engine():
    from jrock.adapters.bitunix import BitunixClient
    from jrock.engine.settings import EngineSettings
    from jrock.engine.engine import Engine
    client = BitunixClient()
    settings = EngineSettings.load()
    return client, settings, Engine(client, settings)


async def build_agent(engine):
    from jrock.agent.llm import LLMConfig
    from jrock.agent.runtime import AgentRuntime
    from jrock.commands.handlers import build_registry
    config = LLMConfig.from_env()
    registry = build_registry()
    agent = AgentRuntime(engine, registry, config)
    # Auto-discover models if no model is set but credentials are available.
    if not agent.llm_config.model_id and agent.llm_config.base_url and agent.llm_config.api_key:
        try:
            from jrock.agent.autoset import discover_models
            discovered = await discover_models(agent.llm_config.base_url, agent.llm_config.api_key)
            working = [m for m in discovered if m.working]
            if working:
                agent.llm_config.model_id = working[0].id
                agent.discovered_models = discovered
                await engine.update_settings(model_id=working[0].id, base_url=agent.llm_config.base_url)
                import logging
                logging.getLogger("jrock").info(
                    "AutoSetModelByKey: selected %s (%d working models)",
                    working[0].id, len(working),
                )
        except Exception as e:
            import logging
            logging.getLogger("jrock").warning("Auto model discovery failed: %s", e)
    return agent


async def run_telegram(agent):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN not set. Set it in .env and restart.")
        sys.exit(1)
    from jrock.bots.telegram import TelegramBot

    async def _agent_factory():
        return agent

    bot = TelegramBot(token, agent_factory=_agent_factory)
    # Inject agent directly since it's already built.
    bot._agent = agent
    bot._agent._confirm = bot._confirmation_gate
    log.info("Starting Telegram bot (long-polling)…")
    await bot.start_polling()


async def run_cli(agent):
    """Minimal interactive CLI."""
    print("J+ROCK CLI  (type /help for commands, Ctrl+C to quit)")
    while True:
        try:
            line = input("→ ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not line:
            continue
        buffer = []
        async for chunk in agent.handle(line):
            print(chunk, end="", flush=True)
            buffer.append(chunk)
        print()


async def run_scan(engine):
    signals = await engine.scan_once()
    if not signals:
        print("No qualifying signals.")
    else:
        print(f"{len(signals)} signal(s):")
        for s in signals:
            print(f"  {s}")


async def amain():
    parser = argparse.ArgumentParser(description="J+ROCK trading agent")
    parser.add_argument("--cli", action="store_true", help="Interactive CLI")
    parser.add_argument("--scan", action="store_true", help="One-shot scan")
    parser.add_argument("--verify", action="store_true", help="Verify codebase")
    parser.add_argument("--discover-models", action="store_true", help="Auto-discover working models")
    args = parser.parse_args()

    client, settings, engine = build_engine()
    agent = await build_agent(engine)

    if args.verify:
        result = await agent.verify("all")
        print(result)
        return

    if args.discover_models:
        base = os.environ.get("LLM_BASE_URL", "")
        key = os.environ.get("LLM_API_KEY", "")
        if not key:
            print("LLM_API_KEY not set.")
            return
        from jrock.agent.autoset import discover_models
        models = await discover_models(base, key)
        for m in models:
            s = "✅" if m.working else "❌"
            print(f"  {s} {m.id}  latency={m.latency_ms:.0f}ms")
        return

    if args.scan:
        await run_scan(engine)
        return

    if args.cli:
        await run_cli(agent)
        return

    # Default: Telegram bot.
    await run_telegram(agent)


def main():
    # Ensure the package root is on sys.path so `import jrock.*` resolves.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Shutdown.")


if __name__ == "__main__":
    main()
