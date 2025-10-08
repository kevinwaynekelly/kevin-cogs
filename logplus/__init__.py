from __future__ import annotations
from typing import Optional

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

# ============================================================================
#  LogPlus — Kevin Wayne Kelly edition
#  Folder layout for local cogs:
#    ./cogs/kevinwaynekelly/logplus/__init__.py  (this file)
#  First time:  !cog path add ./cogs/kevinwaynekelly
#  Load:        !load logplus
# ============================================================================

DEFAULTS_GUILD = {
    "log_channel": None,
    "fast_logs": False,

    # Per-channel destination overrides: { "<source_channel_id>": <dest_channel_id> }
    "overrides": {},

    # MESSAGE
    "message": {
        "edit": True,
        "delete": True,
        "bulk_delete": True,
        "pins": True,
        "exempt_channels": []
    },

    # REACTIONS
    "reactions": {
        "add": True,
        "remove": True,
        "clear": False
    },

    # SERVER / STRUCTURE
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
        "exempt_channels": []
    },

    # INVITES
    "invites": {"create": True, "delete": True},

    # MEMBERS
    "member": {
        "join": True,
        "leave": True,
        "roles_changed": True,
        "nick_changed": True,
        "ban": True,
        "unban": True,
        "timeout": True,
        "presence": False
    },

    # VOICE
    "voice": {
        "join": True,
        "move": True,
        "leave": True,
        "mute": False,
        "deaf": False,
        "video": False,
        "stream": False
    },

    # SCHEDULED EVENTS (community events)
    "sched": {
        "create": True,
        "update": True,
        "delete": True,
        "user_add": False,
        "user_remove": False
    }
}


