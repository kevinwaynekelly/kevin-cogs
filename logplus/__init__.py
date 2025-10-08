# path: cogs/kevinwaynekelly/logplus/__init__.py
from __future__ import annotations

from typing import Optional, Dict, Tuple, Iterable
import time
import re

import discord
from discord.ext import commands

from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config
from redbot.core.utils.chat_formatting import box

__red_end_user_data_statement__ = (
    "This cog stores per-guild settings for logging preferences, channel IDs, and optional per-channel routing overrides. "
    "It does not persist message contents beyond sending to the configured destination channel(s)."
)

# =============================================================================
#  LogPlus — Kevin Wayne Kelly edition
#  First time:  !cog path add ./cogs/kevinwaynekelly
#  Load:        !load logplus
# =============================================================================

# Styling map (emoji + color) used when style.compact is enabled
EVENT_STYLE: Dict[str, Dict[str, object]] = {
    # message
    "message_edited":   {"emoji": "✏️", "color": discord.Color.gold()},
    "message_deleted":  {"emoji": "🗑️", "color": discord.Color.red()},
    "bulk_delete":      {"emoji": "🧹", "color": discord.Color.red()},
    "pins_updated":     {"emoji": "📌", "color": discord.Color.blurple()},
    # reactions
    "reaction_added":   {"emoji": "➕", "color": discord.Color.green()},
    "reaction_removed": {"emoji": "➖", "color": discord.Color.orange()},
    "reaction_cleared": {"emoji": "♻️", "color": discord.Color.orange()},
    # server
    "channel_created":  {"emoji": "📺", "color": discord.Color.green()},
    "channel_deleted":  {"emoji": "📺", "color": discord.Color.red()},
    "channel_updated":  {"emoji": "📺", "color": discord.Color.blurple()},
    "role_created":     {"emoji": "🛡️", "color": discord.Color.green()},
    "role_deleted":     {"emoji": "🛡️", "color": discord.Color.red()},
    "role_updated":     {"emoji": "🛡️", "color": discord.Color.blurple()},
    "server_updated":   {"emoji": "🏠", "color": discord.Color.blurple()},
    "emoji_updated":    {"emoji": "😃", "color": discord.Color.blurple()},
    "sticker_updated":  {"emoji": "🏷️", "color": discord.Color.blurple()},
    "integrations_updated": {"emoji": "🧩", "color": discord.Color.blurple()},
    "webhooks_updated": {"emoji": "🪝", "color": discord.Color.blurple()},
    "thread_created":   {"emoji": "🧵", "color": discord.Color.green()},
    "thread_deleted":   {"emoji": "🧵", "color": discord.Color.red()},
    "thread_updated":   {"emoji": "🧵", "color": discord.Color.blurple()},
    # invites
    "invite_created":   {"emoji": "🔗", "color": discord.Color.green()},
    "invite_deleted":   {"emoji": "❌", "color": discord.Color.red()},
    # members
    "member_joined":    {"emoji": "➕", "color": discord.Color.green()},
    "member_left":      {"emoji": "➖", "color": discord.Color.red()},
    "roles_changed":    {"emoji": "🎭", "color": discord.Color.blurple()},
    "nick_changed":     {"emoji": "✍️", "color": discord.Color.blurple()},
    "timeout_updated":  {"emoji": "⏳", "color": discord.Color.orange()},
    "user_banned":      {"emoji": "🔨", "color": discord.Color.dark_red()},
    "user_unbanned":    {"emoji": "✅", "color": discord.Color.green()},
    # voice
    "voice_join":       {"emoji": "🎤", "color": discord.Color.green()},
    "voice_move":       {"emoji": "🎤", "color": discord.Color.blurple()},
    "voice_leave":      {"emoji": "🎤", "color": discord.Color.red()},
    "voice_mute":       {"emoji": "🔇", "color": discord.Color.orange()},
    "voice_deaf":       {"emoji": "🙉", "color": discord.Color.orange()},
    "voice_video":      {"emoji": "🎥", "color": discord.Color.blurple()},
    "voice_stream":     {"emoji": "📺", "color": discord.Color.blurple()},
    # sched
    "sched_created":    {"emoji": "📅", "color": discord.Color.green()},
    "sched_updated":    {"emoji": "📅", "color": discord.Color.blurple()},
    "sched_deleted":    {"emoji": "📅", "color": discord.Color.red()},
    "sched_user_add":   {"emoji": "➕", "color": discord.Color.green()},
    "sched_user_rem":   {"emoji": "➖", "color": discord.Color.red()},
    # commands
    "cmd_thisbot":      {"emoji": "🤖", "color": discord.Color.green()},
    "cmd_otherbot":     {"emoji": "🤖", "color": discord.Color.blurple()},
}

