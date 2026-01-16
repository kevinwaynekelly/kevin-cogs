# path: cogs/communityplus/__init__.py
from __future__ import annotations

import asyncio
import csv
import io
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Set

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
    "autorole": {"enabled": True, "role_id": None},
    "sticky": {"enabled": True, "ignore": []},
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
    "vcsolo": {"enabled": True, "idle_seconds": 900, "dm_notify": True},
    "seen": {"enabled": True},
}

DEFAULTS_MEMBER = {
    "ever_seen": False,
    "sticky_roles": [],
    "seen": {
        "any": 0, "kind": "", "where": 0,
        "message": 0, "message_ch": 0,
        "voice": 0, "voice_ch": 0,
        "join": 0, "leave": 0,
        "presence": {
            "status": "",
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
    "activity_names": {},
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
        self._solo_tasks: Dict[int, asyncio.Task] = {}
        
        # RESTORE TIMER FIX: Scan for solo users on reload
        self.bot.loop.create_task(self._restore_solo_timers())

    async def _restore_solo_timers(self):
        """Restores solo timers if the bot restarts or cog reloads."""
        await self.bot.wait_until_red_ready()
        for guild in self.bot.guilds:
            if not await self.config.guild(guild).vcsolo.enabled():
                continue
            
            # Check Voice Channels
            for vc in guild.voice_channels:
                humans = [m for m in vc.members if not m.bot]
                if len(humans) == 1:
                    await self._refresh_solo_for_channel(vc)
            
            # Check Stage Channels
            for stage in guild.stage_channels:
                humans = [m for m in stage.members if not m.bot]
                if len(humans) == 1:
                    await self._refresh_solo_for_channel(stage)

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

    async def _mk_embed(
        self, 
        guild: discord.Guild, 
        title: str, 
        *, 
        desc: Optional[str] = None, 
        kind: str = "info", 
        footer: Optional[str] = None
    ) -> discord.Embed:
        color = EVENT_COLOR.get(kind, discord.Color.blurple())
        title = f"• {title}" if await self._embed_compact(guild) else title
        e = discord.Embed(title=title, description=desc, color=color, timestamp=self._utcnow())
        if footer:
            e.set_footer(text=footer)
        return e

    # ---------- status ----------
    async def _status_embed(self, guild: discord.Guild) -> discord.Embed:
        g = await self.config.guild(guild).all()
        ar_role = guild.get_role(g["autorole"]["role_id"])
        ar = ar_role.mention if g["autorole"]["role_id"] and ar_role else "not set"
        
        sticky_ign = [guild.get_role(r).mention for r in g["sticky"]["ignore"] if guild.get_role(r)] or ["none"]
        
        wc_id = g["welcome"]["channel_id"]
        welcome_ch = guild.get_channel(wc_id).mention if wc_id and guild.get_channel(wc_id) else "not set"
        
        cc_id = g["cya"]["channel_id"]
        cya_ch = guild.get_channel(cc_id).mention if cc_id and guild.get_channel(cc_id) else "not set"

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
            inline=True
        )
        e.add_field(
            name="Sticky Roles", 
            value=box(f"enabled = {g['sticky']['enabled']}\nignore  = {', '.join(sticky_ign)}", lang="ini"), 
            inline=True
        )
        e.add_field(
            name="Welcome", 
            value=box(f"enabled = {g['welcome']['enabled']}\nchannel = {welcome_ch}", lang="ini"), 
            inline=True
        )
        e.add_field(
            name="Cya", 
            value=box(f"enabled = {g['cya']['enabled']}\nchannel = {cya_ch}", lang="ini"), 
            inline=True
        )
        e.add_field(
            name="Solo VC",
            value=box(
                f"enabled   = {g['vcsolo']['enabled']}\n"
                f"idle      = {g['vcsolo']['idle_seconds']}s\n"
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

    async def _send_to_channel_id(self, guild: discord.Guild, channel_id: Optional[int], embed: discord.Embed) -> None:
        if not channel_id:
            return
        ch = guild.get_channel(channel_id)
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass

    # ------------------------ stats helpers (OPTIMIZED) ------------------------
    async def _bump_stat(self, member: discord.Member, key: str, delta: int = 1) -> None:
        async with self.config.member(member).stats() as stats:
            stats[key] = int(stats.get(key, 0)) + delta

    async def _seen_mark(self, member: discord.Member, *, kind: str, where: int = 0) -> None:
        async with self.config.member(member).seen() as data:
            if "presence" not in data:
                merged = DEFAULTS_MEMBER["seen"].copy()
                merged.update({k: data.get(k, merged.get(k)) for k in merged.keys()})
                data.update(merged)
            now = self._now_ts()
            data["any"] = now
            data["kind"] = kind
            if where:
                data["where"] = where
            if kind in {"message", "voice", "join", "leave"}:
                data[kind] = now
                if kind in {"message", "voice"}:
                    data[f"{kind}_ch"] = where

    # ------------------------ presence logic (OPTIMIZED) ------------------------
    async def _handle_presence_update_logic(self, before: discord.Member, after: discord.Member) -> None:
        now = self._now_ts()
        status = str(getattr(after, "status", "unknown"))
        desktop = str(getattr(after, "desktop_status", "unknown"))
        mobile = str(getattr(after, "mobile_status", "unknown"))
        web = str(getattr(after, "web_status", "unknown"))
        
        before_set = {(a.type, getattr(a, "name", None)) for a in (before.activities or [])}
        after_set = {(a.type, getattr(a, "name", None)) for a in (after.activities or [])}
        new_activities = after_set - before_set
        
        async with self.config.member(after).all() as member_data:
            seen_data = member_data.setdefault("seen", DEFAULTS_MEMBER["seen"].copy())
            if "presence" not in seen_data: 
                seen_data["presence"] = DEFAULTS_MEMBER["seen"]["presence"].copy()
            p = seen_data["presence"]
            
            should_update_seen = False
            
            if status != p.get("status"):
                stats = member_data.setdefault("stats", DEFAULTS_MEMBER["stats"].copy())
                sc = stats.setdefault("status_changes", DEFAULTS_MEMBER["stats"]["status_changes"].copy())
                sc[status] = sc.get(status, 0) + 1
                should_update_seen = True

            for typ, name in new_activities:
                should_update_seen = True
                stats = member_data.setdefault("stats", DEFAULTS_MEMBER["stats"].copy())
                acts = stats.setdefault("activity_starts", DEFAULTS_MEMBER["stats"]["activity_starts"].copy())
                
                if typ == discord.ActivityType.playing:
                    acts["playing"] += 1
                    stats["game_launches"] += 1
                    if name:
                        names = member_data.setdefault("activity_names", {})
                        names[str(name)] = names.get(str(name), 0) + 1
                elif typ == discord.ActivityType.streaming: 
                    acts["streaming"] += 1
                elif typ == discord.ActivityType.listening: 
                    acts["listening"] += 1
                elif typ == discord.ActivityType.watching: 
                    acts["watching"] += 1
                elif typ == discord.ActivityType.competing: 
                    acts["competing"] += 1
                elif typ == discord.ActivityType.custom: 
                    acts["custom"] += 1

            if should_update_seen or status != "offline":
                p["status"] = status
                p["since"] = now
                p["desktop"] = desktop
                p["mobile"] = mobile
                p["web"] = web
                if status != "offline":
                    p["last_online"] = now
                    seen_data["any"] = max(seen_data.get("any", 0), now)
                    seen_data["kind"] = "presence"
                else:
                    p["last_offline"] = now

    # ------------------------ commands root ------------------------
    @redcommands.group(name="com", invoke_without_command=True)
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def com(self, ctx: redcommands.Context) -> None:
        await ctx.send(embed=await self._status_embed(ctx.guild))

    @com.command(name="help", aliases=["commands", "?"])
    async def com_help(self, ctx: redcommands.Context) -> None:
        p = ctx.clean_prefix
        e = discord.Embed(title="CommunityPlus — Commands", color=discord.Color.blurple())
        e.description = f"✨ Cleaner help • examples use `{p}` as prefix."
        e.add_field(
            name="🧩 Core", 
            value=f"• `{p}com` — status panel\n• `{p}com help` • `{p}com diag`", 
            inline=False
        )
        e.add_field(
            name="🛡️ Autorole", 
            value=f"• `{p}com autorole set @Role` • `clear`\n• `{p}com autorole enable|disable`", 
            inline=False
        )
        e.add_field(
            name="🧷 Sticky Roles", 
            value=f"• `{p}com sticky enable|disable`\n• `{p}com sticky ignore add|remove @Role`\n• `{p}com sticky purge @User`", 
            inline=False
        )
        e.add_field(
            name="🎉 Welcome & 👋 Cya", 
            value=f"• `{p}com welcome channel #ch` • `message <txt>`\n• `{p}com cya channel #ch` • `message <txt>`", 
            inline=False
        )
        e.add_field(
            name="🎧 Solo Voice", 
            value=f"• `{p}com vcsolo enable|disable`\n• `{p}com vcsolo idle <seconds>`", 
            inline=False
        )
        e.add_field(
            name="👀 Seen & Stats", 
            value=f"• `{p}com seen [@User]` • `{p}com stats`\n• `{p}com seenlist`", 
            inline=False
        )
        await ctx.send(embed=e)

    # ------------------------ DIAG ------------------------
    @com.command(name="diag")
    async def com_diag(self, ctx: redcommands.Context) -> None:
        g = ctx.guild
        me: discord.Member = g.me  # type: ignore
        intents = self.bot.intents

        def mark(ok: bool) -> str: return "✅" if ok else "❌"

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

        e = await self._mk_embed(g, "CommunityPlus — Diagnostics", kind="info")
        e.add_field(
            name="Intents", 
            value=f"{mark(intents.presences)} presences\n{mark(intents.members)} members", 
            inline=True
        )
        e.add_field(
            name="Perms", 
            value=f"{mark(perms.manage_roles)} manage_roles\n{mark(perms.move_members)} move_members", 
            inline=True
        )
        e.add_field(
            name="Autorole", 
            value=f"{mark(conf['autorole']['enabled'])} enabled\n{mark(role_ok)} setup valid", 
            inline=True
        )
        e.add_field(
            name="Solo VC", 
            value=f"{mark(conf['vcsolo']['enabled'])} enabled\n{mark(vc_ok)} valid", 
            inline=True
        )
        await ctx.send(embed=e)

    # ------------------------ subcommands ------------------------
    @com.group(name="autorole")
    async def com_autorole(self, ctx: redcommands.Context): 
        pass

    @com_autorole.command(name="set")
    async def car_set(self, ctx: redcommands.Context, role: discord.Role):
        await self.config.guild(ctx.guild).autorole.role_id.set(role.id)
        await ctx.tick()

    @com_autorole.command(name="clear")
    async def car_clear(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).autorole.role_id.set(None)
        await ctx.tick()

    @com_autorole.command(name="enable")
    async def car_en(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).autorole.enabled.set(True)
        await ctx.tick()

    @com_autorole.command(name="disable")
    async def car_dis(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).autorole.enabled.set(False)
        await ctx.tick()

    @com_autorole.command(name="show")
    async def car_show(self, ctx: redcommands.Context):
        g = await self.config.guild(ctx.guild).autorole()
        role = ctx.guild.get_role(g["role_id"])
        e = await self._mk_embed(
            ctx.guild, 
            "Autorole", 
            kind="info", 
            desc=f"**{'enabled' if g['enabled'] else 'disabled'}**, role: {role.mention if role else 'not set'}"
        )
        await ctx.send(embed=e)

    @com.group(name="sticky")
    async def com_sticky(self, ctx: redcommands.Context): 
        pass

    @com_sticky.command(name="enable")
    async def cst_en(self, ctx: redcommands.Context): 
        await self.config.guild(ctx.guild).sticky.enabled.set(True)
        await ctx.tick()

    @com_sticky.command(name="disable")
    async def cst_dis(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).sticky.enabled.set(False)
        await ctx.tick()

    @com_sticky.group(name="ignore")
    async def com_sticky_ignore(self, ctx: redcommands.Context): 
        pass

    @com_sticky_ignore.command(name="add")
    async def cst_i_add(self, ctx: redcommands.Context, role: discord.Role):
        async with self.config.guild(ctx.guild).sticky.ignore() as data:
            if role.id not in data: 
                data.append(role.id)
        await ctx.tick()

    @com_sticky_ignore.command(name="remove")
    async def cst_i_rem(self, ctx: redcommands.Context, role: discord.Role):
        async with self.config.guild(ctx.guild).sticky.ignore() as data:
            if role.id in data: 
                data.remove(role.id)
        await ctx.tick()

    @com_sticky_ignore.command(name="list")
    async def cst_i_list(self, ctx: redcommands.Context):
        data = await self.config.guild(ctx.guild).sticky.ignore()
        roles = [ctx.guild.get_role(r).mention for r in data if ctx.guild.get_role(r)] or ["none"]
        await ctx.send("Sticky ignored: " + ", ".join(roles))

    @com_sticky.command(name="purge")
    async def cst_purge(self, ctx: redcommands.Context, member: discord.Member):
        await self.config.member(member).sticky_roles.set([])
        await ctx.tick()

    @com.group(name="welcome")
    async def com_welcome(self, ctx: redcommands.Context): 
        pass

    @com_welcome.command(name="enable")
    async def cw_en(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).welcome.enabled.set(True)
        await ctx.tick()

    @com_welcome.command(name="disable")
    async def cw_dis(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).welcome.enabled.set(False)
        await ctx.tick()

    @com_welcome.command(name="channel")
    async def cw_ch(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None):
        await self.config.guild(ctx.guild).welcome.channel_id.set(channel.id if channel else None)
        await ctx.tick()

    @com_welcome.command(name="message")
    async def cw_msg(self, ctx: redcommands.Context, *, text: str):
        await self.config.guild(ctx.guild).welcome.message.set(text)
        await ctx.tick()

    @com_welcome.command(name="preview")
    async def cw_prev(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        g = await self.config.guild(ctx.guild).welcome()
        text = self._format_template(g["message"], member)
        e = await self._mk_embed(ctx.guild, "Welcome", desc=text, kind="ok")
        await ctx.send(embed=e)

    @com.group(name="cya")
    async def com_cya(self, ctx: redcommands.Context): 
        pass

    @com_cya.command(name="enable")
    async def cc_en(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).cya.enabled.set(True)
        await ctx.tick()

    @com_cya.command(name="disable")
    async def cc_dis(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).cya.enabled.set(False)
        await ctx.tick()

    @com_cya.command(name="channel")
    async def cc_ch(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None):
        await self.config.guild(ctx.guild).cya.channel_id.set(channel.id if channel else None)
        await ctx.tick()

    @com_cya.command(name="message")
    async def cc_msg(self, ctx: redcommands.Context, *, text: str):
        await self.config.guild(ctx.guild).cya.message.set(text)
        await ctx.tick()

    @com_cya.command(name="preview")
    async def cc_prev(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        g = await self.config.guild(ctx.guild).cya()
        text = self._format_template(g["message"], member)
        e = await self._mk_embed(ctx.guild, "Goodbye", desc=text, kind="warn")
        await ctx.send(embed=e)

    @com.group(name="vcsolo")
    async def com_vc(self, ctx: redcommands.Context): 
        pass

    @com_vc.command(name="enable")
    async def cvc_en(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).vcsolo.enabled.set(True)
        await ctx.tick()

    @com_vc.command(name="disable")
    async def cvc_dis(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).vcsolo.enabled.set(False)
        await ctx.tick()

    @com_vc.command(name="idle")
    async def cvc_idle(self, ctx: redcommands.Context, seconds: int):
        await self.config.guild(ctx.guild).vcsolo.idle_seconds.set(max(60, int(seconds)))
        await ctx.tick()

    # Seen & Stats Commands
    @com.command(name="seen")
    async def com_seen(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
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
    async def com_stats(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
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
        # Sticky
        if g["sticky"]["enabled"]:
            snap = await self.config.member(member).sticky_roles()
            ignored = set(g["sticky"]["ignore"])
            # Snap contains raw IDs.
            roles_to_add = self._eligible_roles(member, [r for r in snap if r not in ignored])
            if roles_to_add:
                try: 
                    await member.add_roles(*roles_to_add, reason="CommunityPlus sticky")
                except discord.Forbidden: 
                    pass

        # Autorole
        if not await self.config.member(member).ever_seen():
            if g["autorole"]["enabled"] and g["autorole"]["role_id"]:
                r = member.guild.get_role(g["autorole"]["role_id"])
                if r and not r.managed and not r.is_default():
                    try: 
                        await member.add_roles(r, reason="CommunityPlus autorole")
                    except discord.Forbidden: 
                        pass

        if g["welcome"]["enabled"] and g["welcome"]["channel_id"]:
            text = self._format_template(g["welcome"]["message"], member)
            e = await self._mk_embed(member.guild, "Welcome", desc=text, kind="ok")
            await self._send_to_channel_id(member.guild, g["welcome"]["channel_id"], e)

        if g["seen"]["enabled"]:
            await self._seen_mark(member, kind="join")
            await self.config.member(member).ever_seen.set(True)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        g = await self.config.guild(member.guild).all()
        # Sticky Snapshot: FIX APPLIED HERE
        if g["sticky"]["enabled"]:
            ignored = set(g["sticky"]["ignore"])
            # Filter out Default AND Managed roles (boosters/bots)
            role_ids = [r.id for r in member.roles if not r.is_default() and not r.managed and r.id not in ignored]
            await self.config.member(member).sticky_roles.set(role_ids)

        if g["cya"]["enabled"] and g["cya"]["channel_id"]:
            text = self._format_template(g["cya"]["message"], member)
            e = await self._mk_embed(member.guild, "Goodbye", desc=text, kind="warn")
            await self._send_to_channel_id(member.guild, g["cya"]["channel_id"], e)
            
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

    # Solo Voice Logic
    async def _cancel_task_for_member(self, member_id: int) -> None:
        t = self._solo_tasks.pop(member_id, None)
        if t and not t.done(): 
            t.cancel()

    async def _schedule_solo_disconnect(self, member: discord.Member, wait_s: int) -> None:
        await self._cancel_task_for_member(member.id)
        async def _job():
            try:
                await asyncio.sleep(wait_s)
                if not member.voice or not member.voice.channel: 
                    return
                ch = member.voice.channel
                humans = [m for m in ch.members if not m.bot]
                if len(humans) == 1 and humans[0].id == member.id:
                    try: 
                        await member.move_to(None, reason="Solo VC timeout")
                        if await self.config.guild(member.guild).vcsolo.dm_notify():
                            await member.send(f"Disconnected from {member.guild.name}: Solo for {wait_s}s.")
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
            for m in channel.members:
                if m.id != humans[0].id: 
                    await self._cancel_task_for_member(m.id)
        else:
            for m in humans: 
                await self._cancel_task_for_member(m.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if await self.config.guild(member.guild).seen.enabled():
            loc = after.channel.id if after.channel else (before.channel.id if before.channel else 0)
            await self._seen_mark(member, kind="voice", where=loc)
        
        # Stat bumps
        if before.channel is None and after.channel is not None: 
            await self._bump_stat(member, "voice_joins")
        elif before.channel is not None and after.channel is None: 
            await self._bump_stat(member, "voice_leaves")
        elif before.channel != after.channel: 
            await self._bump_stat(member, "voice_moves")
        
        if before.self_stream is False and after.self_stream is True: 
            await self._bump_stat(member, "stream_starts")
        if before.self_video is False and after.self_video is True: 
            await self._bump_stat(member, "video_starts")

        # Refresh timers
        if await self.config.guild(member.guild).vcsolo.enabled():
            await self._refresh_solo_for_channel(before.channel)
            await self._refresh_solo_for_channel(after.channel)

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if not await self.config.guild(after.guild).seen.enabled(): 
            return
        try: 
            await self._handle_presence_update_logic(before, after)
        except Exception: 
            pass


async def setup(bot: Red) -> None:
    await bot.add_cog(CommunityPlus(bot))
