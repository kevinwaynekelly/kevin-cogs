"""One native Discord player per guild, with owned FFmpeg and lookup lifetimes."""

from __future__ import annotations

import asyncio
import audioop
import logging
import os
import random
import shlex
import threading
from collections import deque

import discord

from .failures import log_failure, safe_exception
from .resolver import MAX_TRACKS, MediaError, Stream, Track

log = logging.getLogger(__name__)
IDLE_DISCONNECT_SECONDS = 10


class NativeSource(discord.AudioSource):
    def __init__(self, stream: Stream, *, volume: int, start: int = 0, normalize: bool = False):
        before = "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 15000000 -protocol_whitelist http,https,tcp,tls,crypto,pipe"
        if stream.headers:
            headers = "".join(f"{key}: {value}\r\n" for key, value in stream.headers.items())
            before += " -headers " + shlex.quote(headers)
        if start:
            before += f" -ss {start / 1000:.3f}"
        self._stderr = open(os.devnull, "wb")
        try:
            self._audio = discord.FFmpegPCMAudio(
                stream.url,
                before_options=before,
                options="-vn -loglevel error"
                + (" -af loudnorm=I=-16:TP=-1.5:LRA=11" if normalize else ""),
                stderr=self._stderr,
            )
        except BaseException:
            self._stderr.close()
            raise
        self.volume = volume
        self.start = start
        self.frames = 0
        self._cleaned = False
        self._cleanup_lock = threading.Lock()

    @property
    def position(self):
        return self.start + self.frames * 20

    def read(self):
        data = self._audio.read()
        if not data:
            # EOF can be a decoder/network failure rather than a natural track end.
            code = self._audio._process.wait(timeout=2)
            if code or not self.frames:
                raise MediaError(
                    "FFmpeg could not read this audio stream. Try another track or run audio tone to test the local player."
                )
            return b""
        self.frames += 1
        return audioop.mul(data, 2, self.volume / 100) if self.volume != 100 else data

    def is_opus(self):
        return False

    def cleanup(self):
        if getattr(self, "_cleaned", True):
            return
        # Discord's audio thread and the event loop can both request cleanup.
        # A completed flag must mean the child is reaped, not merely claimed.
        with self._cleanup_lock:
            if self._cleaned:
                return
            try:
                self._audio.cleanup()
            finally:
                self._stderr.close()
                self._cleaned = True


