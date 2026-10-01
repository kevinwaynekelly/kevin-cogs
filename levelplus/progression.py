"""Custom goals, bounded daily streaks and automatic monthly season closure."""

import asyncio
import re
import time
import uuid
from datetime import timedelta
from heapq import nlargest
from typing import Optional

import discord
from redbot.core import commands

from .features import day_at, period_totals, safe_role

PROGRESS_SETTINGS = {
    "streak": False,
    "daily_bonus": 10,
    "max_bonus": 100,
    "goals": {},
    "monthly": False,
    "announce_channel": None,
}
CALENDAR_DEFAULTS = {"month": "", "pending": []}
METRICS = {"xp", "message", "reaction", "voice", "level", "streak"}


def new_progress():
    return {"day": "", "streak": 0, "counts": {}, "earned": {}, "pending": 0}


def advance_progress(record, conf, *, source, amount, day, level, now):
    if amount <= 0:
        return
    if record["day"] != day.isoformat():
        yesterday = (day - timedelta(days=1)).isoformat()
        record["streak"] = record["streak"] + 1 if record["day"] == yesterday else 1
        record["day"] = day.isoformat()
        if conf["streak"]:
            record["pending"] = min(
                250000,
                record["pending"]
                + min(conf["max_bonus"], conf["daily_bonus"] * min(record["streak"], 10)),
            )
    counts = record["counts"]
    counts[source] = counts.get(source, 0) + 1
    counts["xp"] = counts.get("xp", 0) + amount
    valid_ids = {goal["id"] for goal in conf["goals"].values()}
    record["earned"] = {key: value for key, value in record["earned"].items() if key in valid_ids}
    for goal in conf["goals"].values():
        value = (
            level
            if goal["metric"] == "level"
            else record["streak"]
            if goal["metric"] == "streak"
            else counts.get(goal["metric"], 0)
        )
        if value >= goal["target"] and goal["id"] not in record["earned"]:
            record["earned"][goal["id"]] = int(now)
            record["pending"] = min(250000, record["pending"] + goal["reward"])