class LogPlus(redcommands.Cog):
    """
    Power logging: message, reactions, server, invites, members, voice, scheduled events.
    Features:
      • Default log channel per guild + per-channel destination overrides
      • Exempt channel lists for Message/Server
      • Audit Log attribution ("By: user") where Discord provides it
    """

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0x51A7E11, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)

    # ---------- helpers ----------
    async def _log_channel(self, guild: discord.Guild, source_channel_id: Optional[int] = None) -> Optional[discord.TextChannel]:
        # Per-channel route override
        if source_channel_id is not None:
            overrides = await self.config.guild(guild).overrides()
            if isinstance(overrides, dict):
                dest_id = overrides.get(str(source_channel_id)) or overrides.get(int(source_channel_id))
                if dest_id:
                    ch = guild.get_channel(int(dest_id))
                    if isinstance(ch, discord.TextChannel):
                        return ch
        # Fallback to default
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
        """Best-effort audit log lookup for the actor who performed an action."""
        try:
            async for entry in guild.audit_logs(limit=5, action=action):
                if target_id is None or (getattr(entry.target, "id", None) == target_id):
                    return f"{entry.user} ({entry.user.id})"
        except Exception:
            return None
        return None

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
            f"Sched: create={g['sched']['create']} update={g['sched']['update']} delete={g['sched']['delete']} user_add={g['sched']['user_add']} user_remove={g['sched']['user_remove']}"
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @logplus.command()
    async def channel(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None):
        """Set the default destination channel."""
        channel = channel or ctx.channel
        await self.config.guild(ctx.guild).log_channel.set(channel.id)
        await ctx.send(f"Log channel set to {channel.mention}")

    @logplus.group()
    async def toggle(self, ctx: redcommands.Context):
        """Toggle a category or specific event."""
        pass

    async def _flip(self, ctx: redcommands.Context, group: str, key: str):
        section = getattr(self.config.guild(ctx.guild), group)
        cur = await getattr(section, key)()
        await getattr(section, key).set(not cur)
        await ctx.send(f"{group}.{key} set to {not cur}")

    # message toggles
    @toggle.group()
    async def message(self, ctx: redcommands.Context):
        pass

    @message.command()
    async def edit(self, ctx): await self._flip(ctx, "message", "edit")
    @message.command()
    async def delete(self, ctx): await self._flip(ctx, "message", "delete")
    @message.command(name="bulk")
    async def message_bulk(self, ctx): await self._flip(ctx, "message", "bulk_delete")
    @message.command()
    async def pins(self, ctx): await self._flip(ctx, "message", "pins")

    # reactions toggles
    @toggle.group()
    async def reactions(self, ctx): pass
    @reactions.command(name="add")
    async def react_add(self, ctx): await self._flip(ctx, "reactions", "add")
    @reactions.command(name="remove")
    async def react_remove(self, ctx): await self._flip(ctx, "reactions", "remove")
    @reactions.command(name="clear")
    async def react_clear(self, ctx): await self._flip(ctx, "reactions", "clear")

    # server toggles
    @toggle.group()
    async def server(self, ctx): pass
    @server.command(name="channelcreate")
    async def t_sc_create(self, ctx): await self._flip(ctx, "server", "channel_create")
    @server.command(name="channeldelete")
    async def t_sc_delete(self, ctx): await self._flip(ctx, "server", "channel_delete")
    @server.command(name="channelupdate")
    async def t_sc_update(self, ctx): await self._flip(ctx, "server", "channel_update")
    @server.command(name="rolecreate")
    async def t_sr_create(self, ctx): await self._flip(ctx, "server", "role_create")
    @server.command(name="roledelete")
    async def t_sr_delete(self, ctx): await self._flip(ctx, "server", "role_delete")
    @server.command(name="roleupdate")
    async def t_sr_update(self, ctx): await self._flip(ctx, "server", "role_update")
    @server.command(name="serverupdate")
    async def t_s_update(self, ctx): await self._flip(ctx, "server", "server_update")
    @server.command(name="emojiupdate")
    async def t_e_update(self, ctx): await self._flip(ctx, "server", "emoji_update")
    @server.command(name="stickerupdate")
    async def t_st_update(self, ctx): await self._flip(ctx, "server", "sticker_update")
    @server.command(name="integrationsupdate")
    async def t_i_update(self, ctx): await self._flip(ctx, "server", "integrations_update")
    @server.command(name="webhooksupdate")
    async def t_w_update(self, ctx): await self._flip(ctx, "server", "webhooks_update")
    @server.command(name="threadcreate")
    async def t_tc(self, ctx): await self._flip(ctx, "server", "thread_create")
    @server.command(name="threaddelete")
    async def t_td(self, ctx): await self._flip(ctx, "server", "thread_delete")
    @server.command(name="thredupdate")
    async def t_tu(self, ctx): await self._flip(ctx, "server", "thread_update")

    # invites toggles
    @toggle.group()
    async def invites(self, ctx): pass
    @invites.command(name="create")
    async def t_inv_c(self, ctx): await self._flip(ctx, "invites", "create")
    @invites.command(name="delete")
    async def t_inv_d(self, ctx): await self._flip(ctx, "invites", "delete")

    # member toggles
    @toggle.group()
    async def member(self, ctx): pass
    @member.command(name="join")
    async def t_m_join(self, ctx): await self._flip(ctx, "member", "join")
    @member.command(name="leave")
    async def t_m_leave(self, ctx): await self._flip(ctx, "member", "leave")
    @member.command(name="roles")
    async def t_m_roles(self, ctx): await self._flip(ctx, "member", "roles_changed")
    @member.command(name="nick")
    async def t_m_nick(self, ctx): await self._flip(ctx, "member", "nick_changed")
    @member.command(name="ban")
    async def t_m_ban(self, ctx): await self._flip(ctx, "member", "ban")
    @member.command(name="unban")
    async def t_m_unban(self, ctx): await self._flip(ctx, "member", "unban")
    @member.command(name="timeout")
    async def t_m_timeout(self, ctx): await self._flip(ctx, "member", "timeout")
    @member.command(name="presence")
    async def t_m_presence(self, ctx): await self._flip(ctx, "member", "presence")

    # voice toggles
    @toggle.group()
    async def voice(self, ctx): pass
    @voice.command(name="join")
    async def t_v_join(self, ctx): await self._flip(ctx, "voice", "join")
    @voice.command(name="move")
    async def t_v_move(self, ctx): await self._flip(ctx, "voice", "move")
    @voice.command(name="leave")
    async def t_v_leave(self, ctx): await self._flip(ctx, "voice", "leave")
    @voice.command(name="mute")
    async def t_v_mute(self, ctx): await self._flip(ctx, "voice", "mute")
    @voice.command(name="deaf")
    async def t_v_deaf(self, ctx): await self._flip(ctx, "voice", "deaf")
    @voice.command(name="video")
    async def t_v_video(self, ctx): await self._flip(ctx, "voice", "video")
    @voice.command(name="stream")
    async def t_v_stream(self, ctx): await self._flip(ctx, "voice", "stream")

    # scheduled events toggles
    @toggle.group()
    async def sched(self, ctx): pass
    @sched.command(name="create")
    async def t_sch_c(self, ctx): await self._flip(ctx, "sched", "create")
    @sched.command(name="update")
    async def t_sch_u(self, ctx): await self._flip(ctx, "sched", "update")
    @sched.command(name="delete")
    async def t_sch_d(self, ctx): await self._flip(ctx, "sched", "delete")
    @sched.command(name="useradd")
    async def t_sch_ua(self, ctx): await self._flip(ctx, "sched", "user_add")
    @sched.command(name="userremove")
    async def t_sch_ur(self, ctx): await self._flip(ctx, "sched", "user_remove")

    # exempt mgmt
    @logplus.group(name="exempt")
    async def exempt(self, ctx): pass

    @exempt.command(name="add")
    async def ex_add(self, ctx, category: str, channel: discord.TextChannel):
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
    async def ex_remove(self, ctx, category: str, channel: discord.TextChannel):
        category = category.lower()
        data = await getattr(self.config.guild(ctx.guild), category).exempt_channels()
        if channel.id not in data:
            return await ctx.send("That channel is not exempt.")
        data = [c for c in data if c != channel.id]
        await getattr(self.config.guild(ctx.guild), category).exempt_channels.set(data)
        await ctx.send(f"Removed {channel.mention} from {category} exempt list.")

    @exempt.command(name="list")
    async def ex_list(self, ctx):
        m = await self.config.guild(ctx.guild).message.exempt_channels()
        s = await self.config.guild(ctx.guild).server.exempt_channels()
        lines = [
            "Message exempt:", *(f"- <#{i}>" for i in m) or ["- none"],
            "",
            "Server exempt:", *(f"- <#{i}>" for i in s) or ["- none"],
        ]
        await ctx.send(box("
".join(lines)))

    # overrides mgmt
    @logplus.group(name="override")
    async def override(self, ctx: redcommands.Context):
        """Per-channel destination overrides."""
        pass

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

    # ---------- listeners ----------
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
        e = discord.Embed(title="Message edited", color=discord.Color.gold())
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
        e = discord.Embed(title="Message deleted", color=discord.Color.red())
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
        e = discord.Embed(title="Bulk delete", description=f"{len(payload.message_ids)} messages", color=discord.Color.red())
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
        e = discord.Embed(title="Pins updated", description=f"#{getattr(channel, 'name', 'unknown')}", color=discord.Color.blurple())
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
        e = discord.Embed(title="Reaction added", color=discord.Color.green())
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
        e = discord.Embed(title="Reaction removed", color=discord.Color.orange())
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
        e = discord.Embed(title="Reactions cleared", color=discord.Color.orange())
        e.add_field(name="Message ID", value=str(payload.message_id))
        await self._send(guild, e, payload.channel_id)

    # server structure
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        g = await self.config.guild(channel.guild).all()
        if g["server"]["channel_create"] and not await self._is_exempt(channel.guild, channel.id, "server"):
            actor = await self._audit_actor(channel.guild, discord.AuditLogAction.channel_create, getattr(channel, 'id', None))
            e = discord.Embed(title="Channel created", description=channel.mention, color=discord.Color.green())
            if actor: e.add_field(name="By", value=actor)
            await self._send(channel.guild, e, getattr(channel, 'id', None))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        g = await self.config.guild(channel.guild).all()
        if g["server"]["channel_delete"] and not await self._is_exempt(channel.guild, channel.id, "server"):
            actor = await self._audit_actor(channel.guild, discord.AuditLogAction.channel_delete, getattr(channel, 'id', None))
            e = discord.Embed(title="Channel deleted", description=f"#{channel.name}", color=discord.Color.red())
            if actor: e.add_field(name="By", value=actor)
            await self._send(channel.guild, e, getattr(channel, 'id', None))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        g = await self.config.guild(after.guild).all()
        if g["server"]["channel_update"] and not await self._is_exempt(after.guild, after.id, "server"):
            actor = await self._audit_actor(after.guild, discord.AuditLogAction.channel_update, getattr(after, 'id', None))
            e = discord.Embed(title="Channel updated", description=after.mention, color=discord.Color.blurple())
            if actor: e.add_field(name="By", value=actor)
            await self._send(after.guild, e, getattr(after, 'id', None))

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        g = await self.config.guild(role.guild).all()
        if g["server"]["role_create"]:
            actor = await self._audit_actor(role.guild, discord.AuditLogAction.role_create, getattr(role, 'id', None))
            e = discord.Embed(title="Role created", description=role.mention, color=discord.Color.green())
            if actor: e.add_field(name="By", value=actor)
            await self._send(role.guild, e)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        g = await self.config.guild(role.guild).all()
        if g["server"]["role_delete"]:
            actor = await self._audit_actor(role.guild, discord.AuditLogAction.role_delete, getattr(role, 'id', None))
            e = discord.Embed(title="Role deleted", description=role.name, color=discord.Color.red())
            if actor: e.add_field(name="By", value=actor)
            await self._send(role.guild, e)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        g = await self.config.guild(after.guild).all()
        if g["server"]["role_update"]:
            actor = await self._audit_actor(after.guild, discord.AuditLogAction.role_update, getattr(after, 'id', None))
            e = discord.Embed(title="Role updated", description=after.mention, color=discord.Color.blurple())
            if actor: e.add_field(name="By", value=actor)
            await self._send(after.guild, e)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        g = await self.config.guild(after).all()
        if g["server"]["server_update"]:
            e = discord.Embed(title="Server updated", color=discord.Color.blurple())
            await self._send(after, e)

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        g = await self.config.guild(guild).all()
        if g["server"]["emoji_update"]:
            e = discord.Embed(title="Emoji list updated", color=discord.Color.blurple())
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: discord.Guild, before, after):
        g = await self.config.guild(guild).all()
        if g["server"]["sticker_update"]:
            e = discord.Embed(title="Sticker list updated", color=discord.Color.blurple())
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_guild_integrations_update(self, guild: discord.Guild):
        g = await self.config.guild(guild).all()
        if g["server"]["integrations_update"]:
            e = discord.Embed(title="Integrations updated", color=discord.Color.blurple())
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel):
        g = await self.config.guild(channel.guild).all()
        if g["server"]["webhooks_update"] and not await self._is_exempt(channel.guild, channel.id, "server"):
            actor = await self._audit_actor(channel.guild, discord.AuditLogAction.webhook_create)
            e = discord.Embed(title="Webhooks updated", description=f"#{getattr(channel, 'name', 'unknown')}", color=discord.Color.blurple())
            if actor: e.add_field(name="By", value=actor)
            await self._send(channel.guild, e, channel.id)

    # invites
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        g = await self.config.guild(invite.guild).all()
        if g["invites"]["create"]:
            e = discord.Embed(title="Invite created", color=discord.Color.green())
            e.add_field(name="Code", value=invite.code)
            if invite.channel:
                e.add_field(name="Channel", value=invite.channel.mention)
            await self._send(invite.guild, e, getattr(invite.channel, 'id', None))

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        g = await self.config.guild(invite.guild).all()
        if g["invites"]["delete"]:
            e = discord.Embed(title="Invite deleted", color=discord.Color.red())
            e.add_field(name="Code", value=invite.code)
            await self._send(invite.guild, e)

    # members
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        g = await self.config.guild(member.guild).all()
        if g["member"]["join"]:
            e = discord.Embed(title="Member joined", description=f"{member} ({member.id})", color=discord.Color.green())
            await self._send(member.guild, e)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        g = await self.config.guild(member.guild).all()
        if g["member"]["leave"]:
            e = discord.Embed(title="Member left", description=f"{member} ({member.id})", color=discord.Color.red())
            await self._send(member.guild, e)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        g = await self.config.guild(after.guild).all()
        if before.nick != after.nick and g["member"]["nick_changed"]:
            e = discord.Embed(title="Nickname changed", color=discord.Color.blurple())
            e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
            e.add_field(name="Before", value=before.nick or "None", inline=True)
            e.add_field(name="After", value=after.nick or "None", inline=True)
            await self._send(after.guild, e)
        if set(before.roles) != set(after.roles) and g["member"]["roles_changed"]:
            e = discord.Embed(title="Roles changed", color=discord.Color.blurple())
            e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
            await self._send(after.guild, e)
        if g["member"]["timeout"] and before.timed_out_until != after.timed_out_until:
            e = discord.Embed(title="Timeout updated", color=discord.Color.orange())
            e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
            e.add_field(name="Until", value=str(after.timed_out_until))
            await self._send(after.guild, e)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        g = await self.config.guild(guild).all()
        if g["member"]["ban"]:
            actor = await self._audit_actor(guild, discord.AuditLogAction.ban, getattr(user, 'id', None))
            e = discord.Embed(title="User banned", description=f"{user} ({user.id})", color=discord.Color.dark_red())
            if actor: e.add_field(name="By", value=actor)
            await self._send(guild, e)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        g = await self.config.guild(guild).all()
        if g["member"]["unban"]:
            actor = await self._audit_actor(guild, discord.AuditLogAction.unban, getattr(user, 'id', None))
            e = discord.Embed(title="User unbanned", description=f"{user} ({user.id})", color=discord.Color.green())
            if actor: e.add_field(name="By", value=actor)
            await self._send(guild, e)

    # voice
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        g = await self.config.guild(member.guild).all()
        ch = await self._log_channel(member.guild)
        if not ch:
            return
        if before.channel is None and after.channel is not None and g["voice"]["join"]:
            await ch.send(embed=discord.Embed(title="Voice join", description=f"{member} → {after.channel.mention}", color=discord.Color.green()))
            return
        if before.channel is not None and after.channel is None and g["voice"]["leave"]:
            await ch.send(embed=discord.Embed(title="Voice leave", description=f"{member} ← {before.channel.mention}", color=discord.Color.red()))
            return
        if before.channel and after.channel and before.channel.id != after.channel.id and g["voice"]["move"]:
            await ch.send(embed=discord.Embed(title="Voice move", description=f"{member}: {before.channel.mention} → {after.channel.mention}", color=discord.Color.blurple()))
        if g["voice"]["mute"] and (before.self_mute != after.self_mute or before.mute != after.mute):
            await ch.send(embed=discord.Embed(title="Mute state change", description=str(member), color=discord.Color.orange()))
        if g["voice"]["deaf"] and (before.self_deaf != after.self_deaf or before.deaf != after.deaf):
            await ch.send(embed=discord.Embed(title="Deaf state change", description=str(member), color=discord.Color.orange()))
        if g["voice"]["video"] and (before.self_video != after.self_video):
            await ch.send(embed=discord.Embed(title="Video state change", description=f"{member} → {'on' if after.self_video else 'off'}", color=discord.Color.blurple()))
        if g["voice"]["stream"] and (before.self_stream != after.self_stream):
            await ch.send(embed=discord.Embed(title="Stream state change", description=f"{member} → {'on' if after.self_stream else 'off'}", color=discord.Color.blurple()))

    # scheduled events (Discord Community Events)
    @commands.Cog.listener()
    async def on_guild_scheduled_event_create(self, event: discord.ScheduledEvent):
        g = await self.config.guild(event.guild).all()
        if g["sched"]["create"]:
            e = discord.Embed(title="Scheduled event created", description=event.name, color=discord.Color.green())
            await self._send(event.guild, e)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent):
        g = await self.config.guild(after.guild).all()
        if g["sched"]["update"]:
            e = discord.Embed(title="Scheduled event updated", description=after.name, color=discord.Color.blurple())
            await self._send(after.guild, e)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_delete(self, event: discord.ScheduledEvent):
        g = await self.config.guild(event.guild).all()
        if g["sched"]["delete"]:
            e = discord.Embed(title="Scheduled event deleted", description=event.name, color=discord.Color.red())
            await self._send(event.guild, e)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_user_add(self, event: discord.ScheduledEvent, user: discord.User):
        g = await self.config.guild(event.guild).all()
        if g["sched"]["user_add"]:
            e = discord.Embed(title="Event RSVP added", description=f"{user} → {event.name}", color=discord.Color.green())
            await self._send(event.guild, e)

    @commands.Cog.listener()
    async def on_guild_scheduled_event_user_remove(self, event: discord.ScheduledEvent, user: discord.User):
        g = await self.config.guild(event.guild).all()
        if g["sched"]["user_remove"]:
            e = discord.Embed(title="Event RSVP removed", description=f"{user} ✕ {event.name}", color=discord.Color.red())
            await self._send(event.guild, e)


async def setup(bot: Red) -> None:
    await bot.add_cog(LogPlus(bot))
