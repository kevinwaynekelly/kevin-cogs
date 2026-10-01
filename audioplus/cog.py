from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Optional

import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

from .backend import diagnostics, require_voice
from .player import GuildPlayer
from .presentation import Presentation
from .resolver import MediaError, MediaResolver, normalize_query

log = logging.getLogger(__name__)
GUILD_ONLY = commands.guild_only()


class AudioPlus(commands.Cog):
    """Music search, native Discord playback, queues, and voice diagnostics."""

    # Retain the old Config namespace/defaults for upgrades and rollbacks. These
    # legacy node values no longer participate in playback or diagnostics.
    default_global = {
        "host": "127.0.0.1",
        "port": 2333,
        "password": "youshallnotpass",
        "secure": False,
        "resume_timeout": 60,
    }

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self._presentation = Presentation("AudioPlus", "audio")
        self.config = Config.get_conf(self, identifier=0xA10DEFAB, force_registration=True)
        self.config.register_global(**self.default_global)
        self._resolver = MediaResolver()
        self._players = {}
        self._player_locks = defaultdict(asyncio.Lock)
        self._lookups = set()
        self._closing = False

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    async def cog_load(self):
        # Setup/help remain available when the container needs dependencies.
        self._closing = False

    async def cog_unload(self):
        self._closing = True
        for task in tuple(self._lookups):
            task.cancel()
        await asyncio.gather(*tuple(self._lookups), return_exceptions=True)
        players = list(self._players.values())
        self._players.clear()
        await asyncio.gather(*(player.close() for player in players), return_exceptions=True)
        await self._resolver.close()
        self._player_locks.clear()

    def _get_player(self, guild):
        player = self._players.get(guild.id)
        return (
            player if player and not player.closed and guild.voice_client is player.voice else None
        )

    async def _stage_unsuppress_if_needed(self, guild, channel):
        if not isinstance(channel, discord.StageChannel):
            return
        voice = getattr(guild.me, "voice", None)
        if voice and voice.suppress:
            try:
                await guild.me.edit(suppress=False, reason="AudioPlus playback")
            except discord.Forbidden:
                try:
                    await guild.me.request_to_speak()
                except discord.HTTPException:
                    log.debug("Could not request Stage speaking access")

    async def _force_undeafen(self, guild, channel):
        try:
            await guild.change_voice_state(channel=channel, self_deaf=False, self_mute=False)
            return True
        except discord.HTTPException:
            return False

    async def _dispose_player(self, guild_id, *, disconnect=True):
        player = self._players.pop(guild_id, None)
        if player:
            await player.close(disconnect=disconnect)

    @staticmethod
    def _snapshot(player):
        resume = player._restart
        if resume is None and player.current:
            resume = (player.current, player.position, player.paused)
        return resume, list(player.queue), player.volume, player.repeat, player.context

    @staticmethod
    def _retain(player, snapshot):
        resume, queued, player.volume, player.repeat, player.context = snapshot
        player.closed = True
        player.queue.clear()
        player.queue.extend(queued)
        player._restart = resume

    async def _restore(self, player, snapshot):
        resume, queued, player.volume, player.repeat, player.context = snapshot
        if resume:
            await player.restart(resume[0], start=resume[1], paused=resume[2])
        await player.enqueue(queued, player.context)

    async def _fetch_or_connect_player(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        voice = getattr(ctx.author, "voice", None)
        if not voice or not voice.channel:
            raise commands.UserInputError("Join a voice channel first.")
        channel = voice.channel
        try:
            require_voice()
        except MediaError as exc:
            raise commands.CommandError(str(exc)) from exc
        async with self._player_locks[ctx.guild.id]:
            if self._closing:
                raise commands.CommandError("AudioPlus is unloading. Try again after it reloads.")
            player = self._get_player(ctx.guild)
            previous = self._players.get(ctx.guild.id)
            existing = ctx.guild.voice_client
            if existing and (previous is None or existing is not previous.voice):
                raise commands.CommandError(
                    "Another cog owns the voice connection. Disconnect it before using AudioPlus."
                )
            permissions = channel.permissions_for(ctx.guild.me)
            missing = [name for name in ("connect", "speak") if not getattr(permissions, name)]
            if missing:
                raise commands.BotMissingPermissions(
                    discord.Permissions(**dict.fromkeys(missing, True))
                )
            try:
                if player is None or not player.voice.is_connected():
                    snapshot = self._snapshot(previous) if previous else None
                    await self._dispose_player(ctx.guild.id)
                    try:
                        vc = await channel.connect(
                            cls=discord.VoiceClient,
                            timeout=30,
                            reconnect=True,
                            self_deaf=False,
                            self_mute=False,
                        )
                    except BaseException:
                        if previous and snapshot:
                            self._retain(previous, snapshot)
                            self._players[ctx.guild.id] = previous
                        raise
                    player = GuildPlayer(vc, self._resolver, self._report_playback_failure)
                    self._players[ctx.guild.id] = player
                    if snapshot:
                        await self._restore(player, snapshot)
                elif player.voice.channel != channel:
                    await player.voice.move_to(channel, timeout=30)
                    await self._force_undeafen(ctx.guild, channel)
            except (
                asyncio.TimeoutError,
                discord.ClientException,
                discord.HTTPException,
                RuntimeError,
            ) as exc:
                raise commands.CommandError(
                    "Discord voice could not connect. Check audio pingnode, channel permissions, and the Red container's UDP network access."
                ) from exc
            player.context = ctx
            await self._stage_unsuppress_if_needed(ctx.guild, channel)
            return player, channel

    @staticmethod
    def _normalize_query(query):
        try:
            return normalize_query(query)
        except MediaError as exc:
            raise commands.BadArgument(str(exc)) from exc

    async def _load_tracks(self, query):
        if self._closing:
            raise commands.CommandError("AudioPlus is unloading.")
        task = asyncio.create_task(self._resolver.search(query))
        self._lookups.add(task)
        try:
            return await task
        except MediaError as exc:
            raise commands.CommandError(str(exc)) from exc
        finally:
            self._lookups.discard(task)

    async def _enqueue(self, player, tracks, ctx):
        try:
            await player.enqueue(tracks, ctx)
        except MediaError as exc:
            raise commands.CommandError(str(exc)) from exc

    async def _report_playback_failure(self, player, track, cause):
        if self._closing or self._players.get(player.guild.id) is not player or not player.context:
            return
        ctx = player.context
        title = discord.utils.escape_markdown(track.title)
        await self._reply(
            ctx,
            f"**{title}**\n{cause}\n\nRun `{ctx.clean_prefix}audio pingnode` for local dependency checks or `{ctx.clean_prefix}audio tone` to test direct audio.",
            title="Playback failed",
            tone="error",
        )

    async def _rebind_voice(self, guild):
        async with self._player_locks[guild.id]:
            old = self._get_player(guild)
            if not old or not old.voice.channel:
                return False
            channel, snapshot = old.voice.channel, self._snapshot(old)
            await self._dispose_player(guild.id)
            try:
                require_voice()
                vc = await channel.connect(
                    cls=discord.VoiceClient,
                    timeout=30,
                    reconnect=True,
                    self_deaf=False,
                    self_mute=False,
                )
                player = GuildPlayer(vc, self._resolver, self._report_playback_failure)
                self._players[guild.id] = player
                await self._restore(player, snapshot)
                await self._stage_unsuppress_if_needed(guild, channel)
                return True
            except (
                MediaError,
                discord.HTTPException,
                discord.ClientException,
                asyncio.TimeoutError,
                RuntimeError,
            ):
                # Retain the tracks if reconnection fails so a later join can recover them.
                await self._dispose_player(guild.id)
                self._retain(old, snapshot)
                self._players[guild.id] = old
                log.warning("AudioPlus voice rejoin failed in guild %s", guild.id)
                return False

    async def _diagnostic_reply(self, ctx):
        state = await diagnostics()
        lines = [
            "**Backend** · Native Discord voice",
            "**Lavalink** · Not used",
            f"**Discord.py** · {discord.__version__}",
            f"**FFmpeg** · {state['ffmpeg']}",
        ]
        lines += [f"**{name}** · {version}" for name, version in state["packages"].items()]
        lines += [
            f"**Deno** · {state['deno']}",
            f"**Node.js** · {state['node']}",
            f"**QuickJS** · {state['quickjs']}",
            "**YouTube runtime** · "
            + (
                ", ".join(state["runtimes"])
                or "Missing, install Deno 2.3+ or Node.js 22+ in the Red container"
            ),
        ]
        if state["voice_error"]:
            lines.append("\n" + state["voice_error"])
        player = self._get_player(ctx.guild)
        if player:
            lines += [
                f"**Voice connected** · {player.voice.is_connected()}",
                f"**Queued tracks** · {len(player.queue)}",
            ]
            if player.last_error:
                lines.append("\n**Last playback failure**\n" + player.last_error)
        lines.append(
            "\nDependency checks do not test YouTube access or a live Discord voice connection."
        )
        await self._reply(
            ctx,
            "\n".join(lines),
            title="Local player diagnostics",
            tone="success" if state["ready"] else "warning",
        )

    @commands.group(name="audio", invoke_without_command=True)
    @GUILD_ONLY
    async def audio(self, ctx: commands.Context) -> None:
        """Play music and manage queues and Discord voice."""
        p = ctx.clean_prefix
        embed = self._presentation.embed(
            "Commands", "Music search and playback run inside Red. No Lavalink node is needed."
        )
        sections = {
            "Playback": f"`{p}audio play <query>`\n`{p}audio np` · `{p}audio queue`\n`{p}audio skip` · `{p}audio stop`",
            "Controls": f"`{p}audio pause` · `{p}audio resume`\n`{p}audio volume [0..1000]` · `{p}audio shuffle`\n`{p}audio repeat [off|track|queue]`",
            "Voice": f"`{p}audio join` · `{p}audio leave`\n`{p}audio speak` · `{p}audio undeafen`\n`{p}audio fixvoice` · `{p}audio rejoin`",
            "Diagnostics": f"`{p}audio pingnode` · `{p}audio playerstate`\n`{p}audio debugvc` · `{p}audio tone`",
        }
        for name, value in sections.items():
            embed.add_field(name=name, value=value, inline=False)
        await self._reply(ctx, embed=embed)

    @audio.command(name="setnode")
    @checks.is_owner()
    async def audio_setnode(
        self,
        ctx: commands.Context,
        host: str,
        port: int,
        password: str,
        secure: Optional[bool] = False,
    ):
        """Preserve legacy node settings for rollback; native playback ignores them.

        Bot owner only. This compatibility command does not connect to Lavalink. Use a
        private channel because its arguments include the legacy password.
        """
        if (
            not host.strip()
            or "://" in host
            or "/" in host
            or not 1 <= port <= 65535
            or not password
        ):
            raise commands.BadArgument(
                "Use a hostname without a scheme/path, a port from 1 to 65535, and a nonempty password."
            )
        async with self.config.all() as data:
            data.update(host=host.strip(), port=port, password=password, secure=bool(secure))
        await self._reply(
            ctx,
            "Legacy settings saved for rollback. AudioPlus uses local Discord playback and does not connect to this node.",
            tone="warning",
        )

    @audio.command(name="shownode")
    @checks.is_owner()
    async def audio_shownode(self, ctx: commands.Context) -> None:
        """Show preserved legacy node settings without revealing the password."""
        data = await self.config.all()
        await self._reply(
            ctx,
            f"**Active backend** · Native Discord voice\n**Legacy node** · {data['host']}:{data['port']} (secure: {'yes' if data['secure'] else 'no'})\nLegacy node settings are preserved for rollback and are not used.",
        )

    @audio.command(name="connectnode")
    @checks.is_owner()
    async def audio_connectnode(self, ctx: commands.Context) -> None:
        """Check the local backend; a Lavalink connection is no longer needed."""
        await self._diagnostic_reply(ctx)

    @audio.command(name="pingnode")
    @GUILD_ONLY
    async def audio_pingnode(self, ctx: commands.Context) -> None:
        """Check FFmpeg, yt-dlp, voice encryption, and JavaScript dependencies."""
        await self._diagnostic_reply(ctx)

    @audio.command(name="playerstate")
    @GUILD_ONLY
    async def audio_playerstate(self, ctx: commands.Context) -> None:
        """Inspect the local queue, playback position, and Discord voice state."""
        player = self._get_player(ctx.guild)
        if not player:
            return await self._reply(ctx, "No AudioPlus voice player is connected.", tone="warning")
        title = discord.utils.escape_markdown(player.current.title) if player.current else "None"
        await self._reply(
            ctx,
            f"**Native player**\nConnected: `{player.voice.is_connected()}`  Playing: `{player.playing}`  Paused: `{player.paused}`\nPreparing stream: `{player.preparing}`\nTrack: {title}\nPosition: `{player.position} ms`\nVolume: `{player.volume}%`  Repeat: `{player.repeat}`\nQueued tracks: `{len(player.queue)}`",
        )

    @audio.command(name="speak")
    @GUILD_ONLY
    async def audio_speak(self, ctx: commands.Context) -> None:
        """Request speaking access in the current Stage channel."""
        voice = getattr(ctx.guild.me, "voice", None)
        if not voice or not voice.channel:
            return await self._reply(ctx, "I'm not connected to voice.", tone="warning")
        await self._stage_unsuppress_if_needed(ctx.guild, voice.channel)
        await self._reply(ctx, "Requested Stage speaking access where applicable.")

    @audio.command(name="undeafen")
    @GUILD_ONLY
    async def audio_undeafen(self, ctx: commands.Context) -> None:
        """Try to clear the bot's self-deafen voice flag."""
        voice = getattr(ctx.guild.me, "voice", None)
        if not voice or not voice.channel:
            return await self._reply(ctx, "I'm not connected to voice.", tone="warning")
        ok = await self._force_undeafen(ctx.guild, voice.channel)
        await self._reply(
            ctx,
            "Undeafen attempt: " + ("OK" if ok else "failed"),
            tone="success" if ok else "warning",
        )

    @audio.command(name="fixvoice")
    @GUILD_ONLY
    async def audio_fixvoice(self, ctx: commands.Context) -> None:
        """Try to recover Stage speaking and self-deafen state."""
        voice = getattr(ctx.guild.me, "voice", None)
        if not voice or not voice.channel:
            return await self._reply(ctx, "I'm not connected to voice.", tone="warning")
        await self._stage_unsuppress_if_needed(ctx.guild, voice.channel)
        ok = await self._force_undeafen(ctx.guild, voice.channel)
        await self._reply(
            ctx,
            "Voice fix attempted: " + ("OK" if ok else "partial"),
            tone="success" if ok else "warning",
        )

    @audio.command(name="debugvc")
    @GUILD_ONLY
    async def audio_debugvc(self, ctx: commands.Context) -> None:
        """Show Discord voice flags and native playback diagnostics."""
        voice = getattr(ctx.guild.me, "voice", None)
        player = self._get_player(ctx.guild)
        if not voice or not voice.channel:
            return await self._reply(ctx, "I'm not connected to voice.", tone="warning")
        lines = [
            f"**Channel** · {voice.channel}",
            f"ServerMuted: `{voice.mute}`  ServerDeaf: `{voice.deaf}`",
            f"SelfMute: `{voice.self_mute}`  SelfDeaf: `{voice.self_deaf}`  Suppressed(Stage): `{getattr(voice, 'suppress', False)}`",
            f"AudioPlus owns connection: `{player is not None}`",
        ]
        if player:
            lines += [
                f"Connected: `{player.voice.is_connected()}`  Playing: `{player.playing}`  Paused: `{player.paused}`",
                f"Preparing: `{player.preparing}`  Position: `{player.position} ms`  Volume: `{player.volume}%`",
            ]
        await self._reply(ctx, "\n".join(lines))

    @audio.command(name="rejoin")
    @GUILD_ONLY
    async def audio_rejoin(self, ctx: commands.Context) -> None:
        """Reconnect to voice and restore the current track, pause, volume, and queue.

        Playback resumes at the prior position for seekable streams. Live streams may
        restart at their live edge.
        """
        ok = await self._rebind_voice(ctx.guild)
        await self._reply(
            ctx, "Rejoin: " + ("OK" if ok else "failed"), tone="success" if ok else "error"
        )

    @audio.command(name="tone")
    @GUILD_ONLY
    async def audio_tone(self, ctx: commands.Context):
        """Queue a direct MP3 to test native playback independently of YouTube."""
        player, _ = await self._fetch_or_connect_player(ctx)
        tracks = await self._load_tracks(
            "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
        )
        await self._enqueue(player, tracks, ctx)
        await self._reply(
            ctx,
            "Queued the direct MP3 test track. Use audio playerstate or audio debugvc to inspect playback.",
            tone="success",
        )

    @audio.command(name="join", aliases=["connect", "summon"])
    @GUILD_ONLY
    async def audio_join(self, ctx: commands.Context) -> None:
        """Join or move to your current voice channel."""
        _, channel = await self._fetch_or_connect_player(ctx)
        await self._reply(ctx, f"Connected to **{channel}**.", tone="success")

    @audio.command(name="leave", aliases=["dc", "disconnect"])
    @GUILD_ONLY
    async def audio_leave(self, ctx: commands.Context) -> None:
        """Disconnect AudioPlus from voice and clear its queue."""
        async with self._player_locks[ctx.guild.id]:
            if not self._get_player(ctx.guild):
                return await self._reply(ctx, "Not connected.", tone="warning")
            await self._dispose_player(ctx.guild.id)
        await self._reply(ctx, "Disconnected.", tone="success")

    @audio.command(name="play", aliases=["p"])
    @GUILD_ONLY
    async def audio_play(self, ctx: commands.Context, *, query: str):
        """Search for music or queue tracks from a URL.

        Plain terms search YouTube. Use scsearch: for SoundCloud. YouTube playlists queue
        up to 100 tracks. Signed stream URLs are resolved when each track starts.
        """
        query = self._normalize_query(query)
        player, _ = await self._fetch_or_connect_player(ctx)
        tracks = await self._load_tracks(query)
        if not tracks:
            return await self._reply(ctx, "No results.", tone="warning")
        await self._enqueue(player, tracks, ctx)
        await self._reply(ctx, f"Queued {len(tracks)} track(s).", tone="success")

    @audio.command(name="skip", aliases=["next", "s"])
    @GUILD_ONLY
    async def audio_skip(self, ctx):
        """Skip the current track or pending stream lookup and advance once."""
        player = self._get_player(ctx.guild)
        if not player:
            return await self._reply(ctx, "Not connected.", tone="warning")
        track = await player.skip()
        await self._reply(
            ctx,
            f"Skipped: {track.title}"
            if track
            else "No current track. Starting queued tracks if available.",
            tone="success",
        )

    @audio.command(name="stop")
    @GUILD_ONLY
    async def audio_stop(self, ctx: commands.Context) -> None:
        """Stop playback, cancel the active lookup, and clear the queue."""
        player = self._get_player(ctx.guild)
        if not player:
            return await self._reply(ctx, "Not connected.", tone="warning")
        await player.stop()
        await self._reply(ctx, "Stopped and cleared the queue.", tone="success")

    @audio.command(name="pause")
    @GUILD_ONLY
    async def audio_pause(self, ctx: commands.Context) -> None:
        """Pause the current track."""
        player = self._get_player(ctx.guild)
        if not player or not player.current or player.preparing:
            return await self._reply(ctx, "No track is ready to pause.", tone="warning")
        player.voice.pause()
        await self._reply(ctx, "Paused.", tone="success")

    @audio.command(name="resume")
    @GUILD_ONLY
    async def audio_resume(self, ctx: commands.Context) -> None:
        """Resume paused playback."""
        player = self._get_player(ctx.guild)
        if not player or not player.paused:
            return await self._reply(ctx, "No paused track.", tone="warning")
        player.voice.resume()
        await self._reply(ctx, "Resumed.", tone="success")

    @audio.command(name="volume", aliases=["vol"])
    @GUILD_ONLY
    async def audio_volume(self, ctx: commands.Context, value: Optional[int] = None) -> None:
        """Show or set volume, clamped to 0 through 1000 percent.

        Volume above 100 percent can clip loud recordings.
        """
        player = self._get_player(ctx.guild)
        if not player:
            return await self._reply(ctx, "Not connected.", tone="warning")
        if value is None:
            return await self._reply(ctx, f"Volume: {player.volume}%")
        await player.set_volume(value)
        await self._reply(ctx, f"Volume set to {player.volume}%.", tone="success")

    @audio.command(name="np", aliases=["nowplaying"])
    @GUILD_ONLY
    async def audio_nowplaying(self, ctx: commands.Context) -> None:
        """Show the current track and playback progress."""
        player = self._get_player(ctx.guild)
        if not player or not player.current:
            return await self._reply(ctx, "Nothing is playing.", tone="warning")
        track = player.current
        embed = self._presentation.embed(
            "Now playing",
            f"**{discord.utils.escape_markdown(track.title)}**\n{discord.utils.escape_markdown(track.author)}",
        )
        if track.length:

            def duration(milliseconds):
                seconds = max(0, int(milliseconds // 1000))
                minutes, seconds = divmod(seconds, 60)
                hours, minutes = divmod(minutes, 60)
                return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"

            embed.add_field(
                name="Progress", value=f"{duration(player.position)} / {duration(track.length)}"
            )
        embed.add_field(name="Volume", value=f"{player.volume}%")
        embed.add_field(
            name="Playback",
            value="Preparing stream"
            if player.preparing
            else "Paused"
            if player.paused
            else "Playing",
        )
        embed.add_field(name="Repeat", value=player.repeat)
        await self._reply(ctx, embed=embed)

    @audio.command(name="queue", aliases=["q"])
    @GUILD_ONLY
    async def audio_queue(self, ctx: commands.Context) -> None:
        """Show the next ten queued tracks."""
        player = self._get_player(ctx.guild)
        if not player:
            return await self._reply(ctx, "Not connected.", tone="warning")
        if not player.queue:
            return await self._reply(ctx, "Queue is empty.", tone="warning")
        items = list(player.queue)
        lines = [
            f"{i}. {discord.utils.escape_markdown(track.title)}"
            for i, track in enumerate(items[:10], 1)
        ]
        if len(items) > 10:
            lines.append(f"… and {len(items) - 10} more.")
        await self._reply(ctx, "\n".join(lines))

    @audio.command(name="shuffle")
    @GUILD_ONLY
    async def audio_shuffle(self, ctx: commands.Context) -> None:
        """Shuffle the upcoming tracks without changing the current one."""
        player = self._get_player(ctx.guild)
        if not player:
            return await self._reply(ctx, "Not connected.", tone="warning")
        await player.shuffle()
        await self._reply(ctx, "Queue shuffled.", tone="success")

    @audio.command(name="repeat")
    @GUILD_ONLY
    async def audio_repeat(self, ctx: commands.Context, mode: Optional[str] = None) -> None:
        """Show or set repeat mode to off, track, or queue."""
        player = self._get_player(ctx.guild)
        if not player:
            return await self._reply(ctx, "Not connected.", tone="warning")
        if mode is not None:
            mode = mode.lower()
            if mode not in {"off", "track", "queue"}:
                raise commands.BadArgument("Choose off, track, or queue.")
            player.repeat = mode
        await self._reply(ctx, f"Repeat: {player.repeat}.")

    async def red_delete_data_for_user(self, *, requester, user_id):
        return

    async def red_get_data_for_user(self, *, user_id):
        return {}

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        await self._dispose_player(guild.id)
        self._player_locks.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id != getattr(self.bot.user, "id", None) or after.channel is not None:
            return
        player = self._players.get(member.guild.id)
        if player and not player.voice.is_connected():
            await self._dispose_player(member.guild.id)
