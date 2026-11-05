"""Telegram bot — primary user interface for J+ROCK.

Uses raw httpx (no aiogram/python-telegram-bot) for minimal dependencies.
Supports both long-polling and webhook mode.

Features:
- Slash command routing to the command registry.
- Streaming replies via sendMessage + editMessageText.
- Inline keyboard confirmations (Approve / Reject) for self-edit and live orders.
- Message chunking for long outputs (Telegram limit: 4096 chars).
- Allowed-user allowlist from .env (TELEGRAM_ALLOWED_USERS=user_id1,user_id2).
- Rate limiting: max 20 messages/min per user.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from typing import Optional, AsyncIterator, Callable, Awaitable, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from jrock.agent.runtime import AgentRuntime

log = logging.getLogger(__name__)

TELEGRAM_BASE = "https://api.telegram.org/bot{token}"
MAX_MSG_LEN = 4000   # Leave headroom under 4096.
RATE_LIMIT = 20      # messages per minute per user.


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
        self._future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()

    def resolve(self, approved: bool) -> None:
        if not self._future.done():
            self._future.set_result(approved)

    async def wait(self, timeout: float = 120.0) -> bool:
        try:
            return await asyncio.wait_for(self._future, timeout=timeout)
        except asyncio.TimeoutError:
            return False


class TelegramBot:
    def __init__(
        self,
        token: str,
        agent_factory: Callable[[], Awaitable["AgentRuntime"]],
        allowed_users: Optional[set[int]] = None,
    ) -> None:
        self._token = token
        self._agent_factory = agent_factory
        self._base = f"https://api.telegram.org/bot{token}"
        self._http = httpx.AsyncClient(timeout=30.0)
        self._allowed = allowed_users or self._load_allowed()
        self._rl = RateLimiter()
        self._pending: dict[str, PendingConfirmation] = {}
        self._agent: Optional[AgentRuntime] = None
        self._offset = 0
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

    async def start_polling(self) -> None:
        self._agent = await self._agent_factory()
        self._agent._confirm = self._confirmation_gate
        self._running = True
        log.info("Telegram bot polling started")
        while self._running:
            try:
                updates = await self._get_updates()
                for upd in updates:
                    asyncio.create_task(self._handle_update(upd))
            except Exception as e:
                log.error("polling error: %s", e)
                await asyncio.sleep(5)

    async def stop(self) -> None:
        self._running = False
        await self._http.aclose()

    async def _api(self, method: str, **kwargs) -> dict:
        url = f"{self._base}/{method}"
        resp = await self._http.post(url, json=kwargs, timeout=10.0)
        data = resp.json()
        if not data.get("ok"):
            log.warning("Telegram API error: %s", data)
        return data

    async def _get_updates(self) -> list[dict]:
        data = await self._api(
            "getUpdates", offset=self._offset, timeout=25, allowed_updates=["message", "callback_query"]
        )
        updates = data.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    async def _send(self, chat_id: int, text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
        """Send a message, return message_id."""
        kwargs = {
            "chat_id": chat_id,
            "text": text[:MAX_MSG_LEN],
            "parse_mode": "Markdown",
        }
        if reply_markup:
            kwargs["reply_markup"] = json.dumps(reply_markup)
        data = await self._api("sendMessage", **kwargs)
        msg = data.get("result", {})
        return msg.get("message_id")

    async def _edit(self, chat_id: int, message_id: int, text: str) -> None:
        await self._api(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text[:MAX_MSG_LEN],
            parse_mode="Markdown",
        )

    async def _send_chunked(self, chat_id: int, text: str) -> None:
        """Split long messages into chunks."""
        for i in range(0, len(text), MAX_MSG_LEN):
            chunk = text[i : i + MAX_MSG_LEN]
            await self._send(chat_id, chunk)
            if i + MAX_MSG_LEN < len(text):
                await asyncio.sleep(0.3)

    async def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
            return

        msg = update.get("message", {})
        if not msg:
            return

        chat_id = msg["chat"]["id"]
        user_id = msg.get("from", {}).get("id", 0)
        text = msg.get("text", "").strip()

        if not text:
            return

        # Access control.
        if self._allowed and user_id not in self._allowed:
            await self._send(chat_id, "⛔ Access denied.")
            return

        # Rate limit.
        if not self._rl.allow(user_id):
            await self._send(chat_id, "⏳ Too many messages. Please slow down.")
            return

        # Stream reply.
        placeholder_id = await self._send(chat_id, "⏳ Thinking...")
        buffer = ""
        last_edit = time.time()

        agent = self._agent
        if agent is None:
            await self._send(chat_id, "⚠️ Agent not ready.")
            return

        try:
            async for chunk in agent.handle(text):
                buffer += chunk
                # Throttle edits to avoid Telegram flood limits (max ~20 edits/sec).
                if time.time() - last_edit > 0.8 and placeholder_id:
                    display = buffer[:MAX_MSG_LEN]
                    try:
                        await self._edit(chat_id, placeholder_id, display + "…")
                        last_edit = time.time()
                    except Exception:
                        pass
        except Exception as e:
            buffer = f"Error: {e}"

        if not buffer:
            buffer = "Done."

        # Final update or send full response.
        if placeholder_id and len(buffer) <= MAX_MSG_LEN:
            try:
                await self._edit(chat_id, placeholder_id, buffer)
            except Exception:
                await self._send_chunked(chat_id, buffer)
        else:
            if placeholder_id:
                try:
                    await self._edit(chat_id, placeholder_id, "(see below)")
                except Exception:
                    pass
            await self._send_chunked(chat_id, buffer)

    async def _handle_callback(self, cb: dict) -> None:
        """Handle inline keyboard button press (Approve/Reject)."""
        callback_id_raw = cb.get("data", "")
        callback_query_id = cb["id"]
        chat_id = cb["message"]["chat"]["id"]
        message_id = cb["message"]["message_id"]

        await self._api("answerCallbackQuery", callback_query_id=callback_query_id)

        # Format: "approve:token" or "reject:token"
        action, _, token = callback_id_raw.partition(":")
        pending = self._pending.get(token)
        if not pending:
            await self._api("editMessageText", chat_id=chat_id, message_id=message_id,
                            text="⚠️ This confirmation has expired.")
            return

        approved = action == "approve"
        pending.resolve(approved)
        del self._pending[token]

        status = "✅ Approved" if approved else "❌ Rejected"
        await self._api("editMessageText", chat_id=chat_id, message_id=message_id,
                        text=f"{status}: {pending.description[:200]}", reply_markup=json.dumps({"inline_keyboard": []}))

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

        markup = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{token}"},
                {"text": "❌ Reject", "callback_data": f"reject:{token}"},
            ]]
        }

        chat_id = next(iter(self._allowed))
        await self._send(
            chat_id,
            f"⚠️ **Action requires approval:**\n```\n{description[:1000]}\n```",
            reply_markup=markup,
        )

        return await pending.wait(timeout=120.0)
