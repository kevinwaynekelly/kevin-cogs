"""Earned badges and once-per-week challenges without recursive XP awards."""

import asyncio
import io
import time
from datetime import timedelta
from typing import Optional

import discord
from redbot.core import commands

from .command_support import check_command
from .features import day_at
from .levels import cumulative_xp
from .rankcards import render_card

MILESTONE_DEFAULTS = {
    "badges": True,
    "challenges": False,
    "goals": {
        "message": {"target": 50, "reward": 100},
        "reaction": {"target": 20, "reward": 50},
        "voice": {"target": 10, "reward": 100},
        "xp": {"target": 1000, "reward": 100},
    },
}
GOAL_LABELS = {
    "message": "Qualifying message awards",
    "reaction": "Qualifying reaction awards",
    "voice": "Qualifying voice intervals",
    "xp": "Earned XP",
}
BADGES = {
    "first": ("First steps", "xp", 1),
    "xp1000": ("A thousand earned", "xp", 1000),
    "xp10000": ("Ten thousand earned", "xp", 10000),
    "level5": ("Level 5", "level", 5),
    "level10": ("Level 10", "level", 10),
    "level25": ("Level 25", "level", 25),
    "level50": ("Level 50", "level", 50),
}


def member_progress():
    return {"earned_xp": 0, "badges": {}, "week": "", "counts": {}, "completed": [], "pending": 0}


def advance_challenges(record, settings, source, amount, day):
    if not settings["challenges"] or amount <= 0:
        return
    week = (day - timedelta(days=day.weekday())).isoformat()
    if record["week"] != week:
        record.update(week=week, counts={}, completed=[])
    counts = record["counts"]
    counts[source] = counts.get(source, 0) + 1
    counts["xp"] = counts.get("xp", 0) + amount
    for metric, goal in settings["goals"].items():
        if metric not in record["completed"] and counts.get(metric, 0) >= goal["target"]:
            record["completed"].append(metric)
            record["pending"] += goal["reward"]


