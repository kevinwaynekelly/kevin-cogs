"""Bounded moderation summaries, burst warnings, incident cases and owner error groups."""

import asyncio
import io
import json
import logging
import re
import time
import uuid
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from redbot.core import checks, commands

from .history import history_text

log = logging.getLogger(__name__)
ALERT_DEFAULTS = {
    "channel": None,
    "bursts": {
        key: {"enabled": False, "threshold": threshold, "window": 60}
        for key, threshold in (("joins", 10), ("deletes", 20), ("permissions", 5))
    },
    "digest": {"enabled": False, "timezone": "America/Chicago", "hour": 9},
    "errors": {"enabled": False, "recipient": None, "threshold": 5, "window": 300},
}
SUMMARY_DEFAULTS = {"days": {}, "last_day": ""}
CASE_LIMIT = 25
CASE_RETENTION = 90 * 86400
CASE_BYTES = 1024 * 1024


def identifiers(text):
    return {int(value) for value in re.findall(r"(?<!\d)\d{15,22}(?!\d)", text)}


def case_users(record):
    users = {record["creator"], record.get("subject"), *identifiers(record["title"])}
    for note in record["notes"]:
        users.update({note["user"], *identifiers(note["text"])})
    for event in record["events"]:
        users.update(event["users"])
    if record["resolution"]:
        users.update({record["resolution"]["user"], *identifiers(record["resolution"]["text"])})
    return users - {0, None}


