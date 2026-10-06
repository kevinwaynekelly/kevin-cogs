"""Opt-in, bounded music session summaries and checked personal playlist saving."""

import asyncio
import io
import json
import logging
import time
import uuid
from collections import Counter

import discord
from redbot.core import commands

from .command_support import check_command
from .features import collection_name, load_saved, saved_track
from .interactive import component_context, component_error
from .presentation import clip
from .requests import (
    PrivacyInterrupted,
    participant_request,
    personal_context,
    personal_request,
    privacy_write,
)

log = logging.getLogger(__name__)
SESSION_TTL = 180
SESSION_BYTES = 256 * 1024


class SessionPlaylistModal(discord.ui.Modal, title="Save music session"):
    name = discord.ui.TextInput(label="Playlist name", min_length=1, max_length=32)

    def __init__(self, view):
        super().__init__(timeout=180)
        self.session_view = view
        self.name.default = "session_" + view.record["id"][:8]

    async def on_submit(self, interaction):
        view = self.session_view
        try:
            async with personal_context(view.cog, interaction.guild_id, interaction.user.id):
                if view.is_finished() or time.time() - view.record["ended"] > SESSION_TTL:
                    raise commands.CheckFailure(
                        "This session expired. Save a playlist during the next session."
                    )
                ctx = await component_context(
                    view.cog, interaction, "playlist session", source_message=view.message
                )
                await view.cog._save_session_playlist(ctx, str(self.name), view.record)
        except commands.CommandError as error:
            await component_error(interaction, error)

    async def on_error(self, interaction, error):
        await component_error(interaction, error)


class SessionView(discord.ui.View):
    def __init__(self, cog, guild_id, record):
        super().__init__(timeout=SESSION_TTL)
        self.cog, self.guild_id, self.record = cog, guild_id, record
        self.message = None
        button = discord.ui.Button(
            label="Save session as playlist", style=discord.ButtonStyle.primary
        )

        async def save(interaction):
            try:
                if (
                    interaction.guild_id != guild_id
                    or self.is_finished()
                    or cog._closing
                    or time.time() - record["ended"] > SESSION_TTL
                ):
                    raise commands.CheckFailure(
                        "This music session expired or belongs to another server."
                    )
                # Check before opening the modal, then repeat on submission using its member.
                ctx = await component_context(cog, interaction, "playlist session", defer=False)
                await check_command(ctx, cog.playlist_save)
                await interaction.response.send_modal(SessionPlaylistModal(self))
            except commands.CommandError as error:
                await component_error(interaction, error)

        button.callback = save
        self.add_item(button)

    async def on_timeout(self):
        self.cog._views.discard(self)
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        await component_error(interaction, error)


