"""Personal YouTube entrance clips with owned voice and optional PCM music overlays."""

import asyncio
import io
import json
import logging
import math
import time
from collections import OrderedDict, defaultdict, deque
from copy import copy
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import urlsplit

import discord
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path

from .backend import diagnostics, require_voice
from .cache import (
    FRAME_BYTES,
    MAX_CACHE_ITEMS,
    CachedSource,
    ClipCache,
    clip_duration,
    clip_key,
)
from .command_support import check_command, finish_configuration_audit, prepare_hybrid
from .constants import (
    DEFAULTS_GUILD,
    DEFAULTS_MEMBER,
    MAX_DURATION,
    MAX_GUILD_WORKERS,
    MAX_PENDING,
    MAX_START,
    QUEUE_TTL,
)
from .playback import play_overlay, play_standalone
from .presentation import Presentation
from .resolver import MediaError, MediaResolver, Track, normalize_query

log = logging.getLogger(__name__)


def seconds(value, low, high):
    if not math.isfinite(value) or not low <= value <= high:
        raise commands.BadArgument(f"Use a number from {low:g} to {high:g} seconds.")
    return round(value, 3)


def youtube_url(value):
    try:
        parts = urlsplit(value)
        if (
            len(value) > 2048
            or parts.scheme not in {"http", "https"}
            or parts.hostname
            not in {
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
                "music.youtube.com",
                "youtu.be",
            }
            or parts.username
            or parts.password
            or parts.port is not None
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError
    except (TypeError, ValueError) as error:
        raise MediaError("Use a public YouTube video link or YouTube search terms.") from error
    return value


def saved_track(clip):
    try:
        track = Track(**clip["track"])
        youtube_url(track.uri)
        seconds(clip["duration"], 0.5, MAX_DURATION)
        seconds(clip["start"], 0, MAX_START)
        if type(track.length) is not int or track.length < 0 or track.direct:
            raise ValueError
        if track.length and clip["start"] * 1000 >= track.length:
            raise ValueError
        return track
    except (KeyError, TypeError, ValueError, commands.BadArgument) as error:
        raise MediaError(
            "This saved intro is invalid. Set the video and clip length again."
        ) from error


@dataclass(frozen=True)
class IntroRequest:
    guild_id: int
    member_id: int
    listener_id: int
    channel_id: int
    clip: dict
    created: float
    manual: bool = False


class IntroPlus(commands.Cog):
    """Play personal YouTube entrance clips when members join voice channels."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=702035012, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self.config.register_member(**DEFAULTS_MEMBER)
        self._presentation = Presentation("IntroPlus", "intro")
        self._resolver = MediaResolver()
        self._cache = ClipCache(cog_data_path(self) / "clips", self._resolver)
        self._warm_task = None
        self._voice_locks = defaultdict(asyncio.Lock)
        self._queues = defaultdict(deque)
        self._workers = {}
        self._active = {}
        self._voices = {}
        self._pending = set()
        self._cooldowns = OrderedDict()
        self._last = OrderedDict()
        self._commands = {}
        self._closing = False
        self._stopping = set()

    async def cog_load(self):
        allowed = {}
        for guild_id, members in (await self.config.all_members()).items():
            for member_id, data in members.items():
                clip = data.get("clip")
                if not clip:
                    continue
                try:
                    saved_track(clip)
                    key = clip_key(guild_id, member_id, clip)
                    allowed[key] = max(1, int(clip_duration(clip) * 1000) // 20) * FRAME_BYTES
                except MediaError:
                    continue
        await asyncio.to_thread(self._cache.initialize, allowed)
        self._warm_task = asyncio.create_task(self._warm_existing(), name="IntroPlus local clips")

    async def _warm_existing(self):
        await self.bot.wait_until_red_ready()
        count = 0
        for guild_id, members in (await self.config.all_members()).items():
            guild = self.bot.get_guild(guild_id)
            if not guild or await self.bot.cog_disabled_in_guild(self, guild):
                continue
            for member_id in members:
                if self._closing or count >= MAX_CACHE_ITEMS:
                    return
                # Backfill only the active slots. Fresh settings/preview jobs
                # should not wait behind a large startup download queue.
                while len(self._cache.jobs) >= 2:
                    await asyncio.wait(
                        tuple(self._cache.jobs.values()), return_when=asyncio.FIRST_COMPLETED
                    )
                group = self.config.member_from_ids(guild_id, member_id)
                async with group.clip.get_lock():
                    clip = await group.clip()
                    if not clip:
                        continue
                    try:
                        track = saved_track(clip)
                        if self._cache.path(guild_id, member_id, clip) is None:
                            self._cache.schedule(guild_id, member_id, clip, track)
                            count += 1
                    except MediaError:
                        continue

    async def _local_source(self, request, track):
        group = self.config.member_from_ids(request.guild_id, request.member_id)
        async with group.clip.get_lock():
            if self._closing or await group.clip() != request.clip:
                raise MediaError("Skipped: the saved intro changed.")
            path = self._cache.path(request.guild_id, request.member_id, request.clip)
            task = (
                None
                if path
                else self._cache.schedule(request.guild_id, request.member_id, request.clip, track)
            )
        if task:
            path = await asyncio.shield(task)
        # Open before waiting for voice. LRU eviction cannot remove audio from
        # an already-open file, including during a slow Discord handshake.
        try:
            return CachedSource(path, volume=100, start=int(request.clip["start"] * 1000))
        except OSError as error:
            raise MediaError("The cached intro was removed. Use intro test to retry.") from error

    def _prepare_later(self, member, clip):
        try:
            if not self._cache.path(member.guild.id, member.id, clip):
                self._cache.schedule(member.guild.id, member.id, clip, saved_track(clip))
        except MediaError:
            # Saving a valid choice succeeds even while the download queue is full.
            pass

    async def cog_before_invoke(self, ctx):
        if getattr(ctx, "interaction", None) and not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=True)
        await prepare_hybrid(ctx)
        self._commands[asyncio.current_task()] = (ctx.guild.id, {ctx.author.id})

    async def cog_after_invoke(self, ctx):
        self._commands.pop(asyncio.current_task(), None)
        finish_configuration_audit(ctx)

    async def cog_command_error(self, ctx, error):
        self._commands.pop(asyncio.current_task(), None)
        original = getattr(error, "original", error)
        if isinstance(original, MediaError):
            error = commands.BadArgument(str(original))
        await self._presentation.command_error(ctx, error)

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    async def _check_write(self, ctx):
        # Red's permission decision and Discord.py's Context.permissions are
        # cached. Rebuild the public context after a lookup or lock wait.
        member = ctx.guild.get_member(ctx.author.id)
        if member is None or self._closing:
            raise commands.CheckFailure("The command's member or cog is no longer available.")
        message = copy(ctx.message)
        message.author = member
        checked = commands.Context(
            message=message,
            bot=ctx.bot,
            view=ctx.view,
            prefix=ctx.prefix,
            command=ctx.command,
            invoked_with=ctx.invoked_with,
        )
        await check_command(checked, ctx.command)

    def _result(self, guild_id, value):
        self._last[guild_id] = value
        self._last.move_to_end(guild_id)
        while len(self._last) > 2000:
            self._last.popitem(last=False)

    def _voice_lock(self, guild_id):
        audio = self.bot.get_cog("AudioPlus")
        shared = getattr(audio, "voice_connection_lock", None)
        return shared(guild_id) if shared else self._voice_locks[guild_id]

    def owns_voice(self, voice):
        return voice is not None and self._voices.get(voice.guild.id) is voice

    async def release_voice(self, guild):
        """AudioPlus calls this while holding the shared connection lock."""
        voice = self._voices.get(guild.id)
        if voice is None:
            return False
        active = self._active.get(guild.id)
        if active:
            task = active[0]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._voices.get(guild.id) is voice:
            await self._disconnect(guild.id, voice)
        return True

    async def _disconnect(self, guild_id, voice):
        try:
            await asyncio.wait_for(voice.disconnect(force=True), 10)
        except Exception as error:
            log.warning(
                "IntroPlus voice cleanup failed in guild %s (%s)", guild_id, type(error).__name__
            )
        finally:
            if self._voices.get(guild_id) is voice:
                self._voices.pop(guild_id, None)

    async def _open_voice(self, channel):
        owned = None

        def factory(client, voice_channel):
            nonlocal owned
            owned = discord.VoiceClient(client, voice_channel)
            return owned

        try:
            voice = await channel.connect(
                cls=factory, timeout=30, reconnect=True, self_deaf=False, self_mute=False
            )
            if self._closing:
                raise asyncio.CancelledError
            return voice
        except BaseException:
            if owned is not None:
                await self._disconnect(channel.guild.id, owned)
            raise

    async def _eligible(self, request):
        guild = self.bot.get_guild(request.guild_id)
        if not guild or self._closing or await self.bot.cog_disabled_in_guild(self, guild):
            return None
        policy = await self.config.guild(guild).all()
        if not request.manual and not policy["enabled"]:
            return None
        channel = guild.get_channel(request.channel_id)
        member = guild.get_member(request.member_id)
        listener = guild.get_member(request.listener_id)
        if not isinstance(channel, discord.VoiceChannel) or not member or not listener:
            return None
        if (
            request.manual
            and request.member_id != request.listener_id
            and not (
                listener.guild_permissions.manage_guild
                or await self.bot.is_admin(listener)
                or await self.bot.is_owner(listener)
            )
        ):
            return None
        voice = getattr(listener, "voice", None)
        if not voice or not voice.channel or voice.channel.id != channel.id:
            return None
        if policy["channel"] and policy["channel"] != channel.id:
            return None
        permissions = channel.permissions_for(guild.me)
        if not all(getattr(permissions, key) for key in ("view_channel", "connect", "speak")):
            raise MediaError("The bot needs View Channel, Connect and Speak in the intro channel.")
        if await self.config.member(member).clip() != request.clip:
            return None
        return guild, channel, policy

    async def _play(self, request):
        selected = await self._eligible(request)
        if not selected:
            self._result(request.guild_id, "Skipped: the member, clip or channel policy changed.")
            return
        track = saved_track(request.clip)
        duration = clip_duration(request.clip)
        source = None
        owned = None
        try:
            source = await self._local_source(request, track)
            async with self._voice_lock(request.guild_id):
                selected = await self._eligible(request)
                if not selected:
                    return
                guild, channel, policy = selected
                if getattr(self.bot, "_audioplus_voice_restart_required", False) is True:
                    raise MediaError(
                        "Voice libraries were changed. Restart Red before playing intros."
                    )
                require_voice()
                voice = guild.voice_client
                if voice is not None:
                    audio = self.bot.get_cog("AudioPlus")
                    player = audio._get_player(guild) if audio else None
                    if (
                        not player
                        or player.voice is not voice
                        or player.source is not voice.source
                        or voice.channel.id != channel.id
                        or not voice.is_playing()
                        or voice.is_paused()
                    ):
                        raise MediaError(
                            "Skipped: the bot is busy in another channel or playback is idle/paused."
                        )
                    overlay = True
                else:
                    if channel.user_limit and len(channel.members) >= channel.user_limit:
                        if not channel.permissions_for(guild.me).move_members:
                            raise MediaError("Skipped: the voice channel is full.")
                    voice = await self._open_voice(channel)
                    owned = voice
                    self._voices[guild.id] = voice
                    if not await self._eligible(request):
                        return
                    overlay = False
                source.volume = policy["volume"]
            if overlay:
                await play_overlay(voice, source, duration)
            else:
                await play_standalone(voice, source, duration)
            self._result(request.guild_id, f"Played a {duration:g}-second intro.")
        finally:
            if source is not None:
                await asyncio.to_thread(source.cleanup)
            if owned is not None:
                await self._disconnect(request.guild_id, owned)

    def _enqueue(self, request, cooldown):
        if self._closing:
            return False
        key = (request.guild_id, request.member_id)
        now = time.monotonic()
        if key in self._pending:
            return False
        if not request.manual and now - self._cooldowns.get(key, -math.inf) < cooldown:
            self._result(request.guild_id, "Skipped: member intro cooldown is active.")
            return False
        queue = self._queues[request.guild_id]
        if len(queue) >= MAX_PENDING or (
            request.guild_id not in self._workers and len(self._workers) >= MAX_GUILD_WORKERS
        ):
            self._result(request.guild_id, "Skipped: the intro queue is full.")
            return False
        self._cooldowns[key] = now
        self._cooldowns.move_to_end(key)
        while len(self._cooldowns) > 10000:
            self._cooldowns.popitem(last=False)
        self._pending.add(key)
        queue.append(request)
        worker = self._workers.get(request.guild_id)
        if worker is None or worker.done():
            self._workers[request.guild_id] = asyncio.create_task(
                self._drain(request.guild_id), name=f"IntroPlus:{request.guild_id}"
            )
        return True

    async def _drain(self, guild_id):
        queue = self._queues[guild_id]
        try:
            while queue and not self._closing:
                request = queue.popleft()
                key = (guild_id, request.member_id)
                task = None
                try:
                    if time.monotonic() - request.created > QUEUE_TTL:
                        self._result(guild_id, "Skipped: this queued intro expired.")
                        continue
                    task = asyncio.create_task(self._play(request))
                    self._active[guild_id] = (task, request)
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if (
                        self._closing
                        or guild_id in self._stopping
                        or not task
                        or not task.cancelled()
                    ):
                        if task:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                        raise
                except MediaError as error:
                    self._result(guild_id, str(error))
                    log.warning(
                        "Voice intro playback failed",
                        extra={
                            "notification_error": type(error).__name__,
                            "notification_stage": "intro_playback",
                            "notification_guild_id": guild_id,
                        },
                    )
                except Exception as error:
                    self._result(
                        guild_id,
                        f"Playback failed ({type(error).__name__}). Run intro diagnostics.",
                    )
                    log.warning(
                        "Voice intro playback failed",
                        extra={
                            "notification_error": type(error).__name__,
                            "notification_stage": "intro_playback",
                            "notification_guild_id": guild_id,
                        },
                    )
                finally:
                    if self._active.get(guild_id, (None,))[0] is task:
                        self._active.pop(guild_id, None)
                    self._pending.discard(key)
        finally:
            for request in queue:
                self._pending.discard((guild_id, request.member_id))
            queue.clear()
            self._queues.pop(guild_id, None)
            if self._workers.get(guild_id) is asyncio.current_task():
                self._workers.pop(guild_id, None)

    async def _cancel_member(self, guild_id, user_id):
        queue = self._queues.get(guild_id)
        if queue is not None:
            retained = [
                request
                for request in queue
                if user_id not in {request.member_id, request.listener_id}
            ]
            for request in queue:
                if request not in retained:
                    self._pending.discard((guild_id, request.member_id))
            queue.clear()
            queue.extend(retained)
        active = self._active.get(guild_id)
        if active and user_id in {active[1].member_id, active[1].listener_id}:
            active[0].cancel()
            await asyncio.gather(active[0], return_exceptions=True)

    async def _stop_guild(self, guild_id):
        task = self._workers.get(guild_id)
        if task:
            self._stopping.add(guild_id)
            try:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            finally:
                self._stopping.discard(guild_id)
        if voice := self._voices.get(guild_id):
            await self._disconnect(guild_id, voice)

    async def cog_unload(self):
        self._closing = True
        tasks = set(self._workers.values()) | set(self._commands)
        if self._warm_task:
            tasks.add(self._warm_task)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for guild_id, voice in tuple(self._voices.items()):
            await self._disconnect(guild_id, voice)
        await self._cache.close()
        await self._resolver.close()
        self._commands.clear()
        self._cooldowns.clear()
        self._last.clear()
        self._voice_locks.clear()

    async def _configure(self, ctx, member, duration, query):
        duration = seconds(duration, 0.5, MAX_DURATION)
        if len(query) > 512:
            raise commands.BadArgument("Use a YouTube link or search shorter than 513 characters.")
        query = normalize_query(query)
        if query.startswith(("http://", "https://")):
            youtube_url(query)
        elif not query.startswith("ytsearch1:"):
            raise commands.BadArgument("Intros use YouTube video links or YouTube searches.")
        tracked = self._commands.get(asyncio.current_task())
        if tracked:
            tracked[1].add(member.id)
        async with self.config.member(member).clip.get_lock():
            tracks = await self._resolver.search(query, limit=1)
            if len(tracks) != 1:
                raise commands.BadArgument(
                    "Choose one YouTube video; playlists and empty results cannot be intros."
                )
            youtube_url(tracks[0].uri)
            await self._check_write(ctx)
            if self._closing or ctx.guild.get_member(member.id) is None:
                raise commands.BadArgument("The member is no longer in this server.")
            clip = {"track": asdict(tracks[0]), "duration": duration, "start": 0.0}
            await self.config.member(member).clip.set(clip)
            await self._cache.remove(guild_id=ctx.guild.id, member_id=member.id)
            self._prepare_later(member, clip)
        await self._cancel_member(ctx.guild.id, member.id)
        await self._reply(
            ctx,
            f"Saved **{tracks[0].title}** as a **{duration:g}-second** intro for {member.mention}.\nDownloading the selected segment for faster joins. Use `intro show` to check readiness.",
            tone="success",
        )

    @commands.hybrid_group(name="intro", fallback="status", invoke_without_command=True)
    @commands.guild_only()
    async def intro(self, ctx):
        """Manage personal YouTube clips played when someone joins voice."""
        policy = await self.config.guild(ctx.guild).all()
        clip = await self.config.member(ctx.author).clip()
        cache = "no intro set"
        if clip:
            try:
                saved_track(clip)
                cache = self._cache.status(ctx.guild.id, ctx.author.id, clip)
            except MediaError:
                cache = "invalid saved clip; set your intro again"
        channel = f"<#{policy['channel']}>" if policy["channel"] else "all regular voice channels"
        await self._reply(
            ctx,
            f"**Automatic intros** · {'on' if policy['enabled'] else 'off'}\n**Your intro** · {'set' if clip else 'not set'}\n**Local copy** · {cache}\n**Volume** · {policy['volume']}%\n**Cooldown** · {policy['cooldown']} seconds\n**Channel** · {channel}\n**Waiting** · {len(self._queues.get(ctx.guild.id, ()))}\n**Latest result** · {self._last.get(ctx.guild.id, 'No playback yet.')}\nUse `intro set 8 <YouTube link or search>` to choose your clip.",
        )

    @intro.command(name="help")
    async def intro_help(self, ctx):
        """Show intro commands and clip examples."""
        await ctx.send_help(self.intro)

    @intro.command(name="progress", aliases=["status"], with_app_command=False)
    async def intro_progress(self, ctx):
        """Show intro status through a text command."""
        await self.intro.callback(self, ctx)

    @intro.command(name="set")
    async def intro_set(self, ctx, duration: float, *, query: str):
        """Set your intro video and its duration in seconds, from 0.5 to 30."""
        await self._configure(ctx, ctx.author, duration, query)

    @intro.command(name="assign")
    @commands.admin_or_permissions(manage_guild=True)
    async def intro_assign(self, ctx, member: discord.Member, duration: float, *, query: str):
        """Set another member's YouTube intro and clip length."""
        if member.bot:
            raise commands.BadArgument("Choose a human member for an entrance intro.")
        await self._configure(ctx, member, duration, query)

    async def _clear(self, member, ctx=None):
        async with self.config.member(member).clip.get_lock():
            if ctx is not None:
                await self._check_write(ctx)
            await self.config.member(member).clip.clear()
            await self._cache.remove(guild_id=member.guild.id, member_id=member.id)
        await self._cancel_member(member.guild.id, member.id)

    @intro.command(name="clear")
    async def intro_clear(self, ctx):
        """Remove your saved intro and stop its pending playback."""
        await self._clear(ctx.author, ctx)
        await self._reply(ctx, "Your intro was removed.", tone="success")

    @intro.command(name="remove")
    @commands.admin_or_permissions(manage_guild=True)
    async def intro_remove(self, ctx, member: discord.Member):
        """Remove another member's saved intro and pending playback."""
        await self._clear(member, ctx)
        await self._reply(ctx, "The member's intro was removed.", tone="success")

    @intro.command(name="show")
    async def intro_show(self, ctx, member: Optional[discord.Member] = None):
        """Show your intro, or another member's public video and clip settings."""
        member = member or ctx.author
        clip = await self.config.member(member).clip()
        if not clip:
            await self._reply(ctx, "This member has no intro set.")
            return
        track = saved_track(clip)
        await self._reply(
            ctx,
            f"**Member** · {member.mention}\n**Video** · [{track.title}]({track.uri})\n**Duration** · {clip['duration']:g} seconds\n**Start** · {clip['start']:g} seconds\n**Local copy** · {self._cache.status(ctx.guild.id, member.id, clip)}",
        )

    async def _clip_setting(self, ctx, field, value):
        async with self.config.member(ctx.author).clip.get_lock():
            await self._check_write(ctx)
            clip = await self.config.member(ctx.author).clip()
            if not clip:
                raise commands.BadArgument("Set an intro with intro set <seconds> <video> first.")
            clip[field] = value
            saved_track(clip)
            await self.config.member(ctx.author).clip.set(clip)
            await self._cache.remove(guild_id=ctx.guild.id, member_id=ctx.author.id)
            self._prepare_later(ctx.author, clip)
        await self._cancel_member(ctx.guild.id, ctx.author.id)
        await self._reply(
            ctx,
            f"Your intro {field} is **{value:g} seconds**.\nPreparing the updated local copy. Use `intro show` to check readiness.",
            tone="success",
        )

    @intro.command(name="duration")
    async def intro_duration(self, ctx, value: float):
        """Change your intro length in seconds without changing its video."""
        await self._clip_setting(ctx, "duration", seconds(value, 0.5, MAX_DURATION))

    @intro.command(name="start")
    async def intro_start(self, ctx, value: float):
        """Choose the starting second of your intro video."""
        await self._clip_setting(ctx, "start", seconds(value, 0, MAX_START))

    @intro.command(name="test")
    async def intro_test(self, ctx, member: Optional[discord.Member] = None):
        """Preview your intro in your voice channel; managers may test another member's."""
        member = member or ctx.author
        if member.id != ctx.author.id and not (
            ctx.author.guild_permissions.manage_guild
            or await self.bot.is_admin(ctx.author)
            or await self.bot.is_owner(ctx.author)
        ):
            raise commands.CheckFailure("Manage Server is required to test another member's intro.")
        voice = getattr(ctx.author, "voice", None)
        if not voice or not isinstance(voice.channel, discord.VoiceChannel):
            raise commands.BadArgument("Join a regular voice channel to test an intro.")
        clip = await self.config.member(member).clip()
        if not clip:
            raise commands.BadArgument("This member has no intro set.")
        saved_track(clip)
        request = IntroRequest(
            ctx.guild.id, member.id, ctx.author.id, voice.channel.id, clip, time.monotonic(), True
        )
        if not await self._eligible(request):
            raise commands.BadArgument(
                "The configured voice-channel restriction prevents this test."
            )
        policy = await self.config.guild(ctx.guild).all()
        if not self._enqueue(request, policy["cooldown"]):
            raise commands.BadArgument("This intro is already pending or the intro queue is full.")
        await self._reply(
            ctx, "Intro preview queued. Use intro status for its latest result.", tone="success"
        )

    @intro.command(name="enable")
    @commands.admin_or_permissions(manage_guild=True)
    async def intro_enable(self, ctx, enabled: bool):
        """Enable or disable automatic intros in this server."""
        async with self.config.guild(ctx.guild).enabled.get_lock():
            await self._check_write(ctx)
            await self.config.guild(ctx.guild).enabled.set(enabled)
        if not enabled:
            await self._stop_guild(ctx.guild.id)
        await self._reply(
            ctx, f"Automatic intros are **{'on' if enabled else 'off'}**.", tone="success"
        )

    @intro.command(name="volume")
    @commands.admin_or_permissions(manage_guild=True)
    async def intro_volume(self, ctx, value: int):
        """Set intro volume from 1 to 100 percent."""
        if not 1 <= value <= 100:
            raise commands.BadArgument("Use a volume from 1 to 100.")
        async with self.config.guild(ctx.guild).volume.get_lock():
            await self._check_write(ctx)
            await self.config.guild(ctx.guild).volume.set(value)
        await self._reply(ctx, f"Intro volume is **{value}%** for upcoming clips.", tone="success")

    @intro.command(name="cooldown")
    @commands.admin_or_permissions(manage_guild=True)
    async def intro_cooldown(self, ctx, value: int):
        """Set the per-member automatic intro cooldown, from 10 to 3600 seconds."""
        if not 10 <= value <= 3600:
            raise commands.BadArgument("Use a cooldown from 10 to 3600 seconds.")
        async with self.config.guild(ctx.guild).cooldown.get_lock():
            await self._check_write(ctx)
            await self.config.guild(ctx.guild).cooldown.set(value)
        await self._reply(ctx, f"Intro cooldown is **{value} seconds**.", tone="success")

    @intro.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def intro_channel(self, ctx, channel: Optional[discord.VoiceChannel] = None):
        """Limit intros to one regular voice channel; omit it to allow all channels."""
        async with self.config.guild(ctx.guild).channel.get_lock():
            await self._check_write(ctx)
            await self.config.guild(ctx.guild).channel.set(channel.id if channel else 0)
        await self._stop_guild(ctx.guild.id)
        await self._reply(
            ctx,
            f"Intros are allowed in {channel.mention if channel else 'all regular voice channels'}.",
            tone="success",
        )

    @intro.command(name="stop")
    @commands.admin_or_permissions(manage_guild=True)
    async def intro_stop(self, ctx):
        """Stop current and queued intros without stopping AudioPlus music."""
        await self._stop_guild(ctx.guild.id)
        await self._reply(ctx, "Current and queued intros stopped.", tone="success")

    @intro.command(name="diagnostics")
    async def intro_diagnostics(self, ctx):
        """Check native voice, FFmpeg, yt-dlp and JavaScript prerequisites."""
        state = await diagnostics()
        packages = "\n".join(
            f"**{name}** · {version}" for name, version in state["packages"].items()
        )
        voice = "\n".join(
            f"**{name}** · {status}" for name, status in state["voice_packages"].items()
        )
        cached = sum(self._cache.entries.values()) / (1024 * 1024)
        await self._reply(
            ctx,
            f"**Local prerequisites** · {'ready' if state['ready'] else 'incomplete'}\n{packages}\n{voice}\n**FFmpeg** · {state['ffmpeg']}\n**JavaScript** · {', '.join(state['runtimes']) or 'missing'}\n**Local cache** · {len(self._cache.entries)} clips, {cached:.1f} MiB\n**Preparing** · {len(self._cache.jobs)} clips\n**Latest result** · {self._last.get(ctx.guild.id, 'No playback yet.')}\nLocal checks do not test YouTube access or a live Discord connection.",
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel or self._closing:
            return
        await self._cancel_member(member.guild.id, member.id)
        if not isinstance(after.channel, discord.VoiceChannel):
            return
        clip = await self.config.member(member).clip()
        if not clip:
            return
        request = IntroRequest(
            member.guild.id, member.id, member.id, after.channel.id, clip, time.monotonic()
        )
        try:
            if not await self._eligible(request):
                return
            policy = await self.config.guild(member.guild).all()
            self._enqueue(request, policy["cooldown"])
        except MediaError as error:
            self._result(member.guild.id, str(error))

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await self._clear(member)
        self._cooldowns.pop((member.guild.id, member.id), None)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        await self._stop_guild(guild.id)
        tasks = [task for task, (guild_id, _) in self._commands.items() if guild_id == guild.id]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.config.clear_all_members(guild)
        await self._cache.remove(guild_id=guild.id)
        await self.config.guild(guild).clear()
        self._voice_locks.pop(guild.id, None)
        self._last.pop(guild.id, None)
        for key in list(self._cooldowns):
            if key[0] == guild.id:
                self._cooldowns.pop(key, None)

    async def red_delete_data_for_user(self, *, requester, user_id):
        tasks = [
            task
            for task, (_, users) in self._commands.items()
            if user_id in users and task is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for guild_id in set(self._queues) | set(self._active):
            await self._cancel_member(guild_id, user_id)
        for guild_id, members in (await self.config.all_members()).items():
            if user_id in members:
                await self._cancel_member(guild_id, user_id)
                group = self.config.member_from_ids(guild_id, user_id)
                async with group.clip.get_lock():
                    await group.clear()
            self._cooldowns.pop((guild_id, user_id), None)
        for key in list(self._cooldowns):
            if key[1] == user_id:
                self._cooldowns.pop(key, None)
        await self._cache.remove(member_id=user_id)

    async def red_get_data_for_user(self, *, user_id):
        result = {
            str(guild_id): members[user_id]
            for guild_id, members in (await self.config.all_members()).items()
            if user_id in members and members[user_id].get("clip")
        }
        return (
            {"intros.json": io.BytesIO(json.dumps(result, ensure_ascii=False).encode())}
            if result
            else {}
        )
