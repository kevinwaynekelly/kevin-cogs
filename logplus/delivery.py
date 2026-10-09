"""Bounded event delivery with current routing and unsent-page retries."""

import asyncio
import io
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field

import discord

from .presentation import chunks

log = logging.getLogger(__name__)
FEATURE_DEFAULTS = {"routes": {}, "retry": True}
CATEGORIES = ("message", "reactions", "server", "invites", "member", "voice", "sched", "commands")
EVENT_SWITCH = {
    "message_edited": ("message", "edit"),
    "message_deleted": ("message", "delete"),
    "bulk_delete": ("message", "bulk_delete"),
    "pins_updated": ("message", "pins"),
    "reaction_added": ("reactions", "add"),
    "reaction_removed": ("reactions", "remove"),
    "reaction_cleared": ("reactions", "clear"),
    "member_joined": ("member", "join"),
    "member_left": ("member", "leave"),
    "member_kicked": ("member", "leave"),
    "roles_changed": ("member", "roles_changed"),
    "nick_changed": ("member", "nick_changed"),
    "timeout_updated": ("member", "timeout"),
    "user_banned": ("member", "ban"),
    "user_unbanned": ("member", "unban"),
    "cmd_thisbot": ("commands", "this_bot"),
    "cmd_otherbot": ("commands", "other_bots"),
    **{
        f"voice_{key}": ("voice", key)
        for key in ("join", "move", "leave", "mute", "deaf", "video", "stream")
    },
    **{
        f"sched_{event}": ("sched", switch)
        for event, switch in (
            ("created", "create"),
            ("updated", "update"),
            ("deleted", "delete"),
            ("user_add", "user_add"),
            ("user_rem", "user_remove"),
        )
    },
    **{
        f"invite_{event}": ("invites", switch)
        for event, switch in (("created", "create"), ("deleted", "delete"))
    },
    **{
        f"{noun}_{event}": ("server", f"{noun}_{switch}")
        for noun in ("channel", "role", "thread")
        for event, switch in (("created", "create"), ("deleted", "delete"), ("updated", "update"))
    },
    **{
        f"{key}_updated": ("server", f"{key}_update")
        for key in ("server", "emoji", "sticker", "integrations", "webhooks")
    },
}


