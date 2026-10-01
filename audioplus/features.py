"""Music panels, vote policies, and bounded saved public track collections."""

import asyncio
import math
import re
from contextlib import suppress
from dataclasses import asdict
from typing import Optional

import discord
from redbot.core import commands

from .interactive import SetupView, component_context, component_error
from .presentation import clip, settings
from .resolver import MAX_TRACKS, MediaError, Track, http_url

DEFAULTS_GUILD = {
    "music": {
        "panel": True,
        "dj_role": None,
        "vote_skip": False,
        "fair_queue": False,
        "autoplay": False,
    },
    "playlists": {},
    "favorites": {},
}


def collection_name(name):
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
        raise commands.BadArgument(
            "Use a name of 1 to 32 lowercase letters, numbers, underscores, or hyphens."
        )
    return name


def saved_track(track):
    """Only public source metadata is stored, never a resolved Stream object."""
    return asdict(track)


def load_saved(records):
    tracks = []
    for record in records[:MAX_TRACKS]:
        try:
            item = {
                key: record[key] for key in ("uri", "title", "author", "length", "source", "direct")
            }
            item["uri"] = http_url(item["uri"])
            tracks.append(Track(**item))
        except (KeyError, TypeError, ValueError, MediaError):
            continue
    return tracks


async def privileged(cog, member, settings):
    return (
        bool(member.guild_permissions.manage_guild)
        or any(role.id == settings["dj_role"] for role in member.roles)
        or await cog.bot.is_owner(member)
    )


async def check_control(cog, ctx, *, vote=False):
    settings = await cog.config.guild(ctx.guild).music()
    # Preserve open legacy controls unless an administrator opts into a policy.
    if not settings["dj_role"] and not settings["vote_skip"]:
        return True
    if await privileged(cog, ctx.author, settings):
        return True
    player = cog._get_player(ctx.guild)
    channel = getattr(getattr(ctx.author, "voice", None), "channel", None)
    if player is None or channel != player.voice.channel:
        raise commands.CheckFailure("Join the bot's voice channel to use music controls.")
    if vote and settings["vote_skip"]:
        return False
    if settings["dj_role"]:
        raise commands.CheckFailure(
            "This control requires the configured DJ role or Manage Server."
        )
    return True


class MusicView(discord.ui.View):
    def __init__(self, cog, guild_id):
        super().__init__(timeout=None)
        self.cog, self.guild_id = cog, guild_id
        for label, path in (
            ("Pause / Resume", "pause"),
            ("Skip", "skip"),
            ("Queue", "queue"),
            ("Stop", "stop"),
        ):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)

            async def callback(interaction, path=path):
                try:
                    if interaction.guild_id != self.guild_id or self.cog._closing:
                        raise commands.CheckFailure("This music panel expired.")
                    player = self.cog._get_player(interaction.guild)
                    selected = "resume" if path == "pause" and player and player.paused else path
                    ctx = await component_context(self.cog, interaction, selected)
                    await ctx.command.callback(self.cog, ctx)
                    await self.cog._update_panel(player)
                except commands.CommandError as error:
                    await component_error(interaction, error)

            button.callback = callback
            self.add_item(button)

    async def on_error(self, interaction, error, item):
        await component_error(interaction, error)


def vote_threshold(channel):
    return max(1, math.ceil(len([member for member in channel.members if not member.bot]) / 2))


class SearchView(discord.ui.View):
    def __init__(self, cog, ctx, tracks):
        super().__init__(timeout=180)
        self.cog, self.owner_id, self.guild_id = cog, ctx.author.id, ctx.guild.id
        self.tracks, self.message, self.used = tracks, None, False
        self.lock = asyncio.Lock()
        selector = discord.ui.Select(
            placeholder="Choose a song",
            options=[
                discord.SelectOption(
                    label=clip(track.title, 100),
                    value=str(index),
                    description=clip(track.author + " · " + cog._duration(track.length), 100),
                )
                for index, track in enumerate(tracks)
            ],
        )

        async def select(interaction):
            try:
                if interaction.guild_id != self.guild_id or cog._closing:
                    raise commands.CheckFailure("This search picker expired.")
                context = await component_context(
                    cog, interaction, "search", owner_id=self.owner_id
                )
                async with self.lock:
                    if self.used or self.is_finished():
                        raise commands.CommandError(
                            "This search picker already finished. Search again."
                        )
                    await cog._queue_saved(context, [self.tracks[int(selector.values[0])]])
                    self.used = True
                    self.stop()
                    await self.on_timeout()
            except commands.CommandError as error:
                await component_error(interaction, error)

        selector.callback = select
        self.add_item(selector)
        cog._views.add(self)

    async def on_timeout(self):
        self.cog._views.discard(self)
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(view=None)

    async def on_error(self, interaction, error, item):
        await component_error(interaction, error)


