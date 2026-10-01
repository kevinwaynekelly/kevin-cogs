"""Self-service roles and bounded participation summaries."""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from datetime import time as daytime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from redbot.core import commands

from .interactive import component_context, component_error

log = logging.getLogger(__name__)

FEATURE_DEFAULTS = {
    "solo_channels": [],
    "solo_roles": [],
    "warning_seconds": 0,
    "self_roles": [],
    "role_menus": [],
    "summary": {
        "enabled": False,
        "channel": None,
        "weekday": 0,
        "hour": 9,
        "timezone": "America/Chicago",
        "last_week": "",
    },
}
PARTICIPATION_DEFAULTS = {"voice_seconds": 0.0, "days": {}}


def local_zone(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def validate_zone(name):
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise commands.BadArgument(
            "Use an installed IANA timezone, such as America/Chicago."
        ) from error
    return name


def safe_role(guild, role):
    dangerous = (
        "administrator",
        "manage_roles",
        "manage_guild",
        "manage_channels",
        "ban_members",
        "kick_members",
        "moderate_members",
        "manage_webhooks",
    )
    if (
        guild.me is None
        or role.is_default()
        or role.managed
        or role >= guild.me.top_role
        or any(getattr(role.permissions, key) for key in dangerous)
    ):
        raise commands.BadArgument(
            "Choose an unmanaged role below the bot without management or moderation permissions."
        )


def add_day(data, day, *, messages=0, voice=0):
    days = data.setdefault("days", {})
    bucket = days.setdefault(day, {"messages": 0, "voice_seconds": 0.0})
    bucket["messages"] += messages
    bucket["voice_seconds"] += voice
    for stale in sorted(days)[:-35]:
        days.pop(stale)


def voice_days(start, end, zone):
    """Split an observed interval at real local midnights, including DST."""
    tz = local_zone(zone)
    while start < end:
        date = datetime.fromtimestamp(start, tz).date()
        midnight = datetime.combine(date + timedelta(days=1), daytime(), tz).timestamp()
        stop = min(end, midnight)
        yield date.isoformat(), stop - start
        start = stop


def week_key(now, settings):
    local = datetime.fromtimestamp(now, local_zone(settings["timezone"]))
    monday = local.date() - timedelta(days=local.weekday())
    due = datetime.combine(
        monday + timedelta(days=settings["weekday"]), daytime(settings["hour"]), local.tzinfo
    )
    return monday.isoformat(), local >= due


class RoleMenuView(discord.ui.View):
    def __init__(self, cog, guild, roles, *, owner_id=None, message_id=None):
        super().__init__(timeout=None if message_id is not None else 180)
        self.cog, self.guild_id, self.owner_id = cog, guild.id, owner_id
        self.message = None
        selector = discord.ui.Select(
            custom_id=f"community:roles:{guild.id}",
            placeholder="Choose your self-service roles",
            min_values=0,
            max_values=len(roles),
            options=[
                discord.SelectOption(label=role.name[:100], value=str(role.id)) for role in roles
            ],
        )

        async def callback(interaction):
            try:
                if interaction.guild_id != self.guild_id:
                    raise commands.CheckFailure("This menu belongs to another server.")
                ctx = await component_context(cog, interaction, "roles", owner_id=self.owner_id)
                await cog._apply_self_roles(ctx.author, {int(value) for value in selector.values})
                await interaction.followup.send("Your roles were updated.", ephemeral=True)
            except (commands.CommandError, discord.HTTPException) as error:
                await component_error(interaction, error)

        selector.callback = callback
        self.add_item(selector)
        if message_id is None:
            cog._views.add(self)

    async def on_timeout(self):
        self.cog._views.discard(self)
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        await component_error(interaction, error)


class CommunityFeatures:
    def _solo_exempt(self, member, features):
        channel = member.voice.channel if member.voice else None
        return (
            member.bot
            or (channel is not None and channel.id in features["solo_channels"])
            or any(role.id in features["solo_roles"] for role in member.roles)
        )

    async def _self_roles(self, guild):
        result = []
        for rid in await self.config.guild(guild).features.self_roles():
            role = guild.get_role(rid)
            if role:
                try:
                    safe_role(guild, role)
                except commands.BadArgument:
                    continue
                result.append(role)
        return result

    async def _apply_self_roles(self, member, selected):
        async with self._role_locks[(member.guild.id, member.id)]:
            roles = await self._self_roles(member.guild)
            available = {role.id for role in roles}
            if not selected <= available:
                raise commands.BadArgument(
                    "This menu changed. Run roles again to see available roles."
                )
            if not member.guild.me.guild_permissions.manage_roles:
                raise commands.CheckFailure("I need Manage Roles to update self-service roles.")
            current = {role.id for role in member.roles}
            remove = [role for role in roles if role.id in current - selected]
            add = [role for role in roles if role.id in selected - current]
            if remove:
                await member.remove_roles(*remove, reason="Community self-service roles")
            if add:
                await member.add_roles(*add, reason="Community self-service roles")

    async def _restore_role_menus(self, guild):
        roles = await self._self_roles(guild)
        for entry in await self.config.guild(guild).features.role_menus():
            if roles:
                view = RoleMenuView(self, guild, roles, message_id=entry["message"])
                self.bot.add_view(view, message_id=entry["message"])
                self._role_views[entry["message"]] = view

    async def _refresh_role_menus(self, guild):
        roles = await self._self_roles(guild)
        for entry in await self.config.guild(guild).features.role_menus():
            channel = guild.get_channel(entry["channel"])
            if not isinstance(channel, discord.TextChannel):
                continue
            old = self._role_views.pop(entry["message"], None)
            if old:
                old.stop()
            try:
                message = await channel.fetch_message(entry["message"])
                view = (
                    RoleMenuView(self, guild, roles, message_id=entry["message"]) if roles else None
                )
                await message.edit(view=view)
                if view:
                    self._role_views[entry["message"]] = view
            except discord.HTTPException:
                log.debug("Self-role menu could not be refreshed", exc_info=True)

    async def _track_voice(self, member, channel):
        key = (member.guild.id, member.id)
        async with self._voice_locks[key]:
            now, mono = time.time(), time.monotonic()
            previous = self._voice_sessions.pop(key, None)
            enabled = (
                not member.bot
                and not await self.bot.cog_disabled_in_guild(self, member.guild)
                and (await self._settings(member.guild))["seen"]["enabled"]
            )
            if not enabled:
                return
            if previous:
                seconds = max(0.0, mono - previous[1])
                zone = (await self._settings(member.guild))["features"]["summary"]["timezone"]
                async with self.config.member(member).all() as data:
                    totals = data["participation"]
                    totals["voice_seconds"] += seconds
                    for day, duration in voice_days(now - seconds, now, zone):
                        add_day(totals, day, voice=duration)
            if channel:
                self._voice_sessions[key] = (now, mono, channel.id)

    async def _summary_embed(self, guild, *, completed_days=False):
        now = datetime.fromtimestamp(
            time.time(),
            local_zone((await self._settings(guild))["features"]["summary"]["timezone"]),
        ).date()
        end = now - timedelta(days=int(completed_days))
        start = end - timedelta(days=6)
        rows = []
        for uid, data in (await self.config.all_members(guild)).items():
            if guild.get_member(uid) is None:
                continue
            buckets = [
                bucket
                for day, bucket in data.get("participation", {}).get("days", {}).items()
                if start.isoformat() <= day <= end.isoformat()
            ]
            messages = sum(bucket["messages"] for bucket in buckets)
            voice = sum(bucket["voice_seconds"] for bucket in buckets)
            if messages or voice:
                rows.append((uid, messages, voice))
        embed = self._presentation.embed(
            "Weekly participation", f"{start.isoformat()} to {end.isoformat()}"
        )
        embed.add_field(name="Messages", value=f"{sum(row[1] for row in rows):,}")
        embed.add_field(name="Voice hours", value=f"{sum(row[2] for row in rows) / 3600:.1f}")
        embed.add_field(name="Active members", value=str(len(rows)))
        for label, index, fmt in (
            ("Messages", 1, lambda n: f"{n:,}"),
            ("Voice hours", 2, lambda n: f"{n / 3600:.1f}"),
        ):
            leaders = sorted(rows, key=lambda row: row[index], reverse=True)[:5]
            embed.add_field(
                name=f"Top {label.lower()}",
                value="\n".join(f"<@{row[0]}> · {fmt(row[index])}" for row in leaders if row[index])
                or "No activity recorded.",
                inline=False,
            )
        return embed

    async def _digest_tick(self, guild):
        async with self._digest_locks[guild.id]:
            await self._deliver_digest(guild)

    async def _deliver_digest(self, guild):
        conf = await self.config.guild(guild).features.summary()
        week, due = week_key(time.time(), conf)
        if not conf["enabled"] or not due or conf["last_week"] == week:
            return
        channel = guild.get_channel(conf["channel"])
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await self._presentation.send(
                channel, embed=await self._summary_embed(guild, completed_days=True)
            )
        except discord.HTTPException:
            log.debug("Weekly summary delivery failed", exc_info=True)
            return
        async with self.config.guild(guild).features() as features:
            if features["summary"] == conf:
                features["summary"]["last_week"] = week
        self._settings_cache.pop(guild.id, None)

    async def _maintenance_tick(self):
        observed = set()
        for guild in self.bot.guilds:
            if await self.bot.cog_disabled_in_guild(self, guild):
                for key in list(self._voice_sessions):
                    if key[0] == guild.id:
                        self._voice_sessions.pop(key)
                continue
            for channel in (*guild.voice_channels, *guild.stage_channels):
                for member in channel.members:
                    if not member.bot:
                        observed.add((guild.id, member.id))
                        await self._track_voice(member, channel)
            await self._digest_tick(guild)
        for key in set(self._voice_sessions) - observed:
            guild = self.bot.get_guild(key[0])
            member = guild.get_member(key[1]) if guild else None
            if member:
                await self._track_voice(member, None)
            else:
                self._voice_sessions.pop(key, None)

    async def _maintenance(self):
        await self.bot.wait_until_red_ready()
        while True:
            try:
                await self._maintenance_tick()
            except Exception:
                log.exception("Community maintenance failed")
            await asyncio.sleep(60)
