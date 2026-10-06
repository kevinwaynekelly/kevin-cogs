"""Persistent daily playback checks with bounded probes and private failure alerts."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from redbot.core import commands

from .failures import PlaybackFailure, log_failure
from .resolver import MediaError, http_url

log = logging.getLogger(__name__)
RETRY_SECONDS = 15 * 60
POLL_SECONDS = 60
PROBE_TIMEOUT = 150
PROBE_FRAMES = 150  # Three seconds of actual FFmpeg PCM frames, sent at zero volume.
DEFAULT_WATCHDOG = {
    "enabled": False,
    "recipient_id": None,
    "guild_id": None,
    "channel_id": None,
    "video_url": "https://www.youtube.com/watch?v=YE7VzlLtp-4",
    "hour": 9,
    "minute": 0,
    "timezone": "America/Chicago",
    "last_check_day": None,
    "retry_at": 0,
    "last_result": {},
    "pending_alert": {},
    "alert_retry_at": 0,
    "last_alert_error": None,
}


@dataclass(frozen=True)
class CheckResult:
    status: str
    detail: str


def schedule_time(value: str, zone: str):
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        raise commands.BadArgument("Use a 24-hour time such as 09:00.")
    hour, minute = map(int, value.split(":"))
    if hour > 23 or minute > 59:
        raise commands.BadArgument("Use a time from 00:00 through 23:59.")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise commands.BadArgument(
            "Use an installed IANA timezone such as America/Chicago or UTC."
        ) from exc
    return hour, minute


def youtube_video(value: str):
    parts = urlsplit(http_url(value.strip()))
    host = parts.hostname.lower()
    video = None
    if host in {"youtu.be", "www.youtu.be"}:
        video = parts.path.strip("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parts.path == "/watch":
            video = parse_qs(parts.query).get("v", [None])[0]
        elif parts.path.startswith(("/shorts/", "/embed/", "/live/")):
            video = parts.path.rsplit("/", 1)[-1]
    if video is None or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
        raise commands.BadArgument("Use a YouTube video URL, not a channel or playlist URL.")
    return "https://www.youtube.com/watch?v=" + video


def check_due(settings, now):
    if not settings["enabled"] or settings["retry_at"] > now.timestamp():
        return False
    local = now.astimezone(ZoneInfo(settings["timezone"]))
    return settings["last_check_day"] != local.date().isoformat() and local.time() >= time(
        settings["hour"], settings["minute"]
    )


class PlaybackWatchdog:
    def __init__(self, config, ready, probe, notify, *, clock=None, report_failure=None):
        self.config, self.ready, self.probe, self.notify = config, ready, probe, notify
        self.report_failure = report_failure
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self._task = None
        self._checks = set()
        self._closed = False
        self._version = 0

    def start(self):
        if self._task is None or self._task.done():
            self._closed = False
            self._task = asyncio.create_task(self._loop(), name="AudioPlusPlaybackCheck")

    async def close(self):
        self._closed = True
        pending = tuple(self._checks) + ((self._task,) if self._task else ())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._checks.clear()
        self._task = None

    async def configure(self, **updates):
        self._version += 1
        # All watchdog writers share the global lock with legacy node-setting writes.
        async with self.config.all() as data:
            if (
                "guild_id" in updates
                and updates["guild_id"] != data["watchdog"]["guild_id"]
                and "last_result" not in updates
            ):
                updates["last_result"] = {}
            data["watchdog"].update(
                updates, pending_alert={}, alert_retry_at=0, last_alert_error=None
            )
        pending = tuple(self._checks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _tracked(self, coro):
        if self._closed:
            coro.close()
            raise asyncio.CancelledError
        task = asyncio.create_task(coro)
        self._checks.add(task)
        try:
            return await task
        finally:
            self._checks.discard(task)

    async def check(self, *, scheduled=False):
        return await self._tracked(self._check(scheduled=scheduled))

    async def _check(self, *, scheduled):
        async with self._lock:
            settings = await self.config.watchdog()
            if scheduled and not check_due(settings, self.clock()):
                return None
            if not settings["guild_id"] or not settings["recipient_id"]:
                raise commands.UserInputError("Run audiocheck enable in your server first.")
            version = self._version
            try:
                result = await asyncio.wait_for(self.probe(settings), PROBE_TIMEOUT)
            except (MediaError, commands.CommandError) as exc:
                result = CheckResult("failed", str(exc))
            except asyncio.TimeoutError:
                result = CheckResult("failed", "The playback check timed out.")
            except Exception as exc:
                log_failure("Playback probe", exc, guild_id=settings["guild_id"])
                result = CheckResult("failed", str(PlaybackFailure("Playback probe", exc)))
            now = self.clock()
            record = {"status": result.status, "detail": result.detail, "at": now.timestamp()}
            async with self.config.all() as data:
                if self._closed or self._version != version:
                    return result
                state = data["watchdog"]
                state["last_result"] = record
                state["retry_at"] = (
                    now.timestamp() + RETRY_SECONDS if result.status == "deferred" else 0
                )
                if result.status != "deferred":
                    state["last_check_day"] = (
                        now.astimezone(ZoneInfo(state["timezone"])).date().isoformat()
                    )
                    state["pending_alert"] = (
                        record if result.status == "failed" and state["enabled"] else {}
                    )
                    state["alert_retry_at"] = 0
                    state["last_alert_error"] = None
            if result.status == "failed" and self.report_failure is not None:
                try:
                    await self.report_failure(settings, record)
                except Exception as failure:
                    log.warning(
                        "Could not record the playback-check failure",
                        extra={"notification_error": type(failure).__name__},
                    )
            await self._deliver_pending()
            return result

    async def _deliver_pending(self):
        settings = await self.config.watchdog()
        alert = settings["pending_alert"]
        if (
            not settings["enabled"]
            or not alert
            or settings["alert_retry_at"] > self.clock().timestamp()
        ):
            return
        version = self._version
        try:
            await asyncio.wait_for(self.notify(settings, alert), 15)
        except Exception as exc:
            log.warning(
                "Could not DM playback-check recipient %s (%s)",
                settings["recipient_id"],
                type(exc).__name__,
            )
            error = "Could not deliver the failure DM. Allow direct messages from this bot."
        else:
            error = None
        async with self.config.all() as data:
            state = data["watchdog"]
            if self._version != version or state["pending_alert"] != alert:
                return
            state["last_alert_error"] = error
            if error:
                state["alert_retry_at"] = self.clock().timestamp() + RETRY_SECONDS
            else:
                state["pending_alert"] = {}
                state["alert_retry_at"] = 0

    async def _retry_alert(self):
        async with self._lock:
            await self._deliver_pending()

    async def tick(self):
        settings = await self.config.watchdog()
        if check_due(settings, self.clock()):
            await self.check(scheduled=True)
        else:
            await self._tracked(self._retry_alert())

    async def _loop(self):
        while not self._closed:
            try:
                settings = await self.config.watchdog()
                if settings["enabled"]:
                    await self.ready()
                    await self.tick()
            except asyncio.CancelledError:
                if self._closed:
                    raise
                # A settings change cancels the probe, not the scheduler.
            except Exception as exc:
                log.warning("Playback-check scheduler error (%s)", type(exc).__name__)
            await asyncio.sleep(POLL_SECONDS)