# Defaults: everything enabled + compact style + sane rate limit
DEFAULTS_GUILD = {
    "log_channel": None,
    "fast_logs": True,
    "overrides": {},

    "message": {"edit": True, "delete": True, "bulk_delete": True, "pins": True, "exempt_channels": []},
    "reactions": {"add": True, "remove": True, "clear": True},
    "server": {
        "channel_create": True, "channel_delete": True, "channel_update": True,
        "role_create": True, "role_delete": True, "role_update": True,
        "server_update": True, "emoji_update": True, "sticker_update": True,
        "integrations_update": True, "webhooks_update": True,
        "thread_create": True, "thread_delete": True, "thread_update": True,
        "exempt_channels": [],
    },
    "invites": {"create": True, "delete": True},
    "member": {
        "join": True, "leave": True, "roles_changed": True, "nick_changed": True,
        "ban": True, "unban": True, "timeout": True, "presence": True,
    },
    "voice": {"join": True, "move": True, "leave": True, "mute": True, "deaf": True, "video": True, "stream": True},
    "sched": {"create": True, "update": True, "delete": True, "user_add": True, "user_remove": True},
    "commands": {"this_bot": True, "other_bots": True},
    "rate": {"seconds": 2.0},
    "style": {"compact": True},
}


class LogPlus(redcommands.Cog):
    """Power logging: messages, reactions, server, invites, members, voice, scheduled events, and command events."""

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0x51A7E11, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)

        self._last_event_at: Dict[str, float] = {}
        self._cmd_prefix_re = re.compile(r"^(<@!?|[/!?.~+\-$&%=>:#])")

    # ---------- helpers ----------
    @staticmethod
    def _now():
        return discord.utils.utcnow()

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
        compact: Optional[bool] = None,
    ) -> discord.Embed:
        # why: consistent Arcane-like timestamp + optional compact emojis/colors
        style = EVENT_STYLE.get(etype or "", {})
        use_compact = bool(compact) if compact is not None else True
        if use_compact and style.get("emoji"):
            title = f"{style['emoji']} {title}"
        if color is None and style.get("color"):
            color = style["color"]  # default color by event type
        e = discord.Embed(title=title, description=description, color=color or discord.Color.blurple(), timestamp=self._now())
        if footer:
            e.set_footer(text=footer)
        return e

    async def _log_channel(self, guild: discord.Guild, source_channel_id: Optional[int] = None) -> Optional[discord.TextChannel]:
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

    async def _audit_actor(self, guild: discord.Guild, action: discord.AuditLogAction, target_id: Optional[int] = None) -> Optional[str]:
        try:
            async for entry in guild.audit_logs(limit=5, action=action):
                if target_id is None or (getattr(entry.target, "id", None) == target_id):
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

    # ---------- commands ----------
    @redcommands.group(name="logplus", invoke_without_command=True)
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def logplus(self, ctx: redcommands.Context):
        g = await self.config.guild(ctx.guild).all()
        lines = [
            f"Log channel: {ctx.guild.get_channel(g['log_channel']).mention if g['log_channel'] else 'Not set'}",
            f"Message: edit={g['message']['edit']} delete={g['message']['delete']} bulk={g['message']['bulk_delete']} pins={g['message']['pins']} exempt={len(g['message']['exempt_channels'])}",
            f"Reactions: add={g['reactions']['add']} remove={g['reactions']['remove']} clear={g['reactions']['clear']}",
            f"Server: chan(c/d/u)={[g['server']['channel_create'], g['server']['channel_delete'], g['server']['channel_update']]} role(c/d/u)={[g['server']['role_create'], g['server']['role_delete'], g['server']['role_update']]} emoji={g['server']['emoji_update']} sticker={g['server']['sticker_update']} integ={g['server']['integrations_update']} webhooks={g['server']['webhooks_update']} threads(c/d/u)={[g['server']['thread_create'], g['server']['thread_delete'], g['server']['thread_update']]} exempt={len(g['server']['exempt_channels'])}",
            f"Invites: create={g['invites']['create']} delete={g['invites']['delete']}",
            f"Member: join={g['member']['join']} leave={g['member']['leave']} roles={g['member']['roles_changed']} nick={g['member']['nick_changed']} ban={g['member']['ban']} unban={g['member']['unban']} timeout={g['member']['timeout']} presence={g['member']['presence']}",
            f"Voice: join={g['voice']['join']} move={g['voice']['move']} leave={g['voice']['leave']} mute={g['voice']['mute']} deaf={g['voice']['deaf']} video={g['voice']['video']} stream={g['voice']['stream']}",
            f"Sched: create={g['sched']['create']} update={g['sched']['update']} delete={g['sched']['delete']} user_add={g['sched']['user_add']} user_remove={g['sched']['user_remove']}",
            f"Commands: this_bot={g['commands']['this_bot']} other_bots={g['commands']['other_bots']}",
            f"Rate limiter: {g['rate']['seconds']}s   |   Style.compact={g['style']['compact']}",
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    # ---- rate command
    @logplus.command(name="rate")
    async def rate(self, ctx: redcommands.Context, seconds: Optional[float] = None):
        """Show or set the per-event rate limit window (seconds). Use 0 to disable."""
        if seconds is None:
            cur = await self._rate_seconds(ctx.guild)
            return await ctx.send(f"Rate limit window is **{cur:.2f}s**.")
        if seconds < 0:
            return await ctx.send("Seconds must be >= 0.")
        await self.config.guild(ctx.guild).rate.seconds.set(float(seconds))
        await ctx.send(f"Rate limit window set to **{seconds:.2f}s**.")

    # ---- style group
    @logplus.group(name="style")
    async def style(self, ctx: redcommands.Context):
        """Style controls for embeds."""
        pass

    @style.command(name="compact")
    async def style_compact(self, ctx: redcommands.Context, flag: Optional[str] = None):
        """Toggle/read compact style. `[p]logplus style compact on|off`"""
        if flag is None:
            cur = await self.config.guild(ctx.guild).style.compact()
            return await ctx.send(f"Compact style is **{'ON' if cur else 'OFF'}**.")
        flag = flag.lower()
        if flag not in {"on", "off"}:
            return await ctx.send("Use `on` or `off`.")
        await self.config.guild(ctx.guild).style.compact.set(flag == "on")
        await ctx.send(f"Compact style **{flag.upper()}**.")

    @style.command(name="preview")
    async def style_preview(self, ctx: redcommands.Context):
        """Send a few sample embeds to preview the style."""
        compact = await self._is_compact(ctx.guild)
        samples = [
            ("Message deleted", "message_deleted"),
            ("Reaction added", "reaction_added"),
            ("Channel created", "channel_created"),
            ("Member joined", "member_joined"),
            ("Voice move", "voice_move"),
            ("Invite created", "invite_created"),
        ]
        for title, etype in samples:
            e = self._mk_embed(title, etype=etype, compact=compact)
            await ctx.send(embed=e)

    @logplus.group()
    async def toggle(self, ctx: redcommands.Context):
        """Toggle a category or specific event."""
        pass

    async def _flip(self, ctx: redcommands.Context, group: str, key: str):
        """Toggle boolean at <group>.<key>. Uses get_attr to avoid collisions (e.g., 'clear')."""
        section = getattr(self.config.guild(ctx.guild), group)
        value = section.get_attr(key)
        cur = await value()
        await value.set(not cur)
        await ctx.send(f"{group}.{key} set to {not cur}")

    # message toggles
    @toggle.group() async def message(self, ctx: redcommands.Context): pass
    @message.command() async def edit(self, ctx: redcommands.Context): await self._flip(ctx, "message", "edit")
    @message.command() async def delete(self, ctx: redcommands.Context): await self._flip(ctx, "message", "delete")
    @message.command(name="bulk") async def message_bulk(self, ctx: redcommands.Context): await self._flip(ctx, "message", "bulk_delete")
    @message.command() async def pins(self, ctx: redcommands.Context): await self._flip(ctx, "message", "pins")

    # reactions toggles
    @toggle.group() async def reactions(self, ctx: redcommands.Context): pass
    @reactions.command(name="add") async def react_add(self, ctx: redcommands.Context): await self._flip(ctx, "reactions", "add")
    @reactions.command(name="remove") async def react_remove(self, ctx: redcommands.Context): await self._flip(ctx, "reactions", "remove")
    @reactions.command(name="clear") async def react_clear(self, ctx: redcommands.Context): await self._flip(ctx, "reactions", "clear")

    # server toggles
    @toggle.group() async def server(self, ctx: redcommands.Context): pass
    @server.command(name="channelcreate") async def t_sc_create(self, ctx: redcommands.Context): await self._flip(ctx, "server", "channel_create")
    @server.command(name="channeldelete") async def t_sc_delete(self, ctx: redcommands.Context): await self._flip(ctx, "server", "channel_delete")
    @server.command(name="channelupdate") async def t_sc_update(self, ctx: redcommands.Context): await self._flip(ctx, "server", "channel_update")
    @server.command(name="rolecreate") async def t_sr_create(self, ctx: redcommands.Context): await self._flip(ctx, "server", "role_create")
    @server.command(name="roledelete") async def t_sr_delete(self, ctx: redcommands.Context): await self._flip(ctx, "server", "role_delete")
    @server.command(name="roleupdate") async def t_sr_update(self, ctx: redcommands.Context): await self._flip(ctx, "server", "role_update")
    @server.command(name="serverupdate") async def t_s_update(self, ctx: redcommands.Context): await self._flip(ctx, "server", "server_update")
    @server.command(name="emojiupdate") async def t_e_update(self, ctx: redcommands.Context): await self._flip(ctx, "server", "emoji_update")
    @server.command(name="stickerupdate") async def t_st_update(self, ctx: redcommands.Context): await self._flip(ctx, "server", "sticker_update")
    @server.command(name="integrationsupdate") async def t_i_update(self, ctx: redcommands.Context): await self._flip(ctx, "server", "integrations_update")
    @server.command(name="webhooksupdate") async def t_w_update(self, ctx: redcommands.Context): await self._flip(ctx, "server", "webhooks_update")
    @server.command(name="threadcreate") async def t_tc(self, ctx: redcommands.Context): await self._flip(ctx, "server", "thread_create")
    @server.command(name="threaddelete") async def t_td(self, ctx: redcommands.Context): await self._flip(ctx, "server", "thread_delete")
    @server.command(name="thredupdate") async def t_tu(self, ctx: redcommands.Context): await self._flip(ctx, "server", "thread_update")

    # invites toggles
    @toggle.group() async def invites(self, ctx: redcommands.Context): pass
    @invites.command(name="create") async def t_inv_c(self, ctx: redcommands.Context): await self._flip(ctx, "invites", "create")
    @invites.command(name="delete") async def t_inv_d(self, ctx: redcommands.Context): await self._flip(ctx, "invites", "delete")

    # member toggles
    @toggle.group() async def member(self, ctx: redcommands.Context): pass
    @member.command(name="join") async def t_m_join(self, ctx: redcommands.Context): await self._flip(ctx, "member", "join")
    @member.command(name="leave") async def t_m_leave(self, ctx: redcommands.Context): await self._flip(ctx, "member", "leave")
    @member.command(name="roles") async def t_m_roles(self, ctx: redcommands.Context): await self._flip(ctx, "member", "roles_changed")
    @member.command(name="nick") async def t_m_nick(self, ctx: redcommands.Context): await self._flip(ctx, "member", "nick_changed")
    @member.command(name="ban") async def t_m_ban(self, ctx: redcommands.Context): await self._flip(ctx, "member", "ban")
    @member.command(name="unban") async def t_m_unban(self, ctx: redcommands.Context): await self._flip(ctx, "member", "unban")
    @member.command(name="timeout") async def t_m_timeout(self, ctx: redcommands.Context): await self._flip(ctx, "member", "timeout")
    @member.command(name="presence") async def t_m_presence(self, ctx: redcommands.Context): await self._flip(ctx, "member", "presence")

    # voice toggles
    @toggle.group() async def voice(self, ctx: redcommands.Context): pass
    @voice.command(name="join") async def t_v_join(self, ctx: redcommands.Context): await self._flip(ctx, "voice", "join")
    @voice.command(name="move") async def t_v_move(self, ctx: redcommands.Context): await self._flip(ctx, "voice", "move")
    @voice.command(name="leave") async def t_v_leave(self, ctx: redcommands.Context): await self._flip(ctx, "voice", "leave")
    @voice.command(name="mute") async def t_v_mute(self, ctx: redcommands.Context): await self._flip(ctx, "voice", "mute")
    @voice.command(name="deaf") async def t_v_deaf(self, ctx: redcommands.Context): await self._flip(ctx, "voice", "deaf")
    @voice.command(name="video") async def t_v_video(self, ctx: redcommands.Context): await self._flip(ctx, "voice", "video")
    @voice.command(name="stream") async def t_v_stream(self, ctx: redcommands.Context): await self._flip(ctx, "voice", "stream")

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

    # exempt mgmt
    @logplus.group(name="exempt") async def exempt(self, ctx: redcommands.Context): pass
    @exempt.command(name="add")
    async def ex_add(self, ctx: redcommands.Context, category: str, channel: discord.TextChannel):
        category = category.lower()
        if category not in {"message", "server"}:
            return await ctx.send("Category must be 'message' or 'server'.")
        data = await getattr(self.config.guild(ctx.guild), category).exempt_channels()
        if channel.id in data:
            return await ctx.send("That channel is already exempt.")
        data.append(channel.id)
        await getattr(self.config.guild(ctx.guild), category).exempt_channels.set(data)
        await ctx.send(f"Added {channel.mention} to {category} exempt list.")
    @exempt.command(name="remove")
    async def ex_remove(self, ctx: redcommands.Context, category: str, channel: discord.TextChannel):
        category = category.lower()
        data = await getattr(self.config.guild(ctx.guild), category).exempt_channels()
        if channel.id not in data:
            return await ctx.send("That channel is not exempt.")
        data = [c for c in data if c != channel.id]
        await getattr(self.config.guild(ctx.guild), category).exempt_channels.set(data)
        await ctx.send(f"Removed {channel.mention} from {category} exempt list.")
    @exempt.command(name="list")
    async def ex_list(self, ctx: redcommands.Context):
        m = await self.config.guild(ctx.guild).message.exempt_channels()
        s = await self.config.guild(ctx.guild).server.exempt_channels()
        lines = ["Message exempt:"]
        lines.extend([f"- <#{i}>" for i in m] if m else ["- none"])
        lines.append("")
        lines.append("Server exempt:")
        lines.extend([f"- <#{i}>" for i in s] if s else ["- none"])
        await ctx.send(box("\n".join(lines), lang="ini"))

    # overrides mgmt
    @logplus.group(name="override") async def override(self, ctx: redcommands.Context): pass
    @override.command(name="set")
    async def override_set(self, ctx: redcommands.Context, source: discord.TextChannel, dest: discord.TextChannel):
        data = await self.config.guild(ctx.guild).overrides()
        if not isinstance(data, dict):
            data = {}
        data[str(source.id)] = int(dest.id)
        await self.config.guild(ctx.guild).overrides.set(data)
        await ctx.send(f"Logs for {source.mention} will go to {dest.mention}.")
    @override.command(name="remove")
    async def override_remove(self, ctx: redcommands.Context, source: discord.TextChannel):
        data = await self.config.guild(ctx.guild).overrides()
        removed = False
        if isinstance(data, dict):
            removed = data.pop(str(source.id), None) is not None
        await self.config.guild(ctx.guild).overrides.set(data)
        await ctx.send(f"Override for {source.mention} {'removed' if removed else 'was not set'}.")
    @override.command(name="list")
    async def override_list(self, ctx: redcommands.Context):
        data = await self.config.guild(ctx.guild).overrides()
        if not data:
            return await ctx.send("No overrides set.")
        lines = [f"<#{k}> → <#{v}>" for k, v in data.items()]
        await ctx.send(box("\n".join(lines), lang="ini"))

    # diag
    @logplus.command(name="diag")
    async def diag(self, ctx: redcommands.Context):
        """Self-check: flip/restore every toggle and verify set/get works."""
        guild_conf = self.config.guild(ctx.guild)
        paths: Iterable[Tuple[str, str]] = []
        def add(group: str, keys: Iterable[str]):
            nonlocal paths
            paths += [(group, k) for k in keys]
        add("message", ["edit", "delete", "bulk_delete", "pins"])
        add("reactions", ["add", "remove", "clear"])
        add("server", ["channel_create", "channel_delete", "channel_update",
                       "role_create", "role_delete", "role_update", "server_update",
                       "emoji_update", "sticker_update", "integrations_update",
                       "webhooks_update", "thread_create", "thread_delete", "thread_update"])
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
        await ctx.send(box("\n".join(lines), lang="ini"))

    # ---------- listeners (all embeds include timestamps automatically) ----------
    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not (before.guild and before.author) or before.author.bot:
            return
        g = await self.config.guild(before.guild).all()
        if not g["message"]["edit"] or before.content == after.content:
            return
        if await self._is_exempt(before.guild, before.channel.id, "message"):
            return
        e = self._mk_embed("Message edited", etype="message_edited", footer=f"User ID: {before.author.id}")
        e.add_field(name="Author", value=f"{before.author} ({before.author.id})", inline=False)
        e.add_field(name="Channel", value=before.channel.mention, inline=True)
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
        e = self._mk_embed("Message deleted", etype="message_deleted", footer=f"User ID: {message.author.id}")
        e.add_field(name="Author", value=f"{message.author} ({message.author.id})", inline=False)
        e.add_field(name="Channel", value=message.channel.mention, inline=True)
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
        e = self._mk_embed("Bulk delete", description=f"{len(payload.message_ids)} messages", etype="bulk_delete")
        if isinstance(ch, discord.TextChannel):
            e.add_field(name="Channel", value=ch.mention)
        await self._send(guild, e, payload.channel_id)

    @commands.Cog.listener()
    async def on_guild_channel_pins_update(self, channel: discord.abc.GuildChannel, last_pin):
        guild = channel.guild
        g = await self.config.guild(guild).all()
        if not g["message"]["pins"]:
            return
        if await self._is_exempt(guild, channel.id, "message"):
            return
        e = self._mk_embed("Pins updated", description=f"#{getattr(channel, 'name', 'unknown')}", etype="pins_updated")
        await self._send(guild, e, channel.id)

    # reactions (suppressed)
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
        if self._should_suppress(f"react_add:{payload.guild_id}:{payload.channel_id}:{payload.message_id}:{str(payload.emoji)}",
                                 await self._rate_seconds(guild)):
            return
        e = self._mk_embed("Reaction added", etype="reaction_added")
        e.add_field(name="Emoji", value=str(payload.emoji))
        e.add_field(name="Message ID", value=str(payload.message_id))
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
        if self._should_suppress(f"react_rm:{payload.guild_id}:{payload.channel_id}:{payload.message_id}:{str(payload.emoji)}",
                                 await self._rate_seconds(guild)):
            return
        e = self._mk_embed("Reaction removed", etype="reaction_removed")
        e.add_field(name="Emoji", value=str(payload.emoji))
        e.add_field(name="Message ID", value=str(payload.message_id))
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
        e = self._mk_embed("Reactions cleared", etype="reaction_cleared")
        e.add_field(name="Message ID", value=str(payload.message_id))
        await self._send(guild, e, payload.channel_id)

    # server structure
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        g = await self.config.guild(channel.guild).all()
        if g["server"]["channel_create"] and not await self._is_exempt(channel.guild, channel.id, "server"):
            actor = await self._audit_actor(channel.guild, discord.AuditLogAction.channel_create, getattr(channel, 'id', None))
            e = self._mk_embed("Channel created", description=channel.mention, etype="channel_created")
            if actor: e.add_field(name="By", value=actor)
            await self._send(channel.guild, e, getattr(channel, 'id', None))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        g = await self.config.guild(channel.guild).all()
        if g["server"]["channel_delete"] and not await self._is_exempt(channel.guild, channel.id, "server"):
            actor = await self._audit_actor(channel.guild, discord.AuditLogAction.channel_delete, getattr(channel, 'id', None))
            e = self._mk_embed("Channel deleted", description=f"#{channel.name}", etype="channel_deleted")
            if actor: e.add_field(name="By", value=actor)
            await self._send(channel.guild, e, getattr(channel, 'id', None))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        g = await self.config.guild(after.guild).all()
        if g["server"]["channel_update"] and not await self._is_exempt(after.guild, after.id, "server"):
            actor = await self._audit_actor(after.guild, discord.AuditLogAction.channel_update, getattr(after, 'id', None))
            e = self._mk_embed("Channel updated", description=after.mention, etype="channel_updated")
            if actor: e.add_field(name="By", value=actor)
            await self._send(after.guild, e, getattr(after, 'id', None))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        g = await self.config.guild(role.guild).all()
        if g["server"]["role_create"]:
            actor = await self._audit_actor(role.guild, discord.AuditLogAction.role_create, getattr(role, 'id', None))
            e = self._mk_embed("Role created", description=role.mention, etype="role_created")
            if actor: e.add_field(name="By", value=actor)
            await self._send(role.guild, e)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        g = await self.config.guild(role.guild).all()
        if g["server"]["role_delete"]:
            actor = await self._audit_actor(role.guild, discord.AuditLogAction.role_delete, getattr(role, 'id', None))
            e = self._mk_embed("Role deleted", description=role.name, etype="role_deleted")
            if actor: e.add_field(name="By", value=actor)
            await self._send(role.guild, e)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        g = await self.config.guild(after.guild).all()
        if g["server"]["role_update"]:
            actor = await self._audit_actor(after.guild, discord.AuditLogAction.role_update, getattr(after, 'id', None))
            e = self._mk_embed("Role updated", description=after.mention, etype="role_updated")
            if actor: e.add_field(name="By", value=actor)
            await self._send(after.guild, e)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        g = await self.config.guild(after).all()
        if g["server"]["server_update"]:
            e = self._mk_embed("Server updated", etype="server_updated")
            await self._send(after, e)

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        g = await self.config.guild(guild).all()
        if g["server"]["emoji_update"]:
            e = self._mk_embed("Emoji list updated", etype="emoji_updated")
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: discord.Guild, before, after):
        g = await self.config.guild(guild).all()
        if g["server"]["sticker_update"]:
            e = self._mk_embed("Sticker list updated", etype="sticker_updated")
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_guild_integrations_update(self, guild: discord.Guild):
        g = await self.config.guild(guild).all()
        if g["server"]["integrations_update"]:
            e = self._mk_embed("Integrations updated", etype="integrations_updated")
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel):
        g = await self.config.guild(channel.guild).all()
        if g["server"]["webhooks_update"] and not await self._is_exempt(channel.guild, channel.id, "server"):
            actor = await self._audit_actor(channel.guild, discord.AuditLogAction.webhook_create)
            e = self._mk_embed("Webhooks updated", description=f"#{getattr(channel, 'name', 'unknown')}", etype="webhooks_updated")
            if actor: e.add_field(name="By", value=actor)
            await self._send(channel.guild, e, channel.id)

    # invites
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        g = await self.config.guild(invite.guild).all()
        if g["invites"]["create"]:
            e = self._mk_embed("Invite created", etype="invite_created")
            e.add_field(name="Code", value=invite.code)
            if invite.channel:
                e.add_field(name="Channel", value=invite.channel.mention)
            await self._send(invite.guild, e, getattr(invite.channel, 'id', None))

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        g = await self.config.guild(invite.guild).all()
        if g["invites"]["delete"]:
            e = self._mk_embed("Invite deleted", etype="invite_deleted")
            e.add_field(name="Code", value=invite.code)
            await self._send(invite.guild, e)

    # members
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        g = await self.config.guild(member.guild).all()
        if g["member"]["join"]:
            e = self._mk_embed("Member joined", description=f"{member} ({member.id})", etype="member_joined")
            await self._send(member.guild, e)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        g = await self.config.guild(member.guild).all()
        if g["member"]["leave"]:
            e = self._mk_embed("Member left", description=f"{member} ({member.id})", etype="member_left")
            await self._send(member.guild, e)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        g = await self.config.guild(after.guild).all()
        if before.nick != after.nick and g["member"]["nick_changed"]:
            e = self._mk_embed("Nickname changed", etype="nick_changed")
            e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
            e.add_field(name="Before", value=before.nick or "None", inline=True)
            e.add_field(name="After", value=after.nick or "None", inline=True)
            await self._send(after.guild, e)
        if set(before.roles) != set(after.roles) and g["member"]["roles_changed"]:
            e = self._mk_embed("Roles changed", etype="roles_changed")
            e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
            await self._send(after.guild, e)
        if g["member"]["timeout"] and before.timed_out_until != after.timed_out_until:
            e = self._mk_embed("Timeout updated", etype="timeout_updated")
            e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
            e.add_field(name="Until", value=str(after.timed_out_until))
            await self._send(after.guild, e)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        g = await self.config.guild(guild).all()
        if g["member"]["ban"]:
            actor = await self._audit_actor(guild, discord.AuditLogAction.ban, getattr(user, 'id', None))
            e = self._mk_embed("User banned", description=f"{user} ({user.id})", etype="user_banned")
            if actor: e.add_field(name="By", value=actor)
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        g = await self.config.guild(guild).all()
        if g["member"]["unban"]:
            actor = await self._audit_actor(guild, discord.AuditLogAction.unban, getattr(user, 'id', None))
            e = self._mk_embed("User unbanned", description=f"{user} ({user.id})", etype="user_unbanned")
            if actor: e.add_field(name="By", value=actor)
            await self._send(guild, e)

    # voice (suppressed on toggly bits)
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        g = await self.config.guild(member.guild).all()
        ch = await self._log_channel(member.guild)
        if not ch:
            return
        rate = await self._rate_seconds(member.guild)

        if before.channel is None and after.channel is not None and g["voice"]["join"]:
            await ch.send(embed=self._mk_embed("Voice join", description=f"{member} → {after.channel.mention}", etype="voice_join"))
            return
        if before.channel is not None and after.channel is None and g["voice"]["leave"]:
            await ch.send(embed=self._mk_embed("Voice leave", description=f"{member} ← {before.channel.mention}", etype="voice_leave"))
            return
        if before.channel and after.channel and before.channel.id != after.channel.id and g["voice"]["move"]:
            await ch.send(embed=self._mk_embed("Voice move", description=f"{member}: {before.channel.mention} → {after.channel.mention}", etype="voice_move"))
        if g["voice"]["mute"] and (before.self_mute != after.self_mute or before.mute != after.mute):
            if not self._should_suppress(f"v_mute:{member.guild.id}:{member.id}", rate):
                await ch.send(embed=self._mk_embed("Mute state change", description=str(member), etype="voice_mute"))
        if g["voice"]["deaf"] and (before.self_deaf != after.self_deaf or before.deaf != after.deaf):
            if not self._should_suppress(f"v_deaf:{member.guild.id}:{member.id}", rate):
                await ch.send(embed=self._mk_embed("Deaf state change", description=str(member), etype="voice_deaf"))
        if g["voice"]["video"] and (before.self_video != after.self_video):
            if not self._should_suppress(f"v_video:{member.guild.id}:{member.id}", rate):
                await ch.send(embed=self._mk_embed("Video state change", description=f"{member} → {'on' if after.self_video else 'off'}", etype="voice_video"))
        if g["voice"]["stream"] and (before.self_stream != after.self_stream):
            if not self._should_suppress(f"v_stream:{member.guild.id}:{member.id}", rate):
                await ch.send(embed=self._mk_embed("Stream state change", description=f"{member} → {'on' if after.self_stream else 'off'}", etype="voice_stream"))

    # scheduled events
    @commands.Cog.listener()
    async def on_guild_scheduled_event_create(self, event: discord.ScheduledEvent):
        g = await self.config.guild(event.guild).all()
        if g["sched"]["create"]:
            e = self._mk_embed("Scheduled event created", description=event.name, etype="sched_created")
            await self._send(event.guild, e)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent):
        g = await self.config.guild(after.guild).all()
        if g["sched"]["update"]:
            e = self._mk_embed("Scheduled event updated", description=after.name, etype="sched_updated")
            await self._send(after.guild, e)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_delete(self, event: discord.ScheduledEvent):
        g = await self.config.guild(event.guild).all()
        if g["sched"]["delete"]:
            e = self._mk_embed("Scheduled event deleted", description=event.name, etype="sched_deleted")
            await self._send(event.guild, e)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_user_add(self, event: discord.ScheduledEvent, user: discord.User):
        g = await self.config.guild(event.guild).all()
        if g["sched"]["user_add"]:
            e = self._mk_embed("Event RSVP added", description=f"{user} → {event.name}", etype="sched_user_add")
            await self._send(event.guild, e)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_user_remove(self, event: discord.ScheduledEvent, user: discord.User):
        g = await self.config.guild(event.guild).all()
        if g["sched"]["user_remove"]:
            e = self._mk_embed("Event RSVP removed", description=f"{user} ✕ {event.name}", etype="sched_user_rem")
            await self._send(event.guild, e)

    # command events
    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        guild = getattr(ctx, "guild", None)
        if not guild:
            return
        g = await self.config.guild(guild).all()
        if not g["commands"]["this_bot"]:
            return
        e = self._mk_embed("Bot command ran", etype="cmd_thisbot")
        e.add_field(name="Command", value=ctx.command.qualified_name if ctx.command else "unknown", inline=True)
        e.add_field(name="User", value=f"{ctx.author} ({ctx.author.id})", inline=True)
        e.add_field(name="Channel", value=getattr(ctx.channel, "mention", "DM"), inline=True)
        await self._send(guild, e, getattr(ctx.channel, "id", None))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # heuristic: if a bot posts something that looks like a command, log it as "Other bot command ran"
        if not (message.guild and message.author and message.author.bot and self.bot.user and message.author.id != self.bot.user.id):
            return
        g = await self.config.guild(message.guild).all()
        if not g["commands"]["other_bots"]:
            return
        content = message.content or ""
        if not self._cmd_prefix_re.match(content):
            return
        if self._should_suppress(f"otherbotcmd:{message.guild.id}:{message.author.id}", await self._rate_seconds(message.guild)):
            return
        e = self._mk_embed("Other bot command ran", etype="cmd_otherbot")
        e.add_field(name="Bot", value=f"{message.author} ({message.author.id})", inline=True)
        e.add_field(name="Channel", value=message.channel.mention, inline=True)
        if content:
            e.add_field(name="Content", value=content[:300], inline=False)
        await self._send(message.guild, e, message.channel.id)


async def setup(bot: Red) -> None:
    await bot.add_cog(LogPlus(bot))
