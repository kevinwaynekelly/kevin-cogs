"""Persistent, bounded short-song copies with fixed three-calendar-month expiry."""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import logging
import math
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .resolver import MediaError, Stream

log = logging.getLogger(__name__)
MAX_LENGTH = 300_000  # Strictly less than five minutes, in milliseconds.
MAX_CACHE_BYTES = 2 * 1024**3
MAX_CACHE_ITEMS = 2000
MAX_FILE_BYTES = 16 * 1024**2
RESERVE_BYTES = MAX_FILE_BYTES + 64 * 1024
MIN_FREE_BYTES = 256 * 1024**2
MAX_DOWNLOADS = 2
MAX_REQUESTERS = 128
DOWNLOAD_TIMEOUT = 90
PRUNE_INTERVAL = 3600
KEY = re.compile(r"([1-9][0-9]{0,19})_([a-f0-9]{64})$")


def expiry(created):
    """Keep the original UTC day/time, clamped to the third month's last day."""
    date = datetime.fromtimestamp(created, timezone.utc)
    month = date.month - 1 + 3
    year, month = date.year + month // 12, month % 12 + 1
    return date.replace(
        year=year, month=month, day=min(date.day, calendar.monthrange(year, month)[1])
    ).timestamp()


def song_key(guild_id, track):
    return f"{guild_id}_{hashlib.sha256(track.uri.encode()).hexdigest()}"