class IncidentCommands:
    def _window_hit(self, key, *, now, window, threshold):
        rows = self._alert_windows.pop(key, deque(maxlen=1000))
        while rows and now - rows[0] > window:
            rows.popleft()
        rows.append(now)
        self._alert_windows[key] = rows
        while len(self._alert_windows) > 200:
            old, _ = self._alert_windows.popitem(last=False)
            self._alert_sent.pop(old, None)
        if len(rows) < threshold or now - self._alert_sent.get(key, -window) < window:
            return False
        self._alert_sent[key] = now
        return True

    def _owned_alert(self, coro):
        task = asyncio.create_task(coro)
        self._alert_tasks.add(task)

        def finished(done):
            self._alert_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                log.warning(
                    "Moderation notification delivery failed: %s", type(done.exception()).__name__
                )

        task.add_done_callback(finished)

    async def _alert_channel(self, guild, text, title):
        policy = await self.config.guild(guild).alert_settings()
        channel = guild.get_channel(policy["channel"]) if policy["channel"] else None
        if channel and not self._closing and not await self.bot.cog_disabled_in_guild(self, guild):
            await asyncio.wait_for(
                self._presentation.send(channel, text, title=title, tone="warning"), 10
            )

    async def _observe_log(self, guild, pending):
        conf = (await self._settings(guild))["alert_settings"]
        if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
            return
        now = time.time()
        if conf["digest"]["enabled"]:
            day = datetime.fromtimestamp(now, ZoneInfo(conf["digest"]["timezone"])).date()
            section = self.config.guild(guild).moderation_summary
            async with section() as state:
                cutoff = (day - timedelta(days=7)).isoformat()
                state["days"] = {
                    key: value for key, value in state["days"].items() if key >= cutoff
                }
                counts = state["days"].setdefault(day.isoformat(), {})
                category = pending.category or "other"
                counts[category] = min(1000000000, counts.get(category, 0) + 1)
        metric = (
            "joins"
            if pending.event_type == "member_joined"
            else "deletes"
            if pending.event_type in {"message_deleted", "bulk_delete"}
            else "permissions"
            if pending.permission_change
            and pending.event_type in {"role_updated", "channel_updated"}
            else None
        )
        if metric:
            policy = conf["bursts"][metric]
            if policy["enabled"] and self._window_hit(
                (guild.id, metric),
                now=time.monotonic(),
                window=policy["window"],
                threshold=policy["threshold"],
            ):
                self._owned_alert(
                    self._alert_channel(
                        guild,
                        f"At least {policy['threshold']} **{metric}** events in {policy['window']} seconds. Review the server logs before acting.",
                        "Activity burst",
                    )
                )

    @commands.hybrid_group(name="logalerts", invoke_without_command=True, fallback="show")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def log_alerts(self, ctx):
        """Configure moderation alerts and daily summaries."""
        conf = await self.config.guild(ctx.guild).alert_settings()
        lines = [
            f"Destination: {('<#' + str(conf['channel']) + '>') if conf['channel'] else 'Not configured'}"
        ]
        lines.extend(
            f"**{name}** · {'On' if row['enabled'] else 'Off'} · {row['threshold']} in {row['window']}s"
            for name, row in conf["bursts"].items()
        )
        lines.append(
            f"Daily digest: {conf['digest']['enabled']} · {conf['digest']['hour']:02}:00 {conf['digest']['timezone']}"
        )
        lines.append(f"Owner error groups: {conf['errors']['enabled']}")
        await self._reply(ctx, "\n".join(lines), title="Moderation alerts")

    async def _set_alerts(self, guild, key, value):
        section = self.config.guild(guild).alert_settings
        async with section.get_lock():
            data = await section()
            data[key] = value
            await section.set(data)
        self._settings_cache.pop(guild.id, None)

    @log_alerts.command(name="channel")
    async def log_alert_channel(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Set a private staff destination for burst alerts and daily digests."""
        await self._set_alerts(ctx.guild, "channel", channel.id if channel else None)
        await self._reply(ctx, "Alert destination saved.")

    @log_alerts.command(name="burst")
    async def log_alert_burst(
        self, ctx, metric: str, enabled: bool, threshold: int = 10, window: int = 60
    ):
        """Warn on joins, deletes or permission edits within a time window."""
        if (
            metric not in {"joins", "deletes", "permissions"}
            or not 2 <= threshold <= 1000
            or not 10 <= window <= 3600
        ):
            raise commands.BadArgument(
                "Choose joins, deletes or permissions, threshold 2 to 1000 and window 10 to 3600 seconds."
            )
        section = self.config.guild(ctx.guild).alert_settings
        async with section.get_lock():
            data = await section()
            data["bursts"][metric] = {"enabled": enabled, "threshold": threshold, "window": window}
            await section.set(data)
        self._settings_cache.pop(ctx.guild.id, None)
        self._alert_windows.pop((ctx.guild.id, metric), None)
        self._alert_sent.pop((ctx.guild.id, metric), None)
        await self._reply(
            ctx,
            "Burst policy saved. Alerts observe enabled logical log events and never take moderation actions.",
        )

    @log_alerts.command(name="digest")
    async def log_alert_digest(
        self, ctx, enabled: bool, timezone_name: str = "America/Chicago", hour: int = 9
    ):
        """Send yesterday's aggregate enabled-log counts daily."""
        try:
            ZoneInfo(timezone_name)
        except (KeyError, ValueError) as error:
            raise commands.BadArgument("Choose an installed IANA timezone.") from error
        if not 0 <= hour <= 23:
            raise commands.BadArgument("Choose an hour from 0 to 23.")
        await self._set_alerts(
            ctx.guild, "digest", {"enabled": enabled, "timezone": timezone_name, "hour": hour}
        )
        await self._reply(
            ctx,
            "Daily moderation summary policy saved. Collection starts when enabled and needs an alert destination.",
        )

    @log_alerts.command(name="errors")
    @checks.is_owner()
    async def log_alert_errors(self, ctx, enabled: bool, threshold: int = 5, window: int = 300):
        """Bot owner: receive grouped unexpected command failures yourself."""
        if not 2 <= threshold <= 50 or not 30 <= window <= 3600:
            raise commands.BadArgument("Use threshold 2 to 50 and window 30 to 3600 seconds.")
        await self._set_alerts(
            ctx.guild,
            "errors",
            {
                "enabled": enabled,
                "recipient": ctx.author.id if enabled else None,
                "threshold": threshold,
                "window": window,
            },
        )
        await self._reply(
            ctx,
            "Owner error notifications enabled for you."
            if enabled
            else "Owner error notifications disabled.",
        )

    async def _owner_error(self, guild, signature, count, recipient_id):
        conf = (await self.config.guild(guild).alert_settings())["errors"]
        if (
            self._closing
            or not conf["enabled"]
            or not recipient_id
            or conf["recipient"] != recipient_id
            or await self.bot.cog_disabled_in_guild(self, guild)
        ):
            return
        user = self.bot.get_user(recipient_id) or await asyncio.wait_for(
            self.bot.fetch_user(recipient_id), 10
        )
        if not await self.bot.is_owner(user):
            return
        latest = (await self.config.guild(guild).alert_settings())["errors"]
        if self._closing or not latest["enabled"] or latest["recipient"] != recipient_id:
            return
        await asyncio.wait_for(
            self._presentation.send(
                user,
                f"Server ID: {guild.id}\nCommand: `{signature[0]}`\nError type: `{signature[1]}`\nAt least {count} failures within the configured window. Check Red logs for details.",
                title="Repeated command error",
                tone="error",
            ),
            10,
        )

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        original = getattr(error, "original", error)
        if not ctx.guild or self._closing or isinstance(original, commands.CommandError):
            return
        policy = (await self.config.guild(ctx.guild).alert_settings())["errors"]
        if not policy["enabled"] or await self.bot.cog_disabled_in_guild(self, ctx.guild):
            return
        signature = (
            getattr(ctx.command, "qualified_name", "unknown")[:100],
            type(original).__name__[:80],
        )
        if self._window_hit(
            (ctx.guild.id, "errors", *signature),
            now=time.monotonic(),
            window=policy["window"],
            threshold=policy["threshold"],
        ):
            self._owned_alert(
                self._owner_error(ctx.guild, signature, policy["threshold"], policy["recipient"])
            )

    async def _moderation_tick(self, guild, *, now=None):
        now = datetime.now(timezone.utc) if now is None else now
        policy = await self.config.guild(guild).alert_settings()
        digest = policy["digest"]
        if digest["enabled"]:
            local = now.astimezone(ZoneInfo(digest["timezone"]))
            section = self.config.guild(guild).moderation_summary
            state = await section()
            today = local.date().isoformat()
            if local.hour >= digest["hour"] and state["last_day"] != today:
                yesterday = (local.date() - timedelta(days=1)).isoformat()
                counts = state["days"].get(yesterday, {})
                channel = guild.get_channel(policy["channel"]) if policy["channel"] else None
                if channel:
                    await asyncio.wait_for(
                        self._presentation.send(
                            channel,
                            f"**{yesterday}**\n"
                            + (
                                "\n".join(
                                    f"{key.title()}: {value:,}"
                                    for key, value in sorted(counts.items())
                                )
                                or "No collected log events."
                            ),
                            title="Daily moderation summary",
                        ),
                        10,
                    )
                    async with section() as current:
                        current["last_day"] = today
        section = self.config.guild(guild).incident_cases
        async with section() as cases:
            for key in list(cases):
                if cases[key]["created"] < now.timestamp() - CASE_RETENTION:
                    cases.pop(key)

    async def _prune_incidents(self):
        now = time.time()
        for gid, values in (await self.config.all_guilds()).items():
            group = self.config.guild_from_id(gid)
            if values.get("incident_cases"):
                async with group.incident_cases() as cases:
                    for key in list(cases):
                        if cases[key]["created"] < now - CASE_RETENTION:
                            cases.pop(key)
            if values.get("moderation_summary", {}).get("days"):
                cutoff = (
                    datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=8)
                ).isoformat()
                async with group.moderation_summary() as state:
                    state["days"] = {
                        day: row for day, row in state["days"].items() if day >= cutoff
                    }

    async def _moderation_loop(self):
        await self.bot.wait_until_red_ready()
        while not self._closing:
            for guild in self.bot.guilds:
                if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
                    continue
                try:
                    await self._moderation_tick(guild)
                except Exception:
                    log.exception("Moderation summary maintenance failed")
            await asyncio.sleep(60)

    @commands.hybrid_group(name="incident", invoke_without_command=True, fallback="list")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def incident(self, ctx, identifier: str = ""):
        """Review staff incidents with linked logs, notes and resolution."""
        records = await self.config.guild(ctx.guild).incident_cases()
        if not identifier:
            return await self._reply(
                ctx,
                "\n".join(
                    f"`{key}` · {discord.utils.escape_markdown(row['title'])} · {'Resolved' if row['resolution'] else 'Open'}"
                    for key, row in records.items()
                )
                or "No retained incidents.",
            )
        row = records.get(identifier)
        if row is None:
            raise commands.BadArgument("Unknown incident ID.")
        lines = [
            f"**{discord.utils.escape_markdown(row['title'])}** · <t:{row['created']}:f>",
            f"Subject: {('<@' + str(row['subject']) + '>') if row['subject'] else 'General'}",
        ]
        lines.extend(f"Note · <@{note['user']}> · {note['text']}" for note in row["notes"])
        lines.extend(
            f"Log · <t:{int(event['time'])}:f> · {event['title']}\n{event['description']}"
            for event in row["events"]
        )
        if row["resolution"]:
            lines.append("Resolution · " + row["resolution"]["text"])
        await self._reply(ctx, "\n\n".join(lines), title="Incident case")

    def _case_budget(self, cases):
        if len(json.dumps(cases).encode()) > CASE_BYTES:
            raise commands.BadArgument("The server case storage limit was reached.")

    @incident.command(name="create")
    async def incident_create(self, ctx, title: str, member: Optional[discord.Member] = None):
        """Open a staff case, optionally identifying a server member."""
        if not 1 <= len(title.strip()) <= 200:
            raise commands.BadArgument("Use a case title of 1 to 200 characters.")
        key = uuid.uuid4().hex[:12]
        async with self.config.guild(ctx.guild).incident_cases() as cases:
            if len(cases) >= CASE_LIMIT:
                raise commands.BadArgument(
                    "The server retains up to 25 cases. Delete an old case first."
                )
            cases[key] = {
                "title": title.strip(),
                "creator": ctx.author.id,
                "subject": member.id if member else None,
                "created": int(time.time()),
                "notes": [],
                "events": [],
                "resolution": {},
            }
            self._case_budget(cases)
        await self._reply(ctx, f"Opened incident `{key}`. Use incident note, attach and resolve.")

    @incident.command(name="note")
    async def incident_note(self, ctx, identifier: str, *, text: str):
        """Add a bounded staff note to an open incident."""
        if not 1 <= len(text.strip()) <= 1000:
            raise commands.BadArgument("Use a note of 1 to 1000 characters.")
        async with self.config.guild(ctx.guild).incident_cases() as cases:
            row = cases.get(identifier)
            if not row or row["resolution"] or len(row["notes"]) >= 10:
                raise commands.BadArgument("Choose an open case with fewer than ten notes.")
            row["notes"].append(
                {"user": ctx.author.id, "at": int(time.time()), "text": text.strip()}
            )
            self._case_budget(cases)
        await self._reply(ctx, "Staff note saved.")

    @incident.command(name="attach")
    async def incident_attach(self, ctx, identifier: str, member: discord.Member, days: int = 1):
        """Attach up to ten retained log records for a member."""
        records = await self._history_query(ctx.guild, member_id=member.id, days=days, limit=10)
        section = self.config.guild(ctx.guild).incident_cases
        async with section() as cases:
            row = cases.get(identifier)
            if not row or row["resolution"]:
                raise commands.BadArgument("Choose an open incident.")
            existing = {event["time"] for event in row["events"]}
            for event in records:
                if len(row["events"]) >= 20:
                    break
                if event["time"] not in existing:
                    row["events"].append(
                        {
                            "time": event["time"],
                            "users": event["users"],
                            "title": event["title"][:200],
                            "description": history_text(event)[:800],
                        }
                    )
                    existing.add(event["time"])
            if len(json.dumps(cases).encode()) > CASE_BYTES:
                raise commands.BadArgument("The server case storage limit was reached.")
        await self._reply(
            ctx, "Available retained logs attached. History must have collected them first."
        )

    @incident.command(name="resolve")
    async def incident_resolve(self, ctx, identifier: str, *, resolution: str):
        """Resolve an incident with a final staff summary."""
        if not 1 <= len(resolution.strip()) <= 1000:
            raise commands.BadArgument("Use a resolution of 1 to 1000 characters.")
        async with self.config.guild(ctx.guild).incident_cases() as cases:
            row = cases.get(identifier)
            if not row or row["resolution"]:
                raise commands.BadArgument("Choose an open incident.")
            row["resolution"] = {
                "user": ctx.author.id,
                "at": int(time.time()),
                "text": resolution.strip(),
            }
            self._case_budget(cases)
        await self._reply(ctx, "Incident resolved.")

    @incident.command(name="delete")
    async def incident_delete(self, ctx, identifier: str):
        """Delete a retained incident and its notes/log copies."""
        async with self.config.guild(ctx.guild).incident_cases() as cases:
            cases.pop(identifier, None)
        await self._reply(ctx, "Incident removed.")

    async def _incident_user_data(self, user_id, *, delete=False):
        result = {}
        for gid in await self.config.all_guilds():
            group = self.config.guild_from_id(gid)
            async with group.incident_cases() as cases:
                found = {
                    key: deepcopy(row) for key, row in cases.items() if user_id in case_users(row)
                }
                if found:
                    result[str(gid)] = {"cases": found}
                if delete:
                    for key in found:
                        cases.pop(key)
            section = group.alert_settings
            async with section.get_lock():
                policy = await section()
                if policy["errors"]["recipient"] == user_id:
                    result.setdefault(str(gid), {})["error_notifications"] = deepcopy(
                        policy["errors"]
                    )
                    if delete:
                        policy["errors"] = deepcopy(ALERT_DEFAULTS["errors"])
                        await section.set(policy)
                        self._settings_cache.pop(gid, None)
        return result

    async def _incident_export(self, user_id):
        data = await self._incident_user_data(user_id)
        return {"incidents.json": io.BytesIO(json.dumps(data, indent=2).encode())} if data else {}