class EventEmbed(discord.Embed):
    __slots__ = ("event_type",)

    def __init__(self, *args, event_type=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event_type = event_type


def fresh_status():
    return {
        "delivered": 0,
        "recovered": 0,
        "failures": 0,
        "dropped": 0,
        "last_error": "None",
        "last_failure": 0,
    }


@dataclass
class PendingLog:
    parts: deque
    source: int | None
    event_type: str | None
    category: str | None
    users: set = field(default_factory=set)
    size: int = 0
    created: float = field(default_factory=time.monotonic)
    retries: int = 0
    permission_change: bool = False


class LogDelivery:
    def _pending_log(self, embed, source, category=None):
        event_type = getattr(embed, "event_type", None)
        switch = EVENT_SWITCH.get(event_type)
        category = category or (switch[0] if switch else None)
        text = "\n".join(
            [
                embed.title or "",
                embed.description or "",
                embed.footer.text or "",
                *(f"{entry.name}\n{entry.value}" for entry in embed.fields),
            ]
        )
        users = {int(value) for value in re.findall(r"(?<!\d)\d{15,22}(?!\d)", text)}
        pages = self._presentation.pages(self._presentation.style(embed))
        return PendingLog(
            deque({"embed": page} for page in pages),
            source,
            event_type,
            category,
            users,
            sum(len(json.dumps(page.to_dict(), ensure_ascii=False).encode()) for page in pages),
        )

    def _delivery_failed(self, guild_id, error):
        status = self._delivery_status[guild_id]
        status["failures"] += 1
        status["last_failure"] = time.time()
        status["last_error"] = (
            f"HTTP {error.status}, Discord code {error.code}"
            if isinstance(error, discord.HTTPException)
            else "Discord send timed out"
        )

    async def _delivery_route(self, guild, record):
        if (
            self._closing
            or self.bot.get_guild(guild.id) is None
            or await self.bot.cog_disabled_in_guild(self, guild)
        ):
            return None
        conf = await self._settings(guild)
        switch = EVENT_SWITCH.get(record.event_type)
        if switch and not conf[switch[0]][switch[1]]:
            return None
        if record.category in {"message", "server"} and await self._is_exempt(
            guild, record.source, record.category
        ):
            return None
        return await self._log_channel(guild, record.source, record.category)

    async def _deliver_log(self, guild, record):
        while record.parts:
            channel = await self._delivery_route(guild, record)
            if channel is None:
                return False
            payload = record.parts[0]
            if "embed" in payload:
                payload = {
                    "embed": self._presentation.apply_theme(
                        payload["embed"], bot=self.bot, guild=guild
                    )
                }
            if not channel.permissions_for(guild.me).embed_links and "embed" in payload:
                page = payload["embed"]
                record.parts.popleft()
                text = f"**{page.title}**\n{page.description or ''}"
                for entry in page.fields:
                    text += f"\n\n**{entry.name}**\n{entry.value}"
                text += f"\n\n{page.footer.text}"
                record.parts.extendleft(
                    reversed([{"content": part} for part in chunks(text, 2000)])
                )
                payload = record.parts[0]
            message = await asyncio.wait_for(
                channel.send(**payload, allowed_mentions=discord.AllowedMentions.none()), 15
            )
            if self._closing:
                return False
            record.parts.popleft()
            self._own_log_ids[(guild.id, message.id)] = None
            while len(self._own_log_ids) > 10000:
                self._own_log_ids.popitem(last=False)
        self._delivery_status[guild.id]["delivered"] += 1
        if record.retries:
            self._delivery_status[guild.id]["recovered"] += 1
        return True

    def _start_retries(self, guild_id):
        task = self._retry_tasks.get(guild_id)
        if task is None or task.done():
            self._retry_tasks[guild_id] = asyncio.create_task(
                self._retry_logs(guild_id), name=f"logplus-delivery-{guild_id}"
            )

    def _enqueue_log(self, guild, record):
        if self._closing or self.bot.get_guild(guild.id) is None:
            return
        queue = self._retry_queues[guild.id]
        if (
            len(queue) >= 100
            or sum(len(entries) for entries in self._retry_queues.values()) >= 1000
            or sum(item.size for item in queue) + record.size > 2 * 1024 * 1024
            or sum(item.size for entries in self._retry_queues.values() for item in entries)
            + record.size
            > 8 * 1024 * 1024
        ):
            self._delivery_status[guild.id]["dropped"] += 1
            return
        queue.append(record)
        self._start_retries(guild.id)

    async def _retry_logs(self, guild_id):
        queue = self._retry_queues[guild_id]
        try:
            while queue:
                record = queue[0]
                guild = self.bot.get_guild(guild_id)
                if guild is None or time.monotonic() - record.created > 300:
                    queue.popleft()
                    self._delivery_status[guild_id]["dropped"] += 1
                    continue
                await asyncio.sleep(2 ** (record.retries + 1))
                record.retries += 1
                try:
                    if (
                        not (await self._settings(guild))["features"]["retry"]
                        or time.monotonic() - record.created > 300
                    ):
                        success = False
                    else:
                        success = await self._deliver_log(guild, record)
                except (discord.HTTPException, asyncio.TimeoutError) as error:
                    log.warning(
                        "Server log notification retry failed",
                        extra={
                            "notification_error": type(error).__name__,
                            "notification_stage": "log_retry",
                            "notification_guild_id": guild_id,
                        },
                    )
                    self._delivery_failed(guild_id, error)
                    if record.retries < 3:
                        continue
                    success = False
                except Exception as error:
                    log.warning(
                        "Server log notification retry failed",
                        extra={
                            "notification_error": type(error).__name__,
                            "notification_stage": "log_retry",
                            "notification_guild_id": guild_id,
                        },
                    )
                    self._delivery_status[guild_id]["last_error"] = "Unexpected delivery error"
                    success = False
                if not success:
                    self._delivery_status[guild_id]["dropped"] += 1
                queue.popleft()
        finally:
            if self._retry_tasks.get(guild_id) is asyncio.current_task():
                self._retry_tasks.pop(guild_id, None)
                self._retry_queues.pop(guild_id, None)

    async def _cancel_retries(self, guild_id):
        task = self._retry_tasks.pop(guild_id, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _queued_data(self, user_id):
        return {
            str(gid): [
                {
                    "source_channel": record.source,
                    "category": record.category,
                    "event_type": record.event_type,
                    "unsent_parts": [
                        {"embed": part["embed"].to_dict()} if "embed" in part else part
                        for part in record.parts
                    ],
                }
                for record in queue
                if user_id in record.users
            ]
            for gid, queue in self._retry_queues.items()
            if any(user_id in record.users for record in queue)
        }

    async def red_get_data_for_user(self, *, user_id):
        data = self._queued_data(user_id)
        result = (
            {"logplus-pending.json": io.BytesIO(json.dumps(data, indent=2).encode())}
            if data
            else {}
        )
        history = await self._history_user_data(user_id)
        if history:
            result["logplus-history.json"] = io.BytesIO(json.dumps(history, indent=2).encode())
        return result

    async def red_delete_data_for_user(self, *, requester, user_id):
        for gid, queue in list(self._retry_queues.items()):
            if any(user_id in record.users for record in queue):
                await self._cancel_retries(gid)
                remaining = deque(record for record in queue if user_id not in record.users)
                if remaining:
                    self._retry_queues[gid] = remaining
                    self._start_retries(gid)
                else:
                    self._retry_queues.pop(gid, None)
        self._audit_cache.clear()
        await self._history_user_data(user_id, delete=True)