class AudioCommands:
    async def _autoplay_next(self, player, previous):
        if self._closing or player.closed or not player.autoplay or player.queue:
            return
        if await self.bot.cog_disabled_in_guild(self, player.guild):
            return
        if not any(not member.bot for member in player.voice.channel.members):
            return
        generation = player._autoplay_generation
        player.begin_queue_request()
        try:
            terms = previous.author if previous.author != "Unknown" else previous.title
            tracks = await asyncio.wait_for(self._resolver.search(terms + " audio", limit=10), 15)
            selected = next((track for track in tracks if track.uri not in player.recent), None)
            if (
                selected
                and not self._closing
                and not player.closed
                and player.autoplay
                and not player.queue
                and generation == player._autoplay_generation
                and not await self.bot.cog_disabled_in_guild(self, player.guild)
            ):
                await player.enqueue([selected])
        except (MediaError, asyncio.TimeoutError):
            # Exhausted or unavailable suggestions fall back to the normal idle departure.
            pass
        finally:
            player.end_queue_request()

    @commands.hybrid_command(name="search")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.member)
    async def search(self, ctx, *, query: str):
        """Choose a song from search results before joining voice."""
        task = asyncio.create_task(self._resolver.search(query, limit=10))
        self._lookups.add(task)
        try:
            tracks = (await task)[:10]
        except MediaError as error:
            raise commands.CommandError(str(error)) from error
        finally:
            self._lookups.discard(task)
        if self._closing:
            raise commands.CommandError("AudioPlus is unloading.")
        if not tracks:
            return await self._reply(ctx, "No results. Try another search.", tone="warning")
        view = SearchView(self, ctx, tracks)
        view.message = await self._reply(
            ctx,
            "Choose a song below. Only you can use this picker, and it expires in three minutes.",
            title="Search results",
            view=view,
        )

    async def _track_started(self, player):
        self._skip_votes.pop(player.guild.id, None)
        await self._update_panel(player)
        if player.guild.id not in self._panel_tasks and player.guild.id in self._panels:

            async def refresh():
                try:
                    while not player.closed:
                        await asyncio.sleep(15)
                        await self._update_panel(player)
                        if player.guild.id not in self._panels:
                            break
                finally:
                    if self._panel_tasks.get(player.guild.id) is asyncio.current_task():
                        self._panel_tasks.pop(player.guild.id, None)

            self._panel_tasks[player.guild.id] = asyncio.create_task(
                refresh(), name=f"AudioPanel:{player.guild.id}"
            )

    async def _update_panel(self, player):
        if not player or player.closed or not player.context:
            return
        if not (await self.config.guild(player.guild).music())["panel"]:
            await self._close_panel(player.guild.id)
            return
        channel = player.context.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        track = player.current
        description = (
            self._track_description(track)
            if track
            else "Playback finished. The player will leave after 10 idle seconds."
        )
        embed = self._presentation.embed("Now playing", clip(description, 1700))
        if track:
            embed.add_field(
                name="Progress",
                value=f"{self._duration(player.position)} / {self._duration(track.length)}",
            )
        embed.add_field(
            name="Player",
            value=f"{'Paused' if player.paused else 'Playing' if track else 'Idle'} · {player.volume}% · Repeat {player.repeat}",
        )
        embed.add_field(name="Upcoming", value=str(len(player.queue)))
        embed = self._presentation.apply_theme(embed, bot=self.bot, guild=player.guild)
        entry = self._panels.get(player.guild.id)
        use_embeds = channel.permissions_for(player.guild.me).embed_links
        requested = getattr(player.context, "embed_requested", None)
        if requested:
            use_embeds = use_embeds and await requested()
        text = clip(
            f"**{embed.title}**\n{embed.description}\n"
            + "\n".join(f"**{field.name}** · {field.value}" for field in embed.fields)
            + f"\n{embed.footer.text}",
            2000,
        )
        try:
            if entry and entry[0].id == channel.id:
                await entry[1].edit(
                    content=None if use_embeds else text,
                    embed=embed if use_embeds else None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await self._close_panel(player.guild.id, cancel_task=False)
                view = MusicView(self, player.guild.id)
                message = await channel.send(
                    content=None if use_embeds else text,
                    embed=embed if use_embeds else None,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                self._panels[player.guild.id] = (channel, message, view)
        except discord.HTTPException:
            await self._close_panel(player.guild.id)

    async def _close_panel(self, guild_id, *, cancel_task=True):
        task = self._panel_tasks.pop(guild_id, None) if cancel_task else None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        entry = self._panels.pop(guild_id, None)
        if entry:
            entry[2].stop()
            with suppress(discord.HTTPException):
                await entry[1].edit(view=None)

    def _active_player(self, guild):
        player = self._get_player(guild)
        if not player:
            raise commands.CommandError("Not connected.")
        return player

    @commands.hybrid_command(name="seek")
    @commands.guild_only()
    async def seek(self, ctx, position: str):
        """Seek to seconds or a minutes:seconds timestamp."""
        await check_control(self, ctx)
        if not re.fullmatch(r"\d+(?::[0-5]\d){0,2}", position):
            raise commands.BadArgument("Use seconds, minutes:seconds, or hours:minutes:seconds.")
        seconds = 0
        for part in position.split(":"):
            seconds = seconds * 60 + int(part)
        try:
            await self._active_player(ctx.guild).seek(seconds * 1000)
        except MediaError as error:
            raise commands.CommandError(str(error)) from error
        await self._reply(ctx, f"Seeking to {position}.", tone="success")

    @commands.hybrid_command(name="remove")
    @commands.guild_only()
    async def remove(self, ctx, position: int):
        """Remove one upcoming song by its queue position."""
        await check_control(self, ctx)
        try:
            track = await self._active_player(ctx.guild).remove(position)
        except MediaError as error:
            raise commands.CommandError(str(error)) from error
        await self._reply(
            ctx, f"Removed **{discord.utils.escape_markdown(track.title)}**.", tone="success"
        )

    @commands.hybrid_command(name="move")
    @commands.guild_only()
    async def move(self, ctx, source: int, destination: int):
        """Move an upcoming song to another queue position."""
        await check_control(self, ctx)
        try:
            track = await self._active_player(ctx.guild).move(source, destination)
        except MediaError as error:
            raise commands.CommandError(str(error)) from error
        await self._reply(
            ctx,
            f"Moved **{discord.utils.escape_markdown(track.title)}** to {destination}.",
            tone="success",
        )

    @commands.hybrid_group(name="playlist", invoke_without_command=True, fallback="list")
    @commands.guild_only()
    async def playlist(self, ctx):
        """Save and play your server-specific playlists."""
        collections = await self.config.guild(ctx.guild).get_raw(
            "playlists", str(ctx.author.id), default={}
        )
        await self._reply(
            ctx,
            "\n".join(
                f"**{name}** · {len(items)} tracks" for name, items in sorted(collections.items())
            )
            or "No saved playlists. Use playlist save <name> while music is queued.",
        )

    @playlist.command(name="save")
    async def playlist_save(self, ctx, name: str):
        """Save the current song and upcoming queue."""
        name = collection_name(name)
        player = self._active_player(ctx.guild)
        async with player.lock:
            tracks = ([player.current] if player.current else []) + list(player.queue)
        if not tracks or len(tracks) > MAX_TRACKS:
            raise commands.CommandError(f"Save between 1 and {MAX_TRACKS} tracks.")
        async with self.config.guild(ctx.guild).playlists() as all_lists:
            personal = all_lists.setdefault(str(ctx.author.id), {})
            if name not in personal and len(personal) >= 10:
                raise commands.CommandError("Delete a playlist first. You can save ten per server.")
            personal[name] = [saved_track(track) for track in tracks]
        await self._reply(ctx, f"Saved **{name}** with {len(tracks)} tracks.", tone="success")

    @playlist.command(name="play")
    async def playlist_play(self, ctx, name: str):
        """Queue one of your saved playlists."""
        records = await self.config.guild(ctx.guild).get_raw(
            "playlists", str(ctx.author.id), collection_name(name), default=[]
        )
        tracks = load_saved(records)
        if not tracks:
            raise commands.BadArgument("That saved playlist is empty or missing.")
        await self._queue_saved(ctx, tracks)

    @playlist.command(name="delete")
    async def playlist_delete(self, ctx, name: str):
        """Delete one of your saved playlists."""
        async with self.config.guild(ctx.guild).playlists() as all_lists:
            all_lists.get(str(ctx.author.id), {}).pop(collection_name(name), None)
        await self._reply(ctx, "Playlist deleted.", tone="success")

    @commands.hybrid_group(name="favorite", invoke_without_command=True, fallback="list")
    @commands.guild_only()
    async def favorite(self, ctx):
        """Save, list, and play your favorite songs."""
        records = await self.config.guild(ctx.guild).get_raw(
            "favorites", str(ctx.author.id), default=[]
        )
        await self._reply(
            ctx,
            "\n".join(
                f"{index}. {discord.utils.escape_markdown(track.title)}"
                for index, track in enumerate(load_saved(records), 1)
            )
            or "No favorites. Use favorite add while a song plays.",
        )

    @favorite.command(name="add")
    async def favorite_add(self, ctx, *, query: str = ""):
        """Favorite the current song or a search result."""
        if query:
            tracks = await self._load_tracks(self._normalize_query(query))
            selected = tracks[0] if tracks else None
        else:
            player = self._active_player(ctx.guild)
            selected = player.current
        if not selected:
            raise commands.CommandError("No song was found or is currently playing.")
        async with self.config.guild(ctx.guild).favorites() as favorites:
            records = favorites.setdefault(str(ctx.author.id), [])
            if not any(record["uri"] == selected.uri for record in records):
                if len(records) >= MAX_TRACKS:
                    raise commands.CommandError("You can save 100 favorites per server.")
                records.append(saved_track(selected))
        await self._reply(
            ctx, f"Saved **{discord.utils.escape_markdown(selected.title)}**.", tone="success"
        )

    @favorite.command(name="remove")
    async def favorite_remove(self, ctx, position: int):
        """Remove a favorite by its list position."""
        async with self.config.guild(ctx.guild).favorites() as favorites:
            records = favorites.get(str(ctx.author.id), [])
            if not 1 <= position <= len(records):
                raise commands.BadArgument("Choose a position shown by favorite.")
            records.pop(position - 1)
        await self._reply(ctx, "Favorite removed.", tone="success")

    @favorite.command(name="play")
    async def favorite_play(self, ctx, position: int = 0):
        """Play all favorites or one numbered favorite."""
        records = await self.config.guild(ctx.guild).get_raw(
            "favorites", str(ctx.author.id), default=[]
        )
        tracks = load_saved(records)
        if position:
            if not 1 <= position <= len(tracks):
                raise commands.BadArgument("Choose a position shown by favorite.")
            tracks = [tracks[position - 1]]
        if not tracks:
            raise commands.CommandError("No favorites saved.")
        await self._queue_saved(ctx, tracks)

    async def _queue_saved(self, ctx, tracks):
        player, _ = await self._fetch_or_connect_player(ctx, queue_request=True)
        try:
            await self._enqueue(player, tracks, ctx)
        finally:
            player.end_queue_request()
        await self._reply_queued(ctx, tracks)

    async def _set_music_setting(self, guild, key, value):
        group = self.config.guild(guild).music
        async with group.get_lock():
            await group.get_attr(key).set(value)
        player = self._get_player(guild)
        if player and key in {"fair_queue", "autoplay"}:
            async with player.lock:
                setattr(player, key, value)
                if key == "fair_queue":
                    player._balance_queue()
                else:
                    player._autoplay_generation += 1

    @commands.hybrid_group(name="audioset", invoke_without_command=True, fallback="status")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def audioset(self, ctx):
        """Configure music panels and DJ controls."""
        conf = await self.config.guild(ctx.guild).music()
        await self._reply(
            ctx,
            settings(
                f"Player panel = {conf['panel']}\n"
                f"DJ role = {'<@&' + str(conf['dj_role']) + '>' if conf['dj_role'] else 'Open controls'}\n"
                f"Vote skipping = {conf['vote_skip']}"
                f"\nFair queue = {conf['fair_queue']}\nAutoplay = {conf['autoplay']}"
            )
            + f"\nUse `{ctx.clean_prefix}audioset setup` for guided settings.",
        )

    @audioset.command(name="panel")
    async def audioset_panel(self, ctx, enabled: bool):
        """Enable or disable automatic player panels."""
        await self._set_music_setting(ctx.guild, "panel", enabled)
        if not enabled:
            await self._close_panel(ctx.guild.id)
        await self._presentation.confirm(ctx)

    @audioset.command(name="dj")
    async def audioset_dj(self, ctx, role: Optional[discord.Role] = None):
        """Set a DJ role, or clear it for open controls."""
        await self._set_music_setting(ctx.guild, "dj_role", role.id if role else None)
        await self._presentation.confirm(ctx)

    @audioset.command(name="voteskip")
    async def audioset_voteskip(self, ctx, enabled: bool):
        """Require a listener vote for non-DJ skips."""
        await self._set_music_setting(ctx.guild, "vote_skip", enabled)
        self._skip_votes.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @audioset.command(name="fairqueue")
    async def audioset_fairqueue(self, ctx, enabled: bool):
        """Alternate requesters while preserving each person's song order."""
        await self._set_music_setting(ctx.guild, "fair_queue", enabled)
        player = self._get_player(ctx.guild)
        if player:
            async with player.lock:
                player.fair_queue = enabled
                player._balance_queue()
        await self._presentation.confirm(ctx)

    @audioset.command(name="autoplay")
    async def audioset_autoplay(self, ctx, enabled: bool):
        """Suggest more music after the queue ends, or retain the idle departure."""
        await self._set_music_setting(ctx.guild, "autoplay", enabled)
        player = self._get_player(ctx.guild)
        if player:
            player.autoplay = enabled
            player._autoplay_generation += 1
        await self._presentation.confirm(ctx)

    @audioset.command(name="setup")
    async def audioset_setup(self, ctx):
        """Choose music settings in a guided panel."""

        async def update(context, key, value):
            await self._set_music_setting(context.guild, key, value)
            if key == "panel" and not value:
                await self._close_panel(context.guild.id)
            self._skip_votes.pop(context.guild.id, None)

        view = SetupView(
            self,
            ctx,
            "audioset setup",
            [
                ("dj_role", "DJ role", "role"),
                ("panel", "player panel", "toggle"),
                ("vote_skip", "vote skip", "toggle"),
                ("fair_queue", "fair queue", "toggle"),
                ("autoplay", "autoplay", "toggle"),
            ],
            update,
        )
        view.message = await self._reply(
            ctx,
            "Select your DJ role and enable or disable the player panel and vote skipping. Omit the role in audioset dj to clear it. This panel expires in three minutes.",
            title="Music setup",
            view=view,
        )

    @audioset.command(name="recovery")
    async def audioset_recovery(self, ctx, enabled: bool):
        """Save queue checkpoints for explicit recovery after a restart."""
        await self._set_continuity(ctx, "recovery", enabled)

    @audioset.command(name="emptypause")
    async def audioset_empty_pause(self, ctx, enabled: bool, grace: int = 60):
        """Pause empty voice rooms and leave after 10 to 3600 seconds."""
        if not 10 <= grace <= 3600:
            raise commands.BadArgument("Choose a grace period of 10 to 3600 seconds.")
        section = self.config.guild(ctx.guild).continuity
        async with section.get_lock():
            state = await section()
            state.update(empty_grace=grace)
            await section.set(state)
        await self._set_continuity(ctx, "empty_pause", enabled)

    @audioset.command(name="normalize")
    async def audioset_normalize(self, ctx, enabled: bool):
        """Normalize loudness on subsequent FFmpeg decoders."""
        await self._set_continuity(ctx, "normalize", enabled)
