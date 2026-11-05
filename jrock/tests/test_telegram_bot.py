"""Tests for the python-telegram-bot based Telegram bot.

The tests never touch the network: python-telegram-bot objects are built
directly and the Bot API methods are monkeypatched. Everything runs through
``asyncio.run`` so no extra pytest plugin is required.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from telegram import Bot, CallbackQuery, Chat, Message, Update, User
from telegram.ext import ExtBot

from jrock.bots.telegram import PendingConfirmation, RateLimiter, TelegramBot

TOKEN = "123456:TEST-token"


class FakeAgent:
    """Minimal stand-in for AgentRuntime.handle()."""

    def __init__(self, chunks: tuple[str, ...] = ("Hello ", "from ", "the agent.")) -> None:
        self._chunks = chunks
        self._confirm: Any = None
        self.seen: list[str] = []

    async def handle(self, text: str):
        self.seen.append(text)
        for chunk in self._chunks:
            yield chunk


class RecordingApi:
    """Holds calls captured from the monkeypatched Telegram Bot methods."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.answered: list[str] = []


def install_recording_api(monkeypatch) -> RecordingApi:
    """Patch Bot/ExtBot network methods with recording coroutines."""
    api = RecordingApi()

    async def send_message(_bot, chat_id, text, parse_mode=None, reply_markup=None, **kwargs):
        api.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return Message(
            message_id=len(api.sent),
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type="private"),
            text=text,
        )

    async def edit_message_text(_bot, chat_id, message_id, text, parse_mode=None, **kwargs):
        api.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return True

    async def answer_callback_query(_bot, callback_query_id, **kwargs):
        api.answered.append(callback_query_id)
        return True

    for cls in (Bot, ExtBot):
        monkeypatch.setattr(cls, "send_message", send_message)
        monkeypatch.setattr(cls, "edit_message_text", edit_message_text)
        monkeypatch.setattr(cls, "answer_callback_query", answer_callback_query)
    return api


class FakeUpdater:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.polling = False

    async def start_polling(self, allowed_updates):
        self.log.append("updater.start_polling")
        self.polling = True

    async def stop(self):
        self.log.append("updater.stop")
        self.polling = False


