from __future__ import annotations

import asyncio
import io
import json
import logging
from collections import defaultdict
from datetime import time
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import discord
from redbot.core import Config, app_commands, checks, commands
from redbot.core.bot import Red

from .backend import diagnostics, require_voice
from .command_support import prepare_hybrid
from .dependencies import VoiceDependencyRepair
from .failures import log_failure, playback_stage
from .features import DEFAULTS_GUILD, AudioCommands, check_control, vote_threshold
from .interactive import close_views
from .player import GuildPlayer
from .presentation import Presentation
from .resolver import MediaError, MediaResolver, normalize_query
from .watchdog import (
    DEFAULT_WATCHDOG,
    PROBE_FRAMES,
    CheckResult,
    PlaybackWatchdog,
    schedule_time,
    youtube_video,
)

log = logging.getLogger(__name__)
GUILD_ONLY = commands.guild_only()


class AudioPlus(AudioCommands, commands.Cog):
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
        self.config.register_global(**self.default_global, watchdog=DEFAULT_WATCHDOG)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._resolver = MediaResolver()
        self._voice_repair = VoiceDependencyRepair()
        self._players = {}
        self._player_locks = defaultdict(asyncio.Lock)
        self._lookups = set()
        self._connections = set()
        self._closing = False
        self._views = set()
        self._panels = {}
        self._panel_tasks = {}
        self._skip_votes = {}
        self._watchdog = PlaybackWatchdog(
            self.config, self._check_ready, self._probe_playback, self._notify_check_failure
        )

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    async def _invoke_control(self, ctx, command, **kwargs):
        interaction = getattr(ctx, "interaction", None)
        if interaction is not None and not interaction.response.is_done():
            # Voice connections and media lookups can exceed Discord's response deadline.
            await ctx.defer()
        return await command.callback(self, ctx, **kwargs)

    async def cog_before_invoke(self, ctx):
        await prepare_hybrid(ctx)

    async def cog_load(self):
        # Setup/help remain available when the container needs dependencies.
        self._closing = False
        self._watchdog.start()

    async def cog_unload(self):
        self._closing = True
        await self._voice_repair.close()
        await self._watchdog.close()
        await close_views(self)
        for guild_id in tuple(self._panels):
            await self._close_panel(guild_id)
        pending = tuple(self._lookups | self._connections)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        players = list(self._players.values())
        self._players.clear()
        await asyncio.gather(*(player.close() for player in players), return_exceptions=True)
        await self._resolver.close()
        self._player_locks.clear()

    async def _check_ready(self):
        ready = getattr(self.bot, "wait_until_red_ready", None) or self.bot.wait_until_ready
        await ready()

    def _voice_maintenance_error(self):
        if self._voice_repair.running:
            return "Voice dependency repair is running. Wait for it to finish and restart Red."
        if getattr(self.bot, "_audioplus_voice_restart_required", False) is True:
            return "Voice libraries were changed. Restart Red, then run audiostatus before playing."
        return None

    def _require_voice(self):
        if error := self._voice_maintenance_error():
            raise MediaError(error)
        require_voice()

    def _check_voice_busy(self, guild):
        player = self._players.get(guild.id)
        return bool(guild.voice_client or (player and (player.queue or player._restart)))

    async def _probe_playback(self, settings):
        with playback_stage("Server setup"):
            guild = self.bot.get_guild(settings["guild_id"])
            if guild is None:
                raise MediaError("The configured test server is unavailable to the bot.")
            if await self.bot.cog_disabled_in_guild(self, guild):
                return CheckResult("deferred", "AudioPlus is disabled in the test server.")
            async with self._player_locks[guild.id]:
                if self._check_voice_busy(guild):
                    return CheckResult(
                        "deferred", "Voice is already in use. I will retry in 15 minutes."
                    )
        with playback_stage("Voice dependencies"):
            if self._voice_repair.running:
                return CheckResult(
                    "deferred", "Voice dependency repair is running. I will retry in 15 minutes."
                )
            self._require_voice()
        with playback_stage("YouTube lookup"):
            tracks = await self._load_tracks(settings["video_url"])
            if not tracks:
                raise MediaError("YouTube returned no playable test video.")
        async with self._player_locks[guild.id]:
            if self._check_voice_busy(guild):
                return CheckResult("deferred", "Voice became busy. I will retry in 15 minutes.")
            with playback_stage("Voice channel selection"):
                channel = (
                    guild.get_channel(settings["channel_id"])
                    if settings["channel_id"]
                    else self._busiest_voice_channel(guild)
                )
                if not isinstance(channel, discord.VoiceChannel):
                    raise MediaError("The configured ordinary voice channel is unavailable.")
                permissions = channel.permissions_for(guild.me)
                if not all(
                    getattr(permissions, name) for name in ("view_channel", "connect", "speak")
                ):
                    raise MediaError(
                        "The test channel needs View Channel, Connect, and Speak permissions."
                    )
                if (
                    channel.user_limit
                    and len(channel.members) >= channel.user_limit
                    and not permissions.move_members
                ):
                    raise MediaError("The configured test voice channel is full.")
            player = None
            voice = None
            primary_failure = None
            try:
                with playback_stage("Discord voice connection"):
                    voice = await self._connect_voice(channel)
                    flags = getattr(guild.me, "voice", None)
                    if flags and (flags.mute or flags.self_mute):
                        raise MediaError("Discord is muting the bot in the test voice channel.")

                async def report_error(player, track, cause):
                    pass  # The watchdog sends the private report after the probe closes.

                with playback_stage("Native playback"):
                    player = GuildPlayer(voice, self._resolver, report_error)
                    player.volume = 0
                    await player.enqueue(tracks[:1])
                    while True:
                        if player.last_error:
                            raise MediaError(player.last_error)
                        if not voice.is_connected():
                            raise MediaError("Discord voice disconnected during the test.")
                        source = player.source
                        if source and source.frames >= PROBE_FRAMES and player.playing:
                            return CheckResult(
                                "ok",
                                "YouTube audio decoded and played silently in Discord for three seconds.",
                            )
                        if player._runner.done():
                            raise MediaError(
                                "The test video ended before three seconds of audio played."
                            )
                        await asyncio.sleep(0.05)
            except BaseException as exc:
                primary_failure = exc
                raise
            finally:
                try:
                    with playback_stage("Voice cleanup"):
                        if player:
                            await player.close()
                        elif voice:
                            await voice.disconnect(force=True)
                except Exception:
                    # Keep the original failure/cancellation; the cleanup failure is logged.
                    if primary_failure is None:
                        raise

    async def _notify_check_failure(self, settings, result):
        user = self.bot.get_user(settings["recipient_id"])
        if user is None:
            user = await self.bot.fetch_user(settings["recipient_id"])
        guild = self.bot.get_guild(settings["guild_id"])
        name = discord.utils.escape_markdown(guild.name if guild else str(settings["guild_id"]))
        embed = self._presentation.embed(
            "Daily playback check failed",
            f"**Server** · {name}\n**Checked** · <t:{int(result['at'])}:f>\n\n{result['detail']}\n\n**Test video** · {settings['video_url']}\n\nRun `audiostatus` for dependencies or `audiocheck now` to retry. A failure can also mean the test video was removed or restricted.",
            tone="error",
        )
        await user.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.group(name="audiocheck", invoke_without_command=True)
    @GUILD_ONLY
    @checks.is_owner()
    async def audiocheck(self, ctx: commands.Context):
        """Check YouTube daily and DM failures.

        Bot owner only. Enable in the server to test and receive failure DMs yourself.
        Uses a silent three-second native voice test when the voice connection is free.
        """
        state = await self.config.watchdog()
        last = state["last_result"]
        lines = [
            "**Daily check** · " + ("Enabled" if state["enabled"] else "Disabled"),
            f"**Schedule** · {state['hour']:02}:{state['minute']:02} {state['timezone']}",
            f"**Server** · {state['guild_id'] or 'Not configured'}",
            f"**Channel** · {('<#' + str(state['channel_id']) + '>') if state['channel_id'] else 'Automatic, busiest available voice channel'}",
            f"**Failure DMs** · {('<@' + str(state['recipient_id']) + '>') if state['recipient_id'] else 'Not configured'}",
            f"**Test video** · {state['video_url']}",
        ]
        if last:
            lines.append(
                f"\n**Last check** · {last['status']} · <t:{int(last['at'])}:f>\n{last['detail']}"
            )
        if state["last_alert_error"]:
            lines.append("\n" + state["last_alert_error"])
        lines.append(
            f"\n`{ctx.clean_prefix}audiocheck enable [voice channel]`\n`{ctx.clean_prefix}audiocheck now` · `{ctx.clean_prefix}audiocheck disable`\n`{ctx.clean_prefix}audiocheck time 09:00 America/Chicago`\n`{ctx.clean_prefix}audiocheck video <YouTube URL>`"
        )
        await self._reply(ctx, "\n".join(lines), title="Daily playback checks")

    @audiocheck.command(name="enable")
    async def audiocheck_enable(
        self, ctx: commands.Context, channel: Optional[discord.VoiceChannel] = None
    ):
        """Enable daily checks in this server and DM failures to you.

        Optionally choose a dedicated ordinary voice channel. Otherwise use the busiest
        available channel. Sends a setup DM before enabling to verify private delivery.
        """
        state = await self.config.watchdog()
        now = self._watchdog.clock()
        schedule_time(f"{state['hour']:02}:{state['minute']:02}", state["timezone"])
        local = now.astimezone(ZoneInfo(state["timezone"]))
        try:
            await ctx.author.send(
                embed=self._presentation.embed(
                    "Daily playback checks enabled",
                    f"I will test YouTube playback daily at **{state['hour']:02}:{state['minute']:02} {state['timezone']}** in **{discord.utils.escape_markdown(ctx.guild.name)}**. You will receive a DM if the check fails. Successful checks stay quiet. Run `{ctx.clean_prefix}audiocheck now` in your server for an immediate test.",
                    tone="success",
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            raise commands.CommandError(
                "I could not DM you. Allow direct messages from this bot, then run audiocheck enable again."
            ) from exc
        await self._watchdog.configure(
            enabled=True,
            recipient_id=ctx.author.id,
            guild_id=ctx.guild.id,
            channel_id=channel.id if channel else None,
            retry_at=0,
            last_result={},
            last_check_day=local.date().isoformat()
            if local.time() >= time(state["hour"], state["minute"])
            else None,
        )
        await self._reply(
            ctx,
            f"Daily checks enabled at **{state['hour']:02}:{state['minute']:02} {state['timezone']}**. Failure alerts will be DMed to you. Use `{ctx.clean_prefix}audiocheck now` to test immediately.",
            title="Daily playback checks",
            tone="success",
        )

    @audiocheck.command(name="disable")
    async def audiocheck_disable(self, ctx: commands.Context):
        """Disable daily checks, cancel an active probe, and clear pending alerts."""
        await self._watchdog.configure(enabled=False, retry_at=0)
        await self._reply(
            ctx, "Daily playback checks disabled.", title="Daily playback checks", tone="success"
        )

    @audiocheck.command(name="now")
    async def audiocheck_now(self, ctx: commands.Context):
        """Run the configured silent playback check now and report its result."""
        state = await self.config.watchdog()
        if state["guild_id"] != ctx.guild.id:
            raise commands.UserInputError("Run audiocheck enable in this server first.")
        await self._reply(
            ctx,
            "Checking YouTube playback. This can take up to 150 seconds.",
            title="Playback check",
        )
        result = await self._watchdog.check()
        await self._reply(
            ctx,
            result.detail,
            title="Playback check",
            tone={"ok": "success", "failed": "error", "deferred": "warning"}[result.status],
        )

    @audiocheck.command(name="time")
    async def audiocheck_time(
        self, ctx: commands.Context, value: str, zone: str = "America/Chicago"
    ):
        """Set the daily 24-hour check time and IANA timezone."""
        hour, minute = schedule_time(value, zone)
        await self._watchdog.configure(hour=hour, minute=minute, timezone=zone, retry_at=0)
        await self._reply(
            ctx,
            f"Daily check time set to **{value} {zone}**.",
            title="Daily playback checks",
            tone="success",
        )

    @audiocheck.command(name="video")
    async def audiocheck_video(self, ctx: commands.Context, *, url: str):
        """Choose a public YouTube video at least three seconds long for the check."""
        try:
            video = youtube_video(url)
        except MediaError as exc:
            raise commands.BadArgument(str(exc)) from exc
        await self._watchdog.configure(video_url=video, retry_at=0)
        await self._reply(
            ctx, f"Test video set to {video}.", title="Daily playback checks", tone="success"
        )

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
        await self._close_panel(guild_id)
        self._skip_votes.pop(guild_id, None)
        player = self._players.pop(guild_id, None)
        if player:
            await player.close(disconnect=disconnect)

    async def _disconnect_idle_player(self, player):
        async with self._player_locks[player.guild.id]:
            if self._closing or self._players.get(player.guild.id) is not player:
                return
            if player.guild.voice_client is not player.voice:
                await self._dispose_player(player.guild.id, disconnect=False)
            elif await player.disconnect_if_idle():
                if self._players.get(player.guild.id) is player:
                    self._players.pop(player.guild.id)
                await self._close_panel(player.guild.id)
                self._skip_votes.pop(player.guild.id, None)

    async def _open_voice(self, channel):
        owned = None

        def factory(client, voice_channel):
            nonlocal owned
            owned = discord.VoiceClient(client, voice_channel)
            return owned

        try:
            voice = await channel.connect(
                cls=factory,
                timeout=30,
                reconnect=True,
                self_deaf=False,
                self_mute=False,
            )
            if self._closing:
                raise asyncio.CancelledError
            return voice
        except BaseException:
            # discord.py's Connectable.connect does not clean up CancelledError.
            if owned:
                try:
                    await owned.disconnect(force=True)
                except Exception as cleanup_error:
                    log_failure(
                        "Voice connection cleanup",
                        cleanup_error,
                        guild_id=getattr(getattr(channel, "guild", None), "id", None),
                    )
            raise

    async def _connect_voice(self, channel):
        if self._closing:
            raise asyncio.CancelledError
        if error := self._voice_maintenance_error():
            raise MediaError(error)
        task = asyncio.create_task(self._open_voice(channel))
        self._connections.add(task)
        try:
            return await task
        finally:
            self._connections.discard(task)

    @staticmethod
    def _snapshot(player):
        resume = player._restart
        if resume is None and player.current:
            resume = (player.current, player.position, player.paused)
        return (
            resume,
            list(player.queue),
            player.volume,
            player.repeat,
            player.context,
            (dict(player._requesters), player._last_requester, list(player.recent)),
        )

    @staticmethod
    def _retain(player, snapshot):
        resume, queued, player.volume, player.repeat, player.context, extras = snapshot
        player.closed = True
        player.queue.clear()
        player.queue.extend(queued)
        player._restart = resume
        player._requesters, player._last_requester, recent = extras
        player.recent.clear()
        player.recent.extend(recent)

    async def _restore(self, player, snapshot):
        resume, queued, player.volume, player.repeat, player.context, extras = snapshot
        player._requesters, player._last_requester, recent = extras
        player.recent.extend(recent)
        preferences = await self.config.guild(player.guild).music()
        player.fair_queue, player.autoplay = preferences["fair_queue"], preferences["autoplay"]
        if resume:
            await player.restart(resume[0], start=resume[1], paused=resume[2])
        await player.enqueue(queued)

    @staticmethod
    def _busiest_voice_channel(guild):
        candidates = []
        for channel in guild.voice_channels:
            if channel == guild.afk_channel:
                continue
            permissions = channel.permissions_for(guild.me)
            if not permissions.view_channel or not permissions.connect or not permissions.speak:
                continue
            if (
                channel.user_limit
                and len(channel.members) >= channel.user_limit
                and not permissions.move_members
                and (guild.voice_client is None or guild.voice_client.channel != channel)
            ):
                continue
            candidates.append(channel)
        if not candidates:
            raise commands.UserInputError(
                "No available voice channel. Give me View Channel, Connect, and Speak permissions in a voice channel with room."
            )
        # Count people rather than bots; ties follow the server's channel order.
        return max(
            candidates, key=lambda channel: sum(not member.bot for member in channel.members)
        )

    async def _fetch_or_connect_player(self, ctx, *, queue_request=False):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        voice = getattr(ctx.author, "voice", None)
        if voice and voice.channel:
            channel = voice.channel
        elif queue_request:
            channel = self._busiest_voice_channel(ctx.guild)
        else:
            raise commands.UserInputError("Join a voice channel first.")
        try:
            self._require_voice()
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
            if player and player.voice.channel != channel:
                await check_control(self, ctx)
            permissions = channel.permissions_for(ctx.guild.me)
            missing = [name for name in ("connect", "speak") if not getattr(permissions, name)]
            if missing:
                raise commands.BotMissingPermissions(
                    discord.Permissions(**dict.fromkeys(missing, True))
                )
            request_player = None
            ready = False
            try:
                if player is None or not player.voice.is_connected():
                    snapshot = self._snapshot(previous) if previous else None
                    await self._dispose_player(ctx.guild.id)
                    try:
                        vc = await self._connect_voice(channel)
                    except BaseException:
                        if previous and snapshot and not self._closing:
                            self._retain(previous, snapshot)
                            self._players[ctx.guild.id] = previous
                        raise
                    player = GuildPlayer(
                        vc,
                        self._resolver,
                        self._report_playback_failure,
                        on_idle=self._disconnect_idle_player,
                        on_start=self._track_started,
                        on_end=self._autoplay_next,
                    )
                    self._players[ctx.guild.id] = player
                    if snapshot:
                        await self._restore(player, snapshot)
                if queue_request:
                    player.begin_queue_request()
                    request_player = player
                if player.voice.channel != channel:
                    await player.voice.move_to(channel, timeout=30)
                    await self._force_undeafen(ctx.guild, channel)
                player.context = ctx
                await self._stage_unsuppress_if_needed(ctx.guild, channel)
                ready = True
                return player, channel
            except (
                asyncio.TimeoutError,
                discord.ClientException,
                discord.HTTPException,
                RuntimeError,
            ) as exc:
                raise commands.CommandError(
                    "Discord voice could not connect. Check audiostatus, channel permissions, and the Red container's UDP network access."
                ) from exc
            finally:
                if request_player and not ready:
                    request_player.end_queue_request()

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
        preferences = await self.config.guild(player.guild).music()
        player.fair_queue, player.autoplay = preferences["fair_queue"], preferences["autoplay"]
        try:
            await player.enqueue(tracks, ctx)
        except MediaError as exc:
            raise commands.CommandError(str(exc)) from exc

    async def _queue_query(self, ctx, query):
        player, _ = await self._fetch_or_connect_player(ctx, queue_request=True)
        try:
            tracks = await self._load_tracks(query)
            if tracks:
                await self._enqueue(player, tracks, ctx)
            return tracks
        finally:
            player.end_queue_request()

    @staticmethod
    def _duration(milliseconds):
        seconds = max(0, int(milliseconds // 1000))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"

    @staticmethod
    def _track_description(track):
        title = discord.utils.escape_markdown(track.title, ignore_links=False)
        title = title.replace("[", "\\[").replace("]", "\\]")
        author = discord.utils.escape_markdown(track.author, ignore_links=False)
        # Link the original source page, never the resolved/signed playback stream.
        uri = quote(track.uri, safe=":/?#[]@!$&'()*+,;=%")
        return f"**[{title}](<{uri}>)**\n{author}"

    async def _reply_queued(self, ctx, tracks):
        entries = [
            self._track_description(track)
            + " · "
            + (self._duration(track.length) if track.length else "Unknown duration")
            for track in tracks[:5]
        ]
        description = "\n\n".join(entries)
        if len(tracks) != 1:
            description = f"Queued {len(tracks)} tracks.\n\n" + description
        if len(tracks) > 5:
            description += f"\n\nAnd {len(tracks) - 5} more tracks."
        await self._reply(
            ctx,
            embed=self._presentation.embed("Added to queue", description, tone="success"),
        )

    async def _report_playback_failure(self, player, track, cause):
        if self._closing or self._players.get(player.guild.id) is not player or not player.context:
            return
        ctx = player.context
        title = discord.utils.escape_markdown(track.title)
        await self._reply(
            ctx,
            f"**{title}**\n{cause}\n\nRun `{ctx.clean_prefix}audiostatus` for local dependency checks or `{ctx.clean_prefix}tone` to test direct audio.",
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
                self._require_voice()
                vc = await self._connect_voice(channel)
                player = GuildPlayer(
                    vc,
                    self._resolver,
                    self._report_playback_failure,
                    on_idle=self._disconnect_idle_player,
                    on_start=self._track_started,
                    on_end=self._autoplay_next,
                )
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
        state = await diagnostics(voice_guard=self._voice_maintenance_error)
        lines = [
            "**Backend** · Native Discord voice",
            "**Lavalink** · Not used",
            f"**Discord.py** · {discord.__version__}",
            "**Discord voice** · " + ("Unavailable" if state["voice_error"] else "Ready"),
            f"**FFmpeg** · {state['ffmpeg']}",
        ]
        for name, version in state["packages"].items():
            status = state.get("voice_packages", {}).get(name)
            lines.append(f"**{name}** · {version}" + (f" · Native API: {status}" if status else ""))
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
        check = await self.config.watchdog()
        last = check["last_result"]
        if check["guild_id"] == ctx.guild.id and last:
            lines.append(
                f"\n**Last daily playback check** · {last['status']} · <t:{int(last['at'])}:f>\n"
                + last["detail"]
            )
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
            tone="success"
            if state["ready"]
            and not (check["guild_id"] == ctx.guild.id and last.get("status") == "failed")
            else "warning",
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
            "Playback": f"`{p}play <query>`\n`{p}np` · `{p}queue`\n`{p}skip` · `{p}stop`",
            "Discovery": f"`{p}search <query>` · choose a result before joining voice\n`{p}audioset fairqueue <enabled>` · `{p}audioset autoplay <enabled>`",
            "Controls": f"`{p}pause` · `{p}resume`\n`{p}volume [0..1000]` · `{p}shuffle`\n`{p}repeat [off|track|queue]`",
            "Voice": f"`{p}join` · `{p}disconnect`\n`{p}speak` · `{p}undeafen`\n`{p}fixvoice` · `{p}rejoin`",
            "Diagnostics": f"`{p}audiostatus` · `{p}playerstate`\n`{p}debugvc` · `{p}tone`\n`{p}audiorepair` · `{p}audiocheck` (owner)",
            "Queue editing": f"`{p}seek <seconds>` · `{p}remove <position>` · `{p}move <position> <destination>`",
            "Saved music": f"`{p}playlist` · `{p}playlist save <name>` · `{p}playlist play <name>`\n`{p}favorite` · `{p}favorite add` · `{p}favorite play`",
            "Music settings": f"`{p}audioset setup` · `{p}audioset panel <enabled>`\n`{p}audioset dj [@role]` · `{p}audioset voteskip <enabled>`",
            "Slash commands": "Use the same controls with `/play`, `/skip`, `/queue`, and more.",
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
        await check_control(self, ctx)
        ok = await self._rebind_voice(ctx.guild)
        await self._reply(
            ctx, "Rejoin: " + ("OK" if ok else "failed"), tone="success" if ok else "error"
        )

    @audio.command(name="tone")
    @GUILD_ONLY
    async def audio_tone(self, ctx: commands.Context):
        """Queue a direct MP3 to test native playback independently of YouTube."""
        tracks = await self._queue_query(
            ctx, "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
        )
        await self._reply_queued(ctx, tracks)

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
        await check_control(self, ctx)
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
        If you are not in voice, joins the available voice channel with the most people.
        Disconnects after the queue has been empty for 10 seconds.
        """
        query = self._normalize_query(query)
        tracks = await self._queue_query(ctx, query)
        if not tracks:
            return await self._reply(ctx, "No results.", tone="warning")
        await self._reply_queued(ctx, tracks)

    @audio.command(name="skip", aliases=["next", "s"])
    @GUILD_ONLY
    async def audio_skip(self, ctx):
        """Skip the current track or pending stream lookup and advance once."""
        player = self._get_player(ctx.guild)
        if not player:
            return await self._reply(ctx, "Not connected.", tone="warning")
        if not await check_control(self, ctx, vote=True):
            key, votes = self._skip_votes.get(ctx.guild.id, (None, set()))
            if key is not player.current:
                votes = set()
            humans = {member.id for member in player.voice.channel.members if not member.bot}
            votes.intersection_update(humans)
            votes.add(ctx.author.id)
            self._skip_votes[ctx.guild.id] = (player.current, votes)
            needed = vote_threshold(player.voice.channel)
            if len(votes) < needed:
                return await self._reply(ctx, f"Skip vote recorded: {len(votes)}/{needed}.")
        self._skip_votes.pop(ctx.guild.id, None)
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
        await check_control(self, ctx)
        await player.stop()
        await self._reply(ctx, "Stopped and cleared the queue.", tone="success")

    @audio.command(name="pause")
    @GUILD_ONLY
    async def audio_pause(self, ctx: commands.Context) -> None:
        """Pause the current track."""
        player = self._get_player(ctx.guild)
        if not player or not player.current or player.preparing:
            return await self._reply(ctx, "No track is ready to pause.", tone="warning")
        await check_control(self, ctx)
        player.voice.pause()
        await self._reply(ctx, "Paused.", tone="success")

    @audio.command(name="resume")
    @GUILD_ONLY
    async def audio_resume(self, ctx: commands.Context) -> None:
        """Resume paused playback."""
        player = self._get_player(ctx.guild)
        if not player or not player.paused:
            return await self._reply(ctx, "No paused track.", tone="warning")
        await check_control(self, ctx)
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
        await check_control(self, ctx)
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
            self._track_description(track),
        )
        if track.length:
            embed.add_field(
                name="Progress",
                value=f"{self._duration(player.position)} / {self._duration(track.length)}",
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
        await check_control(self, ctx)
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
            await check_control(self, ctx)
            mode = mode.lower()
            if mode not in {"off", "track", "queue"}:
                raise commands.BadArgument("Choose off, track, or queue.")
            player.repeat = mode
        await self._reply(ctx, f"Repeat: {player.repeat}.")

    @commands.hybrid_command(name="play", aliases=["p"])
    @GUILD_ONLY
    @app_commands.describe(query="Song name, media URL, or YouTube playlist URL.")
    async def play(self, ctx: commands.Context, *, query: str):
        """Play music in your voice channel or the available channel with the most people."""
        return await self._invoke_control(ctx, self.audio_play, query=query)

    @commands.hybrid_command(name="join", aliases=["connect", "summon"])
    @GUILD_ONLY
    async def join(self, ctx: commands.Context):
        """Join or move to your voice channel."""
        return await self._invoke_control(ctx, self.audio_join)

    @commands.hybrid_command(name="disconnect", aliases=["dc"])
    @GUILD_ONLY
    async def disconnect(self, ctx: commands.Context):
        """Disconnect from voice and clear the music queue."""
        return await self._invoke_control(ctx, self.audio_leave)

    @commands.hybrid_command(name="skip", aliases=["next", "s"])
    @GUILD_ONLY
    async def skip(self, ctx: commands.Context):
        """Skip the current track and advance the queue."""
        return await self._invoke_control(ctx, self.audio_skip)

    @commands.hybrid_command(name="stop")
    @GUILD_ONLY
    async def stop(self, ctx: commands.Context):
        """Stop playback and clear the queue."""
        return await self._invoke_control(ctx, self.audio_stop)

    @commands.hybrid_command(name="pause")
    @GUILD_ONLY
    async def pause(self, ctx: commands.Context):
        """Pause the current track."""
        return await self._invoke_control(ctx, self.audio_pause)

    @commands.hybrid_command(name="resume")
    @GUILD_ONLY
    async def resume(self, ctx: commands.Context):
        """Resume paused playback."""
        return await self._invoke_control(ctx, self.audio_resume)

    @commands.hybrid_command(name="volume", aliases=["vol"])
    @GUILD_ONLY
    @app_commands.describe(value="Volume from 0 to 1000 percent; omit to show the current volume.")
    async def volume(self, ctx: commands.Context, value: Optional[int] = None):
        """Show or set the playback volume."""
        return await self._invoke_control(ctx, self.audio_volume, value=value)

    @commands.hybrid_command(name="np", aliases=["nowplaying"])
    @GUILD_ONLY
    async def np(self, ctx: commands.Context):
        """Show the current track, source link, and playback progress."""
        return await self._invoke_control(ctx, self.audio_nowplaying)

    @commands.hybrid_command(name="queue", aliases=["q"])
    @GUILD_ONLY
    async def queue(self, ctx: commands.Context):
        """Show the upcoming tracks."""
        return await self._invoke_control(ctx, self.audio_queue)

    @commands.hybrid_command(name="shuffle")
    @GUILD_ONLY
    async def shuffle(self, ctx: commands.Context):
        """Shuffle upcoming tracks without changing the current one."""
        return await self._invoke_control(ctx, self.audio_shuffle)

    @commands.hybrid_command(name="repeat")
    @GUILD_ONLY
    @app_commands.describe(mode="Repeat off, the current track, or the whole queue.")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Off", value="off"),
            app_commands.Choice(name="Current track", value="track"),
            app_commands.Choice(name="Queue", value="queue"),
        ]
    )
    async def repeat(self, ctx: commands.Context, mode: Optional[str] = None):
        """Show or set repeat mode."""
        return await self._invoke_control(ctx, self.audio_repeat, mode=mode)

    @commands.hybrid_command(name="tone")
    @GUILD_ONLY
    async def tone(self, ctx: commands.Context):
        """Play a direct MP3 to test voice independently of YouTube."""
        return await self._invoke_control(ctx, self.audio_tone)

    @commands.hybrid_command(name="audiostatus", aliases=["pingnode"])
    @GUILD_ONLY
    async def audiostatus(self, ctx: commands.Context):
        """Check local playback dependencies and the latest failure."""
        return await self._invoke_control(ctx, self.audio_pingnode)

    @commands.command(name="audiorepair")
    @checks.is_owner()
    async def audiorepair(self, ctx: commands.Context):
        """Repair missing or incompatible native voice libraries, then restart Red.

        Bot owner only. Installs binary wheels for failing voice packages and their
        Python dependencies. Does not install FFmpeg, Opus, or a JavaScript runtime.
        Disconnect voice sessions first. No packages are installed by play or updates.
        """
        if self._closing:
            raise commands.CommandError("AudioPlus is unloading. Try again after it reloads.")
        if self._voice_maintenance_error():
            return await self._reply(ctx, self._voice_maintenance_error(), tone="warning")
        if (
            self._lookups
            or self._connections
            or any(voice.is_connected() for voice in self.bot.voice_clients)
        ):
            return await self._reply(
                ctx,
                "Disconnect voice sessions and wait for audio lookups to finish first.",
                tone="warning",
            )

        async def started():
            await self._reply(
                ctx,
                "Installing binary wheels for failing voice libraries into Red's Python "
                "environment. Installation may take up to three minutes, followed by verification. "
                "Restart Red after the repair.",
                title="Repair voice dependencies",
            )

        failure = None
        try:
            installed = await self._voice_repair.repair(started)
        except MediaError as exc:
            failure = str(exc)
        finally:
            if self._voice_repair.changed:
                # The bot object survives cog reloads; restarting clears this in-memory flag.
                self.bot._audioplus_voice_restart_required = True
        if failure is not None:
            if self._voice_repair.changed:
                failure += f"\n\nRestart Red with `{ctx.clean_prefix}restart` before retrying."
            return await self._reply(
                ctx, failure, title="Voice repair needs attention", tone="error"
            )
        if installed:
            await self._reply(
                ctx,
                "PyNaCl and davey now pass the required native API checks in a fresh Python "
                "process.\n\n"
                f"Run `{ctx.clean_prefix}restart`, then `{ctx.clean_prefix}audiostatus` and "
                f"`{ctx.clean_prefix}play <query>`.",
                title="Voice libraries repaired",
                tone="success",
            )
        else:
            await self._reply(
                ctx,
                "PyNaCl and davey already pass the required native API checks. "
                "No packages were changed. "
                f"Run `{ctx.clean_prefix}audiostatus` to check the remaining prerequisites.",
                title="Voice libraries ready",
                tone="success",
            )

    @commands.hybrid_command(name="playerstate")
    @GUILD_ONLY
    async def playerstate(self, ctx: commands.Context):
        """Inspect the player, queue, position, and voice connection."""
        return await self._invoke_control(ctx, self.audio_playerstate)

    @commands.hybrid_command(name="debugvc")
    @GUILD_ONLY
    async def debugvc(self, ctx: commands.Context):
        """Inspect Discord voice flags and playback state."""
        return await self._invoke_control(ctx, self.audio_debugvc)

    @commands.hybrid_command(name="speak")
    @GUILD_ONLY
    async def speak(self, ctx: commands.Context):
        """Request permission to speak on the current Stage channel."""
        return await self._invoke_control(ctx, self.audio_speak)

    @commands.hybrid_command(name="undeafen")
    @GUILD_ONLY
    async def undeafen(self, ctx: commands.Context):
        """Try to clear the bot's self-deafen voice flag."""
        return await self._invoke_control(ctx, self.audio_undeafen)

    @commands.hybrid_command(name="fixvoice")
    @GUILD_ONLY
    async def fixvoice(self, ctx: commands.Context):
        """Try to recover Stage speaking and voice flags."""
        return await self._invoke_control(ctx, self.audio_fixvoice)

    @commands.hybrid_command(name="rejoin")
    @GUILD_ONLY
    async def rejoin(self, ctx: commands.Context):
        """Reconnect and restore the track, pause state, and queue."""
        return await self._invoke_control(ctx, self.audio_rejoin)

    async def red_delete_data_for_user(self, *, requester, user_id):
        if (await self.config.watchdog())["recipient_id"] == user_id:
            await self._watchdog.configure(**DEFAULT_WATCHDOG)
        for guild_id in await self.config.all_guilds():
            group = self.config.guild_from_id(guild_id)
            for section in (group.playlists, group.favorites):
                async with section() as records:
                    records.pop(str(user_id), None)

    async def red_get_data_for_user(self, *, user_id):
        state = await self.config.watchdog()
        data = {"watchdog": state} if state["recipient_id"] == user_id else {}
        saved = {}
        for guild_id, conf in (await self.config.all_guilds()).items():
            records = {
                key: conf.get(key, {}).get(str(user_id))
                for key in ("playlists", "favorites")
                if str(user_id) in conf.get(key, {})
            }
            if records:
                saved[str(guild_id)] = records
        if saved:
            data["collections"] = saved
        return {"audioplus.json": io.BytesIO(json.dumps(data, indent=2).encode())} if data else {}

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        await self._dispose_player(guild.id)
        self._player_locks.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id != getattr(self.bot.user, "id", None) or after.channel is not None:
            return
        player = self._players.get(member.guild.id)
        if player and not player.closed and not player.voice.is_connected():
            await self._dispose_player(member.guild.id)