class MusicSessions:
    def _clear_music_session(self, guild_id):
        self._sessions.pop(guild_id, None)
        self._last_sessions.pop(guild_id, None)
        for view in tuple(self._views):
            if isinstance(view, SessionView) and view.guild_id == guild_id:
                view.record["tracks"].clear()
                view.stop()
                self._views.discard(view)
        player = self._players.get(guild_id)
        if player:
            player._session_entry = None

    def _prune_sessions(self):
        for gid, record in tuple(self._last_sessions.items()):
            if time.time() - record["ended"] > SESSION_TTL:
                self._last_sessions.pop(gid, None)
        for view in tuple(self._views):
            if isinstance(view, SessionView) and time.time() - view.record["ended"] > SESSION_TTL:
                view.stop()
                self._views.discard(view)

    @privacy_write
    async def _session_started(self, player):
        player._session_entry = None
        if (
            self._closing
            or not player.current
            or not player.context
            or await self.bot.cog_disabled_in_guild(self, player.guild)
        ):
            return
        policy = await self.config.guild(player.guild).music()
        if not policy["session_summary"]:
            return
        self._prune_sessions()
        record = self._sessions.setdefault(
            player.guild.id,
            {
                "id": uuid.uuid4().hex[:12],
                "guild": player.guild.id,
                "at": int(time.time()),
                "ended": 0,
                "channel": player.context.channel.id,
                "tracks": [],
                "count": 0,
                "played_ms": 0,
            },
        )
        if (
            player.resuming
            and record["tracks"]
            and record["tracks"][-1]["track"]["uri"] == player.current.uri
        ):
            player._session_entry = record["tracks"][-1]
            return
        if len(player.current.uri) > 2048:
            return
        metadata = saved_track(player.current)
        metadata.update(
            title=clip(player.current.title, 512), author=clip(player.current.author, 128)
        )
        entry = {
            "track": metadata,
            "requester": player._requesters.get(id(player.current), 0),
            "played_ms": 0,
        }
        record["tracks"].append(entry)
        record["count"] += 1
        while len(record["tracks"]) > 100 or len(json.dumps(record).encode()) > SESSION_BYTES:
            record["tracks"].pop(0)
        player._session_entry = entry

    async def _session_segment(self, player, track, played_ms):
        record = self._sessions.get(player.guild.id)
        entry = getattr(player, "_session_entry", None)
        if record and entry and type(played_ms) is int and played_ms > 0:
            record["played_ms"] += played_ms
            entry["played_ms"] += played_ms

    async def _finish_music_session(self, player):
        try:
            await asyncio.wait_for(self._post_music_session(player), 10)
        except PrivacyInterrupted:
            operation = self._privacy.operations.get(asyncio.current_task())
            if operation and operation.invalidated:
                raise asyncio.CancelledError
            # Privacy may cancel the child posting a summary. Still clean up voice.
        except Exception as error:
            # A supplementary summary must never prevent disconnect/player cleanup.
            log.warning(
                "Music session summary delivery failed",
                extra={
                    "notification_error": type(error).__name__,
                    "notification_stage": "session_summary",
                    "notification_guild_id": player.guild.id,
                },
            )

    @participant_request
    async def _post_music_session(self, player):
        record = self._sessions.get(player.guild.id)
        if record:
            self._privacy.related(row["requester"] for row in record["tracks"])
        if (
            not record
            or not record["tracks"]
            or self._closing
            or await self.bot.cog_disabled_in_guild(self, player.guild)
        ):
            return
        policy = await self.config.guild(player.guild).music()
        if (
            not policy["session_summary"]
            or not record["tracks"]
            or self._sessions.get(player.guild.id) is not record
        ):
            return
        self._sessions.pop(player.guild.id, None)
        record["ended"] = int(time.time())
        self._last_sessions[player.guild.id] = record
        while len(self._last_sessions) > 20:
            self._last_sessions.pop(next(iter(self._last_sessions)))
        counts = Counter(row["requester"] for row in record["tracks"])
        requesters = ", ".join(
            f"{('<@' + str(uid) + '>') if uid else 'Autoplay'} ({count})"
            for uid, count in counts.most_common(10)
        )
        lines = [
            f"**Songs started** · {record['count']}\n**Playback time** · {record['played_ms'] // 60000}m {record['played_ms'] // 1000 % 60}s\n**Requesters** · {requesters}",
            "\n".join(
                f"{index}. {discord.utils.escape_markdown(row['track']['title'])}"
                for index, row in enumerate(record["tracks"][-10:], max(1, record["count"] - 9))
            ),
            "Save the retained session tracks as a personal playlist within three minutes. At most the latest 100 starts are retained; repeated songs remain in playback order.",
        ]
        channel = player.guild.get_channel_or_thread(policy["summary_channel"] or record["channel"])
        if channel is None:
            return
        view = SessionView(self, player.guild.id, record)
        active = [item for item in self._views if isinstance(item, SessionView)]
        if len(active) >= 20:
            oldest = min(active, key=lambda item: item.record["ended"])
            oldest.stop()
            self._views.discard(oldest)
        try:
            view.message = await self._reply(
                channel,
                "\n\n".join(lines),
                title="Music session summary",
                view=view,
                tracks=load_saved([row["track"] for row in reversed(record["tracks"][-10:])]),
            )
            self._views.add(view)
        except BaseException:
            view.stop()
            raise

    @personal_request
    async def _save_session_playlist(self, ctx, name, record):
        await check_command(ctx, self.playlist_save)
        if (
            self._closing
            or ctx.guild.id != record["guild"]
            or time.time() - record["ended"] > SESSION_TTL
        ):
            raise commands.CheckFailure("This music session expired.")
        name = collection_name(name)
        tracks = [row["track"] for row in record["tracks"]]
        if not tracks:
            raise commands.CommandError("No retained songs remain in this session.")
        section = self.config.guild(ctx.guild).playlists
        async with section.get_lock():
            all_lists = await section()
            personal = all_lists.setdefault(str(ctx.author.id), {})
            if name in personal or len(personal) >= 10:
                raise commands.BadArgument(
                    "Choose a new playlist name or delete one of your ten playlists first."
                )
            personal[name] = tracks
            await section.set(all_lists)
        await self._reply(
            ctx,
            f"Saved **{name}** with {len(tracks)} tracks.",
            tone="success",
            tracks=load_saved(tracks),
        )

    def _session_user_data(self, user_id, *, delete=False):
        self._prune_sessions()
        records = list(self._sessions.values()) + list(self._last_sessions.values())
        records.extend(view.record for view in self._views if isinstance(view, SessionView))
        personal = {}
        for record in records:
            rows = [row for row in record["tracks"] if row["requester"] == user_id]
            if rows:
                personal[record["id"]] = {"guild": record["guild"], "tracks": rows}
            if delete:
                record["tracks"] = [row for row in record["tracks"] if row["requester"] != user_id]
        if delete:
            for player in self._players.values():
                entry = getattr(player, "_session_entry", None)
                if entry and entry["requester"] == user_id:
                    player._session_entry = None
        return (
            {"music-sessions.json": io.BytesIO(json.dumps(personal).encode())} if personal else {}
        )