class FakeApplication:
    """Just enough Application surface for start_polling()/stop()."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.updater = FakeUpdater(log)

    async def initialize(self):
        self.log.append("app.initialize")

    async def start(self):
        self.log.append("app.start")

    async def stop(self):
        self.log.append("app.stop")

    async def shutdown(self):
        self.log.append("app.shutdown")


def make_message_update(text: str, bot: Bot, user_id: int = 7, chat_id: int = 100) -> Update:
    chat = Chat(id=chat_id, type="private")
    user = User(id=user_id, first_name="Tester", is_bot=False)
    msg = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
    )
    msg.set_bot(bot)
    return Update(update_id=1, message=msg)


def make_callback_update(data: str, bot: Bot, user_id: int = 7, chat_id: int = 100) -> Update:
    chat = Chat(id=chat_id, type="private")
    user = User(id=user_id, first_name="Tester", is_bot=False)
    msg = Message(
        message_id=42,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text="Action requires approval",
    )
    msg.set_bot(bot)
    query = CallbackQuery(id="cb1", from_user=user, chat_instance="ci", data=data, message=msg)
    query.set_bot(bot)
    return Update(update_id=2, callback_query=query)


def new_bot(agent: Optional[FakeAgent], allowed: Optional[set[int]] = None) -> TelegramBot:
    async def factory() -> FakeAgent:
        assert agent is not None
        return agent

    return TelegramBot(TOKEN, agent_factory=cast(Any, factory), allowed_users=allowed)


# ------------------------------------------------------------------ #
# Construction / handlers                                            #
# ------------------------------------------------------------------ #

def test_build_application_registers_handlers():
    bot = new_bot(FakeAgent(), allowed={7})
    app = bot._build_application()
    try:
        assert bot._bot is app.bot
        assert [type(h).__name__ for h in app.handlers[0]] == [
            "MessageHandler",
            "CallbackQueryHandler",
        ]
        assert len(app.error_handlers) == 1
    finally:
        asyncio.run(app.shutdown())


def test_message_streams_agent_reply(monkeypatch):
    api = install_recording_api(monkeypatch)
    agent = FakeAgent()

    async def run() -> None:
        bot = new_bot(agent, allowed={7})
        app = bot._build_application()
        bot._agent = cast(Any, agent)
        try:
            await bot._on_message(make_message_update("/status", app.bot), cast(Any, None))
        finally:
            await app.shutdown()

    asyncio.run(run())

    assert agent.seen == ["/status"]
    assert api.sent[0]["text"] == "⏳ Thinking..."
    assert api.edited[-1]["text"] == "Hello from the agent."


def test_message_from_unauthorised_user_is_rejected(monkeypatch):
    api = install_recording_api(monkeypatch)
    agent = FakeAgent()

    async def run() -> None:
        bot = new_bot(agent, allowed={7})
        app = bot._build_application()
        bot._agent = cast(Any, agent)
        try:
            await bot._on_message(
                make_message_update("/status", app.bot, user_id=999), cast(Any, None)
            )
        finally:
            await app.shutdown()

    asyncio.run(run())

    assert agent.seen == []
    assert "Access denied" in api.sent[0]["text"]


def test_message_without_agent_reports_not_ready(monkeypatch):
    api = install_recording_api(monkeypatch)

    async def run() -> None:
        bot = new_bot(None, allowed={7})
        app = bot._build_application()
        try:
            await bot._on_message(make_message_update("/status", app.bot), cast(Any, None))
        finally:
            await app.shutdown()

    asyncio.run(run())

    assert api.sent[-1]["text"] == "⚠️ Agent not ready."


def test_rate_limiter_blocks_after_limit():
    limiter = RateLimiter(max_per_min=2)
    assert limiter.allow(1) is True
    assert limiter.allow(1) is True
    assert limiter.allow(1) is False
    assert limiter.allow(2) is True  # other users unaffected


# ------------------------------------------------------------------ #
# Confirmation gate                                                  #
# ------------------------------------------------------------------ #

def test_confirmation_gate_approve(monkeypatch):
    api = install_recording_api(monkeypatch)

    async def run() -> bool:
        bot = new_bot(FakeAgent(), allowed={7})
        app = bot._build_application()
        try:
            gate = asyncio.create_task(bot._confirmation_gate("PLACE LONG BTCUSDT 0.01"))
            await asyncio.sleep(0)
            assert len(api.sent) == 1
            markup = api.sent[0]["reply_markup"]
            assert markup is not None
            buttons = markup.inline_keyboard[0]
            assert [b.text for b in buttons] == ["✅ Approve", "❌ Reject"]
            token = next(iter(bot._pending))
            assert buttons[0].callback_data == f"approve:{token}"
            assert buttons[1].callback_data == f"reject:{token}"

            await bot._on_callback(
                make_callback_update(f"approve:{token}", app.bot), cast(Any, None)
            )
            approved = await asyncio.wait_for(gate, timeout=1)
            assert token not in bot._pending
            assert api.answered == ["cb1"]
            assert "✅ Approved" in api.edited[-1]["text"]
            return approved
        finally:
            await app.shutdown()

    assert asyncio.run(run()) is True


def test_confirmation_gate_reject(monkeypatch):
    install_recording_api(monkeypatch)

    async def run() -> bool:
        bot = new_bot(FakeAgent(), allowed={7})
        app = bot._build_application()
        try:
            gate = asyncio.create_task(bot._confirmation_gate("close all positions"))
            await asyncio.sleep(0)
            token = next(iter(bot._pending))
            await bot._on_callback(
                make_callback_update(f"reject:{token}", app.bot), cast(Any, None)
            )
            return await asyncio.wait_for(gate, timeout=1)
        finally:
            await app.shutdown()

    assert asyncio.run(run()) is False


def test_callback_with_unknown_token_reports_expiry(monkeypatch):
    api = install_recording_api(monkeypatch)

    async def run() -> None:
        bot = new_bot(FakeAgent(), allowed={7})
        app = bot._build_application()
        try:
            await bot._on_callback(
                make_callback_update("approve:deadbeef", app.bot), cast(Any, None)
            )
        finally:
            await app.shutdown()

    asyncio.run(run())

    assert "expired" in api.edited[-1]["text"].lower()
    assert api.answered == ["cb1"]


def test_confirmation_gate_auto_approves_without_allowlist():
    bot = new_bot(FakeAgent(), allowed=None)
    assert bot._allowed == set()

    async def run() -> bool:
        return await bot._confirmation_gate("no allowlist configured")

    assert asyncio.run(run()) is True


def test_pending_confirmation_resolves_and_times_out():
    async def run() -> tuple[bool, bool]:
        approved = PendingConfirmation("approve me", "tok1")
        approved.resolve(True)
        resolved = await approved.wait(timeout=1)

        abandoned = PendingConfirmation("timeout me", "tok2")
        timed_out = await abandoned.wait(timeout=0.01)
        return resolved, timed_out

    resolved, timed_out = asyncio.run(run())
    assert resolved is True
    assert timed_out is False


# ------------------------------------------------------------------ #
# Polling lifecycle                                                  #
# ------------------------------------------------------------------ #

def test_start_polling_lifecycle():
    log: list[str] = []
    calls: list[str] = []
    agent = FakeAgent()

    async def factory() -> FakeAgent:
        calls.append("factory")
        return agent

    async def run() -> None:
        bot = TelegramBot(TOKEN, agent_factory=cast(Any, factory), allowed_users={7})
        app = FakeApplication(log)
        bot._build_application = cast(Any, lambda: app)

        task = asyncio.create_task(bot.start_polling())
        for _ in range(100):
            if bot._running:
                break
            await asyncio.sleep(0.01)
        assert bot._running is True
        assert app.updater.polling is True
        await bot.stop()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())

    assert calls == ["factory"]
    assert agent._confirm is not None
    assert log == [
        "app.initialize",
        "app.start",
        "updater.start_polling",
        "updater.stop",
        "app.stop",
        "app.shutdown",
    ]


def test_start_polling_keeps_injected_agent():
    log: list[str] = []
    calls: list[str] = []
    agent = FakeAgent()

    async def factory() -> FakeAgent:
        calls.append("factory")
        return agent

    async def run() -> None:
        bot = TelegramBot(TOKEN, agent_factory=cast(Any, factory), allowed_users={7})
        bot._agent = cast(Any, agent)  # main.py injects the already-built agent
        app = FakeApplication(log)
        bot._build_application = cast(Any, lambda: app)

        task = asyncio.create_task(bot.start_polling())
        for _ in range(100):
            if bot._running:
                break
            await asyncio.sleep(0.01)
        await bot.stop()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())

    assert calls == []
    assert agent._confirm is not None
