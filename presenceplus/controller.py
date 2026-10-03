"""One owned presence worker with coalesced, rate-limited gateway updates."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from .constants import MIN_UPDATE_SECONDS, POLL_SECONDS, UPDATE_TIMEOUT
from .profiles import effective_profile, render, validate

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Display:
    profile: str
    rule: str | None
    status: str
    kind: str | None
    text: str
    index: int
    count: int
    music: bool = False

    def activity(self):
        if self.kind is None:
            return None
        if self.kind == "custom":
            return discord.CustomActivity(name=self.text)
        if self.kind == "playing":
            return discord.Game(name=self.text)
        return discord.Activity(name=self.text, type=getattr(discord.ActivityType, self.kind))


def signature(status, activity):
    state = str(status)
    if state == "offline":
        state = "invisible"
    return (
        state,
        getattr(getattr(activity, "type", None), "value", None),
        getattr(activity, "name", None),
        getattr(activity, "state", None),
        getattr(activity, "url", None),
    )


class PresenceController:
    def __init__(self, cog, *, clock=None, monotonic=None):
        self.cog = cog
        self.bot = cog.bot
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.started = self.monotonic()
        self.task = None
        self.wake = asyncio.Event()
        self.closed = False
        self.generation = 0
        self.force = False
        self.phase = None
        self.phase_start = self.started
        self.offset = 0
        self.baseline = None
        self.last_signature = None
        self.last_display = None
        self.last_error = None
        self.gateway = asyncio.Lock()

    def start(self):
        self.task = asyncio.create_task(self.run(), name="PresencePlus rotation")

    def request(self, *, force=False):
        if not self.closed:
            self.generation += 1
            self.force = self.force or force
            self.wake.set()

    def current(self):
        for guild in self.bot.guilds:
            member = guild.me
            if member is not None:
                return member.status, member.activity
        return self.bot.status, self.bot.activity

    def remaining(self):
        previous = getattr(self.bot, "_kevin_presence_last_update", float("-inf"))
        return max(0, MIN_UPDATE_SECONDS - (self.monotonic() - previous))

    async def music_snapshot(self, settings):
        if not settings["music"]["enabled"]:
            return None
        guild = self.bot.get_guild(settings["music"]["guild_id"])
        audio = self.bot.get_cog("AudioPlus")
        getter = getattr(audio, "music_presence", None)
        if not guild or not callable(getter) or await self.bot.cog_disabled_in_guild(audio, guild):
            return None
        snapshot = getter(guild.id)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("song"), str):
            return None
        listeners = snapshot.get("listeners")
        if type(listeners) is not int or listeners < 0:
            return None
        return snapshot

    def uptime(self, now):
        started = getattr(self.bot, "uptime", None)
        if isinstance(started, datetime):
            started = started.replace(tzinfo=timezone.utc) if started.tzinfo is None else started
            seconds = max(0, int((now - started).total_seconds()))
        else:
            seconds = max(0, int(self.monotonic() - self.started))
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes = seconds // 60
        return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"

    async def display(self, settings, *, preview=False):
        now = self.clock()
        profile_name, rule = effective_profile(settings, now)
        profile = settings["profiles"][profile_name]
        phase = (profile_name, json.dumps(profile, sort_keys=True), settings["interval"])
        if phase != self.phase:
            phase_start, offset = self.monotonic(), 0
            if not preview:
                self.phase, self.phase_start, self.offset = phase, phase_start, offset
        else:
            phase_start, offset = self.phase_start, self.offset
        count = len(profile["entries"])
        index = (
            int(max(0, self.monotonic() - phase_start) // settings["interval"]) + offset
        ) % max(1, count)
        snapshot = await self.music_snapshot(settings)
        fields = {
            "servers": len(self.bot.guilds),
            "members": sum(
                guild.member_count if type(guild.member_count) is int else len(guild.members)
                for guild in self.bot.guilds
            ),
            "uptime": self.uptime(now),
            "song": snapshot["song"] if snapshot else "Nothing playing",
            "listeners": snapshot["listeners"] if snapshot else 0,
        }
        if snapshot:
            kind, text = "listening", settings["music"]["text"]
        elif count:
            kind, text = profile["entries"][index]["kind"], profile["entries"][index]["text"]
        else:
            return Display(profile_name, rule, profile["status"], None, "", 0, 0)
        return Display(
            profile_name,
            rule,
            profile["status"],
            kind,
            render(text, fields),
            index,
            count,
            bool(snapshot),
        )

    async def advance(self, settings):
        await self.display(settings)
        self.offset += 1
        self.request()

    async def restore(self, *, wait=False):
        if self.baseline is None:
            return
        # Core or another status cog may have already replaced our display.
        if signature(*self.current()) != self.last_signature:
            self.baseline = self.last_signature = None
            self.last_display = None
            return
        if self.remaining():
            if not wait:
                return
            await asyncio.sleep(self.remaining())
        if signature(*self.current()) != self.last_signature:
            self.baseline = self.last_signature = self.last_display = None
            return
        status, activity = self.baseline
        await self.send(status, activity)
        self.baseline = self.last_signature = None
        self.last_display = None

    async def send(self, status, activity):
        self.bot._kevin_presence_last_update = self.monotonic()
        await asyncio.wait_for(
            self.bot.change_presence(status=status, activity=activity), UPDATE_TIMEOUT
        )

    async def tick(self):
        generation = self.generation
        settings = validate(await self.cog.config.settings())
        async with self.gateway:
            if self.closed or generation != self.generation:
                return
            if not settings["enabled"]:
                await self.restore()
                self.last_error = None
                return
            display = await self.display(settings)
            activity = display.activity()
            target = signature(display.status, activity)
            if generation != self.generation or self.closed:
                return
            if target == self.last_signature and not self.force:
                self.last_display = display
                return
            if self.remaining():
                return
            if self.baseline is None:
                self.baseline = self.current()
            await self.send(getattr(discord.Status, display.status), activity)
            self.last_signature, self.last_display = target, display
            self.last_error = None
            if generation == self.generation:
                self.force = False

    async def run(self):
        await self.bot.wait_until_red_ready()
        while not self.closed:
            self.wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Raw errors can include gateway URLs, status text or player metadata.
                self.last_error = type(error).__name__
                log.warning("Presence update failed (%s)", self.last_error)
            try:
                await asyncio.wait_for(self.wake.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        async with self.gateway:
            try:
                await self.restore(wait=True)
            except Exception as error:
                log.warning("Presence restore failed (%s)", type(error).__name__)
        self.baseline = self.last_signature = self.last_display = None
