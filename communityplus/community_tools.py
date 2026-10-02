"""Temporary voice ownership, explicit onboarding and opt-in birthday notices."""

import asyncio
import hashlib
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from redbot.core import commands

from .features import safe_role, validate_zone
from .native_events import NATIVE_DEFAULTS

log = logging.getLogger(__name__)
TOOLS_DEFAULTS = {
    "native_events": NATIVE_DEFAULTS,
    "voice_hub": None,
    "voice_category": None,
    "onboarding": {"enabled": False, "role": None, "rules": ""},
    "birthdays": {
        "enabled": False,
        "channel": None,
        "role": None,
        "timezone": "America/Chicago",
        "hour": 9,
    },
}
CELEBRATION_DEFAULTS = {
    "birthday": None,
    "last_year": 0,
    "role": None,
    "role_until": 0,
    "accepted": {},
}


class CommunityTools:
    async def _tools_settings(self, guild, key, values):
        section = self.config.guild(guild).community_tools
        async with section.get_lock():
            data = await section()
            if key:
                data[key].update(values)
            else:
                data.update(values)
            await section.set(data)

    async def _owned_room(self, ctx):
        channel = getattr(getattr(ctx.author, "voice", None), "channel", None)
        rooms = await self.config.guild(ctx.guild).voice_rooms()
        record = rooms.get(str(getattr(channel, "id", 0)))
        if not record or (
            record["owner"] != ctx.author.id and not ctx.author.guild_permissions.manage_guild
        ):
            raise commands.CheckFailure(
                "Join your temporary room to manage it. Server managers can manage any temporary room."
            )
        return channel

    @commands.hybrid_group(name="voiceroom", invoke_without_command=True, fallback="help")
    @commands.guild_only()
    async def voice_room(self, ctx):
        """Create temporary rooms by joining the configured voice hub."""
        settings = await self.config.guild(ctx.guild).community_tools()
        await self._reply(
            ctx,
            (
                f"Join <#{settings['voice_hub']}> to create a room. "
                if settings["voice_hub"]
                else "No room hub configured. "
            )
            + "Owners can use voiceroom name, limit and private. Empty rooms are removed.",
            title="Temporary voice rooms",
        )

    @voice_room.command(name="configure")
    @commands.admin_or_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_channels=True, move_members=True)
    async def voice_room_configure(
        self,
        ctx,
        hub: Optional[discord.VoiceChannel] = None,
        category: Optional[discord.CategoryChannel] = None,
    ):
        """Set the join-to-create hub/category, or omit the hub to disable."""
        await self._tools_settings(
            ctx.guild,
            None,
            {
                "voice_hub": hub.id if hub else None,
                "voice_category": category.id if category else None,
            },
        )
        await self._reply(
            ctx, "Temporary room hub saved. Existing rooms still clean up when empty."
        )

    @voice_room.command(name="name")
    async def voice_room_name(self, ctx, *, name: str):
        """Rename your temporary voice room."""
        if not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
            raise commands.BadArgument("Use a room name of 1 to 80 characters on one line.")
        channel = await self._owned_room(ctx)
        await asyncio.wait_for(
            channel.edit(name=name.strip(), reason="Temporary room owner rename"), 10
        )
        await self._reply(ctx, "Room name saved.")

    @voice_room.command(name="limit")
    async def voice_room_limit(self, ctx, capacity: int):
        """Set room capacity from 0 (unlimited) through 99."""
        if not 0 <= capacity <= 99:
            raise commands.BadArgument("Choose a capacity from 0 to 99.")
        channel = await self._owned_room(ctx)
        await asyncio.wait_for(
            channel.edit(user_limit=capacity, reason="Temporary room owner capacity"), 10
        )
        await self._reply(ctx, "Room capacity saved.")

    @voice_room.command(name="private")
    async def voice_room_private(self, ctx, enabled: bool):
        """Lock or unlock admission to your temporary room."""
        channel = await self._owned_room(ctx)
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.connect = False if enabled else None
        await asyncio.wait_for(
            channel.set_permissions(
                ctx.guild.default_role, overwrite=overwrite, reason="Temporary room owner privacy"
            ),
            10,
        )
        await self._reply(
            ctx,
            "Room locked. The owner can still enter."
            if enabled
            else "Room admission returned to its inherited policy.",
        )

    @voice_room.command(name="invite")
    async def voice_room_invite(self, ctx, member: discord.Member):
        """Allow a server member to enter your locked temporary room."""
        channel = await self._owned_room(ctx)
        overwrite = channel.overwrites_for(member)
        overwrite.connect = True
        overwrite.view_channel = True
        await asyncio.wait_for(
            channel.set_permissions(
                member, overwrite=overwrite, reason="Temporary room owner invitation"
            ),
            10,
        )
        await self._reply(ctx, "Member can enter this room.")

    async def _room_voice(self, member, before, after):
        if member.bot or before.channel == after.channel or self._closing:
            return
        guild = member.guild
        settings = await self.config.guild(guild).community_tools()
        async with self._room_locks[guild.id]:
            rooms = self.config.guild(guild).voice_rooms
            data = await rooms()
            if after.channel and after.channel.id == settings["voice_hub"]:
                if len(data) >= 50:
                    return
                existing = next(
                    (
                        guild.get_channel(int(cid))
                        for cid, row in data.items()
                        if row["owner"] == member.id and guild.get_channel(int(cid))
                    ),
                    None,
                )
                if existing:
                    await asyncio.wait_for(
                        member.move_to(existing, reason="Return to owned room"), 10
                    )
                    return
                category = (
                    guild.get_channel(settings["voice_category"])
                    if settings["voice_category"]
                    else after.channel.category
                )
                overwrites = deepcopy(category.overwrites if category else after.channel.overwrites)
                overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True)
                created = None
                try:
                    created = await asyncio.wait_for(
                        guild.create_voice_channel(
                            name=f"{member.display_name[:55]}'s room",
                            category=category,
                            overwrites=overwrites,
                            reason="Temporary community room",
                        ),
                        10,
                    )
                    if (
                        self._closing
                        or (await self.config.guild(guild).community_tools())["voice_hub"]
                        != settings["voice_hub"]
                    ):
                        raise commands.CommandError("Room creation disabled during request.")
                    async with rooms() as current:
                        current[str(created.id)] = {"owner": member.id, "created": self._now_ts()}
                    await asyncio.wait_for(
                        member.move_to(created, reason="Temporary room owner"), 10
                    )
                except BaseException:
                    if created:
                        try:
                            await asyncio.wait_for(
                                created.delete(reason="Incomplete temporary room creation"), 10
                            )
                            async with rooms() as current:
                                current.pop(str(created.id), None)
                        except Exception:
                            log.exception("Could not clean up an incomplete room")
                    raise
            await self._clean_rooms(guild)

    async def _clean_rooms(self, guild):
        section = self.config.guild(guild).voice_rooms
        for cid, record in (await section()).items():
            channel = guild.get_channel(int(cid))
            if channel and (channel.members or self._now_ts() - record["created"] < 30):
                continue
            if channel:
                try:
                    await asyncio.wait_for(
                        channel.delete(reason="Empty temporary community room"), 10
                    )
                except (discord.HTTPException, asyncio.TimeoutError):
                    continue
            async with section() as data:
                data.pop(cid, None)

    @commands.hybrid_group(name="onboard", invoke_without_command=True, fallback="rules")
    @commands.guild_only()
    async def onboarding(self, ctx):
        """Read server rules and accept them to receive the member role."""
        settings = (await self.config.guild(ctx.guild).community_tools())["onboarding"]
        if not settings["enabled"]:
            return await self._reply(ctx, "Onboarding is disabled.")
        await self._reply(
            ctx,
            settings["rules"]
            + f"\n\nAccept these rules with `{ctx.clean_prefix}onboard accept` or `/onboard accept`.",
            title="Server rules",
        )

    @onboarding.command(name="configure")
    @commands.admin_or_permissions(manage_guild=True)
    async def onboarding_configure(self, ctx, role: discord.Role, *, rules: str):
        """Enable rules acceptance and a safe member-role reward."""
        safe_role(ctx.guild, role)
        if not rules.strip() or len(rules) > 2000:
            raise commands.BadArgument("Provide server rules of 1 to 2000 characters.")
        await self._tools_settings(
            ctx.guild, "onboarding", {"enabled": True, "role": role.id, "rules": rules.strip()}
        )
        await self._reply(ctx, "Onboarding rules and member role saved.")

    @onboarding.command(name="disable")
    @commands.admin_or_permissions(manage_guild=True)
    async def onboarding_disable(self, ctx):
        """Disable rules acceptance without removing existing member roles."""
        await self._tools_settings(ctx.guild, "onboarding", {"enabled": False})
        await self._reply(ctx, "Onboarding disabled.")

    @onboarding.command(name="accept")
    @commands.bot_has_permissions(manage_roles=True)
    async def onboarding_accept(self, ctx):
        """Accept the current rules and receive the configured safe member role."""
        async with self._role_locks[(ctx.guild.id, ctx.author.id)]:
            state = (await self.config.guild(ctx.guild).community_tools())["onboarding"]
            if not state["enabled"]:
                raise commands.BadArgument("Onboarding is disabled.")
            role = ctx.guild.get_role(state["role"])
            if role is None:
                raise commands.BadArgument(
                    "Ask an administrator to replace the missing onboarding role."
                )
            safe_role(ctx.guild, role)
            await asyncio.wait_for(
                ctx.author.add_roles(role, reason="Accepted community rules"), 10
            )
            async with self.config.member(ctx.author).celebrations() as record:
                record["accepted"] = {
                    "at": self._now_ts(),
                    "rules_hash": hashlib.sha256(state["rules"].encode()).hexdigest(),
                }
        await self._reply(ctx, "Rules accepted. Your member role is ready.", tone="success")

    @commands.hybrid_group(name="birthday", invoke_without_command=True, fallback="show")
    @commands.guild_only()
    async def birthday(self, ctx):
        """Manage your opt-in birthday, without storing your birth year."""
        state = await self.config.member(ctx.author).celebrations()
        value = state["birthday"]
        await self._reply(
            ctx,
            f"Your birthday: {value[0]:02}-{value[1]:02}. Use birthday remove to opt out."
            if value
            else "No birthday saved. Use birthday set <month> <day> to opt in to server celebrations.",
        )

    @birthday.command(name="set")
    async def birthday_set(self, ctx, month: int, day: int):
        """Opt in with a valid month and day; birth year is never stored."""
        try:
            datetime(2000, month, day)
        except ValueError as error:
            raise commands.BadArgument("Use a valid month and day.") from error
        async with self.config.member(ctx.author).celebrations() as record:
            record["birthday"] = [month, day]
        await self._reply(
            ctx,
            "Birthday saved. Administrators must enable announcements; birthdays are announced publicly in the configured channel.",
        )

    @birthday.command(name="remove")
    async def birthday_remove(self, ctx):
        """Remove your saved birthday and temporary birthday role."""
        section = self.config.member(ctx.author).celebrations
        async with section() as record:
            await self._remove_birthday_role(ctx.author, record)
            record.update(birthday=None, role=None, role_until=0, last_year=0)
        await self._reply(ctx, "Birthday removed.")

    @birthday.command(name="configure")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthday_configure(
        self,
        ctx,
        channel: discord.TextChannel,
        role: Optional[discord.Role] = None,
        timezone_name: str = "America/Chicago",
        hour: int = 9,
    ):
        """Enable birthday notices and an optional 24-hour safe role."""
        validate_zone(timezone_name)
        if not 0 <= hour <= 23:
            raise commands.BadArgument("Choose an announcement hour from 0 to 23.")
        if role:
            safe_role(ctx.guild, role)
        await self._tools_settings(
            ctx.guild,
            "birthdays",
            {
                "enabled": True,
                "channel": channel.id,
                "role": role.id if role else None,
                "timezone": timezone_name,
                "hour": hour,
            },
        )
        await self._reply(ctx, "Birthday celebrations enabled for members who opt in.")

    @birthday.command(name="disable")
    @commands.admin_or_permissions(manage_guild=True)
    async def birthday_disable(self, ctx):
        """Stop birthday notices; outstanding temporary roles still expire."""
        await self._tools_settings(ctx.guild, "birthdays", {"enabled": False})
        await self._reply(ctx, "Birthday announcements disabled.")

    async def _remove_birthday_role(self, member, record):
        role = member.guild.get_role(record["role"]) if record["role"] else None
        if role and role in member.roles:
            await asyncio.wait_for(
                member.remove_roles(role, reason="Birthday role expired or opted out"), 10
            )

    async def _birthday_tick(self, guild, *, now=None):
        now = datetime.now(timezone.utc) if now is None else now
        slot = int(now.timestamp()) // 3600
        if self._birthday_slots.get(guild.id) == slot:
            return
        self._birthday_slots[guild.id] = slot
        conf = (await self.config.guild(guild).community_tools())["birthdays"]
        local = now.astimezone(ZoneInfo(conf["timezone"]))
        channel = guild.get_channel(conf["channel"]) if conf["channel"] else None
        for uid, values in (await self.config.all_members(guild)).items():
            if self._closing:
                return
            record = values.get("celebrations")
            member = guild.get_member(uid)
            if not record or member is None:
                continue
            section = self.config.member(member).celebrations
            async with section() as current:
                if current["role"] and current["role_until"] <= now.timestamp():
                    try:
                        await self._remove_birthday_role(member, current)
                        current.update(role=None, role_until=0)
                    except (discord.HTTPException, asyncio.TimeoutError):
                        continue
                birthday = current["birthday"]
                if (
                    not birthday
                    or not conf["enabled"]
                    or channel is None
                    or local.hour < conf["hour"]
                    or current["last_year"] == local.year
                ):
                    continue
                month, day = birthday
                if (
                    month == 2
                    and day == 29
                    and (local.year % 4 or (local.year % 100 == 0 and local.year % 400))
                ):
                    day = 28
                if (local.month, local.day) != (month, day):
                    continue
                try:
                    await asyncio.wait_for(
                        self._presentation.send(
                            channel,
                            f"Happy birthday, {member.mention}!",
                            title="Community birthday",
                            tone="success",
                        ),
                        10,
                    )
                    current["last_year"] = local.year
                    role = guild.get_role(conf["role"]) if conf["role"] else None
                    if role and role not in member.roles:
                        safe_role(guild, role)
                        await asyncio.wait_for(
                            member.add_roles(role, reason="Opt-in birthday celebration"), 10
                        )
                        current.update(
                            role=role.id, role_until=int((now + timedelta(days=1)).timestamp())
                        )
                except (discord.HTTPException, asyncio.TimeoutError, commands.BadArgument):
                    log.warning("Birthday delivery/role unavailable in guild %s", guild.id)

    async def _tools_user_data(self, user_id, *, delete=False):
        result = {}
        for gid in await self.config.all_guilds():
            section = self.config.guild_from_id(gid).voice_rooms
            async with section() as rooms:
                owned = {
                    cid: deepcopy(record)
                    for cid, record in rooms.items()
                    if record["owner"] == user_id
                }
                if owned:
                    result[str(gid)] = owned
                if delete:
                    for record in rooms.values():
                        if record["owner"] == user_id:
                            record["owner"] = 0
        return result
