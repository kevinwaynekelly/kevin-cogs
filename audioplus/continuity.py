"""Opt-in queue checkpoints, empty-room policy and shared server playlists."""

import asyncio
import logging
import time
from copy import deepcopy

from redbot.core import commands

from .features import collection_name, load_saved, privileged, saved_track
from .resolver import MAX_TRACKS

log = logging.getLogger(__name__)

CONTINUITY_DEFAULTS = {
    "recovery": False,
    "empty_pause": False,
    "empty_grace": 60,
    "normalize": False,
}


class AudioContinuity:
    async def _save_recovery(self, player):
        group = self.config.guild(player.guild)
        if player._recovery_cleared or not (await group.continuity())["recovery"]:
            return
        async with player.lock:
            tracks = ([player.current] if player.current else []) + list(player.queue)
            if player._restart:
                tracks = [player._restart[0]] + list(player.queue)
            record = {
                "at": int(time.time()),
                "tracks": [saved_track(t) for t in tracks[:MAX_TRACKS]],
                "position": player.position if player.current else 0,
                "paused": bool(player.paused),
                "volume": player.volume,
                "repeat": player.repeat,
                "requesters": [player._requesters.get(id(t), 0) for t in tracks[:MAX_TRACKS]],
            }
        section = group.recovery
        async with section.get_lock():
            if (
                not player._recovery_cleared
                and (await group.continuity())["recovery"]
                and not player.closed
                and self._players.get(player.guild.id) is player
            ):
                record["requesters"] = [
                    player._requesters.get(id(t), 0) for t in tracks[:MAX_TRACKS]
                ]
                await section.set(record if tracks else {})

    async def _continuity_tick(self):
        self._prune_sessions()
        for player in tuple(self._players.values()):
            if self._closing or player.closed:
                continue
            if await self.bot.cog_disabled_in_guild(self, player.guild):
                continue
            await self._save_recovery(player)
            await self._empty_room(player)
        # Recovery is a short-lived playback checkpoint, not listening history.
        if time.monotonic() < self._next_recovery_prune:
            return
        self._next_recovery_prune = time.monotonic() + 3600
        for gid, conf in (await self.config.all_guilds()).items():
            await self._listening_records(self.config.guild_from_id(gid))
            recovery = conf.get("recovery", {})
            if recovery and time.time() - recovery.get("at", 0) > 7 * 86400:
                section = self.config.guild_from_id(gid).recovery
                async with section.get_lock():
                    current = await section()
                    if current and time.time() - current.get("at", 0) > 7 * 86400:
                        await section.set({})

    async def _continuity_loop(self):
        await self._check_ready()
        while not self._closing:
            try:
                await self._continuity_tick()
            except Exception:
                log.exception("Audio continuity maintenance failed")
            await asyncio.sleep(10)

    async def _empty_room(self, player, *, now=None):
        settings = await self.config.guild(player.guild).continuity()
        now = time.monotonic() if now is None else now
        humans = any(not member.bot for member in player.voice.channel.members)
        if humans or not settings["empty_pause"]:
            self._empty_since.pop(player.guild.id, None)
            if player.guild.id in self._empty_paused:
                self._empty_paused.discard(player.guild.id)
                if player.paused and not player.closed:
                    player.resume()
            return
        self._empty_since.setdefault(player.guild.id, now)
        if player.playing:
            player.pause()
            self._empty_paused.add(player.guild.id)
        if now - self._empty_since[player.guild.id] >= settings["empty_grace"]:
            async with self._player_locks[player.guild.id]:
                # Recheck after lock acquisition; a member may have returned.
                latest = await self.config.guild(player.guild).continuity()
                if (
                    self._players.get(player.guild.id) is player
                    and latest["empty_pause"]
                    and not any(not m.bot for m in player.voice.channel.members)
                ):
                    await self._save_recovery(player)
                    await self._dispose_player(player.guild.id, preserve_recovery=True)

    async def _set_continuity(self, ctx, key, value):
        section = self.config.guild(ctx.guild).continuity
        async with section.get_lock():
            state = await section()
            state[key] = value
            await section.set(state)
        player = self._get_player(ctx.guild)
        if key == "normalize" and player:
            player.normalize = value
        if key == "recovery" and not value:
            async with self.config.guild(ctx.guild).recovery.get_lock():
                await self.config.guild(ctx.guild).recovery.set({})
        if key == "empty_pause" and player:
            await self._empty_room(player)
        await self._reply(
            ctx, "Music policy saved. Normalization applies when the next decoder starts."
        )

    @commands.hybrid_command(name="recoverqueue")
    @commands.guild_only()
    async def recover_queue(self, ctx):
        """Restore the saved song, position and queue after a restart."""
        await self._playlist_manager(ctx)
        group = self.config.guild(ctx.guild)
        if not (await group.continuity())["recovery"]:
            raise commands.BadArgument("Enable audioset recovery first.")
        async with self._player_locks[ctx.guild.id]:
            record = await group.recovery()
            if self._get_player(ctx.guild):
                raise commands.BadArgument(
                    "Disconnect the current player before restoring a checkpoint."
                )
            if not record or time.time() - record.get("at", 0) > 7 * 86400:
                raise commands.BadArgument(
                    "No recent saved queue. Checkpoints expire after seven days."
                )
            tracks = load_saved(record.get("tracks", []))
            if not tracks:
                raise commands.BadArgument("The saved queue is empty.")
        # Connection helper owns the same lock; reserve restoration separately.
        lock = self._recovery_locks[ctx.guild.id]
        async with lock:
            if self._get_player(ctx.guild):
                raise commands.BadArgument(
                    "A player was connected while restoring. Try again after disconnecting."
                )
            player, _ = await self._fetch_or_connect_player(ctx, queue_request=True)
            try:
                async with player.lock:
                    if player.current or player.queue or player.preparing:
                        raise commands.BadArgument(
                            "Music was queued while restoring. The checkpoint was kept."
                        )
                    player.volume = max(0, min(1000, int(record.get("volume", 100))))
                    player.repeat = (
                        record.get("repeat", "off")
                        if record.get("repeat") in {"off", "track", "queue"}
                        else "off"
                    )
                    player.normalize = (await group.continuity())["normalize"]
                    player._requesters.update(
                        {id(t): uid for t, uid in zip(tracks, record.get("requesters", []))}
                    )
                    player.queue.extend(tracks[1:])
                    start = (
                        max(0, min(record.get("position", 0), max(0, tracks[0].length - 1000)))
                        if tracks[0].length
                        else 0
                    )
                    player._restart = (tracks[0], start, bool(record.get("paused", False)))
                    player.context = ctx
                    player._start_worker()
                await self._reply(
                    ctx,
                    f"Recovered {len(tracks)} tracks and the saved playback position.",
                    tone="success",
                )
            finally:
                player.end_queue_request()

    async def _playlist_manager(self, ctx):
        if not await privileged(self, ctx.author, await self.config.guild(ctx.guild).music()):
            raise commands.CheckFailure("This action requires the DJ role or Manage Server.")

    @commands.hybrid_group(name="serverplaylist", invoke_without_command=True, fallback="list")
    @commands.guild_only()
    async def server_playlist(self, ctx):
        """Browse shared playlists and propose songs for DJ approval."""
        rows = await self.config.guild(ctx.guild).server_playlists()
        await self._reply(
            ctx,
            "\n".join(
                f"**{name}** · {len(row['tracks'])} tracks · {len(row['suggestions'])} suggestions"
                for name, row in rows.items()
            )
            or "No shared playlists. DJs can use serverplaylist create <name>.",
        )

    @server_playlist.command(name="create")
    async def server_playlist_create(self, ctx, name: str):
        """Create one of up to ten shared server playlists. DJs only."""
        await self._playlist_manager(ctx)
        name = collection_name(name)
        async with self.config.guild(ctx.guild).server_playlists() as rows:
            if name in rows or len(rows) >= 10:
                raise commands.BadArgument(
                    "That name exists or the server already has ten playlists."
                )
            rows[name] = {"tracks": [], "suggestions": []}
        await self._reply(ctx, "Server playlist created.")

    @server_playlist.command(name="suggest")
    @commands.cooldown(1, 10, commands.BucketType.member)
    async def server_playlist_suggest(self, ctx, name: str, *, query: str):
        """Suggest one public track for a shared playlist."""
        name = collection_name(name)
        rows = await self.config.guild(ctx.guild).server_playlists()
        if name not in rows:
            raise commands.BadArgument("Create that server playlist first.")
        tracks = await self._load_tracks(self._normalize_query(query))
        if not tracks:
            raise commands.BadArgument("No track matched.")
        async with self.config.guild(ctx.guild).server_playlists() as rows:
            row = rows.get(name)
            if row is None or len(row["suggestions"]) >= 100:
                raise commands.BadArgument(
                    "That playlist is missing or has 100 pending suggestions."
                )
            row["suggestions"].append({"user": ctx.author.id, "track": saved_track(tracks[0])})
        await self._reply(ctx, "Suggestion saved for DJ approval.")

    @server_playlist.command(name="show")
    async def server_playlist_show(self, ctx, name: str):
        """List approved shared playlist tracks with their positions."""
        row = await self.config.guild(ctx.guild).server_playlists.get_raw(
            collection_name(name), default=None
        )
        if row is None:
            raise commands.BadArgument("Unknown server playlist.")
        await self._reply(
            ctx,
            "\n".join(f"{i}. {item['track']['title']}" for i, item in enumerate(row["tracks"], 1))
            or "No approved tracks.",
        )

    @server_playlist.command(name="review")
    async def server_playlist_review(self, ctx, name: str):
        """List numbered pending suggestions. DJs only."""
        await self._playlist_manager(ctx)
        row = await self.config.guild(ctx.guild).server_playlists.get_raw(
            collection_name(name), default=None
        )
        if row is None:
            raise commands.BadArgument("Unknown server playlist.")
        await self._reply(
            ctx,
            "\n".join(
                f"{i}. {item['track']['title']} · <@{item['user']}>"
                for i, item in enumerate(row["suggestions"], 1)
            )
            or "No pending suggestions.",
        )

    @server_playlist.command(name="approve")
    async def server_playlist_approve(self, ctx, name: str, position: int, approved: bool = True):
        """Approve or reject a numbered suggestion. DJs only."""
        await self._playlist_manager(ctx)
        async with self.config.guild(ctx.guild).server_playlists() as rows:
            row = rows.get(collection_name(name))
            if row is None or not 1 <= position <= len(row["suggestions"]):
                raise commands.BadArgument(
                    "Choose a pending suggestion from serverplaylist review."
                )
            if approved and len(row["tracks"]) >= 100:
                raise commands.BadArgument("That playlist already has 100 songs.")
            item = row["suggestions"].pop(position - 1)
            if approved:
                row["tracks"].append(item)
        await self._reply(ctx, "Suggestion approved." if approved else "Suggestion rejected.")

    @server_playlist.command(name="play")
    async def server_playlist_play(self, ctx, name: str):
        """Queue a shared playlist using the normal voice and DJ policies."""
        row = await self.config.guild(ctx.guild).server_playlists.get_raw(
            collection_name(name), default={}
        )
        tracks = load_saved([item["track"] for item in row.get("tracks", [])])
        if not tracks:
            raise commands.BadArgument("That server playlist is empty or missing.")
        await self._queue_saved(ctx, tracks)

    @server_playlist.command(name="remove")
    async def server_playlist_remove(self, ctx, name: str, position: int):
        """Remove a numbered approved song. DJs only."""
        await self._playlist_manager(ctx)
        async with self.config.guild(ctx.guild).server_playlists() as rows:
            row = rows.get(collection_name(name))
            if row is None or not 1 <= position <= len(row["tracks"]):
                raise commands.BadArgument("Choose an approved track position.")
            row["tracks"].pop(position - 1)
        await self._reply(ctx, "Track removed.")

    @server_playlist.command(name="delete")
    async def server_playlist_delete(self, ctx, name: str):
        """Delete a shared playlist and suggestions. DJs only."""
        await self._playlist_manager(ctx)
        async with self.config.guild(ctx.guild).server_playlists() as rows:
            rows.pop(collection_name(name), None)
        await self._reply(ctx, "Shared playlist removed.")

    async def _continuity_delete_user(self, user_id):
        for player in self._players.values():
            player._requesters = {
                key: 0 if uid == user_id else uid for key, uid in player._requesters.items()
            }
            if player._last_requester == user_id:
                player._last_requester = 0
        for gid in await self.config.all_guilds():
            group = self.config.guild_from_id(gid)
            async with group.server_playlists() as rows:
                for row in rows.values():
                    row["suggestions"] = [r for r in row["suggestions"] if r["user"] != user_id]
                    for r in row["tracks"]:
                        if r["user"] == user_id:
                            r["user"] = 0
            async with group.recovery() as state:
                if state:
                    state["requesters"] = [
                        0 if uid == user_id else uid for uid in state.get("requesters", [])
                    ]
        for player in self._players.values():
            player._requesters = {
                key: 0 if uid == user_id else uid for key, uid in player._requesters.items()
            }

    async def _continuity_user_data(self, user_id):
        result = {}
        for gid, conf in (await self.config.all_guilds()).items():
            owned = {
                name: {
                    kind: [deepcopy(r) for r in row[kind] if r["user"] == user_id]
                    for kind in ("tracks", "suggestions")
                }
                for name, row in conf.get("server_playlists", {}).items()
            }
            owned = {name: row for name, row in owned.items() if any(row.values())}
            recovery = conf.get("recovery", {})
            tracks = [
                deepcopy(track)
                for track, uid in zip(recovery.get("tracks", []), recovery.get("requesters", []))
                if uid == user_id
            ]
            if owned or tracks:
                result[str(gid)] = {"server_playlists": owned, "recovery_tracks": tracks}
        return result
