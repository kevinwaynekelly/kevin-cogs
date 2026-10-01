"""Bounded recently started tracks and replay through the normal request path."""

import json
import time
import uuid

from redbot.core import commands

from .command_support import check_command
from .features import load_saved, saved_track
from .presentation import clip

HISTORY_DAYS = 30
HISTORY_COUNT = 100
HISTORY_BYTES = 512 * 1024


def recent_records(records, now):
    records = [record for record in records if now - record.get("at", 0) <= HISTORY_DAYS * 86400]
    records = records[-HISTORY_COUNT:]
    while records and len(json.dumps(records, ensure_ascii=False).encode()) > HISTORY_BYTES:
        records.pop(0)
    return records


class ListeningCommands:
    async def _record_listening_history(self, player):
        if (
            self._closing
            or player.resuming
            or not player.current
            or await self.bot.cog_disabled_in_guild(self, player.guild)
        ):
            return
        group = self.config.guild(player.guild)
        # Use the same lock as the history switch so disabling and erasing cannot
        # race a player that read the old switch before obtaining the list lock.
        async with group.music.get_lock():
            if not (await group.music())["history"]:
                return
            track = player.current
            if len(track.uri) > 2048:
                return
            metadata = saved_track(track)
            metadata.update(title=clip(track.title, 1024), author=clip(track.author, 256))
            record = {
                "id": uuid.uuid4().hex[:12],
                "at": int(time.time()),
                "requester": player._requesters.get(id(track), 0),
                "track": metadata,
            }
            section = group.listening_history
            async with section.get_lock():
                records = await section()
                await section.set(recent_records([*records, record], time.time()))

    async def _listening_records(self, group):
        section = group.listening_history
        async with section.get_lock():
            records = await section()
            recent = recent_records(records, time.time())
            if records != recent:
                await section.set(recent)
            return recent

    @commands.hybrid_group(name="history", invoke_without_command=True, fallback="list")
    @commands.guild_only()
    async def listening_history(self, ctx, page: int = 1):
        """Browse recent music starts and stable IDs for replay."""
        await check_command(ctx, self.play)
        await check_command(ctx, self.audio_play)
        records = list(reversed(await self._listening_records(self.config.guild(ctx.guild))))
        pages = max(1, (len(records) + 9) // 10)
        if not 1 <= page <= pages:
            raise commands.BadArgument(f"Choose a history page from 1 to {pages}.")
        lines = []
        for record in records[(page - 1) * 10 : page * 10]:
            tracks = load_saved([record["track"]])
            if not tracks:
                continue
            requester = f"<@{record['requester']}>" if record["requester"] else "Autoplay"
            lines.append(
                f"`{record['id']}` · {self._track_description(tracks[0])}\n"
                f"Requested by {requester} · <t:{record['at']}:R>"
            )
        await self._reply(
            ctx,
            "\n\n".join(lines) or "No recent songs. History starts when a song begins playing.",
            title=f"Listening history · {page}/{pages}",
        )

    @commands.hybrid_command(name="replay")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.member)
    async def replay(self, ctx, identifier: str):
        """Queue a recent song using its ID from history."""
        await check_command(ctx, self.play)
        await check_command(ctx, self.audio_play)
        records = await self._listening_records(self.config.guild(ctx.guild))
        record = next((r for r in records if r["id"] == identifier.lower()), None)
        tracks = load_saved([record["track"]]) if record else []
        if not tracks:
            raise commands.BadArgument("Choose a current song ID shown by history.")
        await self._queue_saved(ctx, tracks)

    @listening_history.command(name="clear")
    async def listening_clear(self, ctx):
        """Remove your own requests from this server's listening history."""
        section = self.config.guild(ctx.guild).listening_history
        async with section.get_lock():
            await section.set([r for r in await section() if r["requester"] != ctx.author.id])
        await self._reply(ctx, "Your listening history was cleared.", tone="success")

    @listening_history.command(name="purge")
    @commands.admin_or_permissions(manage_guild=True)
    async def listening_purge(self, ctx):
        """Clear the server's retained listening history."""
        section = self.config.guild(ctx.guild).listening_history
        async with section.get_lock():
            await section.set([])
        await self._reply(ctx, "Server listening history was cleared.", tone="success")

    async def _delete_listening_user(self, user_id):
        for gid in await self.config.all_guilds():
            section = self.config.guild_from_id(gid).listening_history
            async with section.get_lock():
                await section.set([r for r in await section() if r["requester"] != user_id])

    async def _listening_user_data(self, user_id):
        data = {}
        for gid in await self.config.all_guilds():
            records = await self._listening_records(self.config.guild_from_id(gid))
            personal = [r for r in records if r["requester"] == user_id]
            if personal:
                data[str(gid)] = personal
        return data
