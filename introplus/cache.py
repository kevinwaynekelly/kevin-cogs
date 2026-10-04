"""Bounded, persistent intro segments ready for Discord's PCM audio thread."""

import asyncio
import audioop
import hashlib
import json
import os
import re
import shlex
import shutil
import threading
from collections import OrderedDict
from pathlib import Path

import discord

from .media_network import REMOTE_OPTIONS, NetworkPolicyError, create_relay, settle_owned
from .resolver import MediaError

FRAME_BYTES = 3840
MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_CACHE_ITEMS = 256
MAX_PREPARING = 128
DOWNLOAD_TIMEOUT = 60
FILE_NAME = re.compile(r"([0-9]+)_([0-9]+)_([a-f0-9]{64})\.pcm$")


def clip_key(guild_id, member_id, clip):
    timing = (clip["track"]["uri"], clip["duration"], clip["start"])
    digest = hashlib.sha256(json.dumps(timing, allow_nan=False).encode()).hexdigest()
    return f"{guild_id}_{member_id}_{digest}"


def clip_duration(clip):
    duration = clip["duration"]
    length = clip["track"]["length"]
    if length:
        duration = min(duration, length / 1000 - clip["start"])
    return duration


class CachedSource(discord.AudioSource):
    """Read prepared PCM directly, without starting FFmpeg or visiting YouTube."""

    def __init__(self, path, *, volume, start=0):
        self._lock = threading.Lock()
        self._cleaned = True
        self._file = open(path, "rb")
        self.volume, self.start, self.frames = volume, start, 0
        self._cleaned = False

    @property
    def position(self):
        return self.start + self.frames * 20

    @property
    def cleaned(self):
        return self._cleaned

    def read(self):
        with self._lock:
            if self._cleaned:
                return b""
            data = self._file.read(FRAME_BYTES)
            if not data:
                return b""
            if len(data) != FRAME_BYTES:
                raise MediaError("The local intro clip is incomplete. Set the intro again.")
            self.frames += 1
            return audioop.mul(data, 2, self.volume / 100) if self.volume != 100 else data

    def is_opus(self):
        return False

    def cleanup(self):
        with self._lock:
            if not self._cleaned:
                self._cleaned = True
                self._file.close()


