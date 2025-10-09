# path: cogs/kevinwaynekelly/logplus/__init__.py
from __future__ import annotations

from typing import Optional, Dict, Tuple, Iterable, List
import time
import re
from datetime import timedelta

import discord
from discord.ext import commands

from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config
from redbot.core.utils.chat_formatting import box

__red_end_user_data_statement__ = (
    "This cog stores per-guild settings for logging preferences, channel/role/member snapshots for short-lived, "
    "admin-initiated restore actions, channel IDs, and optional per-channel routing overrides. Snapshots are "
    "auto-pruned by TTL and do not include message contents."
)

# ========================= Styling =========================
EVENT_STYLE: Dict[str, Dict[str, object]] = {
    "message_edited": {"emoji": "✏️", "color": discord.Color.gold()},
    "message_deleted": {"emoji": "🗑️", "color": discord.Color.red()},
    "bulk_delete": {"emoji": "🧹", "color": discord.Color.red()},
    "pins_updated": {"emoji": "📌", "color": discord.Color.blurple()},
    "reaction_added": {"emoji": "➕", "color": discord.Color.green()},
    "reaction_removed": {"emoji": "➖", "color": discord.Color.orange()},
    "reaction_cleared": {"emoji": "♻️", "color": discord.Color.orange()},
    "channel_created": {"emoji": "📺", "color": discord.Color.green()},
    "channel_deleted": {"emoji": "📺", "color": discord.Color.red()},
    "channel_updated": {"emoji": "📺", "color": discord.Color.blurple()},
    "role_created": {"emoji": "🛡️", "color": discord.Color.green()},
    "role_deleted": {"emoji": "🛡️", "color": discord.Color.red()},
    "role_updated": {"emoji": "🛡️", "color": discord.Color.blurple()},
    "server_updated": {"emoji": "🏠", "color": discord.Color.blurple()},
    "emoji_updated": {"emoji": "😃", "color": discord.Color.blurple()},
    "sticker_updated": {"emoji": "🏷️", "color": discord.Color.blurple()},
    "integrations_updated": {"emoji": "🧩", "color": discord.Color.blurple()},
    "webhooks_updated": {"emoji": "🪝", "color": discord.Color.blurple()},
    "thread_created": {"emoji": "🧵", "color": discord.Color.green()},
    "thread_deleted": {"emoji": "🧵", "color": discord.Color.red()},
    "thread_updated": {"emoji": "🧵", "color": discord.Color.blurple()},
    "invite_created": {"emoji": "🔗", "color": discord.Color.green()},
    "invite_deleted": {"emoji": "❌", "color": discord.Color.red()},
    "member_joined": {"emoji": "➕", "color": discord.Color.green()},
    "member_left": {"emoji": "➖", "color": discord.Color.red()},
    "roles_changed": {"emoji": "🎭", "color": discord.Color.blurple()},
    "nick_changed": {"emoji": "✍️", "color": discord.Color.blurple()},
    "timeout_updated": {"emoji": "⏳", "color": discord.Color.orange()},
    "user_banned": {"emoji": "🔨", "color": discord.Color.dark_red()},
    "user_unbanned": {"emoji": "✅", "color": discord.Color.green()},
    "voice_join": {"emoji": "🎤", "color": discord.Color.green()},
    "voice_move": {"emoji": "🎤", "color": discord.Color.blurple()},
    "voice_leave": {"emoji": "🎤", "color": discord.Color.red()},
    "voice_mute": {"emoji": "🔇", "color": discord.Color.orange()},
    "voice_deaf": {"emoji": "🙉", "color": discord.Color.orange()},
    "voice_video": {"emoji": "🎥", "color": discord.Color.blurple()},
    "voice_stream": {"emoji": "📺", "color": discord.Color.blurple()},
    "sched_created": {"emoji": "📅", "color": discord.Color.green()},
    "sched_updated": {"emoji": "📅", "color": discord.Color.blurple()},
    "sched_deleted": {"emoji": "📅", "color": discord.Color.red()},
    "sched_user_add": {"emoji": "➕", "color": discord.Color.green()},
    "sched_user_rem": {"emoji": "➖", "color": discord.Color.red()},
    "cmd_thisbot": {"emoji": "🤖", "color": discord.Color.green()},
    "cmd_otherbot": {"emoji": "🤖", "color": discord.Color.blurple()},
}

# Generic UI emojis (for embeds, not events)
_UI = {
    "core": "🛠️",
    "style": "🎨",
    "ctrlz": "⏪",
    "toggles": "🎚️",
    "diag": "🧪",
    "ok": "✅",
    "warn": "⚠️",
}

# ========================= Defaults =========================
DEFAULTS_GUILD = {
    "log_channel": None,
    "fast_logs": True,
    "overrides": {},
    "message": {"edit": True, "delete": True, "bulk_delete": True, "pins": True, "exempt_channels": []},
    "reactions": {"add": True, "remove": True, "clear": True},
    "server": {
        "channel_create": True,
        "channel_delete": True,
        "channel_update": True,
        "role_create": True,
        "role_delete": True,
        "role_update": True,
        "server_update": True,
        "emoji_update": True,
        "sticker_update": True,
        "integrations_update": True,
        "webhooks_update": True,
        "thread_create": True,
        "thread_delete": True,
        "thread_update": True,
        "exempt_channels": [],
    },
    "invites": {"create": True, "delete": True},
    "member": {
        "join": True,
        "leave": True,
        "roles_changed": True,
        "nick_changed": True,
        "ban": True,
        "unban": True,
        "timeout": True,
        "presence": True,
    },
    "voice": {"join": True, "move": True, "leave": True, "mute": True, "deaf": True, "video": True, "stream": True},
    "sched": {"create": True, "update": True, "delete": True, "user_add": True, "user_remove": True},
    "commands": {"this_bot": True, "other_bots": True},
    "rate": {"seconds": 2.0},
    "style": {"compact": True},
    # CTRL-Z snapshot cache (short-lived)
    "ctrlz": {
        "ttl": 86400,  # seconds
        "channels": {},  # {channel_id: {fields..., ts}}
        "deleted_channels": {},  # ditto
        "roles": {},  # {role_id: {fields..., ts}}
        "deleted_roles": {},  # ditto
        "members": {},  # {member_id: {nick, roles:[ids], ts}}
    },
}


