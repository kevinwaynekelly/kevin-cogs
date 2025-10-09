# path: cogs/communityplus/__init__.py
from __future__ import annotations

import asyncio
import csv
import io
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config
from redbot.core.utils.chat_formatting import box, humanize_number

__red_end_user_data_statement__ = (
    "This cog stores per-guild settings for autoroles, sticky-role preferences, welcome/cya message targets, "
    "solo-voice idle settings, and compact-embed preference. It also stores, per member, last-seen timestamps for "
    "message/voice/join/leave/presence, presence status history (last online/offline), and counters for messages sent, "
    "voice joins/moves/leaves, stream/video starts, activity starts (playing/streaming/listening/watching/competing/custom), "
    "and per-game launch counts. No message contents are stored."
)

# ------------------------ defaults ------------------------
DEFAULTS_GUILD = {
    "embeds": {"compact": True},
    "autorole": {"enabled": True, "role_id": None},  # first-time only
    "sticky": {"enabled": True, "ignore": []},       # reapply stored roles
    "welcome": {
        "enabled": True,
        "channel_id": None,
        "message": "Welcome {mention}! You’re member #{count} of **{server}**.",
    },
    "cya": {
        "enabled": True,
        "channel_id": None,
        "message": "Cya {user} 👋",
    },
    # Solo VC idle (default 15 minutes)
    "vcsolo": {"enabled": True, "idle_seconds": 900, "dm_notify": True},
    "seen": {"enabled": True},
}

DEFAULTS_MEMBER = {
    "ever_seen": False,
    "sticky_roles": [],  # snapshot on leave, reapply on join
    "seen": {
        "any": 0, "kind": "", "where": 0,
        "message": 0, "message_ch": 0,
        "voice": 0, "voice_ch": 0,
        "join": 0, "leave": 0,
        "presence": {
            "status": "",           # online|offline/idle/dnd/unknown
            "since": 0,
            "last_online": 0,
            "last_offline": 0,
            "desktop": "unknown",
            "mobile": "unknown",
            "web": "unknown",
        },
    },
    "stats": {
        "messages": 0,
        "voice_joins": 0,
        "voice_moves": 0,
        "voice_leaves": 0,
        "stream_starts": 0,
        "video_starts": 0,
        "game_launches": 0,
        "activity_starts": {
            "playing": 0, "streaming": 0, "listening": 0, "watching": 0, "competing": 0, "custom": 0
        },
        "status_changes": {"online": 0, "offline": 0, "idle": 0, "dnd": 0, "unknown": 0},
    },
    "activity_names": {},  # {game_name: launches}
}

EVENT_COLOR = {
    "ok": discord.Color.green(),
    "info": discord.Color.blurple(),
    "warn": discord.Color.orange(),
    "err": discord.Color.red(),
}

