from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import time
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from heapq import nlargest
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config
from redbot.core.utils.chat_formatting import humanize_number

from .constants import DEFAULTS_GUILD, DEFAULTS_MEMBER, EVENT_COLOR
from .events import guild_enabled
from .presentation import Presentation, settings

log = logging.getLogger(__name__)


# ------------------------ defaults ------------------------


class CommunityPlus(redcommands.Cog):
    """Autorole (first-time), Sticky roles, Welcome/Cya, Solo-VC kick (DM), Deep Seen/Presence, Counters."""

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self._presentation = Presentation("CommunityPlus", "com")
        self.config: Config = Config.get_conf(self, identifier=0xC0DE505, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self.config.register_member(**DEFAULTS_MEMBER)
        self._solo_tasks = {}
        self._startup_task = None
        self._settings_cache = {}
        self._settings_locks = defaultdict(asyncio.Lock)

    async def _restore_solo_timers(self):
        await self.bot.wait_until_red_ready()
        for guild in self.bot.guilds:
            if await self.bot.cog_disabled_in_guild(self, guild):
                continue
            for channel in (*guild.voice_channels, *guild.stage_channels):
                await self._refresh_solo_for_channel(channel)

    # ------------------------ time/format ------------------------
    @staticmethod
    def _now_ts() -> int:
        return int(time.time())

    @staticmethod
    def _utcnow() -> datetime:
        return discord.utils.utcnow()

    @staticmethod
    def _dt_from_ts(ts: int) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    @staticmethod
    def _fmt_rel(ts: int) -> str:
        return (
            "never" if not ts else discord.utils.format_dt(CommunityPlus._dt_from_ts(ts), style="R")
        )

    @staticmethod
    def _humanize_duration(seconds: int) -> str:
        s = max(0, int(seconds))
        days, rem = divmod(s, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        parts: List[str] = []
        if days:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes or not parts:
            parts.append(
                f"{minutes} minute{'s' if minutes != 1 else ''}"
                if minutes
                else "less than a minute"
            )
        return " ".join(parts[:2])

    # ------------------------ embeds ------------------------
    async def _embed_compact(self, guild: discord.Guild) -> bool:
        try:
            return bool((await self._settings(guild))["embeds"]["compact"])
        except Exception:
            return True

    async def _mk_embed(
        self,
        guild: discord.Guild,
        title: str,
        *,
        desc: Optional[str] = None,
        kind: str = "info",
        footer: Optional[str] = None,
    ) -> discord.Embed:
        color = EVENT_COLOR.get(kind, discord.Color.blurple())
        title = title.removeprefix("CommunityPlus - ")
        title = f"• {title}" if await self._embed_compact(guild) else title
        e = discord.Embed(
            title=title[:256],
            description=desc,
            color=color,
            timestamp=self._utcnow(),
        )
        if footer:
            e.set_footer(text=footer)
        return self._presentation.style(e)

    # ---------- status ----------
    async def _status_embed(self, guild: discord.Guild) -> discord.Embed:
        g = await self.config.guild(guild).all()
        ar_role = guild.get_role(g["autorole"]["role_id"])
        ar = ar_role.mention if g["autorole"]["role_id"] and ar_role else "not set"

        sticky_ign = [
            guild.get_role(r).mention for r in g["sticky"]["ignore"] if guild.get_role(r)
        ] or ["none"]

        wc_id = g["welcome"]["channel_id"]
        welcome_ch = (
            guild.get_channel(wc_id).mention if wc_id and guild.get_channel(wc_id) else "not set"
        )

        cc_id = g["cya"]["channel_id"]
        cya_ch = (
            guild.get_channel(cc_id).mention if cc_id and guild.get_channel(cc_id) else "not set"
        )

        e = discord.Embed(
            title="CommunityPlus - Status",
            description="Roles, member activity, and community notices.",
            color=discord.Color.blurple(),
        )
        e.add_field(
            name="Core",
            value=settings(
                f"Compact event headers = {g['embeds']['compact']}\n"
                f"Activity tracking = {g['seen']['enabled']}",
                lang="ini",
            ),
            inline=False,
        )
        e.add_field(
            name="Autorole",
            value=settings(f"Status = {g['autorole']['enabled']}\nRole = {ar}", lang="ini"),
            inline=True,
        )
        e.add_field(
            name="Sticky Roles",
            value=settings(
                f"Status = {g['sticky']['enabled']}\nIgnored roles = {', '.join(sticky_ign)}",
                lang="ini",
            ),
            inline=True,
        )
        e.add_field(
            name="Welcome",
            value=settings(
                f"Status = {g['welcome']['enabled']}\nChannel = {welcome_ch}",
                lang="ini",
            ),
            inline=True,
        )
        e.add_field(
            name="Goodbye",
            value=settings(f"Status = {g['cya']['enabled']}\nChannel = {cya_ch}", lang="ini"),
            inline=True,
        )
        e.add_field(
            name="Solo voice cleanup",
            value=settings(
                f"Status = {g['vcsolo']['enabled']}\n"
                f"Idle timeout = {g['vcsolo']['idle_seconds']}s\n"
                f"DM notifications = {g['vcsolo']['dm_notify']}",
                lang="ini",
            ),
            inline=False,
        )
        e.set_footer(text="Use [p]com help for commands.")
        return e

    @staticmethod
    def _format_template(tpl: str, member: discord.Member) -> str:
        g = member.guild
        try:
            return tpl.format(
                user=str(member),
                mention=member.mention,
                server=g.name,
                count=g.member_count,
                created_at=discord.utils.format_dt(member.created_at, style="R"),
                joined_at=discord.utils.format_dt(member.joined_at, style="R")
                if member.joined_at
                else "unknown",
            )
        except Exception:
            return tpl

    @staticmethod
    def _eligible_roles(member: discord.Member, role_ids: List[int]) -> List[discord.Role]:
        roles: List[discord.Role] = []
        me = member.guild.me
        top = me.top_role if me else None
        for rid in role_ids:
            r = member.guild.get_role(int(rid))
            # Critical Check: Do not attempt to assign managed roles (boosters, bots)
            if not r or r.is_default() or r.managed:
                continue
            if top and r >= top:
                continue
            roles.append(r)
        return roles

    async def _send_to_channel_id(
        self, guild: discord.Guild, channel_id: Optional[int], embed: discord.Embed
    ) -> None:
        if not channel_id:
            return
        ch = guild.get_channel(channel_id)
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try:
                await self._presentation.send(ch, embed=embed)
            except discord.HTTPException:
                pass

    # ------------------------ stats helpers (OPTIMIZED) ------------------------
    async def _bump_stat(self, member: discord.Member, key: str, delta: int = 1) -> None:
        async with self.config.member(member).all() as data:
            stats = data["stats"]
            stats[key] = int(stats.get(key, 0)) + delta

    async def _seen_mark(self, member, *, kind, where=0):
        await self._record_activity(member, kind, where)

    # ------------------------ presence logic (OPTIMIZED) ------------------------
    async def _handle_presence_update_logic(self, before, after):
        now = self._now_ts()
        status = str(after.status)
        before_activities = {(a.type, getattr(a, "name", None)) for a in before.activities}
        additions = {
            (a.type, getattr(a, "name", None)) for a in after.activities
        } - before_activities
        async with self.config.member(after).all() as data:
            seen = data.setdefault("seen", deepcopy(DEFAULTS_MEMBER["seen"]))
            presence = seen.setdefault("presence", deepcopy(DEFAULTS_MEMBER["seen"]["presence"]))
            stats = data.setdefault("stats", deepcopy(DEFAULTS_MEMBER["stats"]))
            changed = status != presence.get("status")
            if changed:
                counts = stats.setdefault("status_changes", {})
                counts[status] = counts.get(status, 0) + 1
                presence["status"] = status
                presence["since"] = now
                presence["last_offline" if status == "offline" else "last_online"] = now
            for platform in ("desktop", "mobile", "web"):
                presence[platform] = str(getattr(after, f"{platform}_status", "unknown"))
            for kind, name in additions:
                label = kind.name
                counts = stats.setdefault("activity_starts", {})
                if label in DEFAULTS_MEMBER["stats"]["activity_starts"]:
                    counts[label] = counts.get(label, 0) + 1
                if kind is discord.ActivityType.playing:
                    stats["game_launches"] = stats.get("game_launches", 0) + 1
                    if name:
                        names = data.setdefault("activity_names", {})
                        names[str(name)] = names.get(str(name), 0) + 1
            if changed or additions:
                seen["any"] = now
                seen["kind"] = "presence"
                seen["where"] = 0

    # ------------------------ commands root ------------------------
    @redcommands.group(name="com", invoke_without_command=True)
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def com(self, ctx: redcommands.Context) -> None:
        await self._reply(ctx, embed=await self._status_embed(ctx.guild))

    @com.command(name="help", aliases=["commands", "?"])
    async def com_help(self, ctx: redcommands.Context) -> None:
        p = ctx.clean_prefix
        e = discord.Embed(title="CommunityPlus - Commands", color=discord.Color.blurple())
        e.description = f"Commands and examples use `{p}` as prefix."
        e.add_field(
            name="Core",
            value=f"• `{p}com` - status panel\n• `{p}com help` • `{p}com diag`",
            inline=False,
        )
        e.add_field(
            name="Autorole",
            value=f"• `{p}com autorole set @Role` • `clear`\n• `{p}com autorole enable|disable`",
            inline=False,
        )
        e.add_field(
            name="Sticky Roles",
            value=f"• `{p}com sticky enable|disable`\n• `{p}com sticky ignore add|remove @Role`\n• `{p}com sticky purge @User`",
            inline=False,
        )
        e.add_field(
            name="Welcome & goodbye",
            value=f"• `{p}com welcome channel #ch` • `message <txt>`\n• `{p}com cya channel #ch` • `message <txt>`",
            inline=False,
        )
        e.add_field(
            name="Solo Voice",
            value=f"• `{p}com vcsolo enable|disable`\n• `{p}com vcsolo idle <seconds>`",
            inline=False,
        )
        e.add_field(
            name="Seen & Stats",
            value=f"• `{p}com seen [@User]` • `{p}com stats`\n• `{p}com seenlist`",
            inline=False,
        )
        await self._reply(ctx, embed=e)

    # ------------------------ DIAG ------------------------
    @com.command(name="diag")
    async def com_diag(self, ctx: redcommands.Context) -> None:
        g = ctx.guild
        me: discord.Member = g.me  # type: ignore
        intents = self.bot.intents

        def mark(ok: bool) -> str:
            return "✅" if ok else "❌"

        perms = me.guild_permissions if me else discord.Permissions.none()
        conf = await self.config.guild(g).all()

        role_ok = True
        role_id = conf["autorole"]["role_id"]
        if role_id:
            role = g.get_role(role_id)
            if not role or role.is_default() or role.managed:
                role_ok = False
        else:
            if conf["autorole"]["enabled"]:
                role_ok = False

        idle = int(conf["vcsolo"]["idle_seconds"])
        vc_ok = conf["vcsolo"]["enabled"] is False or (perms.move_members and idle >= 60)

        e = await self._mk_embed(g, "CommunityPlus - Diagnostics", kind="info")
        e.add_field(
            name="Intents",
            value=f"{mark(intents.presences)} presences\n{mark(intents.members)} members",
            inline=True,
        )
        e.add_field(
            name="Perms",
            value=f"{mark(perms.manage_roles)} manage_roles\n{mark(perms.move_members)} move_members",
            inline=True,
        )
        e.add_field(
            name="Autorole",
            value=f"{mark(conf['autorole']['enabled'])} enabled\n{mark(role_ok)} setup valid",
            inline=True,
        )
        e.add_field(
            name="Solo VC",
            value=f"{mark(conf['vcsolo']['enabled'])} enabled\n{mark(vc_ok)} valid",
            inline=True,
        )
        await self._reply(ctx, embed=e)

    @com.command(name="restore")
    @redcommands.guild_only()
    @redcommands.is_owner()
    async def com_restore(self, ctx: redcommands.Context) -> None:
        """Restore bot owner administrator access."""

        guild = ctx.guild
        member = ctx.author
        me = guild.me

        if me is None:
            return await self._reply(ctx, "I cannot see my own guild member object.", tone="error")

        if not me.guild_permissions.manage_roles:
            return await self._reply(
                ctx, "I need Manage Roles or Administrator to restore access.", tone="error"
            )

        role_name = "Restored Admin"
        role = discord.utils.get(guild.roles, name=role_name)

        if role is None:
            try:
                role = await guild.create_role(
                    name=role_name,
                    permissions=discord.Permissions(administrator=True),
                    reason=f"Restore command used by {member}",
                )
            except discord.HTTPException:
                return await self._reply(
                    ctx, "I do not have permission to create the restore role.", tone="error"
                )
            except discord.HTTPException:
                return await self._reply(
                    ctx, "Discord rejected the restore role creation request.", tone="error"
                )

        if role.is_default():
            return await self._reply(ctx, "I cannot assign @everyone.", tone="error")

        if role.managed:
            return await self._reply(
                ctx, "I cannot assign a managed/integration role.", tone="error"
            )

        if role >= me.top_role:
            return await self._reply(
                ctx,
                "I cannot assign the restore role because it is equal to or above my highest role. "
                "Move my bot role above it in Server Settings > Roles.",
                tone="error",
            )

        if role in member.roles:
            return await self._reply(
                ctx, "You already have the restore admin role.", tone="warning"
            )

        try:
            await member.add_roles(role, reason="Bot owner restore command")
            await self._reply(
                ctx, f"Restored access: {role.mention} added to {member.mention}.", tone="success"
            )
        except discord.HTTPException:
            await self._reply(ctx, "I do not have permission to assign that role.", tone="error")
        except discord.HTTPException:
            await self._reply(ctx, "Discord rejected the role assignment.", tone="error")

    @com.command(name="invites")
    @redcommands.guild_only()
    @redcommands.is_owner()
    async def com_invites(self, ctx: redcommands.Context) -> None:
        """DM the bot owner one-use invites for every guild the bot can invite from."""

        requester = ctx.author

        try:
            await self._presentation.send(
                requester,
                "Creating server invites. Results will appear below.",
                title="Server invites",
            )
        except discord.HTTPException:
            return await self._reply(
                ctx,
                "I cannot DM you. Enable DMs from this server or message me first, then try again.",
                tone="error",
            )

        made: List[str] = []
        failed: List[str] = []

        for guild in sorted(self.bot.guilds, key=lambda g: g.name.lower()):
            me = guild.me
            if me is None:
                failed.append(f"❌ {guild.name} `{guild.id}`: cannot resolve bot member")
                continue

            invite_channel = None

            # Prefer system channel if usable
            if guild.system_channel:
                perms = guild.system_channel.permissions_for(me)
                if perms.create_instant_invite:
                    invite_channel = guild.system_channel

            # Otherwise use the first text channel where the bot can create invites
            if invite_channel is None:
                for channel in guild.text_channels:
                    perms = channel.permissions_for(me)
                    if perms.create_instant_invite:
                        invite_channel = channel
                        break

            if invite_channel is None:
                failed.append(
                    f"❌ {guild.name} `{guild.id}`: no channel with Create Invite permission"
                )
                continue

            try:
                invite = await invite_channel.create_invite(
                    max_age=86400,
                    max_uses=1,
                    unique=True,
                    reason=f"Invite requested by bot owner {requester}",
                )

                made.append(
                    f"✅ **{guild.name}** `{guild.id}`\n"
                    f"Channel: #{invite_channel.name}\n"
                    f"Invite: {invite.url}"
                )

            except discord.HTTPException:
                failed.append(f"❌ {guild.name} `{guild.id}`: forbidden creating invite")
            except discord.HTTPException as e:
                failed.append(f"❌ {guild.name} `{guild.id}`: Discord error `{e}`")
            except Exception as e:
                failed.append(
                    f"❌ {guild.name} `{guild.id}`: unexpected error `{type(e).__name__}: {e}`"
                )

        sections: List[str] = []

        if made:
            sections.append("## Created Invites\n" + "\n\n".join(made))

        if failed:
            sections.append("## Failed / Skipped\n" + "\n".join(failed))

        if not sections:
            sections.append("No guilds found or no invites could be created.")

        full_text = "\n\n".join(sections)

        # Discord message limit safety
        chunks: List[str] = []
        current = ""

        for line in full_text.splitlines():
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = line
            else:
                current += ("\n" if current else "") + line

        if current:
            chunks.append(current)

        for chunk in chunks:
            await self._presentation.send(requester, chunk, title="Server invites")

        await self._reply(ctx, "Invite report sent to your DMs.", tone="success")

    # ------------------------ subcommands ------------------------
    @com.group(name="autorole", autohelp=False)
    async def com_autorole(self, ctx: redcommands.Context):
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @com_autorole.command(name="set")
    async def car_set(self, ctx: redcommands.Context, role: discord.Role):
        await self.config.guild(ctx.guild).autorole.role_id.set(role.id)
        await self._presentation.confirm(ctx)

    @com_autorole.command(name="clear")
    async def car_clear(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).autorole.role_id.set(None)
        await self._presentation.confirm(ctx)

    @com_autorole.command(name="enable")
    async def car_en(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).autorole.enabled.set(True)
        await self._presentation.confirm(ctx)

    @com_autorole.command(name="disable")
    async def car_dis(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).autorole.enabled.set(False)
        await self._presentation.confirm(ctx)

    @com_autorole.command(name="show")
    async def car_show(self, ctx: redcommands.Context):
        g = await self.config.guild(ctx.guild).autorole()
        role = ctx.guild.get_role(g["role_id"])
        e = await self._mk_embed(
            ctx.guild,
            "Autorole",
            kind="info",
            desc=f"**{'enabled' if g['enabled'] else 'disabled'}**, role: {role.mention if role else 'not set'}",
        )
        await self._reply(ctx, embed=e)

    @com.group(name="sticky", autohelp=False)
    async def com_sticky(self, ctx: redcommands.Context):
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @com_sticky.command(name="enable")
    async def cst_en(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).sticky.enabled.set(True)
        await self._presentation.confirm(ctx)

    @com_sticky.command(name="disable")
    async def cst_dis(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).sticky.enabled.set(False)
        await self._presentation.confirm(ctx)

    @com_sticky.group(name="ignore", autohelp=False)
    async def com_sticky_ignore(self, ctx: redcommands.Context):
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @com_sticky_ignore.command(name="add")
    async def cst_i_add(self, ctx: redcommands.Context, role: discord.Role):
        async with self.config.guild(ctx.guild).sticky.ignore() as data:
            if role.id not in data:
                data.append(role.id)
        await self._presentation.confirm(ctx)

    @com_sticky_ignore.command(name="remove")
    async def cst_i_rem(self, ctx: redcommands.Context, role: discord.Role):
        async with self.config.guild(ctx.guild).sticky.ignore() as data:
            if role.id in data:
                data.remove(role.id)
        await self._presentation.confirm(ctx)

    @com_sticky_ignore.command(name="list")
    async def cst_i_list(self, ctx: redcommands.Context):
        data = await self.config.guild(ctx.guild).sticky.ignore()
        roles = [ctx.guild.get_role(r).mention for r in data if ctx.guild.get_role(r)] or ["none"]
        await self._reply(ctx, "Sticky ignored: " + ", ".join(roles))

    @com_sticky.command(name="purge")
    async def cst_purge(self, ctx: redcommands.Context, member: discord.Member):
        group = self.config.member(member)
        async with group.get_lock():
            await group.sticky_roles.set([])
        await self._presentation.confirm(ctx)

    @com.group(name="welcome", autohelp=False)
    async def com_welcome(self, ctx: redcommands.Context):
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @com_welcome.command(name="enable")
    async def cw_en(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).welcome.enabled.set(True)
        await self._presentation.confirm(ctx)

    @com_welcome.command(name="disable")
    async def cw_dis(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).welcome.enabled.set(False)
        await self._presentation.confirm(ctx)

    @com_welcome.command(name="channel")
    async def cw_ch(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None):
        await self.config.guild(ctx.guild).welcome.channel_id.set(channel.id if channel else None)
        await self._presentation.confirm(ctx)

    @com_welcome.command(name="message")
    async def cw_msg(self, ctx: redcommands.Context, *, text: str):
        await self.config.guild(ctx.guild).welcome.message.set(text)
        await self._presentation.confirm(ctx)

    @com_welcome.command(name="preview")
    async def cw_prev(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        g = await self.config.guild(ctx.guild).welcome()
        text = self._format_template(g["message"], member)
        e = await self._mk_embed(ctx.guild, "Welcome", desc=text, kind="ok")
        await self._reply(ctx, embed=e)

    @com.group(name="cya", autohelp=False)
    async def com_cya(self, ctx: redcommands.Context):
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @com_cya.command(name="enable")
    async def cc_en(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).cya.enabled.set(True)
        await self._presentation.confirm(ctx)

    @com_cya.command(name="disable")
    async def cc_dis(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).cya.enabled.set(False)
        await self._presentation.confirm(ctx)

    @com_cya.command(name="channel")
    async def cc_ch(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None):
        await self.config.guild(ctx.guild).cya.channel_id.set(channel.id if channel else None)
        await self._presentation.confirm(ctx)

    @com_cya.command(name="message")
    async def cc_msg(self, ctx: redcommands.Context, *, text: str):
        await self.config.guild(ctx.guild).cya.message.set(text)
        await self._presentation.confirm(ctx)

    @com_cya.command(name="preview")
    async def cc_prev(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        g = await self.config.guild(ctx.guild).cya()
        text = self._format_template(g["message"], member)
        e = await self._mk_embed(ctx.guild, "Goodbye", desc=text, kind="warn")
        await self._reply(ctx, embed=e)

    @com.group(name="vcsolo", autohelp=False)
    async def com_vc(self, ctx: redcommands.Context):
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @com_vc.command(name="enable")
    async def cvc_en(self, ctx):
        await self.config.guild(ctx.guild).vcsolo.enabled.set(True)
        self._settings_cache.pop(ctx.guild.id, None)
        for channel in (*ctx.guild.voice_channels, *ctx.guild.stage_channels):
            await self._refresh_solo_for_channel(channel)
        await self._presentation.confirm(ctx)

    @com_vc.command(name="disable")
    async def cvc_dis(self, ctx):
        await self.config.guild(ctx.guild).vcsolo.enabled.set(False)
        await self._cancel_guild_timers(ctx.guild.id)
        await self._presentation.confirm(ctx)

    @com_vc.command(name="idle")
    async def cvc_idle(self, ctx: redcommands.Context, seconds: int):
        await self.config.guild(ctx.guild).vcsolo.idle_seconds.set(max(60, int(seconds)))
        await self._cancel_guild_timers(ctx.guild.id)
        self._settings_cache.pop(ctx.guild.id, None)
        for channel in (*ctx.guild.voice_channels, *ctx.guild.stage_channels):
            await self._refresh_solo_for_channel(channel)
        await self._presentation.confirm(ctx)

    # Seen & Stats Commands
    @com.command(name="seen")
    async def com_seen(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        data = await self.config.member(member).seen()
        if not data:
            return await self._reply(ctx, f"I haven’t seen **{member}** yet.", tone="warning")
        pres = data.get("presence", {})
        lines = [
            f"Last seen (any): {self._fmt_rel(data.get('any', 0))}"
            + (f" via **{data.get('kind', '')}**" if data.get("kind") else ""),
            (f"in <#{data.get('where')}>" if data.get("where") else ""),
            f"Presence: **{pres.get('status', 'unknown')}** since {self._fmt_rel(pres.get('since', 0))}",
            f"Last online: {self._fmt_rel(pres.get('last_online', 0))}   |   Last offline: {self._fmt_rel(pres.get('last_offline', 0))}",
        ]
        await self._reply(ctx, "\n".join([x for x in lines if x]))

    @com.command(name="seendetail")
    async def com_seen_detail(
        self, ctx: redcommands.Context, member: Optional[discord.Member] = None
    ) -> None:
        member = member or ctx.author
        data = await self.config.member(member).seen()
        if not data:
            return await self._reply(ctx, f"I haven’t seen **{member}** yet.", tone="warning")
        pres = data.get("presence", {})
        fields = [
            (
                "Any",
                f"{self._fmt_rel(data.get('any', 0))} ({data.get('kind', '') or 'n/a'})",
            ),
            (
                "Message",
                f"{self._fmt_rel(data.get('message', 0))} in <#{data.get('message_ch', 0)}>"
                if data.get("message_ch")
                else self._fmt_rel(data.get("message", 0)),
            ),
            (
                "Voice",
                f"{self._fmt_rel(data.get('voice', 0))} in <#{data.get('voice_ch', 0)}>"
                if data.get("voice_ch")
                else self._fmt_rel(data.get("voice", 0)),
            ),
            ("Join", self._fmt_rel(data.get("join", 0))),
            ("Leave", self._fmt_rel(data.get("leave", 0))),
            (
                "Presence Status",
                f"{pres.get('status', 'unknown')} since {self._fmt_rel(pres.get('since', 0))}",
            ),
            (
                "Presence Online",
                f"last_online: {self._fmt_rel(pres.get('last_online', 0))}",
            ),
            (
                "Presence Offline",
                f"last_offline: {self._fmt_rel(pres.get('last_offline', 0))}",
            ),
            (
                "Platforms",
                f"desktop={pres.get('desktop', '?')} mobile={pres.get('mobile', '?')} web={pres.get('web', '?')}",
            ),
        ]
        e = await self._mk_embed(ctx.guild, f"Seen detail - {member}", kind="info")
        for n, v in fields:
            e.add_field(name=n, value=v, inline=False)
        await self._reply(ctx, embed=e)

    @com.command(name="stats")
    async def com_stats(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        stats = await self.config.member(member).stats()
        games = await self.config.member(member).activity_names()
        top_games = nlargest(5, games.items(), key=lambda x: x[1])
        e = await self._mk_embed(ctx.guild, f"Stats - {member}", kind="info")
        e.add_field(name="Messages", value=humanize_number(stats.get("messages", 0)))
        e.add_field(name="Voice joins", value=humanize_number(stats.get("voice_joins", 0)))
        e.add_field(name="Voice moves", value=humanize_number(stats.get("voice_moves", 0)))
        e.add_field(name="Voice leaves", value=humanize_number(stats.get("voice_leaves", 0)))
        e.add_field(name="Stream starts", value=humanize_number(stats.get("stream_starts", 0)))
        e.add_field(name="Video starts", value=humanize_number(stats.get("video_starts", 0)))
        e.add_field(name="Game launches", value=humanize_number(stats.get("game_launches", 0)))
        acts = stats.get("activity_starts", {})
        e.add_field(
            name="Activities",
            value=", ".join(
                f"{k}:{acts.get(k, 0)}"
                for k in [
                    "playing",
                    "streaming",
                    "listening",
                    "watching",
                    "competing",
                    "custom",
                ]
            ),
            inline=False,
        )
        sc = stats.get("status_changes", {})
        e.add_field(
            name="Status changes",
            value=", ".join(f"{k}:{sc.get(k, 0)}" for k in ["online", "idle", "dnd", "offline"]),
            inline=False,
        )
        if top_games:
            e.add_field(
                name="Top games",
                value="\n".join(f"{n[:50]}: {c}" for n, c in top_games),
                inline=False,
            )
        await self._reply(ctx, embed=e)

    @com.command(name="seenlist")
    async def com_seenlist(self, ctx: redcommands.Context, limit: Optional[int] = 25) -> None:
        rows: List[Tuple[discord.Member, Dict]] = []
        saved = await self.config.all_members(ctx.guild)
        for m in ctx.guild.members:
            rows.append((m, saved.get(m.id, {}).get("seen", {})))
        limit = max(1, min(int(limit or 25), 100))
        lines = [f"**Last seen (top {limit})**"]
        for m, d in nlargest(limit, rows, key=lambda t: t[1].get("any", 0)):
            when = self._fmt_rel(d.get("any", 0))
            kind = d.get("kind", "")
            where = f"<#{d.get('where', 0)}>" if d.get("where") else ""
            lines.append(f"- {m} - {when} {kind} {where}".strip())
        await self._reply(ctx, ("\n".join(lines)), allowed_mentions=discord.AllowedMentions.none())

    @com.command(name="seenlistcsv")
    async def com_seenlist_csv(self, ctx: redcommands.Context) -> None:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "member_id",
                "display",
                "last_seen_ts",
                "last_seen_human",
                "kind",
                "where",
                "status",
                "last_online_ts",
                "last_offline_ts",
            ]
        )
        saved = await self.config.all_members(ctx.guild)
        for m in ctx.guild.members:
            d = saved.get(m.id, {}).get("seen", {})
            ts = d.get("any", 0)
            pres = d.get("presence", {})
            human = "" if not ts else discord.utils.format_dt(self._dt_from_ts(ts), style="R")
            writer.writerow(
                [
                    m.id,
                    str(m),
                    ts,
                    human,
                    d.get("kind", ""),
                    d.get("where", 0),
                    pres.get("status", ""),
                    pres.get("last_online", 0),
                    pres.get("last_offline", 0),
                ]
            )
        output.seek(0)
        fp = io.BytesIO(output.getvalue().encode("utf-8"))
        await self._reply(ctx, file=discord.File(fp, filename=f"seen_{ctx.guild.id}.csv"))

    @com.command(name="embeds")
    async def com_embeds(self, ctx: redcommands.Context, compact: Optional[bool] = None) -> None:
        if compact is None:
            cur = await self.config.guild(ctx.guild).embeds.compact()
            return await self._reply(ctx, f"Embeds compact = **{cur}**.")
        await self.config.guild(ctx.guild).embeds.compact.set(bool(compact))
        await self._presentation.confirm(ctx)

    # ------------------------ listeners ------------------------
    @commands.Cog.listener()
    @guild_enabled
    async def on_member_join(self, member: discord.Member) -> None:
        g = await self._settings(member.guild)
        # Sticky
        if g["sticky"]["enabled"]:
            snap = await self.config.member(member).sticky_roles()
            ignored = set(g["sticky"]["ignore"])
            # Snap contains raw IDs.
            roles_to_add = self._eligible_roles(member, [r for r in snap if r not in ignored])
            if roles_to_add:
                try:
                    await member.add_roles(*roles_to_add, reason="CommunityPlus sticky")
                except discord.HTTPException:
                    pass

        # Autorole
        if not await self.config.member(member).ever_seen():
            if g["autorole"]["enabled"] and g["autorole"]["role_id"]:
                r = member.guild.get_role(g["autorole"]["role_id"])
                if r and r in self._eligible_roles(member, [r.id]):
                    try:
                        await member.add_roles(r, reason="CommunityPlus autorole")
                    except discord.HTTPException:
                        pass

        if g["welcome"]["enabled"] and g["welcome"]["channel_id"]:
            text = self._format_template(g["welcome"]["message"], member)
            e = await self._mk_embed(member.guild, "Welcome", desc=text, kind="ok")
            await self._send_to_channel_id(member.guild, g["welcome"]["channel_id"], e)

        if g["seen"]["enabled"]:
            await self._seen_mark(member, kind="join")
        group = self.config.member(member)
        async with group.get_lock():
            await group.ever_seen.set(True)

    @commands.Cog.listener()
    @guild_enabled
    async def on_member_remove(self, member: discord.Member) -> None:
        g = await self._settings(member.guild)
        # Sticky Snapshot: FIX APPLIED HERE
        if g["sticky"]["enabled"]:
            ignored = set(g["sticky"]["ignore"])
            # Filter out Default AND Managed roles (boosters/bots)
            role_ids = [
                r.id
                for r in member.roles
                if not r.is_default() and not r.managed and r.id not in ignored
            ]
            group = self.config.member(member)
            async with group.get_lock():
                await group.sticky_roles.set(role_ids)

        if g["cya"]["enabled"] and g["cya"]["channel_id"]:
            text = self._format_template(g["cya"]["message"], member)
            e = await self._mk_embed(member.guild, "Goodbye", desc=text, kind="warn")
            await self._send_to_channel_id(member.guild, g["cya"]["channel_id"], e)

        if g["seen"]["enabled"]:
            await self._seen_mark(member, kind="leave")

    @commands.Cog.listener()
    @guild_enabled
    async def on_message(self, message):
        if (
            not message.guild
            or message.author.bot
            or not isinstance(message.author, discord.Member)
        ):
            return
        if (await self._settings(message.guild))["seen"]["enabled"]:
            await self._record_activity(
                message.author, "message", message.channel.id, {"messages": 1}
            )

    # Solo Voice Logic
    async def _cancel_task_for_member(self, member_id, guild_id=None):
        for key in list(self._solo_tasks):
            if key[1] == member_id and (guild_id is None or key[0] == guild_id):
                task = self._solo_tasks.pop(key)
                task.cancel()

    async def _schedule_solo_disconnect(self, member, wait_s):
        key = (member.guild.id, member.id)
        if key in self._solo_tasks and not self._solo_tasks[key].done():
            return
        channel_id = member.voice.channel.id if member.voice and member.voice.channel else None

        async def disconnect():
            try:
                await asyncio.sleep(wait_s)
                if await self.bot.cog_disabled_in_guild(self, member.guild):
                    return
                settings = (await self._settings(member.guild))["vcsolo"]
                if not settings["enabled"] or not member.voice or not member.voice.channel:
                    return
                channel = member.voice.channel
                humans = [m for m in channel.members if not m.bot]
                if channel.id != channel_id or len(humans) != 1 or humans[0].id != member.id:
                    return
                await member.move_to(None, reason="Solo VC timeout")
                if settings["dm_notify"]:
                    try:
                        await self._presentation.send(
                            member,
                            f"Disconnected from {member.guild.name}: solo for {wait_s}s.",
                            title="Voice timeout",
                            tone="warning",
                        )
                    except discord.HTTPException:
                        log.debug("Solo timeout DM could not be sent", exc_info=True)
            except discord.HTTPException:
                log.debug("Solo timeout could not disconnect member", exc_info=True)
            finally:
                if self._solo_tasks.get(key) is asyncio.current_task():
                    self._solo_tasks.pop(key, None)

        self._solo_tasks[key] = asyncio.create_task(
            disconnect(), name=f"communityplus-solo-{key[0]}-{key[1]}"
        )

    async def _refresh_solo_for_channel(self, channel):
        if channel is None:
            return
        settings = (await self._settings(channel.guild))["vcsolo"]
        humans = [m for m in channel.members if not m.bot]
        if settings["enabled"] and len(humans) == 1:
            await self._schedule_solo_disconnect(humans[0], int(settings["idle_seconds"]))
        else:
            for member in channel.members:
                await self._cancel_task_for_member(member.id, channel.guild.id)

    @commands.Cog.listener()
    @guild_enabled
    async def on_voice_state_update(self, member, before, after):
        settings = await self._settings(member.guild)
        if not member.bot and settings["seen"]["enabled"]:
            counters = {}
            if before.channel is None and after.channel is not None:
                counters["voice_joins"] = 1
            elif before.channel is not None and after.channel is None:
                counters["voice_leaves"] = 1
            elif before.channel != after.channel:
                counters["voice_moves"] = 1
            if not before.self_stream and after.self_stream:
                counters["stream_starts"] = 1
            if not before.self_video and after.self_video:
                counters["video_starts"] = 1
            location = after.channel or before.channel
            await self._record_activity(member, "voice", location.id if location else 0, counters)
        if before.channel != after.channel:
            await self._cancel_task_for_member(member.id, member.guild.id)
            await self._refresh_solo_for_channel(before.channel)
            await self._refresh_solo_for_channel(after.channel)

    @commands.Cog.listener()
    @guild_enabled
    async def on_presence_update(self, before, after):
        if not after.bot and (await self._settings(after.guild))["seen"]["enabled"]:
            await self._handle_presence_update_logic(before, after)

    async def cog_load(self):
        self._startup_task = asyncio.create_task(
            self._restore_solo_timers(), name="communityplus-restore"
        )

    async def cog_unload(self):
        tasks = list(self._solo_tasks.values())
        if self._startup_task:
            tasks.append(self._startup_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._solo_tasks.clear()
        self._settings_cache.clear()

    async def cog_before_invoke(self, ctx):
        self._settings_cache.pop(ctx.guild.id, None)

    async def cog_after_invoke(self, ctx):
        self._settings_cache.pop(ctx.guild.id, None)

    async def _settings(self, guild):
        now = time.monotonic()
        cached = self._settings_cache.get(guild.id)
        if cached is not None and now - cached[0] < 5:
            return cached[1]
        async with self._settings_locks[guild.id]:
            now = time.monotonic()
            cached = self._settings_cache.get(guild.id)
            if cached is not None and now - cached[0] < 5:
                return cached[1]
            settings = await self.config.guild(guild).all()
            self._settings_cache[guild.id] = (now, settings)
            return settings

    async def _record_activity(self, member, kind, where=0, counters=None):
        async with self.config.member(member).all() as data:
            seen = data.setdefault("seen", deepcopy(DEFAULTS_MEMBER["seen"]))
            now = self._now_ts()
            seen.update(any=now, kind=kind, where=where)
            if kind in {"message", "voice", "join", "leave"}:
                seen[kind] = now
            if kind in {"message", "voice"}:
                seen[f"{kind}_ch"] = where
            stats = data.setdefault("stats", deepcopy(DEFAULTS_MEMBER["stats"]))
            for key, increment in (counters or {}).items():
                stats[key] = int(stats.get(key, 0)) + increment

    async def _cancel_guild_timers(self, guild_id):
        for key in list(self._solo_tasks):
            if key[0] == guild_id:
                task = self._solo_tasks.pop(key)
                task.cancel()

    async def red_delete_data_for_user(self, *, requester, user_id):
        for guild_id in await self.config.all_members():
            group = self.config.member_from_ids(guild_id, user_id)
            async with group.get_lock():
                await group.clear()
        for key in list(self._solo_tasks):
            if key[1] == user_id:
                self._solo_tasks.pop(key).cancel()

    async def red_get_data_for_user(self, *, user_id):
        data = {
            str(gid): members[user_id]
            for gid, members in (await self.config.all_members()).items()
            if user_id in members
        }
        return (
            {"communityplus.json": io.BytesIO(json.dumps(data, indent=2).encode())} if data else {}
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        await self._cancel_guild_timers(guild.id)
        self._settings_cache.pop(guild.id, None)
        self._settings_locks.pop(guild.id, None)
