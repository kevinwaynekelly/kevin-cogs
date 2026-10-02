"""Owned Discord scheduled-event mirrors with bounded, reload-safe reconciliation."""

import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timezone

import discord
from redbot.core import commands

log = logging.getLogger(__name__)
NATIVE_DEFAULTS = {"enabled": False, "channel": None, "minutes": 60}
TERMINAL = {discord.EventStatus.completed, discord.EventStatus.cancelled}


def marker(key, at):
    return f"Kevin's Community event {key}\nOccurrence: {at}"


def native_policy(channel, minutes):
    if not 1 <= minutes <= 1440:
        raise commands.BadArgument("Choose a duration from 1 to 1,440 minutes.")
    return {"enabled": True, "channel": channel.id if channel else None, "minutes": minutes}


class NativeEventBridge:
    def _native_permissions(self, guild, channel_id, *, editing=False):
        if guild.me is None:
            raise commands.CheckFailure("The bot's server member is unavailable.")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel_id and not isinstance(channel, discord.VoiceChannel):
            raise commands.BadArgument(
                "The native event's ordinary voice channel no longer exists."
            )
        permissions = channel.permissions_for(guild.me) if channel else guild.me.guild_permissions
        if not permissions.create_events and not (editing and permissions.manage_events):
            raise commands.CheckFailure("Give the bot Create Events for this event destination.")
        if channel and not (permissions.view_channel and permissions.connect):
            raise commands.CheckFailure(
                "The bot also needs View Channel and Connect in the event's voice channel."
            )
        return channel

    def _native_owned(self, remote, key):
        return remote.creator_id == self.bot.user.id and (remote.description or "").startswith(
            f"Kevin's Community event {key}\nOccurrence: "
        )

    async def _native_save(self, guild, key, native):
        async with self.config.guild(guild).social() as data:
            if key in data["events"]:
                data["events"][key]["native"] = deepcopy(native)

    async def _native_status(self, guild, key, item, remote, *, cancel=False):
        event = remote.get(item.get("id"))
        if not event or event.status in TERMINAL:
            return
        if not self._native_owned(event, key):
            raise commands.CheckFailure("This Discord event is not an owned Community mirror.")
        self._native_permissions(guild, event.channel_id, editing=True)
        now = self._now_ts()
        target = None
        if cancel or now >= item["end"]:
            target = (
                discord.EventStatus.completed
                if event.status is discord.EventStatus.active
                else discord.EventStatus.cancelled
            )
        elif now >= item["at"] and event.status is discord.EventStatus.scheduled:
            target = discord.EventStatus.active
        if target:
            remote[event.id] = await asyncio.wait_for(
                event.edit(status=target, reason="Community event lifecycle"), 15
            )

    async def _native_sync(self, guild, key, remote=None):
        if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
            return
        async with self._social_locks[(guild.id, "events", key)]:
            if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
                return
            record = await self._social_record(guild, "events", key)
            native = deepcopy(record.get("native"))
            if not native:
                return
            try:
                if remote is None:
                    events = await asyncio.wait_for(
                        guild.fetch_scheduled_events(with_counts=False), 10
                    )
                    remote = {event.id: event for event in events}
                if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
                    return
                cancel = not native["enabled"] or record.get("cancelled", False)
                archives = native.setdefault("archives", [])
                for item in archives:
                    await self._native_status(guild, key, item, remote, cancel=cancel)
                archives[:] = [
                    item
                    for item in archives
                    if item["id"] in remote and remote[item["id"]].status not in TERMINAL
                ][-5:]
                current = remote.get(native.get("id"))
                if native.get("id") and current is None:
                    # Manual deletion must not silently recreate the event.
                    native.update(enabled=False, id=None, error="DeletedInDiscord")
                    await self._native_save(guild, key, native)
                    return
                if cancel:
                    if current:
                        await self._native_status(guild, key, native, remote, cancel=True)
                    native.update(id=None, enabled=False, error=None)
                    await self._native_save(guild, key, native)
                    return
                if current and not self._native_owned(current, key):
                    raise commands.CheckFailure(
                        "This Discord event is not an owned Community mirror."
                    )
                if current and native.get("at", record["at"]) <= self._now_ts() < record["at"]:
                    # A repeating local event has moved to its next occurrence.
                    await self._native_status(guild, key, native, remote)
                    if remote[current.id].status not in TERMINAL:
                        archives.append({field: native[field] for field in ("id", "at", "end")})
                    native["id"] = None
                    current = None
                    await self._native_save(guild, key, native)
                if record["at"] <= self._now_ts() or record["closed"]:
                    if current:
                        await self._native_status(guild, key, native, remote)
                        if remote[current.id].status in TERMINAL:
                            native.update(enabled=False, finished=True)
                    native["error"] = None
                    await self._native_save(guild, key, native)
                    return
                channel = self._native_permissions(
                    guild, native["channel"], editing=current is not None
                )
                end = record["at"] + native["minutes"] * 60
                signature = {
                    "title": record["title"][:100],
                    "at": record["at"],
                    "end": end,
                    "channel": native["channel"],
                }
                if current is None:
                    # Reuse a create that reached Discord before a timeout or Config failure.
                    current = next(
                        (
                            event
                            for event in remote.values()
                            if self._native_owned(event, key)
                            and (event.description or "") == marker(key, record["at"])
                            and event.status not in TERMINAL
                        ),
                        None,
                    )
                arguments = {
                    "name": signature["title"],
                    "description": marker(key, record["at"]),
                    "start_time": datetime.fromtimestamp(record["at"], timezone.utc),
                    "end_time": datetime.fromtimestamp(end, timezone.utc),
                    "privacy_level": discord.PrivacyLevel.guild_only,
                    "entity_type": discord.EntityType.voice
                    if channel
                    else discord.EntityType.external,
                    "channel": channel,
                    "reason": "Community event mirror",
                }
                if not channel:
                    arguments["location"] = (
                        f"https://discord.com/channels/{guild.id}/{record['channel']}"
                    )
                if current is None:
                    if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
                        return
                    current = await asyncio.wait_for(guild.create_scheduled_event(**arguments), 15)
                elif (
                    current.status is discord.EventStatus.scheduled
                    and native.get("synced") != signature
                ):
                    current = await asyncio.wait_for(current.edit(**arguments), 15)
                remote[current.id] = current
                native.update(
                    id=current.id,
                    at=record["at"],
                    end=end,
                    synced=signature,
                    error=None,
                    finished=False,
                )
                await self._native_save(guild, key, native)
            except (
                discord.HTTPException,
                asyncio.TimeoutError,
                commands.CommandError,
                ValueError,
                TypeError,
            ) as error:
                native["error"] = type(error).__name__
                await self._native_save(guild, key, native)
                log.warning(
                    "Native event sync failed in guild %s (%s)", guild.id, type(error).__name__
                )

    async def _native_tick(self, guild):
        if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
            return
        rows = await self.config.guild(guild).social.events()
        keys = [
            key
            for key, row in rows.items()
            if (native := row.get("native"))
            and (
                native.get("enabled")
                or (native.get("id") and not native.get("finished"))
                or native.get("archives")
            )
        ]
        if not keys:
            return
        try:
            events = await asyncio.wait_for(guild.fetch_scheduled_events(with_counts=False), 10)
        except (discord.HTTPException, asyncio.TimeoutError) as error:
            for key in keys:
                async with self._social_locks[(guild.id, "events", key)]:
                    async with self.config.guild(guild).social() as data:
                        if key in data["events"] and data["events"][key].get("native"):
                            data["events"][key]["native"]["error"] = type(error).__name__
            return
        remote = {event.id: event for event in events}
        for key in keys:
            try:
                await self._native_sync(guild, key, remote)
            except commands.BadArgument:
                continue  # A local entry was pruned during the fetch.

    async def _native_link(self, guild, key, policy):
        async with self._social_locks[(guild.id, "events", key)]:
            if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
                raise commands.CheckFailure("Community is unloaded or disabled here.")
            async with self.config.guild(guild).social() as data:
                record = data["events"].get(key)
                if not record or record["closed"] or record["at"] <= self._now_ts():
                    raise commands.BadArgument("Choose an open future event.")
                native = record.setdefault("native", {})
                native.update(policy)
                native["guild_id"] = guild.id
        await self._native_sync(guild, key)
        await self._social_refresh(guild, "events", key)

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before, after):
        guild = after.guild
        if not guild or self._closing or await self.bot.cog_disabled_in_guild(self, guild):
            return
        rows = await self.config.guild(guild).social.events()
        for key, row in rows.items():
            if (
                not row.get("native", {}).get("enabled")
                or row["native"].get("id") != after.id
                or not self._native_owned(after, key)
            ):
                continue
            async with self._social_locks[(guild.id, "events", key)]:
                async with self.config.guild(guild).social() as data:
                    record = data["events"].get(key)
                    if not record or record.get("native", {}).get("id") != after.id:
                        continue
                    native = record["native"]
                    at = int(after.start_time.timestamp())
                    end = (
                        int(after.end_time.timestamp())
                        if after.end_time
                        else at + native["minutes"] * 60
                    )
                    projection = {
                        "title": after.name,
                        "at": at,
                        "end": end,
                        "channel": after.channel_id,
                    }
                    natural_repeat_end = (
                        after.status is discord.EventStatus.completed
                        and record.get("repeat_days")
                        and self._now_ts() >= native.get("end", end)
                    )
                    if after.status in TERMINAL and not record["closed"] and not natural_repeat_end:
                        record.update(closed=True, cancelled=True)
                        native.update(enabled=False, finished=True)
                    elif after.status not in TERMINAL and native.get("synced") != projection:
                        if record["at"] != at:
                            delay = record["at"] - record["reminder_at"]
                            record.update(
                                at=at, reminder_at=at - delay, notified=[], announced=False
                            )
                        record["title"] = after.name
                        native.update(
                            at=at,
                            end=end,
                            channel=after.channel_id,
                            minutes=max(1, min(1440, (end - at) // 60)),
                            synced=projection,
                        )
            await self._social_refresh(guild, "events", key)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event):
        guild = event.guild
        if not guild or self._closing or await self.bot.cog_disabled_in_guild(self, guild):
            return
        for key, row in (await self.config.guild(guild).social.events()).items():
            if row.get("native", {}).get("id") != event.id or not self._native_owned(event, key):
                continue
            async with self._social_locks[(guild.id, "events", key)]:
                async with self.config.guild(guild).social() as data:
                    current = data["events"].get(key)
                    if current and current.get("native", {}).get("id") == event.id:
                        current["native"].update(id=None, enabled=False, error="DeletedInDiscord")
            await self._social_refresh(guild, "events", key)