class ProgressionCommands:
    async def _progress_award(
        self, guild, member, amount, source, day, now, total, settings, budget
    ):
        conf = settings["progress_settings"]
        if not conf["streak"] and not conf["goals"]:
            return 0
        section = self.config.guild(guild).progress
        async with section.get_lock():
            record = await section.get_raw(str(member.id), default=new_progress())
            advance_progress(
                record,
                conf,
                source=source,
                amount=amount,
                day=day,
                level=self._level(total, settings),
                now=now,
            )
            bonus = min(record["pending"], budget) if budget is not None else record["pending"]
            record["pending"] -= bonus
            if amount or bonus:
                await section.set_raw(str(member.id), value=record)
            return bonus

    async def _custom_reward_roles(self, member, settings):
        record = await self.config.guild(member.guild).progress.get_raw(
            str(member.id), default=new_progress()
        )
        desired = []
        for goal in settings["progress_settings"]["goals"].values():
            if goal["id"] in record["earned"] and goal["role"]:
                role = member.guild.get_role(goal["role"])
                if role:
                    try:
                        safe_role(member.guild, role)
                        desired.append(role)
                    except commands.BadArgument:
                        pass
        return list(dict.fromkeys(desired))

    async def _progress_policy(self, ctx, updates):
        section = self.config.guild(ctx.guild).progress_settings
        async with section.get_lock():
            conf = await section()
            conf.update(updates)
            await section.set(conf)
        self._settings_cache.pop(ctx.guild.id, None)

    @commands.hybrid_group(name="achievement", invoke_without_command=True, fallback="list")
    @commands.guild_only()
    async def custom_achievement(self, ctx, member: Optional[discord.Member] = None):
        """View server-defined achievements, goals and rewards."""
        member = member or ctx.author
        conf = await self.config.guild(ctx.guild).progress_settings()
        record = await self.config.guild(ctx.guild).progress.get_raw(
            str(member.id), default=new_progress()
        )
        lines = []
        for name, goal in conf["goals"].items():
            status = (
                "Earned"
                if goal["id"] in record["earned"]
                else f"Target {goal['target']} {goal['metric']}"
            )
            lines.append(
                f"**{name}** · {status} · {goal['reward']} XP"
                + (f" · <@&{goal['role']}>" if goal["role"] else "")
            )
        await self._reply(
            ctx,
            member.mention + "\n" + ("\n".join(lines) or "No custom achievements configured."),
            title="Custom achievements",
        )

    @custom_achievement.command(name="create")
    @commands.admin_or_permissions(manage_guild=True)
    async def achievement_create(
        self,
        ctx,
        name: str,
        metric: str,
        target: int,
        reward: int = 0,
        role: Optional[discord.Role] = None,
    ):
        """Create a goal for XP, messages, reactions, voice, level or streak."""
        name, metric = name.lower(), metric.lower()
        if (
            not re.fullmatch(r"[a-z0-9_-]{1,32}", name)
            or metric not in METRICS
            or not 1 <= target <= 1000000000
            or not 0 <= reward <= 10000
        ):
            raise commands.BadArgument(
                "Choose a 1 to 32 character slug, a supported metric, target 1 to 1 billion, and reward 0 to 10000 XP."
            )
        if role:
            safe_role(ctx.guild, role)
        section = self.config.guild(ctx.guild).progress_settings
        async with section.get_lock():
            conf = await section()
            if name in conf["goals"] or len(conf["goals"]) >= 25:
                raise commands.BadArgument(
                    "That goal exists or the server already has 25 goals. Delete one first."
                )
            conf["goals"][name] = {
                "id": uuid.uuid4().hex[:12],
                "metric": metric,
                "target": target,
                "reward": reward,
                "role": role.id if role else None,
            }
            await section.set(conf)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._reply(
            ctx,
            "Custom achievement created. Progress counts qualifying activity from when these tools are enabled.",
        )

    @custom_achievement.command(name="delete")
    @commands.admin_or_permissions(manage_guild=True)
    async def achievement_delete(self, ctx, name: str):
        """Delete a custom goal without removing already granted rewards."""
        section = self.config.guild(ctx.guild).progress_settings
        async with section.get_lock():
            conf = await section()
            conf["goals"].pop(name.lower(), None)
            await section.set(conf)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._reply(ctx, "Custom goal removed. Granted XP and roles stay in place.")

    @commands.hybrid_command(name="streak")
    @commands.guild_only()
    async def streak(self, ctx, member: Optional[discord.Member] = None):
        """Show consecutive local days of qualifying activity and pending rewards."""
        member = member or ctx.author
        record = await self.config.guild(ctx.guild).progress.get_raw(
            str(member.id), default=new_progress()
        )
        settings = await self._settings(ctx.guild)
        today = day_at(time.time(), settings["xp_features"]["timezone"])
        active = record["day"] in {today.isoformat(), (today - timedelta(days=1)).isoformat()}
        await self._reply(
            ctx,
            f"{member.mention}\nStreak: {record['streak'] if active else 0} day(s)\nLast active: {record['day'] or 'Never'}\nPending earned bonuses: {record['pending']} XP",
            title="Activity streak",
        )

    @commands.hybrid_command(name="streakset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def streak_settings(
        self, ctx, enabled: bool, daily_bonus: int = 10, max_bonus: int = 100
    ):
        """Enable one bounded bonus per active day, within the normal daily cap."""
        if not 0 <= daily_bonus <= 100 or not 0 <= max_bonus <= 1000:
            raise commands.BadArgument(
                "Use a daily step of 0 to 100 XP and maximum daily bonus of 0 to 1000 XP."
            )
        await self._progress_policy(
            ctx, {"streak": enabled, "daily_bonus": daily_bonus, "max_bonus": max_bonus}
        )
        await self._reply(
            ctx,
            "Streak bonus policy saved. Each day earns at most one bonus; unpayable rewards remain pending within the existing daily XP cap.",
        )

    @commands.hybrid_command(name="roleboard")
    @commands.guild_only()
    async def role_leaderboard(self, ctx, role: discord.Role, period: str = "all", top: int = 10):
        """Rank current role members by lifetime, week, month or season XP."""
        if not 1 <= top <= 25 or period not in {"all", "week", "month", "season"}:
            raise commands.BadArgument(
                "Choose all, week, month or season and a top count of 1 to 25."
            )
        if period == "all":
            totals = await self.config.guild(ctx.guild).xp()
        else:
            settings = await self._settings(ctx.guild)
            totals = period_totals(
                await self.config.guild(ctx.guild).period_xp(),
                period,
                day_at(time.time(), settings["xp_features"]["timezone"]),
            )
        eligible = {str(member.id) for member in role.members if not member.bot}
        rows = nlargest(
            top,
            ((uid, xp) for uid, xp in totals.items() if uid in eligible),
            key=lambda row: (row[1], row[0]),
        )
        await self._reply(
            ctx,
            f"{role.mention} · {period}\n"
            + (
                "\n".join(f"{i}. <@{uid}> · {xp:,} XP" for i, (uid, xp) in enumerate(rows, 1))
                or "No ranked current members."
            ),
            title="Role leaderboard",
        )

    async def _roll_month(self, guild, periods, conf, day, now):
        if not conf["progress_settings"]["monthly"] or not conf["xp_features"]["periods"]:
            return
        section = self.config.guild(guild).season_calendar
        async with section() as calendar:
            month = day.strftime("%Y-%m")
            if calendar["month"] == month:
                return
            if periods["season"] or calendar["month"]:
                top = dict(nlargest(50, periods["season"].items(), key=lambda row: row[1]))
                archive = {"name": periods["season_name"], "ended": int(now), "xp": top}
                periods["archives"].append(archive)
                periods["archives"] = periods["archives"][-5:]
                if calendar["month"]:
                    calendar["pending"].append(
                        {
                            "id": uuid.uuid4().hex[:12],
                            "name": archive["name"],
                            "at": int(now),
                            "xp": dict(list(top.items())[:3]),
                        }
                    )
                    calendar["pending"] = calendar["pending"][-5:]
            calendar["month"] = month
            periods.update(season_name=day.strftime("%B %Y"), season_start=int(now), season={})

    @commands.hybrid_command(name="monthlyseason")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def monthly_season(
        self, ctx, enabled: bool, channel: Optional[discord.TextChannel] = None
    ):
        """Automatically archive monthly seasons and optionally announce winners."""
        if enabled and not (await self.config.guild(ctx.guild).xp_features())["periods"]:
            raise commands.BadArgument("Enable level periods before automatic seasons.")
        await self._progress_policy(
            ctx, {"monthly": enabled, "announce_channel": channel.id if channel else None}
        )
        if not enabled:
            async with self.config.guild(ctx.guild).season_calendar() as calendar:
                calendar.update(month="", pending=[])
        else:
            await self._season_tick(ctx.guild)
        await self._reply(
            ctx,
            "Monthly season policy saved. Lifetime XP is preserved; existing seasonal standings are archived when monthly tracking begins.",
        )

    async def _season_tick(self, guild, *, now=None):
        now = time.time() if now is None else now
        conf = await self._settings(guild)
        if not conf["progress_settings"]["monthly"]:
            return
        group = self.config.guild(guild)
        async with group.xp.get_lock():
            async with group.period_xp() as periods:
                await self._roll_month(
                    guild, periods, conf, day_at(now, conf["xp_features"]["timezone"]), now
                )
        policy = conf["progress_settings"]
        if not policy["monthly"]:
            return
        channel = (
            guild.get_channel(policy["announce_channel"]) if policy["announce_channel"] else None
        )
        section = group.season_calendar
        pending = (await section())["pending"]
        if not channel:
            async with section() as calendar:
                calendar["pending"] = []
            return
        if pending and not self._closing:
            record = pending[0]
            winners = (
                "\n".join(
                    f"{i}. <@{uid}> · {amount:,} XP"
                    for i, (uid, amount) in enumerate(record["xp"].items(), 1)
                )
                or "No qualifying participants."
            )
            await asyncio.wait_for(
                self._presentation.send(
                    channel,
                    f"**{record['name']}** closed.\n{winners}\nA new monthly season is open. Lifetime XP stays intact.",
                    title="Season winners",
                    tone="success",
                ),
                10,
            )
            async with section() as calendar:
                calendar["pending"] = [r for r in calendar["pending"] if r["id"] != record["id"]]

    async def _progress_loop(self):
        await self.bot.wait_until_red_ready()
        while not self._closing:
            for guild in self.bot.guilds:
                if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
                    continue
                try:
                    await self._season_tick(guild)
                except Exception:
                    self._progress_log.exception("Monthly season maintenance failed")
            await asyncio.sleep(60)

    async def _calendar_user_data(self, user_id, *, delete=False):
        result = {}
        uid = str(user_id)
        for gid in await self.config.all_guilds():
            section = self.config.guild_from_id(gid).season_calendar
            async with section() as calendar:
                records = [
                    {"name": r["name"], "at": r["at"], "xp": r["xp"][uid]}
                    for r in calendar["pending"]
                    if uid in r["xp"]
                ]
                if records:
                    result[str(gid)] = records
                if delete:
                    for r in calendar["pending"]:
                        r["xp"].pop(uid, None)
        return result