class GuildPlayer:
    def __init__(
        self,
        voice,
        resolver,
        report_error,
        *,
        source_factory=NativeSource,
        on_idle=None,
        on_start=None,
        on_end=None,
    ):
        self.voice = voice
        self.resolver = resolver
        self.report_error = report_error
        self.source_factory = source_factory
        self.on_idle = on_idle
        self.on_start = on_start
        self.on_end = on_end
        self.fair_queue = False
        self.autoplay = False
        self._autoplay_generation = 0
        self._last_requester = 0
        self._requesters = {}
        self.recent = deque(maxlen=50)
        self.guild = voice.guild
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.context = None
        self.volume = 100
        self.normalize = False
        self._recovery_cleared = False
        self.repeat = "off"
        self.source = None
        self.preparing = False
        self.closed = False
        self.last_error = None
        self._runner = None
        self._track_task = None
        self._restart = None
        self._idle_task = None
        self._queue_requests = 0
        self.lock = asyncio.Lock()

    @property
    def paused(self):
        return self.voice.is_paused()

    @property
    def playing(self):
        return self.voice.is_playing()

    @property
    def position(self):
        return self.source.position if self.source else 0

    def _start_worker(self):
        if not self.closed and (self._runner is None or self._runner.done()):
            self._runner = asyncio.create_task(self._run(), name=f"AudioPlus:{self.guild.id}")

    def _cancel_idle(self):
        task = self._idle_task
        self._idle_task = None
        if task and task is not asyncio.current_task():
            task.cancel()
        return task

    def begin_queue_request(self):
        """Reserve this connection while a command searches for tracks."""
        if self.closed:
            raise MediaError("The voice player disconnected. Join again before queueing music.")
        self._queue_requests += 1
        self._cancel_idle()

    def end_queue_request(self):
        self._queue_requests -= 1
        self._schedule_idle()

    def _is_idle(self):
        return (
            not self.closed
            and self.current is None
            and not self.queue
            and self._restart is None
            and not self.preparing
            and not self._queue_requests
            and not self.playing
            and not self.paused
        )

    def _schedule_idle(self):
        if self._is_idle() and (self._idle_task is None or self._idle_task.done()):
            self._idle_task = asyncio.create_task(
                self._leave_after_idle(), name=f"AudioPlusIdle:{self.guild.id}"
            )

    async def _leave_after_idle(self):
        try:
            await asyncio.sleep(IDLE_DISCONNECT_SECONDS)
            if self.on_idle:
                await self.on_idle(self)
            else:
                await self.disconnect_if_idle()
        except Exception:
            log.warning(
                "AudioPlus idle disconnect failed in guild %s", self.guild.id, exc_info=True
            )
        finally:
            if self._idle_task is asyncio.current_task():
                self._idle_task = None

    async def disconnect_if_idle(self):
        async with self.lock:
            if not self._is_idle():
                return False
            # Claim the disconnect before allowing a concurrent enqueue to proceed.
            self.closed = True
        await self.close()
        return True

    async def enqueue(self, tracks, ctx=None):
        async with self.lock:
            if self.closed:
                raise MediaError("The voice player disconnected. Join again before queueing music.")
            if len(self.queue) + len(tracks) > MAX_TRACKS:
                raise MediaError(
                    f"The queue holds at most {MAX_TRACKS} upcoming tracks. Clear some tracks first."
                )
            self._cancel_idle()
            if ctx is not None:
                uid = getattr(getattr(ctx, "author", None), "id", 0)
                self._requesters.update({id(track): uid for track in tracks})
            self._recovery_cleared = False
            self.queue.extend(tracks)
            self._balance_queue()
            if ctx is not None:
                self.context = ctx
            self._start_worker()

    def _balance_queue(self):
        """Round robin requesters while preserving each requester's song order."""
        active = {id(track) for track in self.queue}
        if self.current:
            active.add(id(self.current))
        if self._restart:
            active.add(id(self._restart[0]))
        self._requesters = {key: value for key, value in self._requesters.items() if key in active}
        if not self.fair_queue:
            return
        groups = {}
        for track in self.queue:
            groups.setdefault(self._requesters.get(id(track), 0), deque()).append(track)
        ordered = deque()
        previous = (
            self._requesters.get(id(self.current), 0) if self.current else self._last_requester
        )
        turns = deque(groups)
        if previous in turns:
            turns.remove(previous)
            turns.append(previous)
        while turns:
            uid = turns.popleft()
            ordered.append(groups[uid].popleft())
            if groups[uid]:
                turns.append(uid)
        self.queue = ordered

    async def _play_one(self, track, start=0, paused=False):
        self.preparing = True
        source = None
        try:
            stream = await self.resolver.resolve(track)
            if not self.voice.is_connected():
                raise MediaError("Discord voice disconnected. Use audio rejoin or audio join.")
            options = {"normalize": True} if self.normalize else {}
            source = self.source_factory(stream, volume=self.volume, start=start, **options)
            self.source = source
            loop = asyncio.get_running_loop()
            ended = loop.create_future()

            def complete(error):
                if not ended.done():
                    ended.set_result(error)

            def after(error):
                if not loop.is_closed():
                    loop.call_soon_threadsafe(complete, error)

            self.voice.play(source, after=after)
            if paused:
                self.voice.pause()
            self.preparing = False
            self.last_error = None
            if self.on_start:
                try:
                    await self.on_start(self)
                except Exception:
                    log.warning("Could not update the music panel", exc_info=True)
            error = await ended
            if error:
                raise error
        finally:
            self.preparing = False
            # A cancelled preparation/start must not leave a decoder playing behind the queue.
            if source is not None:
                self.voice.stop()
                source.cleanup()
            self.source = None

    async def _run(self):
        try:
            while not self.closed:
                async with self.lock:
                    if self._restart:
                        track, start, paused = self._restart
                        self._restart = None
                    elif self.queue:
                        self._balance_queue()
                        track, start, paused = self.queue.popleft(), 0, False
                    else:
                        self._schedule_idle()
                        return
                    self.current = track
                    self._last_requester = self._requesters.get(id(track), 0)
                    self.recent.append(track.uri)
                    self._track_task = asyncio.create_task(self._play_one(track, start, paused))
                completed = False
                try:
                    await self._track_task
                    completed = True
                except asyncio.CancelledError:
                    if self.closed:
                        return
                except Exception as exc:
                    self.last_error = (
                        str(exc)
                        if isinstance(exc, MediaError)
                        else safe_exception(exc) + " Check audiostatus and the Red logs."
                    )
                    log_failure("Native playback", exc, guild_id=self.guild.id)
                    try:
                        await self.report_error(self, track, self.last_error)
                    except Exception:
                        log.warning("Could not deliver AudioPlus playback failure", exc_info=True)
                finally:
                    async with self.lock:
                        self.current = None
                        self._track_task = None
                        if completed and not self.closed:
                            if self.repeat == "track":
                                self.queue.appendleft(track)
                            elif self.repeat == "queue":
                                self.queue.append(track)
                if completed and not self.closed and not self.queue and self.on_end:
                    try:
                        await self.on_end(self, track)
                    except Exception:
                        log.warning("AudioPlus autoplay lookup failed", exc_info=True)
        finally:
            self.current = None

    def _cancel_track(self):
        if self._track_task and not self._track_task.done():
            self._track_task.cancel()
        self.voice.stop()

    async def skip(self):
        async with self.lock:
            track = self.current
            self._restart = None
            self._cancel_track()
            self._schedule_idle()
            self._start_worker()
            return track

    async def stop(self):
        async with self.lock:
            self._recovery_cleared = True
            self._autoplay_generation += 1
            self.queue.clear()
            self._restart = None
            self._cancel_track()
            self._schedule_idle()

    async def set_volume(self, value):
        self.volume = max(0, min(1000, int(value)))
        if self.source:
            self.source.volume = self.volume

    async def shuffle(self):
        async with self.lock:
            items = list(self.queue)
            random.shuffle(items)
            self.queue = deque(items)

    async def remove(self, position):
        async with self.lock:
            if not 1 <= position <= len(self.queue):
                raise MediaError("Choose an upcoming queue position shown by queue.")
            items = list(self.queue)
            selected = items.pop(position - 1)
            self.queue = deque(items)
            self._balance_queue()
            return selected

    async def move(self, source, destination):
        async with self.lock:
            if not 1 <= source <= len(self.queue) or not 1 <= destination <= len(self.queue):
                raise MediaError("Both positions must be in the upcoming queue.")
            items = list(self.queue)
            selected = items.pop(source - 1)
            items.insert(destination - 1, selected)
            self.queue = deque(items)
            self._balance_queue()
            return selected

    async def seek(self, position):
        async with self.lock:
            if not self.current or self.preparing or not self.current.length:
                raise MediaError(
                    "Wait for a track with a known duration before seeking. Live streams cannot seek."
                )
            if not 0 <= position < self.current.length:
                raise MediaError("Choose a position before the end of this track.")
            self._restart = (self.current, position, self.paused)
            self._cancel_idle()
            self._cancel_track()
            self._start_worker()

    async def restart(self, track, *, start=0, paused=False):
        async with self.lock:
            self._cancel_idle()
            self._restart = (track, start, paused)
            self._cancel_track()
            self._start_worker()

    async def close(self, *, disconnect=True):
        self.closed = True
        self.queue.clear()
        self._requesters.clear()
        self._restart = None
        idle = self._cancel_idle()
        self._cancel_track()
        pending = []
        if idle and idle is not asyncio.current_task():
            pending.append(idle)
        if self._runner:
            self._runner.cancel()
            pending.append(self._runner)
        await asyncio.gather(*pending, return_exceptions=True)
        if disconnect:
            await self.voice.disconnect(force=True)
