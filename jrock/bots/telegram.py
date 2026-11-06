"""Telegram bot — primary user interface for J+ROCK.

Built on python-telegram-bot (async), long-polling mode.

Features:
- Slash command routing to the command registry (all text is forwarded to the
  agent, which owns command parsing).
- Streaming replies via sendMessage + editMessageText (throttled).
- Inline keyboard confirmations (Approve / Reject) for self-edit and live orders.
- Message chunking for long outputs (Telegram limit: 4096 chars).
- Allowed-user allowlist from .env (TELEGRAM_ALLOWED_USERS=user_id1,user_id2).
- Rate limiting: max 20 messages/min per user.
- Graceful start/stop that integrates with the asyncio app in main.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import defaultdict
from typing import Awaitable, Callable, Optional, TYPE_CHECKING

import telegram
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

if TYPE_CHECKING:
    from jrock.agent.runtime import AgentRuntime

log = logging.getLogger(__name__)

MAX_MSG_LEN = 4000   # Leave headroom under 4096.
RATE_LIMIT = 20      # messages per minute per user.
APPROVE_PATTERN = r"^(approve|reject):"


class RateLimiter:
    def __init__(self, max_per_min: int = RATE_LIMIT) -> None:
        self._max = max_per_min
        self._counts: dict[int, list[float]] = defaultdict(list)

    def allow(self, user_id: int) -> bool:
        now = time.time()
        self._counts[user_id] = [t for t in self._counts[user_id] if now - t < 60]
        if len(self._counts[user_id]) >= self._max:
            return False
        self._counts[user_id].append(now)
        return True


class PendingConfirmation:
    """A live order or self-edit waiting for user approval."""

    def __init__(self, description: str, callback_id: str) -> None:
        self.description = description
        self.callback_id = callback_id
        self._future: Optional[asyncio.Future[bool]] = None

    def _get_future(self) -> asyncio.Future[bool]:
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        return self._future

    def resolve(self, approved: bool) -> None:
        future = self._get_future()
        if not future.done():
            future.set_result(approved)

    async def wait(self, timeout: float = 120.0) -> bool:
        try:
            return await asyncio.wait_for(self._get_future(), timeout=timeout)
        except asyncio.TimeoutError:
            return False



class TelegramBot:
    """Telegram front end powered by python-telegram-bot."""

    def __init__(
        self,
        token: str,
        agent_factory: Callable[[], Awaitable["AgentRuntime"]],
        allowed_users: Optional[set[int]] = None,
    ) -> None:
        self._token = token
        self._agent_factory = agent_factory
        self._allowed = allowed_users or self._load_allowed()
        self._rl = RateLimiter()
        self._pending: dict[str, PendingConfirmation] = {}
        self._agent: Optional[AgentRuntime] = None
        self._app: Optional[Application] = None
        self._bot: Optional[Bot] = None
        self._running = False

    @staticmethod
    def _load_allowed() -> set[int]:
        raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
        ids = set()
        for s in raw.split(","):
            s = s.strip()
            if s.isdigit():
                ids.add(int(s))
        return ids

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def _build_application(self) -> Application:
        """Create the python-telegram-bot application and register handlers."""
        app = Application.builder().token(self._token).build()
        app.add_handler(MessageHandler(filters.TEXT, self._on_message))
        app.add_handler(CallbackQueryHandler(self._on_callback, pattern=APPROVE_PATTERN))
        app.add_error_handler(self._on_error)
        self._bot = app.bot
        return app

    async def start_polling(self) -> None:
        if self._agent is None:
            self._agent = await self._agent_factory()
        self._agent._confirm = self._confirmation_gate

        app = self._build_application()
        self._app = app
        await app.initialize()
        await app.start()
        updater = app.updater
        if updater is None:  # pragma: no cover - polling always has an updater
            await app.shutdown()
            raise RuntimeError("Telegram application has no updater; polling unavailable")

        # Populate the "/" menu (bottom-left) with every registered command
        # so the user can tap a command instead of typing it.
        try:
            registry = getattr(self._agent, "registry", None)
            bot = self._bot
            if registry is not None and bot is not None:
                cmds = registry.all_defs()
                cmds.sort(key=lambda c: c.name)
                # Telegram command names: lowercase letters, digits, underscore only.
                # Names with spaces (e.g. "settings trade") are exposed as "settings_trade".
                menu = [
                    BotCommand(c.name.lower().replace(" ", "_"), (c.help_text or c.name)[:255])
                    for c in cmds
                ]
                await bot.set_my_commands(menu)
                log.info("Telegram command menu populated (%d commands)", len(menu))
        except Exception as exc:
            log.warning("set_my_commands failed (menu may be empty): %s", exc)

        await updater.start_polling(allowed_updates=Update.ALL_TYPES)
        self._running = True
        log.info("Telegram bot polling started (python-telegram-bot %s)", telegram.__version__)
        try:
            while self._running:
                await asyncio.sleep(0.5)
        finally:
            await updater.stop()
            await app.stop()
            await app.shutdown()
            self._running = False
            log.info("Telegram bot stopped")

    async def stop(self) -> None:
        self._running = False

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Telegram handler error: %s", context.error)

    # ------------------------------------------------------------------ #
    # Telegram API helpers                                               #
    # ------------------------------------------------------------------ #

    async def _send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> Optional[Message]:
        """Send a message and return it, or None if Telegram rejected it."""
        bot = self._bot
        if bot is None:
            log.warning("Telegram bot not started; dropping message")
            return None
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=text[:MAX_MSG_LEN],
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            log.warning("sendMessage failed (%s); retrying as plain text", exc)
            try:
                return await bot.send_message(
                    chat_id=chat_id,
                    text=text[:MAX_MSG_LEN],
                    reply_markup=reply_markup,
                )
            except TelegramError as retry_exc:
                log.error("sendMessage failed: %s", retry_exc)
                return None

    async def _edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        bot = self._bot
        if bot is None:
            return
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text[:MAX_MSG_LEN],
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError as exc:
            if "not modified" in str(exc).lower():
                return
            log.debug("editMessageText failed (%s); retrying as plain text", exc)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text[:MAX_MSG_LEN],
                )
            except TelegramError as retry_exc:
                log.debug("editMessageText failed: %s", retry_exc)

    async def _send_chunked(self, chat_id: int, text: str) -> None:
        """Split long messages into chunks."""
        for i in range(0, len(text), MAX_MSG_LEN):
            chunk = text[i : i + MAX_MSG_LEN]
            await self._send_message(chat_id, chunk)
            if i + MAX_MSG_LEN < len(text):
                await asyncio.sleep(0.3)

    async def _stream_agent_reply(self, chat_id: int, text: str) -> None:
        """Run the agent and stream its output into a single Telegram message."""
        agent = self._agent
        if agent is None:
            await self._send_message(chat_id, "⚠️ Agent not ready.")
            return

        placeholder = await self._send_message(chat_id, "⏳ Thinking...")
        placeholder_id = placeholder.message_id if placeholder is not None else None

        buffer = ""
        last_edit = time.time()
        try:
            async for chunk in agent.handle(text):
                buffer += chunk
                # Throttle edits to avoid Telegram flood limits (max ~20 edits/sec).
                if time.time() - last_edit > 0.8 and placeholder_id:
                    try:
                        await self._edit_message(chat_id, placeholder_id, buffer[:MAX_MSG_LEN] + "…")
                        last_edit = time.time()
                    except Exception:
                        pass
        except Exception as e:
            buffer = f"Error: {e}"

        if not buffer:
            buffer = "Done."

        # Final update or send full response.
        if placeholder_id and len(buffer) <= MAX_MSG_LEN:
            await self._edit_message(chat_id, placeholder_id, buffer)
        else:
            if placeholder_id:
                await self._edit_message(chat_id, placeholder_id, "(see below)")
            await self._send_chunked(chat_id, buffer)

    # ------------------------------------------------------------------ #
    # Handlers                                                           #
    # ------------------------------------------------------------------ #

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None or msg.chat is None:
            return

        text = (msg.text or "").strip()
        if not text:
            return

        chat_id = msg.chat.id
        user_id = user.id

        # Access control.
        if self._allowed and user_id not in self._allowed:
            await self._send_message(chat_id, "⛔ Access denied.")
            return

        # Rate limit.
        if not self._rl.allow(user_id):
            await self._send_message(chat_id, "⏳ Too many messages. Please slow down.")
            return

        await self._stream_agent_reply(chat_id, text)

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline keyboard button press (Approve / Reject)."""
        query = update.callback_query
        if query is None:
            return

        await query.answer()

        action, _, token = (query.data or "").partition(":")
        pending = self._pending.pop(token, None)
        if pending is None:
            try:
                await query.edit_message_text("⚠️ This confirmation has expired.")
            except TelegramError as exc:
                log.debug("could not edit expired confirmation: %s", exc)
            return

        approved = action == "approve"
        pending.resolve(approved)

        status = "✅ Approved" if approved else "❌ Rejected"
        try:
            await query.edit_message_text(f"{status}: {pending.description[:200]}")
        except TelegramError as exc:
            log.debug("could not edit confirmation result: %s", exc)

    async def _confirmation_gate(self, description: str) -> bool:
        """Send a Telegram confirmation message and wait for the user's response."""
        # Find the first allowed chat (simplistic: send to the first known user).
        if not self._allowed:
            log.warning("confirmation gate: no allowed users configured, auto-approving")
            return True

        # Use a short token as the callback key.
        token = hashlib.sha1(f"{description}{time.time()}".encode()).hexdigest()[:12]
        pending = PendingConfirmation(description, token)
        self._pending[token] = pending

        markup = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{token}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{token}"),
            ]]
        )

        chat_id = next(iter(self._allowed))
        await self._send_message(
            chat_id,
            f"⚠️ *Action requires approval:*\n```\n{description[:1000]}\n```",
            reply_markup=markup,
        )

        return await pending.wait(timeout=120.0)
