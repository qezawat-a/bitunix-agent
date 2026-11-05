"""Slack bot for J+ROCK — alternate control surface to Telegram.

Uses Slack Socket Mode + Events API via raw httpx (no slack-sdk dependency).
Requires:
    SLACK_BOT_TOKEN=xoxb-...      (Bot User OAuth Token, App-level token for Socket Mode)
    SLACK_APP_TOKEN=xapp-...      (Socket Mode token, connections:write scope)
    SLACK_ALLOWED_USERS=U123,U456 (Slack user IDs allowed to control the agent)

Slack slash commands:
    /jrock-engine on|off|status
    /jrock-scan on|off|now
    /jrock-status
    /jrock-pnl
    /jrock-help
    /jrock-settings key=val ...

Interactive buttons (Approve / Reject) are sent for self-edit and live order
confirmations, routed through Slack's block_actions endpoint.

Message chunking splits long outputs into multiple messages (Slack limit: 4000
chars per block, 3000 per message recommended).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from typing import Optional

import httpx

log = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
MAX_MSG_LEN = 3000
RATE_LIMIT = 20  # messages per minute per user


class RateLimiter:
    def __init__(self, max_per_min: int = RATE_LIMIT) -> None:
        self._max = max_per_min
        self._counts: dict[str, list[float]] = defaultdict(list)

    def allow(self, user_id: str) -> bool:
        now = time.time()
        self._counts[user_id] = [t for t in self._counts[user_id] if now - t < 60]
        if len(self._counts[user_id]) >= self._max:
            return False
        self._counts[user_id].append(now)
        return True


class PendingConfirmation:
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


class SlackBot:
    def __init__(
        self,
        bot_token: str,
        app_token: str,
        agent_factory,
        allowed_users: Optional[set[str]] = None,
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._agent_factory = agent_factory
        self._allowed = allowed_users or self._load_allowed()
        self._rl = RateLimiter()
        self._pending: dict[str, PendingConfirmation] = {}
        self._agent = None
        self._running = False
        self._http = httpx.AsyncClient(timeout=30.0)
        self._headers = {"Authorization": f"Bearer {bot_token}"}

    @staticmethod
    def _load_allowed() -> set[str]:
        raw = os.environ.get("SLACK_ALLOWED_USERS", "")
        return {s.strip() for s in raw.split(",") if s.strip()}

    # ------------------------------------------------------------------ #
    # Slack Web API helpers                                               #
    # ------------------------------------------------------------------ #

    async def _api(self, method: str, *, files: Optional[dict] = None, **kwargs) -> dict:
        if files:
            data = {"method": method}
            for k, v in kwargs.items():
                if v is not None:
                    data[k] = json.dumps(v) if not isinstance(v, str) else v
            resp = await self._http.post(
                f"{SLACK_API}/{method}",
                headers={"Authorization": f"Bearer {self._bot_token}"},
                data=data,
                files=files,
                timeout=15.0,
            )
        else:
            resp = await self._http.post(
                f"{SLACK_API}/{method}",
                headers={**self._headers, "Content-Type": "application/json; charset=utf-8"},
                json=kwargs,
                timeout=15.0,
            )
        return resp.json()

    async def _post_message(
        self,
        channel: str,
        text: str,
        blocks: Optional[list] = None,
        thread_ts: Optional[str] = None,
    ) -> Optional[str]:
        kwargs = {
            "channel": channel,
            "text": text[:MAX_MSG_LEN],
        }
        if blocks:
            kwargs["blocks"] = blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        data = await self._api("chat.postMessage", **kwargs)
        if not data.get("ok"):
            log.warning("chat.postMessage failed: %s", data.get("error"))
            return None
        return data.get("ts")

    async def _update_message(
        self, channel: str, ts: str, text: str, blocks: Optional[list] = None
    ) -> None:
        kwargs = {"channel": channel, "ts": ts, "text": text[:MAX_MSG_LEN]}
        if blocks:
            kwargs["blocks"] = blocks
        await self._api("chat.update", **kwargs)

    async def _send_chunked(self, channel: str, text: str) -> None:
        for i in range(0, len(text), MAX_MSG_LEN):
            chunk = text[i : i + MAX_MSG_LEN]
            await self._post_message(channel, chunk)
            if i + MAX_MSG_LEN < len(text):
                await asyncio.sleep(0.3)

    # ------------------------------------------------------------------ #
    # Confirmation gates                                                  #
    # ------------------------------------------------------------------ #

    def _confirm_blocks(self, token: str, description: str) -> list:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⚠️ *Action requires approval:*\n```\n{description[:900]}\n```",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve"},
                        "action_id": f"approve:{token}",
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Reject"},
                        "action_id": f"reject:{token}",
                        "style": "danger",
                    },
                ],
            },
        ]

    async def _confirmation_gate(self, description: str) -> bool:
        if not self._allowed:
            log.warning("confirmation gate: no allowed users configured, auto-approving")
            return True

        token = hashlib.sha1(f"{description}{time.time()}".encode()).hexdigest()[:12]
        pending = PendingConfirmation(description, token)
        self._pending[token] = pending

        channel = next(iter(self._allowed))
        await self._post_message(
            channel,
            f"⚠️ Action requires approval: {description[:100]}",
            blocks=self._confirm_blocks(token, description),
        )

        return await pending.wait(timeout=120.0)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._agent = await self._agent_factory()
        self._agent._confirm = self._confirmation_gate
        self._running = True
        log.info("Slack bot starting (Socket Mode)…")

        # Connect via Socket Mode.
        await self._api("apps.connections.open")  # returns {url: wss://...}
        # Note: apps.connections.open is HTTP-based; the actual Socket Mode
        # connection is a WebSocket. We do it here via websockets.
        await self._run_socket_mode()

    async def stop(self) -> None:
        self._running = False
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Socket Mode WebSocket                                               #
    # ------------------------------------------------------------------ #

    async def _run_socket_mode(self) -> None:
        import websockets
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        while self._running:
            try:
                # Negotiate the Socket Mode URL.
                resp = await self._api("apps.connections.open")
                url = resp.get("url")
                if not url:
                    log.error("apps.connections.open failed: %s", resp)
                    await asyncio.sleep(5)
                    continue

                async with websockets.connect(url, ssl=ctx, ping_interval=None) as ws:
                    log.info("Slack Socket Mode connected")
                    async for raw in ws:
                        await self._handle_socket_message(raw)

            except Exception as e:
                log.error("Slack Socket Mode error: %s", e)
                if self._running:
                    await asyncio.sleep(5)

    async def _handle_socket_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")

        # Envelope handling: Slack wraps events in an envelope with `type`.
        if msg_type == "hello":
            log.debug("Slack Socket Mode hello")
            return

        if msg_type == "disconnect":
            log.warning("Slack requested disconnect: %s", msg)
            return

        # Event payload.
        envelope_type = msg.get("type")
        if envelope_type == "events_api":
            event = msg.get("event", {})
            await self._handle_event(event)
        elif envelope_type == "slash_commands":
            await self._handle_slash_command(msg)
        elif envelope_type == "block_actions":
            await self._handle_block_action(msg)
        else:
            # Fallback: treat top-level as an event if it has `event`.
            if "event" in msg:
                await self._handle_event(msg["event"])

    # ------------------------------------------------------------------ #
    # Event handlers                                                      #
    # ------------------------------------------------------------------ #

    async def _handle_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype not in ("app_mention", "message"):
            return

        # Ignore bot messages / subtypes.
        if event.get("subtype") in ("bot_message", "message_changed", "message_deleted"):
            return

        user_id = event.get("user", "")
        text = event.get("text", "").strip()
        channel = event.get("channel", "")
        thread_ts = event.get("ts")

        if not text or not user_id:
            return

        # Strip @bot mention if present.
        text = text.split(">", 1)[-1].strip() if ">" in text else text

        if not self._allowed or user_id not in self._allowed:
            await self._post_message(channel, "⛔ Access denied.", thread_ts=thread_ts)
            return

        if not self._rl.allow(user_id):
            await self._post_message(channel, "⏳ Too many messages. Please slow down.", thread_ts=thread_ts)
            return

        ts = await self._post_message(channel, "⏳ Thinking…", thread_ts=thread_ts)
        buffer_parts = []

        try:
            async for chunk in self._agent.handle(text):
                buffer_parts.append(chunk)
        except Exception as e:
            buffer_parts.append(f"\n[Agent error: {e}]")

        full = "".join(buffer_parts) or "Done."

        if ts and len(full) <= MAX_MSG_LEN:
            await self._update_message(channel, ts, full)
        else:
            if ts:
                await self._update_message(channel, ts, "(see below)")
            await self._send_chunked(channel, full)

    # ------------------------------------------------------------------ #
    # Slash commands                                                      #
    # ------------------------------------------------------------------ #

    async def _handle_slash_command(self, msg: dict) -> None:
        # Slack slash commands arrive as `command`, `text`, `user_id`, `channel_id`.
        cmd = msg.get("command", "").strip()
        text = msg.get("text", "").strip()
        user_id = msg.get("user_id", "")
        channel = msg.get("channel_id", "")

        if not self._allowed or user_id not in self._allowed:
            await self._post_message(channel, "⛔ Access denied.")
            return

        if not self._rl.allow(user_id):
            await self._post_message(channel, "⏳ Too many messages. Please slow down.")
            return

        # Build the full command text.
        full = f"/{cmd}"
        if text:
            full = f"{full} {text}"

        ts = await self._post_message(channel, "⏳ Running…")
        buffer_parts = []

        try:
            async for chunk in self._agent.handle(full):
                buffer_parts.append(chunk)
        except Exception as e:
            buffer_parts.append(f"\n[Agent error: {e}]")

        full_text = "".join(buffer_parts) or "Done."

        if ts and len(full_text) <= MAX_MSG_LEN:
            await self._update_message(channel, ts, full_text)
        else:
            if ts:
                await self._update_message(channel, ts, "(see below)")
            await self._send_chunked(channel, full_text)

    # ------------------------------------------------------------------ #
    # Interactive buttons                                                 #
    # ------------------------------------------------------------------ #

    async def _handle_block_action(self, msg: dict) -> None:
        actions = msg.get("actions", [])
        if not actions:
            return

        action_id = actions[0].get("action_id", "")
        action_ts = msg.get("message", {}).get("ts", "")
        channel = msg.get("channel", {}).get("id", "")
        user = msg.get("user", {}).get("id", "")

        action, _, token = action_id.partition(":")
        pending = self._pending.get(token)
        if not pending:
            await self._api(
                "chat.update",
                channel=channel,
                ts=action_ts,
                text="⚠️ This confirmation has expired.",
                blocks=[],
            )
            return

        approved = action == "approve"
        pending.resolve(approved)
        del self._pending[token]

        status = "✅ Approved" if approved else "❌ Rejected"
        await self._api(
            "chat.update",
            channel=channel,
            ts=action_ts,
            text=f"{status}: {pending.description[:200]}",
            blocks=[],
        )

        # Acknowledge the action to Slack.
        await self._api("chat.postMessage", channel=channel, text=f"{status}")