class LogPlus(redcommands.Cog):
    """Power logging + lightweight CTRL-Z (revert) for server changes."""

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0x51A7E11, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)

        self._last_event_at: Dict[str, float] = {}
        self._cmd_prefix_re = re.compile(r"^(<@!?|[/!?.~+\-$&%=>:#])")

    # ---------------- helpers ----------------
    @staticmethod
    def _now():
        return discord.utils.utcnow()

    @staticmethod
    def _onoff(v: bool) -> str:
        return "on" if v else "off"

    @staticmethod
    def _yn(v: bool) -> str:
        return "✅" if v else "❌"

    async def _is_compact(self, guild: discord.Guild) -> bool:
        try:
            return bool(await self.config.guild(guild).style.compact())
        except Exception:
            return True

    def _mk_embed(
        self,
        title: str,
        description: Optional[str] = None,
        *,
        color: Optional[discord.Color] = None,
        footer: Optional[str] = None,
        etype: Optional[str] = None,
        compact: bool = True,
    ) -> discord.Embed:
        style = EVENT_STYLE.get(etype or "", {})
        if compact and style.get("emoji"):
            title = f"{style['emoji']} {title}"
        if color is None and style.get("color"):
            color = style["color"]  # consistent visual look per event
        e = discord.Embed(
            title=title, description=description, color=color or discord.Color.blurple(), timestamp=self._now()
        )
        if footer:
            e.set_footer(text=footer)
        return e

    async def _E(
        self, guild: discord.Guild, title: str, description: Optional[str] = None, *, color=None, footer=None, etype=None
    ) -> discord.Embed:
        return self._mk_embed(
            title, description, color=color, footer=footer, etype=etype, compact=await self._is_compact(guild)
        )

    async def _log_channel(
        self, guild: discord.Guild, source_channel_id: Optional[int] = None
    ) -> Optional[discord.TextChannel]:
        if source_channel_id is not None:
            overrides = await self.config.guild(guild).overrides()
            if isinstance(overrides, dict):
                dest_id = overrides.get(str(source_channel_id)) or overrides.get(int(source_channel_id))
                if dest_id:
                    ch = guild.get_channel(int(dest_id))
                    if isinstance(ch, discord.TextChannel):
                        return ch
        cid = await self.config.guild(guild).log_channel()
        ch = guild.get_channel(cid) if cid else None
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _is_exempt(self, guild: discord.Guild, channel_id: Optional[int], group: str) -> bool:
        if not channel_id:
            return False
        ex = await getattr(self.config.guild(guild), group).exempt_channels()
        return int(channel_id) in set(ex)

    async def _send(self, guild: discord.Guild, embed: discord.Embed, source_channel_id: Optional[int] = None):
        ch = await self._log_channel(guild, source_channel_id)
        if not ch:
            return
        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            pass

    async def _audit_actor(
        self, guild: discord.Guild, action: discord.AuditLogAction, target_id: Optional[int] = None
    ) -> Optional[str]:
        # Simple "latest" matcher
        try:
            async for entry in guild.audit_logs(limit=5, action=action):
                if target_id is None or (getattr(entry.target, "id", None) == target_id):
                    return f"{entry.user} ({entry.user.id})"
        except Exception:
            return None
        return None

    async def _audit_actor_recent(
        self,
        guild: discord.Guild,
        actions,
        *,
        target_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        lookback_s: int = 60,
    ) -> Optional[str]:
        """
        Best-effort: find a recent audit entry within lookback_s seconds that matches any of `actions`,
        and optionally the target and channel. Returns "User (ID)" or None.
        """
        if not isinstance(actions, (list, tuple, set)):
            actions = [actions]
        actions = [a for a in actions if a is not None]
        if not actions:
            return None
        after = discord.utils.utcnow() - timedelta(seconds=max(1, lookback_s))
        try:
            async for entry in guild.audit_logs(limit=25, after=after):
                if entry.action not in actions:
                    continue
                if target_id is not None and getattr(entry.target, "id", None) != target_id:
                    continue
                if channel_id is not None:
                    extra_ch = getattr(getattr(entry, "extra", None), "channel", None)
                    if getattr(extra_ch, "id", None) != channel_id:
                        continue
                return f"{entry.user} ({entry.user.id})"
        except Exception:
            return None
        return None

    async def _rate_seconds(self, guild: discord.Guild) -> float:
        r = await self.config.guild(guild).rate.seconds()
        try:
            return max(0.0, float(r))
        except Exception:
            return 2.0

    def _should_suppress(self, key: str, window_s: float) -> bool:
        now = time.monotonic()
        prev = self._last_event_at.get(key, 0.0)
        if now - prev < window_s:
            return True
        self._last_event_at[key] = now
        return False

    # ---------- pretty status ----------
    async def _status_embed(self, guild: discord.Guild) -> discord.Embed:
        g = await self.config.guild(guild).all()
        log_ch = guild.get_channel(g["log_channel"]) if g["log_channel"] else None
        e = discord.Embed(
            title="LogPlus — Status", description="Event logging + CTRL-Z snapshots.", color=discord.Color.blurple(), timestamp=self._now()
        )
        e.add_field(
            name=f"{_UI['core']} Core",
            value=box(
                f"log_channel   = {getattr(log_ch, 'mention', 'not set')}\n"
                f"rate          = {g['rate']['seconds']}s\n"
                f"style.compact = {g['style']['compact']}",
                lang="ini",
            ),
            inline=False,
        )
        e.add_field(
            name="Message",
            value=box(
                f"edit={self._onoff(g['message']['edit'])} "
                f"delete={self._onoff(g['message']['delete'])} "
                f"bulk={self._onoff(g['message']['bulk_delete'])} "
                f"pins={self._onoff(g['message']['pins'])} "
                f"exempt={len(g['message']['exempt_channels'])}",
                lang="ini",
            ),
            inline=False,
        )
        e.add_field(
            name="Reactions",
            value=box(
                f"add={self._onoff(g['reactions']['add'])} "
                f"remove={self._onoff(g['reactions']['remove'])} "
                f"clear={self._onoff(g['reactions']['clear'])}",
                lang="ini",
            ),
            inline=True,
        )
        e.add_field(
            name="Server",
            value=box(
                f"channels c/d/u={[g['server']['channel_create'], g['server']['channel_delete'], g['server']['channel_update']]}\n"
                f"roles    c/d/u={[g['server']['role_create'], g['server']['role_delete'], g['server']['role_update']]}\n"
                f"emoji={g['server']['emoji_update']} sticker={g['server']['sticker_update']} integ={g['server']['integrations_update']}\n"
                f"webhooks={g['server']['webhooks_update']} threads c/d/u={[g['server']['thread_create'], g['server']['thread_delete'], g['server']['thread_update']]}\n"
                f"exempt={len(g['server']['exempt_channels'])}",
                lang="ini",
            ),
            inline=False,
        )
        e.add_field(
            name="Member/Voice/Sched/Commands",
            value=box(
                f"member: join={g['member']['join']} leave={g['member']['leave']} roles={g['member']['roles_changed']} nick={g['member']['nick_changed']} "
                f"ban={g['member']['ban']} unban={g['member']['unban']} timeout={g['member']['timeout']} presence={g['member']['presence']}\n"
                f"voice: join={g['voice']['join']} move={g['voice']['move']} leave={g['voice']['leave']} mute={g['voice']['mute']} "
                f"deaf={g['voice']['deaf']} video={g['voice']['video']} stream={g['voice']['stream']}\n"
                f"sched: create={g['sched']['create']} update={g['sched']['update']} delete={g['sched']['delete']} user_add={g['sched']['user_add']} user_remove={g['sched']['user_remove']}\n"
                f"cmds: this_bot={g['commands']['this_bot']} other_bots={g['commands']['other_bots']}",
                lang="ini",
            ),
            inline=False,
        )
        e.set_footer(text="Use [p]logplus help for commands.")
        return e

    # ---------------- CTRL-Z snapshot helpers ----------------
    async def _ctrlz_ttl(self, guild: discord.Guild) -> int:
        return int(await self.config.guild(guild).ctrlz.ttl())

    async def _ctrlz_prune(self, guild: discord.Guild) -> None:
        """Remove expired snapshots."""
        ttl = await self._ctrlz_ttl(guild)
        cutoff = time.time() - ttl
        gconf = self.config.guild(guild).ctrlz

        async def prune_dict(path):
            data = await path()
            if not isinstance(data, dict):
                return
            removed = [k for k, v in data.items() if not isinstance(v, dict) or v.get("ts", 0) < cutoff]
            for k in removed:
                data.pop(k, None)
            await path.set(data)

        await prune_dict(gconf.channels)
        await prune_dict(gconf.roles)
        await prune_dict(gconf.members)
        await prune_dict(gconf.deleted_channels)
        await prune_dict(gconf.deleted_roles)

    # snapshot builders
    @staticmethod
    def _snap_channel(ch: discord.abc.GuildChannel) -> Dict[str, object]:
        base = {
            "id": ch.id,
            "type": int(getattr(ch, "type", discord.ChannelType.text).value),
            "name": getattr(ch, "name", None),
            "position": getattr(ch, "position", None),
            "parent_id": getattr(ch, "category_id", None),
            "ts": time.time(),
        }
        # text
        if isinstance(ch, (discord.TextChannel, discord.Thread, discord.StageChannel, discord.ForumChannel)):
            base["nsfw"] = getattr(ch, "nsfw", None)
            base["topic"] = getattr(ch, "topic", None)
            base["slowmode"] = getattr(ch, "slowmode_delay", getattr(ch, "rate_limit_per_user", None))
        # voice
        if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            base["bitrate"] = getattr(ch, "bitrate", None)
            base["user_limit"] = getattr(ch, "user_limit", None)
        return base

    @staticmethod
    def _snap_role(r: discord.Role) -> Dict[str, object]:
        return {
            "id": r.id,
            "name": r.name,
            "color": r.color.value,
            "permissions": r.permissions.value,
            "hoist": r.hoist,
            "mentionable": r.mentionable,
            "position": r.position,
            "ts": time.time(),
        }

    @staticmethod
    def _snap_member(m: discord.Member) -> Dict[str, object]:
        return {"id": m.id, "nick": m.nick, "roles": [role.id for role in m.roles if not role.is_default()], "ts": time.time()}

    # ---------------- commands: main & settings ----------------
    @redcommands.group(name="logplus", invoke_without_command=True)
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def logplus(self, ctx: redcommands.Context):
        await ctx.send(embed=await self._status_embed(ctx.guild))

    @logplus.command(name="help")
    async def help_(self, ctx: redcommands.Context):
        p = ctx.clean_prefix
        e = discord.Embed(title="LogPlus — Commands", color=discord.Color.blurple())
        e.add_field(
            name=f"{_UI['core']} Core",
            value=(
                f"• `{p}logplus` • `{p}logplus help` • `{p}logplus diag`\n"
                f"• `{p}logplus rate [seconds]`\n"
                f"• `{p}logplus style compact <on|off>` • `{p}logplus style preview`"
            ),
            inline=False,
        )
        e.add_field(
            name=f"{_UI['ctrlz']} CTRL-Z",
            value=(
                f"• `{p}logplus ctrlz ttl [seconds]` • `{p}logplus ctrlz list`\n"
                f"• `{p}logplus ctrlz nick @user` • `{p}logplus ctrlz roles @user`\n"
                f"• `{p}logplus ctrlz channel #channel` • `{p}logplus ctrlz role @role`\n"
                f"• `{p}logplus ctrlz recreatechannel <id>` • `{p}logplus ctrlz recreaterole <id>`"
            ),
            inline=False,
        )
        e.add_field(
            name=f"{_UI['toggles']} Toggles",
            value=(
                f"• `{p}logplus toggle message <edit|delete|bulk|pins>`\n"
                f"• `{p}logplus toggle reactions <add|remove|clear>`\n"
                f"• `{p}logplus toggle server <channelcreate|channeldelete|channelupdate|rolecreate|roledelete|roleupdate|serverupdate|emojiupdate|stickerupdate|integrationsupdate|webhooksupdate|threadcreate|threaddelete|thredupdate>`\n"
                f"• `{p}logplus toggle invites <create|delete>`\n"
                f"• `{p}logplus toggle member <join|leave|roles|nick|ban|unban|timeout|presence>`\n"
                f"• `{p}logplus toggle voice <join|move|leave|mute|deaf|video|stream>`\n"
                f"• `{p}logplus toggle commands <thisbot|otherbots>`"
            ),
            inline=False,
        )
        e.add_field(
            name=f"{_UI['diag']} Notes",
            value="Route destination uses the configured log channel; per-channel overrides are respected if present.",
            inline=False,
        )
        await ctx.send(embed=e)

    @logplus.command(name="rate")
    async def cmd_rate(self, ctx: redcommands.Context, seconds: Optional[float] = None):
        if seconds is None:
            cur = await self._rate_seconds(ctx.guild)
            return await ctx.send(embed=await self._E(ctx.guild, "Rate limit", f"Current window: **{cur:.2f}s**"))
        if seconds < 0:
            return await ctx.send(
                embed=await self._E(ctx.guild, "Rate limit", f"{_UI['warn']} Seconds must be ≥ 0.", color=discord.Color.orange())
            )
        await self.config.guild(ctx.guild).rate.seconds.set(float(seconds))
        await ctx.send(embed=await self._E(ctx.guild, "Rate limit", f"{_UI['ok']} Window set to **{seconds:.2f}s**"))

    @logplus.group(name="style")
    async def style(self, ctx: redcommands.Context):
        pass

    @style.command(name="compact")
    async def style_compact(self, ctx: redcommands.Context, flag: Optional[str] = None):
        if flag is None:
            cur = await self.config.guild(ctx.guild).style.compact()
            return await ctx.send(
                embed=await self._E(ctx.guild, "Style: compact", f"Compact style is **{'ON' if cur else 'OFF'}**.")
            )
        flag = flag.lower()
        if flag not in {"on", "off"}:
            return await ctx.send(
                embed=await self._E(ctx.guild, "Style: compact", "Use `on` or `off`.", color=discord.Color.orange())
            )
        await self.config.guild(ctx.guild).style.compact.set(flag == "on")
        await ctx.send(embed=await self._E(ctx.guild, "Style: compact", f"Compact style **{flag.upper()}**."))

    @style.command(name="preview")
    async def style_preview(self, ctx: redcommands.Context):
        samples = [("Message deleted", "message_deleted"), ("Reaction added", "reaction_added"), ("Channel created", "channel_created")]
        for title, etype in samples:
            await ctx.send(embed=await self._E(ctx.guild, title, etype=etype))

    # ---------------- CTRL-Z commands ----------------
    @logplus.group(name="ctrlz")
    async def ctrlz(self, ctx: redcommands.Context):
        """Revert actions (admin)."""
        pass

    @ctrlz.command(name="ttl")
    async def ctrlz_ttl(self, ctx: redcommands.Context, seconds: Optional[int] = None):
        """Get/set snapshot TTL (seconds)."""
        if seconds is None:
            ttl = await self._ctrlz_ttl(ctx.guild)
            return await ctx.send(embed=await self._E(ctx.guild, "CTRL-Z TTL", f"Snapshot TTL: **{ttl}s**."))
        if seconds < 60:
            return await ctx.send(
                embed=await self._E(ctx.guild, "CTRL-Z TTL", "TTL must be ≥ 60s.", color=discord.Color.orange())
            )
        await self.config.guild(ctx.guild).ctrlz.ttl.set(int(seconds))
        await ctx.send(embed=await self._E(ctx.guild, "CTRL-Z TTL", f"{_UI['ok']} TTL set to **{seconds}s**."))

    @ctrlz.command(name="list")
    async def ctrlz_list(self, ctx: redcommands.Context):
        """List recently deleted channels/roles that can be recreated."""
        await self._ctrlz_prune(ctx.guild)
        g = self.config.guild(ctx.guild).ctrlz
        dch = await g.deleted_channels()
        drl = await g.deleted_roles()

        ch_lines = []
        if dch:
            for cid, snap in dch.items():
                ch_lines.append(f"- #{snap.get('name', '?')} (id {cid}) type={snap.get('type')}")
        else:
            ch_lines.append("- none")

        rl_lines = []
        if drl:
            for rid, snap in drl.items():
                rl_lines.append(f"- {snap.get('name', '?')} (id {rid})")
        else:
            rl_lines.append("- none")

        e = discord.Embed(title="CTRL-Z Available Restores", color=discord.Color.blurple(), timestamp=self._now())
        e.add_field(name="Deleted channels", value=box("\n".join(ch_lines), lang="ini"), inline=False)
        e.add_field(name="Deleted roles", value=box("\n".join(rl_lines), lang="ini"), inline=False)
        await ctx.send(embed=e)

    @ctrlz.command(name="nick")
    async def ctrlz_nick(self, ctx: redcommands.Context, member: discord.Member):
        """Restore member's previous nickname from snapshot."""
        await self._ctrlz_prune(ctx.guild)
        snaps = await self.config.guild(ctx.guild).ctrlz.members()
        snap = snaps.get(str(member.id))
        if not snap or "nick" not in snap:
            return await ctx.send(
                embed=await self._E(ctx.guild, "CTRL-Z Nick", "No snapshot for that member.", color=discord.Color.orange())
            )
        try:
            await member.edit(nick=snap["nick"])
            await ctx.send(
                embed=await self._E(ctx.guild, "CTRL-Z Nick", f"Restored nickname for **{member}** to **{snap['nick'] or 'None'}**.")
            )
        except discord.Forbidden:
            await ctx.send(
                embed=await self._E(ctx.guild, "CTRL-Z Nick", "I lack permission to change that member's nick.", color=discord.Color.red())
            )

    @ctrlz.command(name="roles")
    async def ctrlz_roles(self, ctx: redcommands.Context, member: discord.Member):
        """Restore member's previous role set."""
        await self._ctrlz_prune(ctx.guild)
        snaps = await self.config.guild(ctx.guild).ctrlz.members()
        snap = snaps.get(str(member.id))
        if not snap or "roles" not in snap:
            return await ctx.send(
                embed=await self._E(ctx.guild, "CTRL-Z Roles", "No snapshot for that member.", color=discord.Color.orange())
            )
        roles: List[discord.Role] = []
        for rid in snap["roles"]:
            r = ctx.guild.get_role(int(rid))
            if r:
                roles.append(r)
        try:
            await member.edit(roles=roles)
            await ctx.send(embed=await self._E(ctx.guild, "CTRL-Z Roles", f"Restored roles for **{member}**."))
        except discord.Forbidden:
            await ctx.send(
                embed=await self._E(ctx.guild, "CTRL-Z Roles", "I lack permission to edit that member's roles.", color=discord.Color.red())
            )

    @ctrlz.command(name="channel")
    async def ctrlz_channel(self, ctx: redcommands.Context, channel: discord.abc.GuildChannel):
        """Revert last channel update (name/topic/nsfw/slowmode/parent/voice settings/position)."""
        await self._ctrlz_prune(ctx.guild)
        snaps = await self.config.guild(ctx.guild).ctrlz.channels()
        snap = snaps.get(str(channel.id))
        if not snap:
            return await ctx.send(
                embed=await self._E(ctx.guild, "CTRL-Z Channel", "No snapshot for that channel.", color=discord.Color.orange())
            )
        kwargs = {}
        if "name" in snap:
            kwargs["name"] = snap["name"]
        if "topic" in snap and hasattr(channel, "topic"):
            kwargs["topic"] = snap["topic"]
        if "nsfw" in snap and hasattr(channel, "nsfw"):
            kwargs["nsfw"] = snap["nsfw"]
        if "slowmode" in snap and hasattr(channel, "slowmode_delay"):
            kwargs["slowmode_delay"] = snap["slowmode"]
        if "parent_id" in snap:
            parent = ctx.guild.get_channel(snap["parent_id"]) if snap["parent_id"] else None
            if isinstance(parent, discord.CategoryChannel):
                kwargs["category"] = parent
            elif snap["parent_id"] is None:
                kwargs["category"] = None
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            if "bitrate" in snap:
                kwargs["bitrate"] = snap["bitrate"]
            if "user_limit" in snap:
                kwargs["user_limit"] = snap["user_limit"]
        try:
            await channel.edit(**kwargs)
            if "position" in snap and snap["position"] is not None:
                await channel.edit(position=int(snap["position"]))
            await ctx.send(embed=await self._E(ctx.guild, "CTRL-Z Channel", f"Reverted settings for {channel.mention}."))
        except discord.Forbidden:
            await ctx.send(
                embed=await self._E(ctx.guild, "CTRL-Z Channel", "I lack permission to edit that channel.", color=discord.Color.red())
            )

    @ctrlz.command(name="role")
    async def ctrlz_role(self, ctx: redcommands.Context, role: discord.Role):
        """Revert last role update (name/color/perms/hoist/mentionable/position)."""
        await self._ctrlz_prune(ctx.guild)
        snaps = await self.config.guild(ctx.guild).ctrlz.roles()
        snap = snaps.get(str(role.id))
        if not snap:
            return await ctx.send(
                embed=await self._E(ctx.guild, "CTRL-Z Role", "No snapshot for that role.", color=discord.Color.orange())
            )
        kwargs = {
            "name": snap.get("name", role.name),
            "colour": discord.Colour(int(snap.get("color", role.color.value))),
            "permissions": discord.Permissions(int(snap.get("permissions", role.permissions.value))),
            "hoist": bool(snap.get("hoist", role.hoist)),
            "mentionable": bool(snap.get("mentionable", role.mentionable)),
        }
        try:
            await role.edit(**kwargs)
            if "position" in snap and snap["position"] is not None:
                await role.edit(position=int(snap["position"]))
            await ctx.send(embed=await self._E(ctx.guild, "CTRL-Z Role", f"Reverted settings for role **{role.name}**."))
        except discord.Forbidden:
            await ctx.send(
                embed=await self._E(ctx.guild, "CTRL-Z Role", "I lack permission to edit that role.", color=discord.Color.red())
            )

    @ctrlz.command(name="recreatechannel")
    async def ctrlz_recreate_channel(self, ctx: redcommands.Context, channel_id: int):
        """Recreate a deleted channel by ID (from snapshot)."""
        await self._ctrlz_prune(ctx.guild)
        snaps = await self.config.guild(ctx.guild).ctrlz.deleted_channels()
        snap = snaps.get(str(channel_id))
        if not snap:
            return await ctx.send(
                embed=await self._E(ctx.guild, "CTRL-Z Recreate Channel", "No snapshot for that channel ID.", color=discord.Color.orange())
            )
        ctype = discord.ChannelType(int(snap.get("type", discord.ChannelType.text.value)))
        name = snap.get("name", "restored-channel")
        parent = ctx.guild.get_channel(snap.get("parent_id")) if snap.get("parent_id") else None
        try:
            if ctype in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                new = await ctx.guild.create_voice_channel(
                    name, category=parent if isinstance(parent, discord.CategoryChannel) else None
                )
                if "bitrate" in snap:
                    await new.edit(bitrate=snap["bitrate"])
                if "user_limit" in snap:
                    await new.edit(user_limit=snap["user_limit"])
            else:
                new = await ctx.guild.create_text_channel(
                    name, category=parent if isinstance(parent, discord.CategoryChannel) else None, nsfw=snap.get("nsfw")
                )
                if "topic" in snap:
                    await new.edit(topic=snap["topic"])
                if "slowmode" in snap:
                    await new.edit(slowmode_delay=snap["slowmode"])
            if "position" in snap and snap["position"] is not None:
                await new.edit(position=int(snap["position"]))
            await ctx.send(embed=await self._E(ctx.guild, "CTRL-Z Recreate Channel", f"Recreated channel {new.mention}."))
        except discord.Forbidden:
            await ctx.send(
                embed=await self._E(
                    ctx.guild, "CTRL-Z Recreate Channel", "I lack permission to create channels here.", color=discord.Color.red()
                )
            )

    @ctrlz.command(name="recreaterole")
    async def ctrlz_recreate_role(self, ctx: redcommands.Context, role_id: int):
        """Recreate a deleted role by ID (from snapshot)."""
        await self._ctrlz_prune(ctx.guild)
        snaps = await self.config.guild(ctx.guild).ctrlz.deleted_roles()
        snap = snaps.get(str(role_id))
        if not snap:
            return await ctx.send(
                embed=await self._E(ctx.guild, "CTRL-Z Recreate Role", "No snapshot for that role ID.", color=discord.Color.orange())
            )
        try:
            new = await ctx.guild.create_role(
                name=snap.get("name", "restored-role"),
                colour=discord.Colour(int(snap.get("color", 0))),
                permissions=discord.Permissions(int(snap.get("permissions", 0))),
                hoist=bool(snap.get("hoist", False)),
                mentionable=bool(snap.get("mentionable", False)),
            )
            if "position" in snap and snap["position"] is not None:
                await new.edit(position=int(snap["position"]))
            await ctx.send(embed=await self._E(ctx.guild, "CTRL-Z Recreate Role", f"Recreated role **{new.name}**."))
        except discord.Forbidden:
            await ctx.send(
                embed=await self._E(
                    ctx.guild, "CTRL-Z Recreate Role", "I lack permission to create roles here.", color=discord.Color.red()
                )
            )

    # ---------------- toggles (unchanged API) ----------------
    @logplus.group()
    async def toggle(self, ctx: redcommands.Context):
        pass

    async def _flip(self, ctx: redcommands.Context, group: str, key: str):
        section = getattr(self.config.guild(ctx.guild), group)
        value = section.get_attr(key)
        cur = await value()
        await value.set(not cur)
        await ctx.send(embed=await self._E(ctx.guild, "Toggle", f"{group}.{key} → **{self._onoff(not cur)}**"))

    # message toggles
    @toggle.group()
    async def message(self, ctx: redcommands.Context):
        pass

    @message.command()
    async def edit(self, ctx: redcommands.Context):
        await self._flip(ctx, "message", "edit")

    @message.command()
    async def delete(self, ctx: redcommands.Context):
        await self._flip(ctx, "message", "delete")

    @message.command(name="bulk")
    async def message_bulk(self, ctx: redcommands.Context):
        await self._flip(ctx, "message", "bulk_delete")

    @message.command()
    async def pins(self, ctx: redcommands.Context):
        await self._flip(ctx, "message", "pins")

    # reactions toggles
    @toggle.group()
    async def reactions(self, ctx: redcommands.Context):
        pass

    @reactions.command(name="add")
    async def react_add(self, ctx: redcommands.Context):
        await self._flip(ctx, "reactions", "add")

    @reactions.command(name="remove")
    async def react_remove(self, ctx: redcommands.Context):
        await self._flip(ctx, "reactions", "remove")

    @reactions.command(name="clear")
    async def react_clear(self, ctx: redcommands.Context):
        await self._flip(ctx, "reactions", "clear")

    # server toggles
    @toggle.group()
    async def server(self, ctx: redcommands.Context):
        pass

    @server.command(name="channelcreate")
    async def t_sc_create(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "channel_create")

    @server.command(name="channeldelete")
    async def t_sc_delete(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "channel_delete")

    @server.command(name="channelupdate")
    async def t_sc_update(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "channel_update")

    @server.command(name="rolecreate")
    async def t_sr_create(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "role_create")

    @server.command(name="roledelete")
    async def t_sr_delete(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "role_delete")

    @server.command(name="roleupdate")
    async def t_sr_update(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "role_update")

    @server.command(name="serverupdate")
    async def t_s_update(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "server_update")

    @server.command(name="emojiupdate")
    async def t_e_update(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "emoji_update")

    @server.command(name="stickerupdate")
    async def t_st_update(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "sticker_update")

    @server.command(name="integrationsupdate")
    async def t_i_update(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "integrations_update")

    @server.command(name="webhooksupdate")
    async def t_w_update(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "webhooks_update")

    @server.command(name="threadcreate")
    async def t_tc(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "thread_create")

    @server.command(name="threaddelete")
    async def t_td(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "thread_delete")

    @server.command(name="thredupdate")
    async def t_tu(self, ctx: redcommands.Context):
        await self._flip(ctx, "server", "thread_update")

    # invites toggles
    @toggle.group()
    async def invites(self, ctx: redcommands.Context):
        pass

    @invites.command(name="create")
    async def t_inv_c(self, ctx: redcommands.Context):
        await self._flip(ctx, "invites", "create")

    @invites.command(name="delete")
    async def t_inv_d(self, ctx: redcommands.Context):
        await self._flip(ctx, "invites", "delete")

    # member toggles
    @toggle.group()
    async def member(self, ctx: redcommands.Context):
        pass

    @member.command(name="join")
    async def t_m_join(self, ctx: redcommands.Context):
        await self._flip(ctx, "member", "join")

    @member.command(name="leave")
    async def t_m_leave(self, ctx: redcommands.Context):
        await self._flip(ctx, "member", "leave")

    @member.command(name="roles")
    async def t_m_roles(self, ctx: redcommands.Context):
        await self._flip(ctx, "member", "roles_changed")

    @member.command(name="nick")
    async def t_m_nick(self, ctx: redcommands.Context):
        await self._flip(ctx, "member", "nick_changed")

    @member.command(name="ban")
    async def t_m_ban(self, ctx: redcommands.Context):
        await self._flip(ctx, "member", "ban")

    @member.command(name="unban")
    async def t_m_unban(self, ctx: redcommands.Context):
        await self._flip(ctx, "member", "unban")

    @member.command(name="timeout")
    async def t_m_timeout(self, ctx: redcommands.Context):
        await self._flip(ctx, "member", "timeout")

    @member.command(name="presence")
    async def t_m_presence(self, ctx: redcommands.Context):
        await self._flip(ctx, "member", "presence")

    # voice toggles
    @toggle.group()
    async def voice(self, ctx: redcommands.Context):
        pass

    @voice.command(name="join")
    async def t_v_join(self, ctx: redcommands.Context):
        await self._flip(ctx, "voice", "join")

    @voice.command(name="move")
    async def t_v_move(self, ctx: redcommands.Context):
        await self._flip(ctx, "voice", "move")

    @voice.command(name="leave")
    async def t_v_leave(self, ctx: redcommands.Context):
        await self._flip(ctx, "voice", "leave")

    @voice.command(name="mute")
    async def t_v_mute(self, ctx: redcommands.Context):
        await self._flip(ctx, "voice", "mute")

    @voice.command(name="deaf")
    async def t_v_deaf(self, ctx: redcommands.Context):
        await self._flip(ctx, "voice", "deaf")

    @voice.command(name="video")
    async def t_v_video(self, ctx: redcommands.Context):
        await self._flip(ctx, "voice", "video")

    @voice.command(name="stream")
    async def t_v_stream(self, ctx: redcommands.Context):
        await self._flip(ctx, "voice", "stream")

    # commands toggles
    @toggle.group()
    async def commands_(self, ctx: redcommands.Context):
        """`[p]logplus toggle commands <thisbot|otherbots>`"""
        pass

    @commands_.command(name="thisbot")
    async def t_cmd_this(self, ctx: redcommands.Context):
        await self._flip(ctx, "commands", "this_bot")

    @commands_.command(name="otherbots")
    async def t_cmd_others(self, ctx: redcommands.Context):
        await self._flip(ctx, "commands", "other_bots")

    # ---------------- diagnostics ----------------
    @logplus.command(name="diag")
    async def diag(self, ctx: redcommands.Context):
        guild_conf = self.config.guild(ctx.guild)
        paths: Iterable[Tuple[str, str]] = []

        def add(group: str, keys: Iterable[str]):
            nonlocal paths
            paths += [(group, k) for k in keys]

        add("message", ["edit", "delete", "bulk_delete", "pins"])
        add("reactions", ["add", "remove", "clear"])
        add(
            "server",
            [
                "channel_create",
                "channel_delete",
                "channel_update",
                "role_create",
                "role_delete",
                "role_update",
                "server_update",
                "emoji_update",
                "sticker_update",
                "integrations_update",
                "webhooks_update",
                "thread_create",
                "thread_delete",
                "thread_update",
            ],
        )
        add("invites", ["create", "delete"])
        add("member", ["join", "leave", "roles_changed", "nick_changed", "ban", "unban", "timeout", "presence"])
        add("voice", ["join", "move", "leave", "mute", "deaf", "video", "stream"])
        add("sched", ["create", "update", "delete", "user_add", "user_remove"])
        add("commands", ["this_bot", "other_bots"])

        ok, bad = [], []
        for group, key in paths:
            section = getattr(guild_conf, group)
            attr = section.get_attr(key)
            try:
                cur = await attr()
                await attr.set(not cur)
                cur2 = await attr()
                if cur2 != (not cur):
                    bad.append(f"{group}.{key} (flip failed)")
                await attr.set(cur)
                ok.append(f"{group}.{key}")
            except Exception as e:
                bad.append(f"{group}.{key} ({type(e).__name__})")

        lines = ["[LogPlus Diagnostics]"]
        lines.append(f"OK ({len(ok)}): " + ", ".join(ok))
        if bad:
            lines.append("")
            lines.append(f"FAILED ({len(bad)}): " + ", ".join(bad))
        await ctx.send(embed=await self._E(ctx.guild, "Diagnostics", box("\n".join(lines), lang="ini")))

    # ---------------- listeners (timestamps everywhere) ----------------
    # snapshots for CTRL-Z are captured in relevant events below.

    # messages
    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not (before.guild and before.author) or before.author.bot:
            return
        g = await self.config.guild(before.guild).all()
        if not g["message"]["edit"] or before.content == after.content:
            return
        if await self._is_exempt(before.guild, before.channel.id, "message"):
            return
        e = await self._E(
            before.guild, "Message edited", etype="message_edited", footer=f"Author ID: {before.author.id}"
        )
        e.add_field(name="Author", value=f"{before.author} ({before.author.id})", inline=False)
        e.add_field(name="Channel", value=before.channel.mention, inline=True)
        e.add_field(name="By", value=f"{before.author} ({before.author.id})", inline=True)
        e.add_field(name="Jump", value=f"[link]({after.jump_url})", inline=True)
        e.add_field(name="Before", value=(before.content or "<empty>")[:1000], inline=False)
        e.add_field(name="After", value=(after.content or "<empty>")[:1000], inline=False)
        await self._send(before.guild, e, before.channel.id)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not (message.guild and message.author) or message.author.bot:
            return
        g = await self.config.guild(message.guild).all()
        if not g["message"]["delete"]:
            return
        if await self._is_exempt(message.guild, message.channel.id, "message"):
            return
        # Try to find the responsible actor (moderator/bot) via recent audit logs.
        actor = await self._audit_actor_recent(
            message.guild,
            [discord.AuditLogAction.message_delete, discord.AuditLogAction.message_bulk_delete],
            target_id=getattr(message.author, "id", None),
            channel_id=getattr(message.channel, "id", None),
            lookback_s=60,
        )
        e = await self._E(
            message.guild, "Message deleted", etype="message_deleted", footer=f"Author ID: {message.author.id}"
        )
        e.add_field(name="Author", value=f"{message.author} ({message.author.id})", inline=False)
        e.add_field(name="Channel", value=message.channel.mention, inline=True)
        e.add_field(name="By", value=actor or "Author / Unknown (no audit entry)", inline=True)
        if message.content:
            e.add_field(name="Content", value=message.content[:1500], inline=False)
        await self._send(message.guild, e, message.channel.id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        g = await self.config.guild(guild).all()
        if not g["message"]["bulk_delete"]:
            return
        if await self._is_exempt(guild, payload.channel_id, "message"):
            return
        ch = guild.get_channel(payload.channel_id)
        actor = await self._audit_actor_recent(
            guild,
            [discord.AuditLogAction.message_bulk_delete, discord.AuditLogAction.message_delete],
            channel_id=payload.channel_id,
            lookback_s=60,
        )
        e = await self._E(guild, "Bulk delete", description=f"{len(payload.message_ids)} messages", etype="bulk_delete")
        if isinstance(ch, discord.TextChannel):
            e.add_field(name="Channel", value=ch.mention, inline=True)
        e.add_field(name="By", value=actor or "Unknown (no audit entry)", inline=True)
        await self._send(guild, e, payload.channel_id)

    @commands.Cog.listener()
    async def on_guild_channel_pins_update(self, channel: discord.abc.GuildChannel, last_pin):
        guild = channel.guild
        g = await self.config.guild(guild).all()
        if not g["message"]["pins"]:
            return
        if await self._is_exempt(guild, channel.id, "message"):
            return
        actor = await self._audit_actor_recent(
            guild,
            [getattr(discord.AuditLogAction, "message_pin", None), getattr(discord.AuditLogAction, "message_unpin", None)],
            channel_id=getattr(channel, "id", None),
            lookback_s=60,
        )
        e = await self._E(
            guild, "Pins updated", description=f"#{getattr(channel, 'name', 'unknown')}", etype="pins_updated"
        )
        if last_pin:
            try:
                e.add_field(name="Last Pin", value=discord.utils.format_dt(last_pin, style="R"), inline=True)
            except Exception:
                pass
        e.add_field(name="By", value=actor or "Unknown (no audit entry)", inline=True)
        await self._send(guild, e, channel.id)

    # reactions
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        g = await self.config.guild(guild).all()
        if not g["reactions"]["add"]:
            return
        if await self._is_exempt(guild, payload.channel_id, "message"):
            return
        if self._should_suppress(
            f"react_add:{payload.guild_id}:{payload.channel_id}:{payload.message_id}:{str(payload.emoji)}",
            await self._rate_seconds(guild),
        ):
            return
        user = guild.get_member(payload.user_id)
        by = f"{user} ({user.id})" if user else f"{payload.user_id}"
        jump = f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}"
        e = await self._E(guild, "Reaction added", etype="reaction_added")
        e.add_field(name="Emoji", value=str(payload.emoji), inline=True)
        e.add_field(name="Message", value=f"[jump]({jump})", inline=True)
        e.add_field(name="By", value=by, inline=True)
        await self._send(guild, e, payload.channel_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        g = await self.config.guild(guild).all()
        if not g["reactions"]["remove"]:
            return
        if await self._is_exempt(guild, payload.channel_id, "message"):
            return
        if self._should_suppress(
            f"react_rm:{payload.guild_id}:{payload.channel_id}:{payload.message_id}:{str(payload.emoji)}",
            await self._rate_seconds(guild),
        ):
            return
        user = guild.get_member(payload.user_id)
        by = f"{user} ({user.id})" if user else f"{payload.user_id}"
        jump = f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}"
        e = await self._E(guild, "Reaction removed", etype="reaction_removed")
        e.add_field(name="Emoji", value=str(payload.emoji), inline=True)
        e.add_field(name="Message", value=f"[jump]({jump})", inline=True)
        e.add_field(name="By", value=by, inline=True)
        await self._send(guild, e, payload.channel_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        g = await self.config.guild(guild).all()
        if not g["reactions"]["clear"]:
            return
        if await self._is_exempt(guild, payload.channel_id, "message"):
            return
        jump = f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}"
        e = await self._E(guild, "Reactions cleared", etype="reaction_cleared")
        e.add_field(name="Message", value=f"[jump]({jump})", inline=True)
        e.add_field(name="By", value="Unknown (Discord does not audit reaction clears)", inline=True)
        await self._send(guild, e, payload.channel_id)

    # server structure (also snapshotting)
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        g = await self.config.guild(channel.guild).all()
        if g["server"]["channel_create"] and not await self._is_exempt(channel.guild, channel.id, "server"):
            actor = await self._audit_actor_recent(
                channel.guild, discord.AuditLogAction.channel_create, target_id=getattr(channel, "id", None)
            )
            e = await self._E(channel.guild, "Channel created", description=channel.mention, etype="channel_created")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(channel.guild, e, getattr(channel, "id", None))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        # snapshot for recreation
        snap = self._snap_channel(channel)
        data = await self.config.guild(channel.guild).ctrlz.deleted_channels()
        data[str(channel.id)] = snap
        await self.config.guild(channel.guild).ctrlz.deleted_channels.set(data)

        g = await self.config.guild(channel.guild).all()
        if g["server"]["channel_delete"] and not await self._is_exempt(channel.guild, channel.id, "server"):
            actor = await self._audit_actor_recent(
                channel.guild, discord.AuditLogAction.channel_delete, target_id=getattr(channel, "id", None)
            )
            e = await self._E(
                channel.guild, "Channel deleted", description=f"#{channel.name}", etype="channel_deleted"
            )
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(channel.guild, e, getattr(channel, "id", None))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        # snapshot previous state
        gconf = self.config.guild(after.guild).ctrlz
        data = await gconf.channels()
        data[str(after.id)] = self._snap_channel(before)
        await gconf.channels.set(data)

        g = await self.config.guild(after.guild).all()
        if g["server"]["channel_update"] and not await self._is_exempt(after.guild, after.id, "server"):
            actor = await self._audit_actor_recent(
                after.guild, discord.AuditLogAction.channel_update, target_id=getattr(after, "id", None)
            )
            e = await self._E(after.guild, "Channel updated", description=after.mention, etype="channel_updated")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(after.guild, e, getattr(after, "id", None))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        g = await self.config.guild(role.guild).all()
        if g["server"]["role_create"]:
            actor = await self._audit_actor_recent(
                role.guild, discord.AuditLogAction.role_create, target_id=getattr(role, "id", None)
            )
            e = await self._E(role.guild, "Role created", description=role.mention, etype="role_created")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(role.guild, e)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        # snapshot for recreation
        snap = self._snap_role(role)
        data = await self.config.guild(role.guild).ctrlz.deleted_roles()
        data[str(role.id)] = snap
        await self.config.guild(role.guild).ctrlz.deleted_roles.set(data)

        g = await self.config.guild(role.guild).all()
        if g["server"]["role_delete"]:
            actor = await self._audit_actor_recent(
                role.guild, discord.AuditLogAction.role_delete, target_id=getattr(role, "id", None)
            )
            e = await self._E(role.guild, "Role deleted", description=role.name, etype="role_deleted")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(role.guild, e)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        # snapshot previous state
        gconf = self.config.guild(after.guild).ctrlz
        data = await gconf.roles()
        data[str(after.id)] = self._snap_role(before)
        await gconf.roles.set(data)

        g = await self.config.guild(after.guild).all()
        if g["server"]["role_update"]:
            actor = await self._audit_actor_recent(
                after.guild, discord.AuditLogAction.role_update, target_id=getattr(after, "id", None)
            )
            e = await self._E(after.guild, "Role updated", description=after.mention, etype="role_updated")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(after.guild, e)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        g = await self.config.guild(after).all()
        if g["server"]["server_update"]:
            actor = await self._audit_actor_recent(after, discord.AuditLogAction.guild_update, lookback_s=60)
            e = await self._E(after, "Server updated", etype="server_updated")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(after, e)

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        g = await self.config.guild(guild).all()
        if g["server"]["emoji_update"]:
            actor = await self._audit_actor_recent(
                guild,
                [
                    getattr(discord.AuditLogAction, "emoji_create", None),
                    getattr(discord.AuditLogAction, "emoji_delete", None),
                    getattr(discord.AuditLogAction, "emoji_update", None),
                ],
                lookback_s=60,
            )
            e = await self._E(guild, "Emoji list updated", etype="emoji_updated")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: discord.Guild, before, after):
        g = await self.config.guild(guild).all()
        if g["server"]["sticker_update"]:
            actor = await self._audit_actor_recent(
                guild,
                [
                    getattr(discord.AuditLogAction, "sticker_create", None),
                    getattr(discord.AuditLogAction, "sticker_delete", None),
                    getattr(discord.AuditLogAction, "sticker_update", None),
                ],
                lookback_s=60,
            )
            e = await self._E(guild, "Sticker list updated", etype="sticker_updated")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_guild_integrations_update(self, guild: discord.Guild):
        g = await self.config.guild(guild).all()
        if g["server"]["integrations_update"]:
            actor = await self._audit_actor_recent(
                guild,
                [
                    getattr(discord.AuditLogAction, "integration_create", None),
                    getattr(discord.AuditLogAction, "integration_delete", None),
                    getattr(discord.AuditLogAction, "integration_update", None),
                ],
                lookback_s=60,
            )
            e = await self._E(guild, "Integrations updated", etype="integrations_updated")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel):
        g = await self.config.guild(channel.guild).all()
        if g["server"]["webhooks_update"] and not await self._is_exempt(channel.guild, channel.id, "server"):
            actor = await self._audit_actor_recent(
                channel.guild,
                [
                    getattr(discord.AuditLogAction, "webhook_create", None),
                    getattr(discord.AuditLogAction, "webhook_delete", None),
                    getattr(discord.AuditLogAction, "webhook_update", None),
                ],
                channel_id=getattr(channel, "id", None),
                lookback_s=60,
            )
            e = await self._E(
                channel.guild, "Webhooks updated", description=f"#{getattr(channel, 'name', 'unknown')}", etype="webhooks_updated"
            )
            e.add_field(name="By", value=actor or "Unknown (no audit entry)", inline=True)
            await self._send(channel.guild, e, channel.id)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        g = await self.config.guild(invite.guild).all()
        if g["invites"]["create"]:
            e = await self._E(invite.guild, "Invite created", etype="invite_created")
            e.add_field(name="Code", value=invite.code, inline=True)
            if invite.channel:
                e.add_field(name="Channel", value=invite.channel.mention, inline=True)
            inv = getattr(invite, "inviter", None)
            e.add_field(name="By", value=(f"{inv} ({inv.id})" if inv else "Unknown"), inline=True)
            await self._send(invite.guild, e, getattr(invite.channel, "id", None))

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        g = await self.config.guild(invite.guild).all()
        if g["invites"]["delete"]:
            actor = await self._audit_actor_recent(invite.guild, getattr(discord.AuditLogAction, "invite_delete", None), lookback_s=60)
            e = await self._E(invite.guild, "Invite deleted", etype="invite_deleted")
            e.add_field(name="Code", value=invite.code, inline=True)
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(invite.guild, e)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        g = await self.config.guild(member.guild).all()
        if g["member"]["join"]:
            e = await self._E(member.guild, "Member joined", description=f"{member} ({member.id})", etype="member_joined")
            e.add_field(name="By", value=f"{member} ({member.id})", inline=True)
            await self._send(member.guild, e)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        g = await self.config.guild(member.guild).all()
        if g["member"]["leave"]:
            e = await self._E(member.guild, "Member left", description=f"{member} ({member.id})", etype="member_left")
            e.add_field(name="By", value=f"{member} ({member.id})", inline=True)
            await self._send(member.guild, e)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        gconf = self.config.guild(after.guild).ctrlz
        data = await gconf.members()
        data[str(after.id)] = self._snap_member(before)
        await gconf.members.set(data)

        g = await self.config.guild(after.guild).all()
        # Nick change
        if before.nick != after.nick and g["member"]["nick_changed"]:
            actor = await self._audit_actor_recent(after.guild, discord.AuditLogAction.member_update, target_id=after.id)
            e = await self._E(after.guild, "Nickname changed", etype="nick_changed")
            e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
            e.add_field(name="Before", value=before.nick or "None", inline=True)
            e.add_field(name="After", value=after.nick or "None", inline=True)
            e.add_field(name="By", value=actor or f"{after} (self / unknown)", inline=True)
            await self._send(after.guild, e)
        # Roles changed
        if set(before.roles) != set(after.roles) and g["member"]["roles_changed"]:
            actor = await self._audit_actor_recent(after.guild, discord.AuditLogAction.member_role_update, target_id=after.id)
            e = await self._E(after.guild, "Roles changed", etype="roles_changed")
            e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(after.guild, e)
        # Timeout updated
        if g["member"]["timeout"] and before.timed_out_until != after.timed_out_until:
            actor = await self._audit_actor_recent(after.guild, discord.AuditLogAction.member_update, target_id=after.id)
            e = await self._E(after.guild, "Timeout updated", etype="timeout_updated")
            e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
            e.add_field(name="Until", value=str(after.timed_out_until), inline=True)
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(after.guild, e)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        g = await self.config.guild(guild).all()
        if g["member"]["ban"]:
            actor = await self._audit_actor_recent(guild, discord.AuditLogAction.ban, target_id=getattr(user, "id", None))
            e = await self._E(guild, "User banned", description=f"{user} ({user.id})", etype="user_banned")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        g = await self.config.guild(guild).all()
        if g["member"]["unban"]:
            actor = await self._audit_actor_recent(guild, discord.AuditLogAction.unban, target_id=getattr(user, "id", None))
            e = await self._E(guild, "User unbanned", description=f"{user} ({user.id})", etype="user_unbanned")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        g = await self.config.guild(member.guild).all()
        ch = await self._log_channel(member.guild)
        if not ch:
            return
        rate = await self._rate_seconds(member.guild)

        # Joins/Leaves/Moves (try to determine actor; otherwise member self)
        if before.channel is None and after.channel is not None and g["voice"]["join"]:
            actor = None  # joins are almost always self
            e = await self._E(
                member.guild, "Voice join", description=f"{member} → {after.channel.mention}", etype="voice_join"
            )
            e.add_field(name="By", value=f"{member} ({member.id})", inline=True)
            await ch.send(embed=e)
            return
        if before.channel is not None and after.channel is None and g["voice"]["leave"]:
            actor = await self._audit_actor_recent(
                member.guild, getattr(discord.AuditLogAction, "member_disconnect", None), target_id=member.id, lookback_s=30
            )
            e = await self._E(
                member.guild, "Voice leave", description=f"{member} ← {before.channel.mention}", etype="voice_leave"
            )
            e.add_field(name="By", value=actor or f"{member} (self)", inline=True)
            await ch.send(embed=e)
            return
        if before.channel and after.channel and before.channel.id != after.channel.id and g["voice"]["move"]:
            actor = await self._audit_actor_recent(
                member.guild, getattr(discord.AuditLogAction, "member_move", None), target_id=member.id, lookback_s=30
            )
            e = await self._E(
                member.guild,
                "Voice move",
                description=f"{member}: {before.channel.mention} → {after.channel.mention}",
                etype="voice_move",
            )
            e.add_field(name="By", value=actor or f"{member} (self)", inline=True)
            await ch.send(embed=e)

        # Server mute/deaf (might be by a mod)
        if g["voice"]["mute"] and (before.self_mute != after.self_mute or before.mute != after.mute):
            if not self._should_suppress(f"v_mute:{member.guild.id}:{member.id}", rate):
                actor = None
                if before.mute != after.mute:  # server mute toggled
                    actor = await self._audit_actor_recent(
                        member.guild, discord.AuditLogAction.member_update, target_id=member.id, lookback_s=30
                    )
                e = await self._E(member.guild, "Mute state change", description=str(member), etype="voice_mute")
                e.add_field(name="By", value=actor or f"{member} (self)", inline=True)
                await ch.send(embed=e)

        if g["voice"]["deaf"] and (before.self_deaf != after.self_deaf or before.deaf != after.deaf):
            if not self._should_suppress(f"v_deaf:{member.guild.id}:{member.id}", rate):
                actor = None
                if before.deaf != after.deaf:  # server deafen toggled
                    actor = await self._audit_actor_recent(
                        member.guild, discord.AuditLogAction.member_update, target_id=member.id, lookback_s=30
                    )
                e = await self._E(member.guild, "Deaf state change", description=str(member), etype="voice_deaf")
                e.add_field(name="By", value=actor or f"{member} (self)", inline=True)
                await ch.send(embed=e)

        # Self-only toggles (video/stream)
        if g["voice"]["video"] and (before.self_video != after.self_video):
            if not self._should_suppress(f"v_video:{member.guild.id}:{member.id}", rate):
                e = await self._E(
                    member.guild,
                    "Video state change",
                    description=f"{member} → {'on' if after.self_video else 'off'}",
                    etype="voice_video",
                )
                e.add_field(name="By", value=f"{member} (self)", inline=True)
                await ch.send(embed=e)
        if g["voice"]["stream"] and (before.self_stream != after.self_stream):
            if not self._should_suppress(f"v_stream:{member.guild.id}:{member.id}", rate):
                e = await self._E(
                    member.guild,
                    "Stream state change",
                    description=f"{member} → {'on' if after.self_stream else 'off'}",
                    etype="voice_stream",
                )
                e.add_field(name="By", value=f"{member} (self)", inline=True)
                await ch.send(embed=e)

    # scheduled events
    @commands.Cog.listener()
    async def on_guild_scheduled_event_create(self, event: discord.ScheduledEvent):
        g = await self.config.guild(event.guild).all()
        if g["sched"]["create"]:
            actor = await self._audit_actor_recent(
                event.guild, getattr(discord.AuditLogAction, "guild_scheduled_event_create", None), target_id=getattr(event, "id", None)
            )
            e = await self._E(event.guild, "Scheduled event created", description=event.name, etype="sched_created")
            e.add_field(name="By", value=actor or getattr(event, "creator", None) or "Unknown", inline=True)
            await self._send(event.guild, e)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent):
        g = await self.config.guild(after.guild).all()
        if g["sched"]["update"]:
            actor = await self._audit_actor_recent(
                after.guild, getattr(discord.AuditLogAction, "guild_scheduled_event_update", None), target_id=getattr(after, "id", None)
            )
            e = await self._E(after.guild, "Scheduled event updated", description=after.name, etype="sched_updated")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(after.guild, e)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_delete(self, event: discord.ScheduledEvent):
        g = await self.config.guild(event.guild).all()
        if g["sched"]["delete"]:
            actor = await self._audit_actor_recent(
                event.guild, getattr(discord.AuditLogAction, "guild_scheduled_event_delete", None), target_id=getattr(event, "id", None)
            )
            e = await self._E(event.guild, "Scheduled event deleted", description=event.name, etype="sched_deleted")
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(event.guild, e)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_user_add(self, event: discord.ScheduledEvent, user: discord.User):
        g = await self.config.guild(event.guild).all()
        if g["sched"]["user_add"]:
            e = await self._E(event.guild, "Event RSVP added", description=f"{user} → {event.name}", etype="sched_user_add")
            e.add_field(name="By", value=f"{user} ({user.id})", inline=True)
            await self._send(event.guild, e)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_user_remove(self, event: discord.ScheduledEvent, user: discord.User):
        g = await self.config.guild(event.guild).all()
        if g["sched"]["user_remove"]:
            e = await self._E(event.guild, "Event RSVP removed", description=f"{user} ✕ {event.name}", etype="sched_user_rem")
            e.add_field(name="By", value=f"{user} ({user.id})", inline=True)
            await self._send(event.guild, e)

    # commands
    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        guild = getattr(ctx, "guild", None)
        if not guild:
            return
        g = await self.config.guild(guild).all()
        if not g["commands"]["this_bot"]:
            return
        e = await self._E(guild, "Bot command ran", etype="cmd_thisbot")
        e.add_field(name="Command", value=ctx.command.qualified_name if ctx.command else "unknown", inline=True)
        e.add_field(name="User", value=f"{ctx.author} ({ctx.author.id})", inline=True)
        e.add_field(name="Channel", value=getattr(ctx.channel, "mention", "DM"), inline=True)
        await self._send(guild, e, getattr(ctx.channel, "id", None))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not (message.guild and message.author and message.author.bot and self.bot.user and message.author.id != self.bot.user.id):
            return
        g = await self.config.guild(message.guild).all()
        if not g["commands"]["other_bots"]:
            return
        content = message.content or ""
        if not self._cmd_prefix_re.match(content):
            return
        if self._should_suppress(
            f"otherbotcmd:{message.guild.id}:{message.author.id}", await self._rate_seconds(message.guild)
        ):
            return
        e = await self._E(message.guild, "Other bot command ran", etype="cmd_otherbot")
        e.add_field(name="Bot", value=f"{message.author} ({message.author.id})", inline=True)
        e.add_field(name="Channel", value=message.channel.mention, inline=True)
        if content:
            e.add_field(name="Content", value=content[:300], inline=False)
        await self._send(message.guild, e, message.channel.id)


async def setup(bot: Red) -> None:
    await bot.add_cog(LogPlus(bot))