class MilestoneCommands:
    async def _milestone_award(
        self, guild, member, amount, source, day, now, old_xp, settings, budget
    ):
        conf = settings["milestone_settings"]
        if not conf["badges"] and not conf["challenges"]:
            return 0
        group = self.config.guild(guild).milestones
        async with group.get_lock():
            record = await group.get_raw(str(member.id), default=member_progress())
            advance_challenges(record, conf, source, amount, day)
            bonus = min(record["pending"], budget) if budget is not None else record["pending"]
            record["pending"] -= bonus
            record["earned_xp"] += amount + bonus
            if conf["badges"] and amount + bonus > 0:
                level = self._level(old_xp + amount + bonus, settings)
                for key, (_, metric, threshold) in BADGES.items():
                    value = level if metric == "level" else record["earned_xp"]
                    if value >= threshold:
                        record["badges"].setdefault(key, now)
            if amount or bonus:
                await group.set_raw(str(member.id), value=record)
            return bonus

    @commands.hybrid_command(name="achievements")
    @commands.guild_only()
    async def achievements(self, ctx, member: Optional[discord.Member] = None):
        """Show earned achievement badges."""
        member = member or ctx.author
        record = await self.config.guild(ctx.guild).milestones.get_raw(
            str(member.id), default=member_progress()
        )
        rows = [
            f"**{name}** · {'Earned <t:' + str(int(record['badges'][key])) + ':d>' if key in record['badges'] else 'Reach ' + str(threshold) + (' earned XP' if metric == 'xp' else ' levels')}"
            for key, (name, metric, threshold) in BADGES.items()
        ]
        await self._reply(ctx, member.mention + "\n\n" + "\n".join(rows), title="Achievements")

    @commands.hybrid_command(name="challenges")
    @commands.guild_only()
    async def challenges(self, ctx, member: Optional[discord.Member] = None):
        """Show this week's goals and earned rewards."""
        member = member or ctx.author
        conf = await self.config.guild(ctx.guild).milestone_settings()
        record = await self.config.guild(ctx.guild).milestones.get_raw(
            str(member.id), default=member_progress()
        )

        day = day_at(time.time(), (await self._settings(ctx.guild))["xp_features"]["timezone"])
        week = (day - timedelta(days=day.weekday())).isoformat()
        current = record["week"] == week
        rows = [
            f"**{GOAL_LABELS[key]}** · {record['counts'].get(key, 0) if current else 0}/{goal['target']} · {goal['reward']} XP · {'Complete' if current and key in record['completed'] else 'In progress'}"
            for key, goal in conf["goals"].items()
        ]
        await self._reply(
            ctx,
            member.mention
            + f"\nWeek of {week} · {'Enabled' if conf['challenges'] else 'Disabled'}\n\n"
            + "\n".join(rows)
            + f"\n\nPending rewards · {record['pending']} XP",
            title="Weekly challenges",
        )

    @commands.hybrid_command(name="rankcard")
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.member)
    async def rankcard(self, ctx, member: Optional[discord.Member] = None):
        """Show a themed rank image with progress and achievements."""
        await check_command(ctx, self.show)
        if self._closing:
            raise commands.CommandError("Level is unloading. Try again after reload.")
        member = member or ctx.author
        if not ctx.channel.permissions_for(ctx.guild.me).attach_files:
            raise commands.BotMissingPermissions(discord.Permissions(attach_files=True))
        conf = await self._settings(ctx.guild)
        xp = await self._get_xp(ctx.guild, member.id)
        level = self._level(xp, conf)
        args = (
            conf["curve"],
            float(conf["multiplier"]),
            float(conf["linear"]["base"]),
            float(conf["linear"]["inc"]),
        )
        lower = cumulative_xp(level, *args)
        upper = (
            None
            if conf["max_level"] and level >= conf["max_level"]
            else cumulative_xp(level + 1, *args)
        )
        position = 1 + sum(
            int(value) > xp for value in (await self.config.guild(ctx.guild).xp()).values()
        )
        record = await self.config.guild(ctx.guild).milestones.get_raw(
            str(member.id), default=member_progress()
        )

        async with self._card_slots:
            if self._closing:
                raise commands.CommandError("Level is unloading. Try again after reload.")
            task = asyncio.create_task(
                asyncio.to_thread(
                    render_card,
                    member.display_name,
                    level,
                    xp,
                    lower,
                    upper,
                    position,
                    len(record["badges"]),
                )
            )
            self._card_tasks.add(task)
            try:
                image = await task
            except ImportError as error:
                raise commands.CommandError(
                    "Install LevelPlus's Pillow dependency, then reload levelplus. The text rank command remains available."
                ) from error
            finally:
                self._card_tasks.discard(task)
        if self._closing:
            raise commands.CommandError("Level is unloading. Try again after reload.")
        embed = self._presentation.embed(
            "Rank card",
            f"{member.mention} · Level {level} · Rank #{position}\n{len(record['badges'])} achievements earned",
        )
        embed.set_image(url="attachment://rank.png")
        await self._reply(
            ctx, embed=embed, file=discord.File(io.BytesIO(image), filename="rank.png")
        )

    async def _milestone_setting(self, guild, key, value):
        section = self.config.guild(guild).milestone_settings
        async with section.get_lock():
            await section.get_attr(key).set(value)
        self._settings_cache.pop(guild.id, None)

    async def _challenge_settings(self, ctx):
        conf = await self.config.guild(ctx.guild).milestone_settings()
        lines = [
            f"{GOAL_LABELS[key]} · {goal['target']} · {goal['reward']} reward XP"
            for key, goal in conf["goals"].items()
        ]
        await self._reply(
            ctx,
            f"Challenges · {'Enabled' if conf['challenges'] else 'Disabled'}\nBadges · {'Enabled' if conf['badges'] else 'Disabled'}\n"
            + "\n".join(lines),
            title="Challenge settings",
        )