class CommunityPlus(redcommands.Cog):
    """Autorole (first-time), Sticky roles, Welcome/Cya, Solo-VC kick (DM), Deep Seen/Presence, Counters."""

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0xC0DE505, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self.config.register_member(**DEFAULTS_MEMBER)
        self._solo_tasks: Dict[int, asyncio.Task] = {}  # solo VC timers

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
        return "never" if not ts else discord.utils.format_dt(CommunityPlus._dt_from_ts(ts), style="R")

    @staticmethod
    def _humanize_duration(seconds: int) -> str:
        """Return a short human string like '15 minutes', '1 hour 5 minutes'."""
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
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}" if minutes else "less than a minute")
        return " ".join(parts[:2])

    # ------------------------ embeds ------------------------
    async def _embed_compact(self, guild: discord.Guild) -> bool:
        try:
            return bool(await self.config.guild(guild).embeds.compact())
        except Exception:
            return True

    async def _mk_embed(self, guild: discord.Guild, title: str, *, desc: Optional[str] = None, kind: str = "info", footer: Optional[str] = None) -> discord.Embed:
        color = EVENT_COLOR.get(kind, discord.Color.blurple())
        title = f"• {title}" if await self._embed_compact(guild) else title
        e = discord.Embed(title=title, description=desc, color=color, timestamp=self._utcnow())
        if footer:
            e.set_footer(text=footer)
        return e

    # ---------- status ----------
    async def _status_embed(self, guild: discord.Guild) -> discord.Embed:
        g = await self.config.guild(guild).all()
        ar = guild.get_role(g["autorole"]["role_id"]).mention if g["autorole"]["role_id"] and guild.get_role(g["autorole"]["role_id"]) else "not set"
        sticky_ign = [guild.get_role(r).mention for r in g["sticky"]["ignore"] if guild.get_role(r)] or ["none"]
        welcome_ch = guild.get_channel(g["welcome"]["channel_id"]).mention if g["welcome"]["channel_id"] and guild.get_channel(g["welcome"]["channel_id"]) else "not set"
        cya_ch = guild.get_channel(g["cya"]["channel_id"]).mention if g["cya"]["channel_id"] and guild.get_channel(g["cya"]["channel_id"]) else "not set"

        e = discord.Embed(title="CommunityPlus — Status", color=discord.Color.blurple(), timestamp=self._utcnow())
        e.add_field(
            name="Core",
            value=box(
                f"embeds.compact = {g['embeds']['compact']}\n"
                f"seen.enabled   = {g['seen']['enabled']}",
                lang="ini",
            ),
            inline=False,
        )
        e.add_field(
            name="Autorole",
            value=box(f"enabled = {g['autorole']['enabled']}\nrole    = {ar}", lang="ini"),
            inline=True,
        )
        e.add_field(
            name="Sticky Roles",
            value=box(f"enabled = {g['sticky']['enabled']}\nignore  = {', '.join(sticky_ign)}", lang="ini"),
            inline=True,
        )
        e.add_field(
            name="Welcome",
            value=box(f"enabled = {g['welcome']['enabled']}\nchannel = {welcome_ch}", lang="ini"),
            inline=True,
        )
        e.add_field(
            name="Cya",
            value=box(f"enabled = {g['cya']['enabled']}\nchannel = {cya_ch}", lang="ini"),
            inline=True,
        )
        e.add_field(
            name="Solo VC",
            value=box(
                f"enabled = {g['vcsolo']['enabled']}\n"
                f"idle    = {g['vcsolo']['idle_seconds']}s\n"
                f"dm_notify = {g['vcsolo']['dm_notify']}",
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
                joined_at=discord.utils.format_dt(member.joined_at, style="R") if member.joined_at else "unknown",
            )
        except Exception:
            return tpl  # why: template errors must not break messages

    @staticmethod
    def _eligible_roles(member: discord.Member, role_ids: List[int]) -> List[discord.Role]:
        roles: List[discord.Role] = []
        me = member.guild.me
        top = me.top_role if me else None
        for rid in role_ids:
            r = member.guild.get_role(int(rid))
            if not r or r.is_default():
                continue
            if top and r >= top:
                continue  # why: cannot assign roles >= bot's top role
            roles.append(r)
        return roles

    async def _send_to_channel_id(self, guild: discord.Guild, channel_id: Optional[int], embed: discord.Embed) -> None:
        if not channel_id:
            return
        ch = guild.get_channel(channel_id)
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass

    # ------------------------ stats helpers ------------------------
    async def _bump_stat(self, member: discord.Member, key: str, delta: int = 1) -> None:
        stats = await self.config.member(member).stats()
        stats[key] = int(stats.get(key, 0)) + delta
        await self.config.member(member).stats.set(stats)

    async def _bump_nested(self, member: discord.Member, group: str, sub: str, delta: int = 1) -> None:
        stats = await self.config.member(member).stats()
        grp = dict(stats.get(group, {}))
        grp[sub] = int(grp.get(sub, 0)) + delta
        stats[group] = grp
        await self.config.member(member).stats.set(stats)

    async def _bump_game_name(self, member: discord.Member, name: str, delta: int = 1) -> None:
        names = await self.config.member(member).activity_names()
        names[name] = int(names.get(name, 0)) + delta
        await self.config.member(member).activity_names.set(names)

    # ------------------------ seen helpers ------------------------
    async def _seen_mark(self, member: discord.Member, *, kind: str, where: int = 0) -> None:
        data = await self.config.member(member).seen()
        if "presence" not in data:
            merged = DEFAULTS_MEMBER["seen"].copy()
            merged.update({k: data.get(k, merged.get(k)) for k in merged.keys()})
            data = merged
        now = self._now_ts()
        data["any"] = now
        data["kind"] = kind
        if where:
            data["where"] = where
        if kind in {"message", "voice", "join", "leave"}:
            data[kind] = now
            if kind in {"message", "voice"}:
                data[f"{kind}_ch"] = where
        await self.config.member(member).seen.set(data)

    async def _last_message_ts(self, member: discord.Member) -> int:
        data = await self.config.member(member).seen()
        return int(data.get("message", 0) or 0)

    async def _seen_mark_presence(self, before: discord.Member, after: discord.Member) -> None:
        data = await self.config.member(after).seen()
        if "presence" not in data:
            data = DEFAULTS_MEMBER["seen"].copy()
        p = data["presence"]
        status = str(getattr(after, "status", "unknown"))
        desktop = str(getattr(after, "desktop_status", "unknown"))
        mobile = str(getattr(after, "mobile_status", "unknown"))
        web = str(getattr(after, "web_status", "unknown"))

        now = self._now_ts()
        if status != p.get("status"):
            await self._bump_nested(after, "status_changes", status, 1)

        # activity deltas
        before_set = {(a.type, getattr(a, "name", None)) for a in (before.activities or [])}
        after_set = {(a.type, getattr(a, "name", None)) for a in (after.activities or [])}
        for typ, name in (after_set - before_set):
            if typ == discord.ActivityType.playing:
                await self._bump_nested(after, "activity_starts", "playing", 1)
                await self._bump_stat(after, "game_launches", 1)
                if name:
                    await self._bump_game_name(after, str(name), 1)
            elif typ == discord.ActivityType.streaming:
                await self._bump_nested(after, "activity_starts", "streaming", 1)
            elif typ == discord.ActivityType.listening:
                await self._bump_nested(after, "activity_starts", "listening", 1)
            elif typ == discord.ActivityType.watching:
                await self._bump_nested(after, "activity_starts", "watching", 1)
            elif typ == discord.ActivityType.competing:
                await self._bump_nested(after, "activity_starts", "competing", 1)
            elif typ == discord.ActivityType.custom:
                await self._bump_nested(after, "activity_starts", "custom", 1)

        # presence fields/stamps
        p["status"] = status
        p["since"] = now
        p["desktop"] = desktop
        p["mobile"] = mobile
        p["web"] = web
        if status != "offline":
            p["last_online"] = now
            data["any"] = max(data.get("any", 0), now)
            data["kind"] = "presence"
        else:
            p["last_offline"] = now
        data["presence"] = p
        await self.config.member(after).seen.set(data)

    # ------------------------ commands root ------------------------
    @redcommands.group(name="com", invoke_without_command=True)
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def com(self, ctx: redcommands.Context) -> None:
        await ctx.send(embed=await self._status_embed(ctx.guild))

    # ---- HELP (grouped) ----
    @com.command(name="help", aliases=["commands", "?"])
    async def com_help(self, ctx: redcommands.Context) -> None:
        p = ctx.clean_prefix
        e = discord.Embed(title="CommunityPlus — Commands", color=discord.Color.blurple())
        e.add_field(
            name="Autorole",
            value=f"• `{p}com autorole set @Role|clear|enable|disable|show`",
            inline=False,
        )
        e.add_field(
            name="Sticky Roles",
            value=f"• `{p}com sticky enable|disable`  •  `{p}com sticky ignore add|remove @Role`  •  `{p}com sticky ignore list`  •  `{p}com sticky purge @User`",
            inline=False,
        )
        e.add_field(
            name="Welcome",
            value=f"• `{p}com welcome channel #ch|none`  •  `{p}com welcome message <text>`  •  `{p}com welcome enable|disable|preview [@User]`",
            inline=False,
        )
        e.add_field(
            name="Cya",
            value=f"• `{p}com cya channel #ch|none`  •  `{p}com cya message <text>`  •  `{p}com cya enable|disable|preview [@User]`",
            inline=False,
        )
        e.add_field(
            name="Solo Voice",
            value=f"• `{p}com vcsolo enable|disable`  •  `{p}com vcsolo idle <seconds≥60>`",
            inline=False,
        )
        e.add_field(
            name="Seen & Stats",
            value=f"• `{p}com seen [@User]`  •  `{p}com seendetail [@User]`  •  `{p}com stats [@User]`  •  `{p}com seenlist [N]`  •  `{p}com seenlistcsv`",
            inline=False,
        )
        e.add_field(
            name="Embeds & Diagnostics",
            value=f"• `{p}com embeds [true|false]`  •  `{p}com diag`",
            inline=False,
        )
        await ctx.send(embed=e)

    # ------------------------ DIAG ------------------------
    @com.command(name="diag")
    async def com_diag(self, ctx: redcommands.Context) -> None:
        """Self-check: intents, permissions, channels/roles, config sanity."""
        g = ctx.guild
        me: discord.Member = g.me  # type: ignore
        intents = self.bot.intents

        def mark(ok: bool) -> str:
            return "✅" if ok else "❌"

        # intents
        i_pres = bool(getattr(intents, "presences", False))
        i_members = bool(getattr(intents, "members", False))
        i_msg_content = bool(getattr(intents, "message_content", False))

        # perms
        perms = me.guild_permissions if me else discord.Permissions.none()
        p_manage_roles = perms.manage_roles
        p_move_members = perms.move_members
        p_embed_links = perms.embed_links
        p_send_messages = perms.send_messages

        conf = await self.config.guild(g).all()
        # autorole role checks
        role_ok = True
        role_msgs: List[str] = []
        role_id = conf["autorole"]["role_id"]
        if role_id:
            role = g.get_role(role_id)
            if not role:
                role_ok = False
                role_msgs.append("Role not found.")
            else:
                if role.is_default():
                    role_ok = False
                    role_msgs.append("Autorole cannot be @everyone.")
                if role.managed:
                    role_ok = False
                    role_msgs.append("Autorole cannot be a managed role.")
                top = me.top_role if me else None
                if top and role >= top:
                    role_ok = False
                    role_msgs.append("Bot's top role is not above autorole.")
        else:
            role_ok = conf["autorole"]["enabled"] is False
            if not role_ok:
                role_msgs.append("Autorole enabled but no role set.")

        # channels check helper
        async def can_post(ch_id: Optional[int]) -> Tuple[bool, str]:
            if not ch_id:
                return (False, "Not set.")
            ch = g.get_channel(ch_id)
            if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                return (False, "Channel missing or not a text/thread.")
            p = ch.permissions_for(me)
            if not p.send_messages:
                return (False, f"No send_messages in {ch.mention}.")
            if not p.embed_links:
                return (False, f"No embed_links in {ch.mention}.")
            return (True, f"OK: {ch.mention}")

        w_ok, w_msg = await can_post(conf["welcome"]["channel_id"])
        c_ok, c_msg = await can_post(conf["cya"]["channel_id"])

        # sticky ignore roles exist
        sticky_ok = True
        for rid in conf["sticky"]["ignore"]:
            if not g.get_role(rid):
                sticky_ok = False
                break

        # vcsolo config (solo only)
        idle = int(conf["vcsolo"]["idle_seconds"])
        vc_ok = conf["vcsolo"]["enabled"] is False or (p_move_members and idle >= 60)

        # seen/presence
        seen_ok = bool(conf["seen"]["enabled"])
        presence_note = "Enable Presence Intent for richer Seen." if not i_pres else "Presence Intent OK."

        # build report
        e = await self._mk_embed(
            g, "CommunityPlus — Diagnostics", kind="info",
            desc="Configuration & permission checks. Use the hints below to fix issues."
        )
        e.add_field(
            name="Intents",
            value="\n".join([
                f"{mark(i_pres)} presences",
                f"{mark(i_members)} members",
                f"{mark(i_msg_content)} message_content",
            ]),
            inline=True,
        )
        e.add_field(
            name="Guild Perms (bot)",
            value="\n".join([
                f"{mark(p_manage_roles)} manage_roles",
                f"{mark(p_move_members)} move_members",
                f"{mark(p_send_messages)} send_messages",
                f"{mark(p_embed_links)} embed_links",
            ]),
            inline=True,
        )
        e.add_field(
            name="Autorole",
            value=f"{mark(conf['autorole']['enabled'])} enabled\n{mark(role_ok)} role setup\n" + ("; ".join(role_msgs) or "OK"),
            inline=False,
        )
        e.add_field(
            name="Sticky",
            value=f"{mark(conf['sticky']['enabled'])} enabled • ignore list: {mark(sticky_ok)}",
            inline=False,
        )
        e.add_field(
            name="Welcome",
            value=f"{mark(conf['welcome']['enabled'])} enabled • {w_msg}",
            inline=False,
        )
        e.add_field(
            name="Cya",
            value=f"{mark(conf['cya']['enabled'])} enabled • {c_msg}",
            inline=False,
        )
        e.add_field(
            name="Solo VC",
            value=f"{mark(conf['vcsolo']['enabled'])} enabled • idle={idle}s • move_members: {mark(p_move_members)} • overall: {mark(vc_ok)}",
            inline=False,
        )
        e.add_field(
            name="Seen/Presence",
            value=f"{mark(seen_ok)} seen enabled • {presence_note}",
            inline=False,
        )

        hints: List[str] = []
        if conf["autorole"]["enabled"] and not role_id:
            hints.append(f"Set autorole: `{ctx.clean_prefix}com autorole set @Role`")
        if not w_ok and conf["welcome"]["enabled"]:
            hints.append(f"Pick welcome channel: `{ctx.clean_prefix}com welcome channel #welcome`")
        if not c_ok and conf["cya"]["enabled"]:
            hints.append(f"Pick cya channel: `{ctx.clean_prefix}com cya channel #goodbye`")
        if conf["vcsolo"]["enabled"] and idle < 60:
            hints.append(f"Increase solo idle: `{ctx.clean_prefix}com vcsolo idle 900`")
        if not i_pres and conf["seen"]["enabled"]:
            hints.append("Enable Presence Intent in Dev Portal and run `{}set intents presences true`".format(ctx.clean_prefix))
        if hints:
            e.add_field(name="Fix Hints", value="\n".join(f"- {h}" for h in hints), inline=False)

        await ctx.send(embed=e)

    # ------------------------ autorole ------------------------
    @com.group(name="autorole")
    async def com_autorole(self, ctx: redcommands.Context) -> None: pass

    @com_autorole.command(name="set")
    async def com_autorole_set(self, ctx: redcommands.Context, role: discord.Role) -> None:
        await self.config.guild(ctx.guild).autorole.role_id.set(role.id)
        await ctx.tick()

    @com_autorole.command(name="clear")
    async def com_autorole_clear(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).autorole.role_id.set(None)
        await ctx.tick()

    @com_autorole.command(name="enable")
    async def com_autorole_enable(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).autorole.enabled.set(True)
        await ctx.tick()

    @com_autorole.command(name="disable")
    async def com_autorole_disable(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).autorole.enabled.set(False)
        await ctx.tick()

    @com_autorole.command(name="show")
    async def com_autorole_show(self, ctx: redcommands.Context) -> None:
        g = await self.config.guild(ctx.guild).autorole()
        role = ctx.guild.get_role(g["role_id"])
        e = await self._mk_embed(ctx.guild, "Autorole", kind="info",
                                 desc=f"**{'enabled' if g['enabled'] else 'disabled'}**, role: {role.mention if role else 'not set'}")
        await ctx.send(embed=e)

    # ------------------------ sticky ------------------------
    @com.group(name="sticky")
    async def com_sticky(self, ctx: redcommands.Context) -> None: pass

    @com_sticky.command(name="enable")
    async def com_sticky_enable(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).sticky.enabled.set(True)
        await ctx.tick()

    @com_sticky.command(name="disable")
    async def com_sticky_disable(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).sticky.enabled.set(False)
        await ctx.tick()

    @com_sticky.group(name="ignore")
    async def com_sticky_ignore(self, ctx: redcommands.Context) -> None: pass

    @com_sticky_ignore.command(name="add")
    async def com_sticky_ignore_add(self, ctx: redcommands.Context, role: discord.Role) -> None:
        data = await self.config.guild(ctx.guild).sticky.ignore()
        if role.id in data:
            return await ctx.send("Already ignored.")
        data.append(role.id)
        await self.config.guild(ctx.guild).sticky.ignore.set(data)
        await ctx.tick()

    @com_sticky_ignore.command(name="remove")
    async def com_sticky_ignore_remove(self, ctx: redcommands.Context, role: discord.Role) -> None:
        data = await self.config.guild(ctx.guild).sticky.ignore()
        if role.id not in data:
            return await ctx.send("Not ignored.")
        data = [r for r in data if r != role.id]
        await self.config.guild(ctx.guild).sticky.ignore.set(data)
        await ctx.tick()

    @com_sticky_ignore.command(name="list")
    async def com_sticky_ignore_list(self, ctx: redcommands.Context) -> None:
        data = await self.config.guild(ctx.guild).sticky.ignore()
        roles = [ctx.guild.get_role(r).mention for r in data if ctx.guild.get_role(r)] or ["none"]
        await ctx.send("Sticky ignored: " + ", ".join(roles))

    @com_sticky.command(name="purge")
    async def com_sticky_purge(self, ctx: redcommands.Context, member: discord.Member) -> None:
        await self.config.member(member).sticky_roles.set([])
        await ctx.send(f"Cleared sticky snapshot for **{member}**.")

    # ------------------------ welcome & cya ------------------------
    @com.group(name="welcome")
    async def com_welcome(self, ctx: redcommands.Context) -> None: pass

    @com_welcome.command(name="enable")
    async def com_welcome_enable(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).welcome.enabled.set(True)
        await ctx.tick()

    @com_welcome.command(name="disable")
    async def com_welcome_disable(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).welcome.enabled.set(False)
        await ctx.tick()

    @com_welcome.command(name="channel")
    async def com_welcome_channel(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        await self.config.guild(ctx.guild).welcome.channel_id.set(channel.id if channel else None)
        await ctx.tick()

    @com_welcome.command(name="message")
    async def com_welcome_message(self, ctx: redcommands.Context, *, text: str) -> None:
        await self.config.guild(ctx.guild).welcome.message.set(text)
        await ctx.tick()

    @com_welcome.command(name="preview")
    async def com_welcome_preview(self, ctx: redcommands.Context, member: Optional[discord.Member] = None) -> None:
        member = member or ctx.author
        g = await self.config.guild(ctx.guild).welcome()
        text = self._format_template(g["message"], member)
        e = await self._mk_embed(ctx.guild, "Welcome", desc=text, kind="ok")
        await ctx.send(embed=e)

    @com.group(name="cya")
    async def com_cya(self, ctx: redcommands.Context) -> None: pass

    @com_cya.command(name="enable")
    async def com_cya_enable(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).cya.enabled.set(True)
        await ctx.tick()

    @com_cya.command(name="disable")
    async def com_cya_disable(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).cya.enabled.set(False)
        await ctx.tick()

    @com_cya.command(name="channel")
    async def com_cya_channel(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        await self.config.guild(ctx.guild).cya.channel_id.set(channel.id if channel else None)
        await ctx.tick()

    @com_cya.command(name="message")
    async def com_cya_message(self, ctx: redcommands.Context, *, text: str) -> None:
        await self.config.guild(ctx.guild).cya.message.set(text)
        await ctx.tick()

    @com_cya.command(name="preview")
    async def com_cya_preview(self, ctx: redcommands.Context, member: Optional[discord.Member] = None) -> None:
        member = member or ctx.author
        g = await self.config.guild(ctx.guild).cya()
        text = self._format_template(g["message"], member)
        e = await self._mk_embed(ctx.guild, "Goodbye", desc=text, kind="warn")
        await ctx.send(embed=e)

    # ------------------------ VC SOLO COMMANDS ------------------------
    @com.group(name="vcsolo")
    async def com_vc(self, ctx: redcommands.Context) -> None:
        """Disconnects solo users after a delay."""
        pass

    @com_vc.command(name="enable")
    async def com_vc_enable(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).vcsolo.enabled.set(True)
        await ctx.tick()

    @com_vc.command(name="disable")
    async def com_vc_disable(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).vcsolo.enabled.set(False)
        await ctx.tick()

    @com_vc.command(name="idle")
    async def com_vc_idle(self, ctx: redcommands.Context, seconds: int) -> None:
        if seconds < 60:
            return await ctx.send("Idle seconds must be ≥ 60.")
        await self.config.guild(ctx.guild).vcsolo.idle_seconds.set(int(seconds))
        await ctx.tick()

    # ------------------------ seen/stats commands ------------------------
    @com.command(name="seen")
    async def com_seen(self, ctx: redcommands.Context, member: Optional[discord.Member] = None) -> None:
        member = member or ctx.author
        data = await self.config.member(member).seen()
        if not data:
            return await ctx.send(f"I haven’t seen **{member}** yet.")
        pres = data.get("presence", {})
        lines = [
            f"Last seen (any): {self._fmt_rel(data.get('any', 0))}" + (f" via **{data.get('kind','')}**" if data.get('kind') else ""),
            (f"in <#{data.get('where')}>" if data.get("where") else ""),
            f"Presence: **{pres.get('status','unknown')}** since {self._fmt_rel(pres.get('since', 0))}",
            f"Last online: {self._fmt_rel(pres.get('last_online', 0))}   |   Last offline: {self._fmt_rel(pres.get('last_offline', 0))}",
        ]
        await ctx.send("\n".join([x for x in lines if x]))

    @com.command(name="seendetail")
    async def com_seen_detail(self, ctx: redcommands.Context, member: Optional[discord.Member] = None) -> None:
        member = member or ctx.author
        data = await self.config.member(member).seen()
        if not data:
            return await ctx.send(f"I haven’t seen **{member}** yet.")
        pres = data.get("presence", {})
        fields = [
            ("Any", f"{self._fmt_rel(data.get('any', 0))} ({data.get('kind','') or 'n/a'})"),
            ("Message", f"{self._fmt_rel(data.get('message', 0))} in <#{data.get('message_ch', 0)}>" if data.get("message_ch") else self._fmt_rel(data.get("message", 0))),
            ("Voice", f"{self._fmt_rel(data.get('voice', 0))} in <#{data.get('voice_ch', 0)}>" if data.get("voice_ch") else self._fmt_rel(data.get("voice", 0))),
            ("Join", self._fmt_rel(data.get("join", 0))),
            ("Leave", self._fmt_rel(data.get("leave", 0))),
            ("Presence Status", f"{pres.get('status','unknown')} since {self._fmt_rel(pres.get('since',0))}"),
            ("Presence Online", f"last_online: {self._fmt_rel(pres.get('last_online',0))}"),
            ("Presence Offline", f"last_offline: {self._fmt_rel(pres.get('last_offline',0))}"),
            ("Platforms", f"desktop={pres.get('desktop','?')} mobile={pres.get('mobile','?')} web={pres.get('web','?')}"),
        ]
        e = await self._mk_embed(ctx.guild, f"Seen detail — {member}", kind="info")
        for n, v in fields:
            e.add_field(name=n, value=v, inline=False)
        await ctx.send(embed=e)

    @com.command(name="stats")
    async def com_stats(self, ctx: redcommands.Context, member: Optional[discord.Member] = None) -> None:
        member = member or ctx.author
        stats = await self.config.member(member).stats()
        games = await self.config.member(member).activity_names()
        top_games = sorted(games.items(), key=lambda x: x[1], reverse=True)[:5]
        e = await self._mk_embed(ctx.guild, f"Stats — {member}", kind="info")
        e.add_field(name="Messages", value=humanize_number(stats.get("messages", 0)))
        e.add_field(name="Voice joins", value=humanize_number(stats.get("voice_joins", 0)))
        e.add_field(name="Voice moves", value=humanize_number(stats.get("voice_moves", 0)))
        e.add_field(name="Voice leaves", value=humanize_number(stats.get("voice_leaves", 0)))
        e.add_field(name="Stream starts", value=humanize_number(stats.get("stream_starts", 0)))
        e.add_field(name="Video starts", value=humanize_number(stats.get("video_starts", 0)))
        e.add_field(name="Game launches", value=humanize_number(stats.get("game_launches", 0)))
        acts = stats.get("activity_starts", {})
        e.add_field(name="Activities", value=", ".join(f"{k}:{acts.get(k,0)}" for k in ["playing","streaming","listening","watching","competing","custom"]), inline=False)
        sc = stats.get("status_changes", {})
        e.add_field(name="Status changes", value=", ".join(f"{k}:{sc.get(k,0)}" for k in ["online","idle","dnd","offline"]), inline=False)
        if top_games:
            e.add_field(name="Top games", value="\n".join(f"{n}: {c}" for n,c in top_games), inline=False)
        await ctx.send(embed=e)

    @com.command(name="seenlist")
    async def com_seenlist(self, ctx: redcommands.Context, limit: Optional[int] = 25) -> None:
        rows: List[Tuple[discord.Member, Dict]] = []
        for m in ctx.guild.members:
            rows.append((m, await self.config.member(m).seen()))
        rows.sort(key=lambda t: t[1].get("any", 0), reverse=True)
        limit = max(1, min(int(limit or 25), 100))
        lines = [f"**Last seen (top {limit})**"]
        for m, d in rows[:limit]:
            when = self._fmt_rel(d.get("any", 0))
            kind = d.get("kind", "")
            where = f"<#{d.get('where', 0)}>" if d.get("where") else ""
            lines.append(f"- {m} — {when} {kind} {where}".strip())
        await ctx.send("\n".join(lines))

    @com.command(name="seenlistcsv")
    async def com_seenlist_csv(self, ctx: redcommands.Context) -> None:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["member_id","display","last_seen_ts","last_seen_human","kind","where","status","last_online_ts","last_offline_ts"])
        for m in ctx.guild.members:
            d = await self.config.member(m).seen()
            ts = d.get("any", 0)
            pres = d.get("presence", {})
            human = "" if not ts else discord.utils.format_dt(self._dt_from_ts(ts), style="R")
            writer.writerow([m.id, str(m), ts, human, d.get("kind",""), d.get("where",0),
                             pres.get("status",""), pres.get("last_online",0), pres.get("last_offline",0)])
        output.seek(0)
        fp = io.BytesIO(output.getvalue().encode("utf-8"))
        await ctx.send(file=discord.File(fp, filename=f"seen_{ctx.guild.id}.csv"))

    @com.command(name="embeds")
    async def com_embeds(self, ctx: redcommands.Context, compact: Optional[bool] = None) -> None:
        if compact is None:
            cur = await self.config.guild(ctx.guild).embeds.compact()
            return await ctx.send(f"Embeds compact = **{cur}**.")
        await self.config.guild(ctx.guild).embeds.compact.set(bool(compact))
        await ctx.tick()

    # ------------------------ listeners ------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        g = await self.config.guild(member.guild).all()
        # sticky reapply first
        if g["sticky"]["enabled"]:
            snap = await self.config.member(member).sticky_roles()
            ignored = set(g["sticky"]["ignore"])
            snap = [r for r in snap if r not in ignored]
            roles = self._eligible_roles(member, snap)
            if roles:
                try:
                    await member.add_roles(*roles, reason="CommunityPlus sticky reapply")
                except discord.Forbidden:
                    pass

        # autorole only if never seen before
        ever_seen = await self.config.member(member).ever_seen()
        if not ever_seen and g["autorole"]["enabled"] and g["autorole"]["role_id"]:
            role = member.guild.get_role(g["autorole"]["role_id"])
            if role and role < member.guild.me.top_role and not role.is_default():
                try:
                    await member.add_roles(role, reason="CommunityPlus autorole (first-time only)")
                except discord.Forbidden:
                    pass

        # welcome
        if g["welcome"]["enabled"] and g["welcome"]["channel_id"]:
            text = self._format_template(g["welcome"]["message"], member)
            e = await self._mk_embed(member.guild, "Welcome", desc=text, kind="ok")
            await self._send_to_channel_id(member.guild, g["welcome"]["channel_id"], e)

        # seen
        if g["seen"]["enabled"]:
            await self._seen_mark(member, kind="join")
            await self.config.member(member).ever_seen.set(True)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        g = await self.config.guild(member.guild).all()
        # snapshot sticky roles
        if g["sticky"]["enabled"]:
            ignored = set(g["sticky"]["ignore"])
            role_ids = [r.id for r in member.roles if not r.is_default() and r.id not in ignored]
            await self.config.member(member).sticky_roles.set(role_ids)
        # cya
        if g["cya"]["enabled"] and g["cya"]["channel_id"]:
            text = self._format_template(g["cya"]["message"], member)
            e = await self._mk_embed(member.guild, "Goodbye", desc=text, kind="warn")
            await self._send_to_channel_id(member.guild, g["cya"]["channel_id"], e)
        # seen
        if g["seen"]["enabled"]:
            await self._seen_mark(member, kind="leave")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        if not await self.config.guild(message.guild).seen.enabled():
            return
        await self._seen_mark(message.author, kind="message", where=message.channel.id)
        await self._bump_stat(message.author, "messages", 1)

    async def _cancel_task_for_member(self, member_id: int) -> None:
        t = self._solo_tasks.pop(member_id, None)
        if t and not t.done():
            t.cancel()

    async def _schedule_solo_disconnect(self, member: discord.Member, wait_s: int) -> None:
        await self._cancel_task_for_member(member.id)

        async def _job():
            try:
                await asyncio.sleep(wait_s)
                ch = getattr(member.voice, "channel", None)
                if not ch:
                    return
                humans = [m for m in ch.members if not m.bot]
                if len(humans) == 1 and humans[0].id == member.id:
                    try:
                        reason = f"Solo in voice for {self._humanize_duration(wait_s)}"
                        await member.move_to(None, reason=reason)
                        if await self.config.guild(member.guild).vcsolo.dm_notify():
                            try:
                                await member.send(
                                    f"You were disconnected from **{member.guild.name}** voice: alone for {self._humanize_duration(wait_s)}."
                                )
                            except discord.Forbidden:
                                pass
                    except discord.Forbidden:
                        pass
            except asyncio.CancelledError:
                return

        self._solo_tasks[member.id] = asyncio.create_task(_job())

    async def _refresh_solo_for_channel(self, channel: Optional[discord.VoiceChannel | discord.StageChannel]) -> None:
        if not channel:
            return
        gset = await self.config.guild(channel.guild).vcsolo()
        if not gset["enabled"]:
            return
        humans = [m for m in channel.members if not m.bot]
        if len(humans) == 1:
            await self._schedule_solo_disconnect(humans[0], int(gset["idle_seconds"]))
            for m in [x for x in channel.members if not x.bot and x.id != humans[0].id]:
                await self._cancel_task_for_member(m.id)
        else:
            for m in humans:
                await self._cancel_task_for_member(m.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if await self.config.guild(member.guild).seen.enabled():
            loc = after.channel.id if after.channel else (before.channel.id if before.channel else 0)
            await self._seen_mark(member, kind="voice", where=loc)

        # counters
        if before.channel is None and after.channel is not None:
            await self._bump_stat(member, "voice_joins", 1)
        elif before.channel is not None and after.channel is None:
            await self._bump_stat(member, "voice_leaves", 1)
        elif before.channel and after.channel and before.channel.id != after.channel.id:
            await self._bump_stat(member, "voice_moves", 1)

        if before.self_stream is False and after.self_stream is True:
            await self._bump_stat(member, "stream_starts", 1)
        if before.self_video is False and after.self_video is True:
            await self._bump_stat(member, "video_starts", 1)

        # refresh solo timers for affected channels (solo-only logic)
        gset = await self.config.guild(member.guild).vcsolo()
        if gset["enabled"]:
            await self._refresh_solo_for_channel(before.channel if before else None)
            await self._refresh_solo_for_channel(after.channel if after else None)

    # Presence tracking (needs Presence Intent)
    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if not await self.config.guild(after.guild).seen.enabled():
            return
        try:
            await self._seen_mark_presence(before, after)
        except Exception:
            pass


async def setup(bot: Red) -> None:
    await bot.add_cog(CommunityPlus(bot))