class ClipCache:
    def __init__(self, root, resolver):
        self.root, self.resolver = Path(root), resolver
        self.entries = OrderedDict()
        self.jobs = {}
        self.failed = set()
        self._slots = asyncio.Semaphore(2)
        self._closed = False

    def initialize(self, allowed):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        entries = []
        for path in self.root.iterdir():
            match = FILE_NAME.fullmatch(path.name)
            if not match and path.suffix != ".part":
                continue
            if path.is_symlink() or path.suffix == ".part" or path.stem not in allowed:
                path.unlink(missing_ok=True)
                continue
            size = path.stat().st_size
            if not size or size % FRAME_BYTES or size > allowed[path.stem]:
                path.unlink(missing_ok=True)
                continue
            path.chmod(0o600)
            entries.append((path.stat().st_mtime, path.stem, size))
        self.entries.update((key, size) for _, key, size in sorted(entries))
        self._trim()

    def _trim(self, reserve=0):
        while self.entries and (
            sum(self.entries.values()) + reserve > MAX_CACHE_BYTES
            or len(self.entries) + bool(reserve) > MAX_CACHE_ITEMS
        ):
            key, _ = self.entries.popitem(last=False)
            (self.root / f"{key}.pcm").unlink(missing_ok=True)

    def path(self, guild_id, member_id, clip):
        key = clip_key(guild_id, member_id, clip)
        if key not in self.entries:
            return None
        path = self.root / f"{key}.pcm"
        if not path.is_file() or path.is_symlink() or path.stat().st_size != self.entries[key]:
            self.entries.pop(key, None)
            path.unlink(missing_ok=True)
            return None
        self.entries.move_to_end(key)
        return path

    def status(self, guild_id, member_id, clip):
        key = clip_key(guild_id, member_id, clip)
        if self.path(guild_id, member_id, clip):
            return "ready, plays from a local copy"
        if key in self.jobs:
            return "downloading the selected segment"
        if key in self.failed:
            return "download failed; intro test retries it"
        return "not prepared yet; the first preview or join downloads it"

    def peek_status(self, guild_id, member_id, clip):
        """Inspect readiness without updating recency, deleting files or starting work."""
        key = clip_key(guild_id, member_id, clip)
        size = self.entries.get(key)
        path = self.root / f"{key}.pcm"
        try:
            ready = (
                size is not None
                and not path.is_symlink()
                and path.is_file()
                and path.stat().st_size == size
            )
        except OSError:
            ready = False
        if ready:
            return "ready, plays from a local copy"
        if key in self.jobs:
            return "downloading the selected segment"
        if key in self.failed:
            return "download failed; intro test retries it"
        return "not prepared yet; the first preview or join downloads it"

    def schedule(self, guild_id, member_id, clip, track):
        if self._closed:
            raise MediaError("IntroPlus is unloading. Try again after it reloads.")
        key = clip_key(guild_id, member_id, clip)
        if key in self.jobs and not self.jobs[key].done():
            return self.jobs[key]
        if len(self.jobs) >= MAX_PREPARING:
            raise MediaError("Too many intros are being prepared. Try again shortly.")
        task = asyncio.create_task(self._prepare(key, clip, track), name="IntroPlus clip download")
        self.jobs[key] = task
        self.failed.discard(key)

        def done(finished):
            if self.jobs.get(key) is finished:
                self.jobs.pop(key, None)
            if not finished.cancelled() and finished.exception() is not None:
                self.failed.add(key)
                # Error messages, signed streams and provider output stay private.
                while len(self.failed) > MAX_CACHE_ITEMS:
                    self.failed.pop()

        task.add_done_callback(done)
        return task

    async def get(self, guild_id, member_id, clip, track):
        path = self.path(guild_id, member_id, clip)
        if path is None:
            path = await asyncio.shield(self.schedule(guild_id, member_id, clip, track))
        return path

    async def _prepare(self, key, clip, track):
        async with self._slots:
            if self._closed:
                raise MediaError("IntroPlus is unloading.")
            stream = await self.resolver.resolve(track)
            temporary = self.root / f"{key}.part"
            try:
                await self._download(stream, clip, temporary)
                size = temporary.stat().st_size
                limit = max(1, int(clip_duration(clip) * 1000) // 20) * FRAME_BYTES
                # FFmpeg may finish with a partial frame at the video's end.
                size = min(size, limit) // FRAME_BYTES * FRAME_BYTES
                if not size:
                    raise MediaError("The selected intro segment contains no playable audio.")
                with temporary.open("r+b") as output:
                    output.truncate(size)
                self._trim(reserve=size)
                path = temporary.replace(self.root / f"{key}.pcm")
                self.entries[key] = size
                return path
            finally:
                temporary.unlink(missing_ok=True)

    async def _download(self, stream, clip, temporary):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise MediaError("FFmpeg is missing. Install it in Red's container.")
        duration = max(0.02, int(clip_duration(clip) * 1000) // 20 * 0.02)
        try:
            relay = await create_relay(stream.url, stream.headers)
        except NetworkPolicyError as error:
            raise MediaError(str(error)) from error
        args = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-rw_timeout",
            "15000000",
            *shlex.split(REMOTE_OPTIONS),
        ]
        if clip["start"]:
            args += ["-ss", f"{clip['start']:.3f}"]
        args += [
            "-i",
            relay.url,
            "-t",
            f"{duration:.3f}",
            "-vn",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-fs",
            str(int(duration * 48000) * 4 + FRAME_BYTES),
            "-f",
            "s16le",
            "pipe:1",
        ]
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            process = None
            with os.fdopen(descriptor, "wb") as output:
                spawning = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *args, stdout=output, stderr=asyncio.subprocess.DEVNULL
                    )
                )
                try:
                    process = await asyncio.shield(spawning)
                    await asyncio.wait_for(process.wait(), DOWNLOAD_TIMEOUT)
                    if process.returncode:
                        raise MediaError(
                            "The intro clip could not be downloaded. Try another public video."
                        )
                except asyncio.CancelledError:
                    if process is None:
                        process = await spawning
                    raise
                except asyncio.TimeoutError as error:
                    raise MediaError(
                        "The intro download timed out. Use intro test to retry."
                    ) from error
                finally:
                    if process is not None and process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await process.wait()
        finally:
            await settle_owned(asyncio.create_task(asyncio.to_thread(relay.close)))

    async def remove(self, *, guild_id=None, member_id=None):
        def matches(key):
            guild, member, _ = key.split("_", 2)
            return (guild_id is None or int(guild) == guild_id) and (
                member_id is None or int(member) == member_id
            )

        tasks = [task for key, task in self.jobs.items() if matches(key)]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for key in list(self.entries):
            if matches(key):
                self.entries.pop(key, None)
                (self.root / f"{key}.pcm").unlink(missing_ok=True)
        self.failed.difference_update(key for key in tuple(self.failed) if matches(key))

    async def close(self):
        self._closed = True
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        self.failed.clear()