class SongCache:
    def __init__(self, *, clock=time.time):
        self.root = None
        self.clock = clock
        self.entries = {}
        self.jobs = {}
        self._requesters = {}
        self._maintenance = None
        self._closed = False
        self._suspended = 0
        self.epoch = 0
        self.last_error = None

    def initialize(self, root):
        self.root = Path(root)
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
            now = self.clock()
            for path in self.root.iterdir():
                if path.suffix == ".part":
                    path.unlink(missing_ok=True)
                elif path.suffix == ".json" and KEY.fullmatch(path.stem):
                    try:
                        if path.is_symlink() or path.stat().st_size > 16 * 1024:
                            raise ValueError
                        entry = json.loads(path.read_text())
                        audio = self.root / f"{path.stem}.ogg"
                        self._validate(entry, path.stem, audio, now)
                        if (
                            len(self.entries) >= MAX_CACHE_ITEMS
                            or self._bytes() + entry["size"] > MAX_CACHE_BYTES
                        ):
                            raise ValueError
                        audio.chmod(0o600)
                        path.chmod(0o600)
                        self.entries[path.stem] = entry
                    except (OSError, ValueError, TypeError, KeyError, OverflowError):
                        self.invalidate(path.stem)
            for path in self.root.glob("*.ogg"):
                if KEY.fullmatch(path.stem) and path.stem not in self.entries:
                    path.unlink(missing_ok=True)
        except OSError as error:
            self.last_error = type(error).__name__
            self.root = None

    @staticmethod
    def _validate(entry, key, audio, now):
        if not isinstance(entry, dict) or set(entry) != {
            "version",
            "length",
            "created",
            "size",
            "requesters",
        }:
            raise ValueError
        if (
            entry["version"] != 1
            or type(entry["length"]) is not int
            or not 0 < entry["length"] < MAX_LENGTH
        ):
            raise ValueError
        if type(entry["size"]) is not int or not 0 < entry["size"] <= MAX_FILE_BYTES:
            raise ValueError
        created = entry["created"]
        if (
            type(created) not in (int, float)
            or not math.isfinite(created)
            or not 0 < created <= now + 300
            or expiry(created) <= now
        ):
            raise ValueError
        users = entry["requesters"]
        if (
            not isinstance(users, list)
            or len(users) > MAX_REQUESTERS
            or any(type(uid) is not int or not 0 < uid < 2**64 for uid in users)
        ):
            raise ValueError
        if (
            not KEY.fullmatch(key)
            or audio.is_symlink()
            or not audio.is_file()
            or audio.stat().st_size != entry["size"]
        ):
            raise ValueError
        with audio.open("rb") as source:
            if source.read(4) != b"OggS":
                raise ValueError

    def _bytes(self):
        return sum(entry["size"] for entry in self.entries.values())

    def _save(self, key, entry):
        temporary = self.root / f"{key}.json.part"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(entry, output, allow_nan=False)
            temporary.replace(self.root / f"{key}.json")
        finally:
            temporary.unlink(missing_ok=True)

    def invalidate(self, key):
        self.entries.pop(key, None)
        if self.root and KEY.fullmatch(key):
            for suffix in (".ogg", ".json"):
                try:
                    (self.root / (key + suffix)).unlink(missing_ok=True)
                except OSError as error:
                    self.last_error = type(error).__name__

    def prune(self):
        for key, entry in tuple(self.entries.items()):
            if expiry(entry["created"]) <= self.clock():
                self.invalidate(key)

    def get(self, guild_id, track, requester=0):
        if self._closed or self._suspended or not self.root:
            return None
        key = song_key(guild_id, track)
        entry = self.entries.get(key)
        if not entry:
            return None
        path = self.root / f"{key}.ogg"
        try:
            if expiry(entry["created"]) <= self.clock():
                self.invalidate(key)
                return None
            self._validate(entry, key, path, self.clock())
            if (
                requester
                and requester not in entry["requesters"]
                and len(entry["requesters"]) < MAX_REQUESTERS
            ):
                updated = {**entry, "requesters": [*entry["requesters"], requester]}
                self._save(key, updated)
                self.entries[key] = updated
            return Stream(str(path), length=entry["length"], local=True)
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
            self.last_error = type(error).__name__
            self.invalidate(key)
            return None

    def schedule(self, guild_id, track, stream, requester=0, *, epoch):
        # Full extraction, rather than flat search metadata, decides eligibility.
        length = getattr(stream, "length", 0)
        if (
            self._closed
            or self._suspended
            or not self.root
            or epoch != self.epoch
            or getattr(stream, "local", False)
            or getattr(stream, "live", False)
            or track.direct
            or type(length) is not int
            or not 0 < length < MAX_LENGTH
        ):
            return None
        key = song_key(guild_id, track)
        if key in self.entries:
            return None
        if key in self.jobs:
            if requester and len(self._requesters[key]) < MAX_REQUESTERS:
                self._requesters[key].add(requester)
            return self.jobs[key]
        self.prune()
        if (
            len(self.jobs) >= MAX_DOWNLOADS
            or len(self.entries) + len(self.jobs) >= MAX_CACHE_ITEMS
            or self._bytes() + (len(self.jobs) + 1) * RESERVE_BYTES > MAX_CACHE_BYTES
        ):
            return None
        try:
            if (
                shutil.disk_usage(self.root).free
                < MIN_FREE_BYTES + (len(self.jobs) + 1) * RESERVE_BYTES
            ):
                self.last_error = "Low disk space"
                return None
        except OSError as error:
            self.last_error = type(error).__name__
            return None
        self._requesters[key] = {requester} if requester else set()
        task = asyncio.create_task(self._prepare(key, stream), name="AudioPlus song cache")
        self.jobs[key] = task

        def finished(done):
            if self.jobs.get(key) is done:
                self.jobs.pop(key, None)
                self._requesters.pop(key, None)
            if not done.cancelled() and done.exception():
                self.last_error = type(done.exception()).__name__
                log.warning("AudioPlus song cache preparation failed (%s)", self.last_error)

        task.add_done_callback(finished)
        return task

    async def _prepare(self, key, stream):
        temporary = self.root / f"{key}.ogg.part"
        try:
            await self._download(stream, temporary)
            size = temporary.stat().st_size
            if not 0 < size <= MAX_FILE_BYTES:
                raise MediaError("The downloaded song exceeded the cache file limit.")
            duration = await self._duration(temporary)
            if not 0 < duration < MAX_LENGTH + 1000 or abs(duration - stream.length) > 2000:
                raise MediaError("The downloaded song was incomplete or too long to cache.")
            if self._closed:
                return
            entry = {
                "version": 1,
                "length": stream.length,
                "created": self.clock(),
                "size": size,
                "requesters": sorted(self._requesters[key]),
            }
            path = temporary.replace(self.root / f"{key}.ogg")
            try:
                self._save(key, entry)
                self.entries[key] = entry
                self.last_error = None
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    async def _process(args, *, stdout, timeout):
        async def settle(pending):
            # A clear and an unload can both cancel the owning job. Neither may
            # interrupt transport creation or reaping and orphan the child.
            while True:
                try:
                    return await asyncio.shield(pending)
                except asyncio.CancelledError:
                    if pending.cancelled():
                        raise

        process = None
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(*args, stdout=stdout, stderr=asyncio.subprocess.DEVNULL)
        )
        try:
            process = await asyncio.shield(spawning)
            if stdout == asyncio.subprocess.PIPE:
                output, _ = await asyncio.wait_for(process.communicate(), timeout)
            else:
                await asyncio.wait_for(process.wait(), timeout)
                output = b""
            if process.returncode:
                raise MediaError("The song cache could not prepare this source.")
            return output
        except asyncio.CancelledError:
            if process is None:
                process = await settle(spawning)
            raise
        except asyncio.TimeoutError as error:
            raise MediaError("The song cache preparation timed out.") from error
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await settle(asyncio.create_task(process.communicate()))

    async def _download(self, stream, temporary):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise MediaError("FFmpeg is missing from the Red container.")
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
            "-protocol_whitelist",
            "http,https,httpproxy,tcp,tls,crypto,pipe",
        ]
        if stream.headers:
            args += [
                "-headers",
                "".join(f"{key}: {value}\r\n" for key, value in stream.headers.items()),
            ]
        args += [
            "-i",
            stream.url,
            "-map",
            "0:a:0",
            "-vn",
            "-map_metadata",
            "-1",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "libopus",
            "-b:a",
            "128k",
            "-t",
            "300",
            "-fs",
            str(MAX_FILE_BYTES),
            "-f",
            "ogg",
            "pipe:1",
        ]
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            await self._process(args, stdout=output, timeout=DOWNLOAD_TIMEOUT)

    async def _duration(self, path):
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise MediaError("FFprobe is required to validate cached songs.")
        output = await self._process(
            [
                ffprobe,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            stdout=asyncio.subprocess.PIPE,
            timeout=10,
        )
        if len(output) > 16 * 1024:
            raise MediaError("The cached song returned invalid metadata.")
        try:
            return float(json.loads(output)["format"]["duration"]) * 1000
        except (ValueError, KeyError, TypeError) as error:
            raise MediaError("The cached song returned invalid metadata.") from error

    def start(self):
        if not self._closed and (self._maintenance is None or self._maintenance.done()):
            self._maintenance = asyncio.create_task(self._maintain(), name="AudioPlus cache expiry")

    async def _maintain(self):
        while True:
            await asyncio.sleep(PRUNE_INTERVAL)
            self.prune()

    def status(self, guild_id):
        self.prune()
        entries = [entry for key, entry in self.entries.items() if key.startswith(f"{guild_id}_")]
        return {
            "ready": self.root is not None and not self._closed,
            "songs": len(entries),
            "bytes": sum(entry["size"] for entry in entries),
            "pending": sum(key.startswith(f"{guild_id}_") for key in self.jobs),
            "next_expiry": min((expiry(entry["created"]) for entry in entries), default=0),
            "error": self.last_error,
        }

    def user_data(self, user_id):
        self.prune()
        return [
            {
                "server_id": int(key.split("_", 1)[0]),
                "source_hash": key.split("_", 1)[1],
                "length": entry["length"],
                "cached_at": entry["created"],
                "expires_at": expiry(entry["created"]),
            }
            for key, entry in self.entries.items()
            if user_id in entry["requesters"]
        ]

    async def remove(self, *, guild_id=None, user_id=None):
        def matches(key, users):
            return (guild_id is None or key.startswith(f"{guild_id}_")) and (
                user_id is None or user_id in users
            )

        self.epoch += 1
        self._suspended += 1
        try:
            tasks = [task for key, task in self.jobs.items() if matches(key, self._requesters[key])]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for key, entry in tuple(self.entries.items()):
                if matches(key, entry["requesters"]):
                    self.invalidate(key)
        finally:
            self._suspended -= 1

    async def close(self):
        self._closed = True
        tasks = list(self.jobs.values())
        if self._maintenance:
            tasks.append(self._maintenance)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        self._requesters.clear()
