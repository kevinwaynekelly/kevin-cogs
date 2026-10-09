from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import OrderedDict, defaultdict, deque
from datetime import timedelta
from typing import Literal, Optional, Union

import discord
from discord.ext import commands
from redbot.core import app_commands
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config

from .command_support import (
    attach_prefix_groups,
    check_command,
    finish_configuration_audit,
    invoke_shortcut,
    prefix_group,
    prepare_hybrid,
)
from .constants import _UI, DEFAULTS_GUILD, EVENT_STYLE
from .delivery import CATEGORIES, FEATURE_DEFAULTS, EventEmbed, LogDelivery, fresh_status
from .diffs import attribute_changes, overwrite_changes, permission_changes
from .events import guild_enabled
from .history import HISTORY_DEFAULTS, LogHistory, export_history
from .incidents import ALERT_DEFAULTS, SUMMARY_DEFAULTS, IncidentCommands
from .interactive import SetupView, close_views
from .presentation import Presentation, settings

log = logging.getLogger(__name__)

EVENT_SWITCHES = tuple(
    f"{group}.{key}"
    for group in (
        "message",
        "reactions",
        "server",
        "invites",
        "member",
        "voice",
        "sched",
        "commands",
    )
    for key, default in DEFAULTS_GUILD[group].items()
    if isinstance(default, bool)
)


# ========================= Styling =========================

# Generic UI emojis (for embeds, not events)

# ========================= Defaults =========================


class LogPlus(IncidentCommands, LogDelivery, LogHistory, redcommands.Cog):
    """Server event logging with channel routing and audit attribution."""

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    def __init__(self, bot: Red) -> None:
        attach_prefix_groups(self)
        self.bot: Red = bot
        self._presentation = Presentation("LogPlus", "log")
        self.config: Config = Config.get_conf(self, identifier=0x51A7E11, force_registration=True)
        self.config.register_guild(
            **DEFAULTS_GUILD,
            features=FEATURE_DEFAULTS,
            history_settings=HISTORY_DEFAULTS,
            history_records=[],
            alert_settings=ALERT_DEFAULTS,
            moderation_summary=SUMMARY_DEFAULTS,
            incident_cases={},
        )
        # Red reuses Config instances on reload, including previously registered defaults.
        self.config._defaults[Config.GUILD]["member"].pop("presence", None)

        self._last_event_at = OrderedDict()
        self._settings_cache = {}
        self._settings_locks = defaultdict(asyncio.Lock)
        self._audit_cache = {}
        self._audit_locks = defaultdict(asyncio.Lock)
        self._retry_queues = defaultdict(deque)
        self._retry_tasks = {}
        self._delivery_status = defaultdict(fresh_status)
        self._own_log_ids = OrderedDict()
        self._views = set()
        self._closing = False
        self._history_task = None
        self._moderation_task = None
        self._alert_tasks = set()
        self._alert_windows = OrderedDict()
        self._alert_sent = {}
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

    async def _is_compact(self, guild):
        return bool((await self._settings(guild))["style"]["compact"])

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
            color = style["color"]
        e = EventEmbed(
            event_type=etype,
            title=title,
            description=description,
            color=color or discord.Color.blurple(),
            timestamp=self._now(),
        )
        if footer:
            e.set_footer(text=footer)
        return self._presentation.style(e)

    async def _E(
        self,
        guild: discord.Guild,
        title: str,
        description: Optional[str] = None,
        *,
        color=None,
        footer=None,
        etype=None,
    ) -> discord.Embed:
        return self._mk_embed(
            title,
            description,
            color=color,
            footer=footer,
            etype=etype,
            compact=await self._is_compact(guild),
        )

    async def _log_channel(self, guild, source_channel_id=None, category=None):
        settings = await self._settings(guild)
        overrides = settings["overrides"]
        destination = None
        if source_channel_id is not None:
            destination = overrides.get(str(source_channel_id))
            source = guild.get_channel_or_thread(source_channel_id)
            if not destination and isinstance(source, discord.Thread):
                destination = overrides.get(str(source.parent_id))
        destination = (
            destination or settings["features"]["routes"].get(category) or settings["log_channel"]
        )
        channel = guild.get_channel(int(destination)) if destination else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _is_exempt(self, guild, channel_id, group):
        if not channel_id:
            return False
        settings = await self._settings(guild)
        excluded = settings.get(group, {}).get("exempt_channels", [])
        channel = guild.get_channel_or_thread(channel_id)
        return channel_id in excluded or (
            isinstance(channel, discord.Thread) and channel.parent_id in excluded
        )

    async def _send(
        self, guild, embed, source_channel_id=None, category=None, *, permission_change=False
    ):
        if self._closing:
            return
        record = self._pending_log(embed, source_channel_id, category)
        record.permission_change = permission_change
        try:
            await self._observe_log(guild, record)
        except Exception:
            log.exception("Moderation observer failed; continuing normal log delivery")
        if (await self._settings(guild))["history_settings"]["enabled"]:
            await self._save_history(guild, embed, record)
        try:
            if not await self._deliver_log(guild, record):
                self._delivery_status[guild.id]["dropped"] += 1
        except (discord.HTTPException, asyncio.TimeoutError) as error:
            if self._closing:
                return
            log.warning(
                "Server log notification delivery failed",
                extra={
                    "notification_error": type(error).__name__,
                    "notification_stage": "log_delivery",
                    "notification_guild_id": guild.id,
                },
            )
            self._delivery_failed(guild.id, error)
            if (await self._settings(guild))["features"]["retry"]:
                self._enqueue_log(guild, record)
            else:
                self._delivery_status[guild.id]["dropped"] += 1

    async def _audit_actor(self, guild, action, target_id=None):
        return await self._audit_actor_recent(guild, action, target_id=target_id)

    async def _audit_actor_recent(
        self, guild, actions, *, target_id=None, channel_id=None, lookback_s=60
    ):
        if not guild.me or not guild.me.guild_permissions.view_audit_log:
            return None
        if isinstance(actions, discord.AuditLogAction) or actions is None:
            actions = {actions}
        else:
            actions = set(actions)
        actions.discard(None)
        if not actions:
            return None
        settings = await self._settings(guild)
        if (
            not settings["log_channel"]
            and not settings["overrides"]
            and not settings["features"]["routes"]
        ):
            return None
        try:
            async with self._audit_locks[guild.id]:
                now = time.monotonic()
                cached = self._audit_cache.get(guild.id)
                if cached is None or now - cached[0] >= 1:
                    entries = [
                        entry
                        async for entry in guild.audit_logs(
                            limit=25, after=self._now() - timedelta(seconds=60)
                        )
                    ]
                    cached = (now, entries)
                    self._audit_cache[guild.id] = cached
            cutoff = self._now() - timedelta(seconds=max(1, lookback_s))
            for entry in cached[1]:
                if entry.created_at < cutoff or entry.action not in actions:
                    continue
                if target_id is not None and getattr(entry.target, "id", None) != target_id:
                    continue
                if channel_id is not None:
                    extra_channel = getattr(getattr(entry, "extra", None), "channel", None)
                    if (
                        getattr(extra_channel, "id", None) != channel_id
                        and getattr(entry.target, "id", None) != channel_id
                    ):
                        continue
                if entry.user is not None:
                    return f"{entry.user} ({entry.user.id})"
        except discord.HTTPException:
            log.debug("Audit attribution unavailable", exc_info=True)
        return None

    async def _rate_seconds(self, guild):
        return max(0.0, float((await self._settings(guild))["rate"]["seconds"]))

    def _should_suppress(self, key, window_s):
        if not math.isfinite(window_s) or window_s <= 0:
            return False
        now = time.monotonic()
        previous = self._last_event_at.get(key)
        if previous is not None and now - previous < window_s:
            return True
        self._last_event_at[key] = now
        self._last_event_at.move_to_end(key)
        while len(self._last_event_at) > 10000:
            self._last_event_at.popitem(last=False)
        return False

    # ---------- pretty status ----------
    async def _status_embed(self, guild: discord.Guild) -> discord.Embed:
        g = await self._settings(guild)
        log_ch = guild.get_channel(g["log_channel"]) if g["log_channel"] else None
        e = self._presentation.embed(
            "Status", "Event logging and delivery settings for this server."
        )
        e.add_field(
            name="Delivery",
            value=f"**Default channel** · {getattr(log_ch, 'mention', 'Not set')}\n"
            f"**Routing overrides** · {len(g['overrides'])}\n**Duplicate window** · {g['rate']['seconds']:g}s\n"
            f"**Compact event headers** · {'Enabled' if g['style']['compact'] else 'Disabled'}",
            inline=False,
        )
        groups = [
            ("message", "Messages"),
            ("reactions", "Reactions"),
            ("server", "Server changes"),
            ("invites", "Invites"),
            ("member", "Members"),
            ("voice", "Voice"),
            ("sched", "Scheduled events"),
            ("commands", "Commands"),
        ]
        for key, label in groups:
            values = []
            for setting, value in g[key].items():
                name = setting.replace("_", " ").capitalize()
                if isinstance(value, bool):
                    values.append(f"**{name}** · {'Enabled' if value else 'Disabled'}")
                elif setting == "exempt_channels":
                    values.append(f"**Excluded channels** · {len(value)}")
            e.add_field(name=label, value="\n".join(values), inline=True)
        e.add_field(
            name="Delivery recovery",
            value=f"**Category routes** · {len(g['features']['routes'])}\n"
            f"**Retries** · {'Enabled' if g['features']['retry'] else 'Disabled'}\n"
            f"**Pending events** · {len(self._retry_queues.get(guild.id, ()))}\n"
            f"**Last error** · {self._delivery_status[guild.id]['last_error']}",
            inline=False,
        )
        return e

    # ---------------- commands: main & settings ----------------
    @redcommands.hybrid_group(name="log", invoke_without_command=True, fallback="status")
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def logplus(self, ctx: redcommands.Context):
        """Configure server event logs and channel routing.

        Run this command alone to see settings and event switches. Set a default channel or a
        source-channel route before logs can be delivered.
        """
        await self._reply(ctx, embed=await self._status_embed(ctx.guild))

    @logplus.command(name="help")
    async def help_(self, ctx: redcommands.Context):
        """Show the event logging command overview."""
        p = ctx.clean_prefix
        e = discord.Embed(title="LogPlus - Commands", color=discord.Color.blurple())
        e.add_field(
            name="Core",
            value=(
                f"• `{p}log` • `{p}log help` • `{p}log diag`\n"
                f"• `{p}log channel` • `{p}log setchannel #chan` • `{p}log clearchannel`\n"
                f"• `{p}log route set #source #dest` • `{p}log route clear #source` • `{p}log route list`\n"
                f"• `{p}log rate [seconds]`\n"
                f"• `{p}log style compact <on|off>` • `{p}log style preview`"
            ),
            inline=False,
        )
        e.add_field(
            name="Direct and slash commands",
            value=f"• `{p}logstatus` • `{p}logchannel [#channel]` • `{p}lograte [seconds]`\n"
            "• `/log status` • `/log route set`\n"
            "• `/log event` selects an event and turns it on or off, including scheduled events.",
            inline=False,
        )
        e.add_field(
            name="Retained history",
            value=f"`{p}log history enabled true` · `{p}log history retention <days>`\n`{p}timeline @Member` · `{p}logsearch <query>` · `{p}logexport [json|csv]`",
            inline=False,
        )
        e.add_field(
            name="Toggles",
            value=(
                f"• `{p}log toggle message <edit|delete|bulk|pins>`\n"
                f"• `{p}log toggle reactions <add|remove|clear>`\n"
                f"• `{p}log toggle server <channelcreate|channeldelete|channelupdate|rolecreate|roledelete|roleupdate|serverupdate|emojiupdate|stickerupdate|integrationsupdate|webhooksupdate|threadcreate|threaddelete|thredupdate>`\n"
                f"• `{p}log toggle invites <create|delete>`\n"
                f"• `{p}log toggle member <join|leave|roles|nick|ban|unban|timeout>`\n"
                f"• `{p}log toggle voice <join|move|leave|mute|deaf|video|stream>`\n"
                f"• `{p}log toggle commands <thisbot|otherbots>`"
            ),
            inline=False,
        )
        e.add_field(
            name="Notes",
            value="Routing checks the source channel, its thread parent, the event category, then the default channel.",
            inline=False,
        )
        e.add_field(
            name="Routing and recovery",
            value=f"`{p}log route category <category> [#channel]` · `{p}log route categories`\n"
            f"`{p}log ignore add <channel> [scope]` · `remove <channel> [scope]` · `list`\n"
            f"`{p}log delivery [retry]` · `{p}log setup`",
            inline=False,
        )
        await self._reply(ctx, embed=e)

    @logplus.command(name="rate")
    async def cmd_rate(self, ctx: redcommands.Context, seconds: Optional[float] = None):
        """Show or set the duplicate-event suppression window.

        Omit the value to show the current window. Use zero to disable suppression. The value
        must be finite and nonnegative.
        """
        if seconds is None:
            cur = await self._rate_seconds(ctx.guild)
            return await self._reply(
                ctx, embed=await self._E(ctx.guild, "Rate limit", f"Current window: **{cur:.2f}s**")
            )
        if not math.isfinite(seconds) or seconds < 0:
            return await self._reply(
                ctx,
                embed=await self._E(
                    ctx.guild,
                    "Rate limit",
                    f"{_UI['warn']} Seconds must be finite and ≥ 0.",
                    color=discord.Color.orange(),
                ),
            )
        await self.config.guild(ctx.guild).rate.seconds.set(float(seconds))
        await self._reply(
            ctx,
            embed=await self._E(
                ctx.guild, "Rate limit", f"{_UI['ok']} Window set to **{seconds:.2f}s**"
            ),
        )

    @logplus.group(name="style", autohelp=False)
    async def style(self, ctx: redcommands.Context):
        """Configure and preview event log presentation."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @style.command(name="compact")
    async def style_compact(self, ctx: redcommands.Context, flag: Optional[str] = None):
        """Show or set emojis in compact event headers."""
        if flag is None:
            cur = await self.config.guild(ctx.guild).style.compact()
            return await self._reply(
                ctx,
                embed=await self._E(
                    ctx.guild,
                    "Style: compact",
                    f"Compact style is **{'ON' if cur else 'OFF'}**.",
                ),
            )
        flag = flag.lower()
        if flag not in {"on", "off"}:
            return await self._reply(
                ctx,
                embed=await self._E(
                    ctx.guild,
                    "Style: compact",
                    "Use `on` or `off`.",
                    color=discord.Color.orange(),
                ),
            )
        await self.config.guild(ctx.guild).style.compact.set(flag == "on")
        await self._reply(
            ctx,
            embed=await self._E(ctx.guild, "Style: compact", f"Compact style **{flag.upper()}**."),
        )

    @style.command(name="preview")
    async def style_preview(self, ctx: redcommands.Context):
        """Send three sample event log notices."""
        samples = [
            ("Message deleted", "message_deleted"),
            ("Reaction added", "reaction_added"),
            ("Channel created", "channel_created"),
        ]
        for title, etype in samples:
            await self._reply(ctx, embed=await self._E(ctx.guild, title, etype=etype))

    # ---------------- intuitive channel commands ----------------
    @logplus.command(name="channel")
    async def channel_show(self, ctx: redcommands.Context):
        """Show the default destination for event logs."""
        cid = await self.config.guild(ctx.guild).log_channel()
        ch = ctx.guild.get_channel(cid) if cid else None
        msg = f"Current log channel: **{getattr(ch, 'mention', 'not set')}**"
        await self._reply(ctx, embed=await self._E(ctx.guild, "Log channel", msg))

    @logplus.command(name="setchannel")
    async def channel_set(self, ctx: redcommands.Context, channel: discord.TextChannel):
        """Set the default destination for event logs."""
        await self.config.guild(ctx.guild).log_channel.set(channel.id)
        await self._reply(
            ctx,
            embed=await self._E(ctx.guild, "Log channel", f"{_UI['ok']} Set to {channel.mention}."),
        )

    @logplus.command(name="clearchannel")
    async def channel_clear(self, ctx: redcommands.Context):
        """Clear the default log channel without removing routes.

        Existing source-channel routes remain active.
        """
        await self.config.guild(ctx.guild).log_channel.set(None)
        await self._reply(
            ctx,
            embed=await self._E(
                ctx.guild, "Log channel", f"{_UI['ok']} Cleared (logging disabled)."
            ),
        )

    # ---------------- per-channel routing overrides ----------------
    @logplus.group(name="route", autohelp=False)
    async def route(self, ctx: redcommands.Context):
        """Manage log destinations for specific source channels.

        Source-specific routes override the default log destination for events carrying that
        source channel.
        """
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @route.command(name="set")
    async def route_set(
        self, ctx: redcommands.Context, source: discord.TextChannel, dest: discord.TextChannel
    ):
        """Route logs from a source channel to a destination."""
        async with self.config.guild(ctx.guild).overrides() as overrides:
            overrides[str(source.id)] = dest.id
        await self._reply(
            ctx,
            embed=await self._E(
                ctx.guild, "Route", f"{_UI['ok']} {source.mention} → {dest.mention}"
            ),
        )

    @route.command(name="clear")
    async def route_clear(self, ctx: redcommands.Context, source: discord.TextChannel):
        """Remove a source-channel logging route."""
        async with self.config.guild(ctx.guild).overrides() as overrides:
            removed = overrides.pop(str(source.id), None)
        text = (
            f"{_UI['ok']} Cleared for {source.mention}"
            if removed is not None
            else "No override for that channel."
        )
        await self._reply(ctx, embed=await self._E(ctx.guild, "Route", text))

    @route.command(name="list")
    async def route_list(self, ctx: redcommands.Context):
        """List source-channel logging routes."""
        overrides = await self.config.guild(ctx.guild).overrides()
        lines = []
        if isinstance(overrides, dict) and overrides:
            for sid, did in overrides.items():
                s = ctx.guild.get_channel(int(sid))
                d = ctx.guild.get_channel(int(did))
                s_name = getattr(s, "mention", f"<#{sid}>")
                d_name = getattr(d, "mention", f"<#{did}>")
                lines.append(f"{s_name} → {d_name}")
        else:
            lines.append("(none)")
        e = await self._E(ctx.guild, "Routing overrides", settings("\n".join(lines), lang="ini"))
        await self._reply(ctx, embed=e)

    @route.command(name="category")
    @app_commands.choices(category=[app_commands.Choice(name=key, value=key) for key in CATEGORIES])
    async def route_category(
        self, ctx, category: str, channel: Optional[discord.TextChannel] = None
    ):
        """Set or clear a destination for an event category."""
        category = category.lower()
        if category not in CATEGORIES:
            raise redcommands.BadArgument("Choose " + ", ".join(CATEGORIES) + ".")
        async with self.config.guild(ctx.guild).features() as features:
            if channel:
                features["routes"][category] = channel.id
            else:
                features["routes"].pop(category, None)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @route.command(name="categories")
    async def route_categories(self, ctx):
        """List configured destinations for event categories."""
        routes = await self.config.guild(ctx.guild).features.routes()
        await self._reply(
            ctx,
            "\n".join(f"**{key}** · <#{uid}>" for key, uid in routes.items())
            or "No category routes configured.",
        )

    @logplus.group(name="ignore", autohelp=False)
    async def ignore(self, ctx):
        """Manage message and server-event channel exemptions."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @ignore.command(name="add")
    @app_commands.choices(
        scope=[app_commands.Choice(name=key, value=key) for key in ("message", "server", "all")]
    )
    async def ignore_add(
        self,
        ctx,
        channel: Union[
            discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel
        ],
        scope: str = "all",
    ):
        """Exclude a channel and its threads from selected logs."""
        await self._set_exemption(ctx.guild, channel.id, scope, True)
        await self._presentation.confirm(ctx)

    @ignore.command(name="remove")
    @app_commands.choices(
        scope=[app_commands.Choice(name=key, value=key) for key in ("message", "server", "all")]
    )
    async def ignore_remove(
        self,
        ctx,
        channel: Union[
            discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel
        ],
        scope: str = "all",
    ):
        """Remove message or server-event channel exemptions."""
        await self._set_exemption(ctx.guild, channel.id, scope, False)
        await self._presentation.confirm(ctx)

    async def _set_exemption(self, guild, channel_id, scope, enabled):
        if scope not in {"message", "server", "all"}:
            raise redcommands.BadArgument("Choose message, server, or all.")
        for name in ("message", "server") if scope == "all" else (scope,):
            async with self.config.guild(guild).get_attr(name)() as section:
                values = section["exempt_channels"]
                if enabled and channel_id not in values:
                    values.append(channel_id)
                elif not enabled and channel_id in values:
                    values.remove(channel_id)
        self._settings_cache.pop(guild.id, None)

    @ignore.command(name="list")
    async def ignore_list(self, ctx):
        """List channels excluded from message and server logs."""
        conf = await self._settings(ctx.guild)
        embed = self._presentation.embed("Channel exemptions")
        for scope in ("message", "server"):
            embed.add_field(
                name=scope.capitalize(),
                value="\n".join(f"<#{uid}>" for uid in conf[scope]["exempt_channels"]) or "None",
                inline=False,
            )
        await self._reply(ctx, embed=embed)

    @logplus.command(name="delivery")
    async def delivery(self, ctx, retry: Optional[bool] = None):
        """Inspect delivery failures and optionally toggle retries."""
        if retry is not None:
            async with self.config.guild(ctx.guild).features() as features:
                features["retry"] = retry
            self._settings_cache.pop(ctx.guild.id, None)
            if not retry:
                await self._cancel_retries(ctx.guild.id)
                queue = self._retry_queues.pop(ctx.guild.id, ())
                self._delivery_status[ctx.guild.id]["dropped"] += len(queue)
        status = self._delivery_status[ctx.guild.id]
        enabled = (await self._settings(ctx.guild))["features"]["retry"]
        embed = self._presentation.embed(
            "Delivery status",
            f"Retries {'enabled' if enabled else 'disabled'} · {len(self._retry_queues.get(ctx.guild.id, ()))} pending",
        )
        for key in ("delivered", "recovered", "failures", "dropped", "last_error"):
            embed.add_field(
                name=key.replace("_", " ").capitalize(), value=str(status[key]), inline=True
            )
        if status["last_failure"]:
            embed.add_field(name="Last failure", value=f"<t:{int(status['last_failure'])}:R>")
        await self._reply(ctx, embed=embed)

    @logplus.command(name="setup")
    async def setup(self, ctx):
        """Open guided destination and event-group setup."""

        async def update(ctx, key, value):
            if key == "channel":
                await self.config.guild(ctx.guild).log_channel.set(value)
            else:
                async with self.config.guild(ctx.guild).get_attr(key)() as section:
                    for option in section:
                        if isinstance(section[option], bool):
                            section[option] = value
            self._settings_cache.pop(ctx.guild.id, None)

        view = SetupView(
            self,
            ctx,
            "log setup",
            [
                ("channel", "Default destination", "text"),
                ("message", "Message logs", "toggle"),
                ("member", "Member logs", "toggle"),
                ("voice", "Voice logs", "toggle"),
                ("server", "Server changes", "toggle"),
            ],
            update,
        )
        view.message = await self._reply(
            ctx, "Choose a destination and the event groups to enable.", title="Setup", view=view
        )

    @redcommands.hybrid_command(name="logstatus")
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def logstatus(self, ctx: redcommands.Context):
        """Show event switches, the log channel, and routing settings."""
        await invoke_shortcut(self, ctx, self.logplus)

    @logplus.group(name="history", autohelp=False, fallback="status")
    async def history(self, ctx):
        """Configure optional local history and retention."""
        policy = await self.config.guild(ctx.guild).history_settings()
        records = await self._history_query(ctx.guild, days=90, limit=1000)
        await self._reply(
            ctx,
            f"Collecting: {'Yes' if policy['enabled'] else 'No'}\nRetention: {policy['days']} days\nRetained events: {len(records)} / 1000\nEnable collection explicitly; existing Discord logs are not imported.",
        )

    @history.command(name="enabled")
    async def history_enabled(self, ctx, enabled: bool):
        """Enable or pause collection of local event history."""
        await self._set_history_policy(ctx.guild, "enabled", enabled)
        await self._presentation.confirm(ctx)

    @history.command(name="retention")
    async def history_retention(self, ctx, days: int):
        """Set retention from 1 to 90 days and prune now."""
        if not 1 <= days <= 90:
            raise redcommands.BadArgument("Choose 1 to 90 days.")
        await self._set_history_policy(ctx.guild, "days", days)
        await self._presentation.confirm(ctx)

    @history.command(name="clear")
    async def history_clear(self, ctx, confirm: str):
        """Erase retained local events using confirmation yes."""
        if confirm != "yes":
            raise redcommands.BadArgument("Use log history clear yes to erase local history.")
        group = self.config.guild(ctx.guild).history_records
        async with group.get_lock():
            await group.set([])
        await self._presentation.confirm(ctx)

    @redcommands.hybrid_command(name="timeline")
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def timeline(self, ctx, member: discord.Member, days: int = 7):
        """Show recent retained events identifying a member."""
        await check_command(ctx, self.logplus)
        records = await self._history_query(
            ctx.guild, member_id=member.id, days=days, limit=25, ctx=ctx
        )
        await self._history_report(ctx, records)

    @redcommands.hybrid_command(name="logsearch")
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def logsearch(self, ctx, query: str, category: str = "all", days: int = 7):
        """Search retained event text by category and date."""
        await check_command(ctx, self.logplus)
        await self._history_report(
            ctx,
            await self._history_query(
                ctx.guild, query=query, category=category, days=days, limit=25, ctx=ctx
            ),
        )

    @redcommands.hybrid_command(name="logexport")
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    @redcommands.bot_has_permissions(attach_files=True)
    async def logexport(
        self,
        ctx,
        format: Literal["json", "csv"] = "json",
        member: Optional[discord.Member] = None,
        category: str = "all",
        days: int = 7,
        query: str = "",
    ):
        """Export filtered retained events as JSON or CSV."""
        await check_command(ctx, self.logplus)
        records = await self._history_query(
            ctx.guild,
            query=query,
            category=category,
            days=days,
            member_id=member.id if member else None,
            limit=1000,
            ctx=ctx,
        )
        await self._reply(
            ctx,
            f"Exported {len(records)} matching events.",
            file=discord.File(export_history(records, format), filename=f"log-history.{format}"),
        )

    @redcommands.hybrid_command(name="logchannel")
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def logchannel(
        self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None
    ):
        """Show the log channel, or set it to the supplied channel."""
        if channel is None:
            await invoke_shortcut(self, ctx, self.channel_show)
        else:
            await invoke_shortcut(self, ctx, self.channel_set, channel=channel)

    @redcommands.hybrid_command(name="lograte")
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def lograte(self, ctx: redcommands.Context, seconds: Optional[float] = None):
        """Show or set the duplicate-event suppression window."""
        await invoke_shortcut(self, ctx, self.cmd_rate, seconds=seconds)

    @logplus.command(name="event")
    @app_commands.describe(
        event="Event switch, such as message.delete or voice.join.",
        enabled="Turn this event on or off; omit to inspect it.",
    )
    async def event(self, ctx: redcommands.Context, event: str, enabled: Optional[bool] = None):
        """Show or set an event switch, including scheduled events."""
        event = event.lower()
        if event not in EVENT_SWITCHES:
            return await self._reply(
                ctx,
                "Unknown event. Choose one of:\n" + "\n".join(f"`{key}`" for key in EVENT_SWITCHES),
                tone="warning",
            )
        group, key = event.split(".")
        # Use the same section lock as the existing toggle commands.
        section = self.config.guild(ctx.guild).get_attr(group)
        if enabled is None:
            current = (await section())[key]
        else:
            async with section() as values:
                values[key] = current = enabled
        await self._reply(
            ctx,
            f"**{event}** · {'Enabled' if current else 'Disabled'}",
            tone="info" if enabled is None else "success",
        )

    @event.autocomplete("event")
    async def event_autocomplete(self, interaction: discord.Interaction, current: str):
        """Offer matching event switches within Discord's 25-choice limit."""
        query = current.lower()
        return [app_commands.Choice(name=key, value=key) for key in EVENT_SWITCHES if query in key][
            :25
        ]

    # ---------------- toggles (unchanged arguments) ----------------
    @prefix_group(logplus, autohelp=False)
    async def toggle(self, ctx: redcommands.Context):
        """Turn individual event logs on or off."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    async def _flip(self, ctx: redcommands.Context, group: str, key: str):
        async with self.config.guild(ctx.guild).get_attr(group)() as section:
            section[key] = not section[key]
            enabled = section[key]
        await self._reply(
            ctx,
            embed=await self._E(ctx.guild, "Toggle", f"{group}.{key} → **{self._onoff(enabled)}**"),
            tone="success",
        )

    # message toggles
    @toggle.group(autohelp=False)
    async def message(self, ctx: redcommands.Context):
        """Toggle logging for message events."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @message.command()
    async def edit(self, ctx: redcommands.Context):
        """Toggle message edit logging."""
        await self._flip(ctx, "message", "edit")

    @message.command()
    async def delete(self, ctx: redcommands.Context):
        """Toggle message deletion logging."""
        await self._flip(ctx, "message", "delete")

    @message.command(name="bulk")
    async def message_bulk(self, ctx: redcommands.Context):
        """Toggle bulk message deletion logging."""
        await self._flip(ctx, "message", "bulk_delete")

    @message.command()
    async def pins(self, ctx: redcommands.Context):
        """Toggle message pin change logging."""
        await self._flip(ctx, "message", "pins")

    # reactions toggles
    @toggle.group(autohelp=False)
    async def reactions(self, ctx: redcommands.Context):
        """Toggle logging for reaction events."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @reactions.command(name="add")
    async def react_add(self, ctx: redcommands.Context):
        """Toggle reaction addition logging."""
        await self._flip(ctx, "reactions", "add")

    @reactions.command(name="remove")
    async def react_remove(self, ctx: redcommands.Context):
        """Toggle reaction removal logging."""
        await self._flip(ctx, "reactions", "remove")

    @reactions.command(name="clear")
    async def react_clear(self, ctx: redcommands.Context):
        """Toggle reaction clearing logging."""
        await self._flip(ctx, "reactions", "clear")

    # server toggles
    @toggle.group(autohelp=False)
    async def server(self, ctx: redcommands.Context):
        """Toggle logging for server changes."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @server.command(name="channelcreate")
    async def t_sc_create(self, ctx: redcommands.Context):
        """Toggle channel creation logging."""
        await self._flip(ctx, "server", "channel_create")

    @server.command(name="channeldelete")
    async def t_sc_delete(self, ctx: redcommands.Context):
        """Toggle channel deletion logging."""
        await self._flip(ctx, "server", "channel_delete")

    @server.command(name="channelupdate")
    async def t_sc_update(self, ctx: redcommands.Context):
        """Toggle channel update logging."""
        await self._flip(ctx, "server", "channel_update")

    @server.command(name="rolecreate")
    async def t_sr_create(self, ctx: redcommands.Context):
        """Toggle role creation logging."""
        await self._flip(ctx, "server", "role_create")

    @server.command(name="roledelete")
    async def t_sr_delete(self, ctx: redcommands.Context):
        """Toggle role deletion logging."""
        await self._flip(ctx, "server", "role_delete")

    @server.command(name="roleupdate")
    async def t_sr_update(self, ctx: redcommands.Context):
        """Toggle role update logging."""
        await self._flip(ctx, "server", "role_update")

    @server.command(name="serverupdate")
    async def t_s_update(self, ctx: redcommands.Context):
        """Toggle server update logging."""
        await self._flip(ctx, "server", "server_update")

    @server.command(name="emojiupdate")
    async def t_e_update(self, ctx: redcommands.Context):
        """Toggle emoji update logging."""
        await self._flip(ctx, "server", "emoji_update")

    @server.command(name="stickerupdate")
    async def t_st_update(self, ctx: redcommands.Context):
        """Toggle sticker update logging."""
        await self._flip(ctx, "server", "sticker_update")

    @server.command(name="integrationsupdate")
    async def t_i_update(self, ctx: redcommands.Context):
        """Toggle integration update logging."""
        await self._flip(ctx, "server", "integrations_update")

    @server.command(name="webhooksupdate")
    async def t_w_update(self, ctx: redcommands.Context):
        """Toggle webhook update logging."""
        await self._flip(ctx, "server", "webhooks_update")

    @server.command(name="threadcreate")
    async def t_tc(self, ctx: redcommands.Context):
        """Toggle thread creation logging."""
        await self._flip(ctx, "server", "thread_create")

    @server.command(name="threaddelete")
    async def t_td(self, ctx: redcommands.Context):
        """Toggle thread deletion logging."""
        await self._flip(ctx, "server", "thread_delete")

    @server.command(name="threadupdate", aliases=["thredupdate"])
    async def t_tu(self, ctx: redcommands.Context):
        """Toggle thread update logging."""
        await self._flip(ctx, "server", "thread_update")

    # invites toggles
    @toggle.group(autohelp=False)
    async def invites(self, ctx: redcommands.Context):
        """Toggle logging for invite events."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @invites.command(name="create")
    async def t_inv_c(self, ctx: redcommands.Context):
        """Toggle invite creation logging."""
        await self._flip(ctx, "invites", "create")

    @invites.command(name="delete")
    async def t_inv_d(self, ctx: redcommands.Context):
        """Toggle invite deletion logging."""
        await self._flip(ctx, "invites", "delete")

    # member toggles
    @toggle.group(autohelp=False)
    async def member(self, ctx: redcommands.Context):
        """Toggle logging for member events."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @member.command(name="join")
    async def t_m_join(self, ctx: redcommands.Context):
        """Toggle member join logging."""
        await self._flip(ctx, "member", "join")

    @member.command(name="leave")
    async def t_m_leave(self, ctx: redcommands.Context):
        """Toggle member departure logging."""
        await self._flip(ctx, "member", "leave")

    @member.command(name="roles")
    async def t_m_roles(self, ctx: redcommands.Context):
        """Toggle member role change logging."""
        await self._flip(ctx, "member", "roles_changed")

    @member.command(name="nick")
    async def t_m_nick(self, ctx: redcommands.Context):
        """Toggle nickname change logging."""
        await self._flip(ctx, "member", "nick_changed")

    @member.command(name="ban")
    async def t_m_ban(self, ctx: redcommands.Context):
        """Toggle member ban logging."""
        await self._flip(ctx, "member", "ban")

    @member.command(name="unban")
    async def t_m_unban(self, ctx: redcommands.Context):
        """Toggle member unban logging."""
        await self._flip(ctx, "member", "unban")

    @member.command(name="timeout")
    async def t_m_timeout(self, ctx: redcommands.Context):
        """Toggle member timeout logging."""
        await self._flip(ctx, "member", "timeout")

    # voice toggles
    @toggle.group(autohelp=False)
    async def voice(self, ctx: redcommands.Context):
        """Toggle logging for voice events."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @voice.command(name="join")
    async def t_v_join(self, ctx: redcommands.Context):
        """Toggle voice channel join logging."""
        await self._flip(ctx, "voice", "join")

    @voice.command(name="move")
    async def t_v_move(self, ctx: redcommands.Context):
        """Toggle voice channel move logging."""
        await self._flip(ctx, "voice", "move")

    @voice.command(name="leave")
    async def t_v_leave(self, ctx: redcommands.Context):
        """Toggle voice channel departure logging."""
        await self._flip(ctx, "voice", "leave")

    @voice.command(name="mute")
    async def t_v_mute(self, ctx: redcommands.Context):
        """Toggle voice mute change logging."""
        await self._flip(ctx, "voice", "mute")

    @voice.command(name="deaf")
    async def t_v_deaf(self, ctx: redcommands.Context):
        """Toggle voice deafen change logging."""
        await self._flip(ctx, "voice", "deaf")

    @voice.command(name="video")
    async def t_v_video(self, ctx: redcommands.Context):
        """Toggle voice video change logging."""
        await self._flip(ctx, "voice", "video")

    @voice.command(name="stream")
    async def t_v_stream(self, ctx: redcommands.Context):
        """Toggle voice stream change logging."""
        await self._flip(ctx, "voice", "stream")

    # commands toggles
    @toggle.group(name="commands", aliases=["commands_"], autohelp=False)
    async def commands_(self, ctx: redcommands.Context):
        """Toggle logging for bot commands."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @commands_.command(name="thisbot")
    async def t_cmd_this(self, ctx: redcommands.Context):
        """Toggle this bot's command logging."""
        await self._flip(ctx, "commands", "this_bot")

    @commands_.command(name="otherbots")
    async def t_cmd_others(self, ctx: redcommands.Context):
        """Toggle other bots' apparent command logging."""
        await self._flip(ctx, "commands", "other_bots")

    # ---------------- diagnostics ----------------
    @logplus.command(name="diag")
    async def diag(self, ctx):
        """Check event switches, destinations, and audit access.

        Reads current settings without changing live switches.
        """
        settings = await self._settings(ctx.guild)
        switches = [
            (group, key, value)
            for group, section in settings.items()
            if isinstance(section, dict)
            for key, value in section.items()
            if isinstance(value, bool)
        ]
        destination = await self._log_channel(ctx.guild)
        lines = [
            f"Default destination: {getattr(destination, 'mention', 'not set')}",
            f"Routes: {len(settings['overrides'])}",
            f"Switches: {len(switches)}",
            f"Audit-log permission: {bool(ctx.guild.me and ctx.guild.me.guild_permissions.view_audit_log)}",
        ]
        lines.extend(f"{group}.{key} = {value}" for group, key, value in switches)
        embed = await self._E(ctx.guild, "Diagnostics", "\n".join(lines))
        await self._reply(ctx, embed=self._fit_embed(embed))

    # ---------------- listeners ----------------
    # messages
    @commands.Cog.listener()
    @guild_enabled
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not (before.guild and before.author) or before.author.bot:
            return
        g = await self._settings(before.guild)
        if not g["message"]["edit"] or before.content == after.content:
            return
        if await self._is_exempt(before.guild, before.channel.id, "message"):
            return
        e = await self._E(
            before.guild,
            "Message edited",
            etype="message_edited",
            footer=f"Author ID: {before.author.id}",
        )
        e.add_field(name="Author", value=f"{before.author} ({before.author.id})", inline=False)
        e.add_field(name="Channel", value=before.channel.mention, inline=True)
        e.add_field(name="By", value=f"{before.author} ({before.author.id})", inline=True)
        e.add_field(name="Jump", value=f"[link]({after.jump_url})", inline=True)
        e.add_field(name="Before", value=(before.content or "<empty>")[:1000], inline=False)
        e.add_field(name="After", value=(after.content or "<empty>")[:1000], inline=False)
        await self._send(before.guild, e, before.channel.id)

    @commands.Cog.listener()
    @guild_enabled
    async def on_message_delete(self, message: discord.Message):
        if not (message.guild and message.author) or message.author.bot:
            return
        g = await self._settings(message.guild)
        if not g["message"]["delete"]:
            return
        if await self._is_exempt(message.guild, message.channel.id, "message"):
            return
        actor = await self._audit_actor_recent(
            message.guild,
            [
                discord.AuditLogAction.message_delete,
                discord.AuditLogAction.message_bulk_delete,
            ],
            target_id=getattr(message.author, "id", None),
            channel_id=getattr(message.channel, "id", None),
            lookback_s=60,
        )
        e = await self._E(
            message.guild,
            "Message deleted",
            etype="message_deleted",
            footer=f"Author ID: {message.author.id}",
        )
        e.add_field(name="Author", value=f"{message.author} ({message.author.id})", inline=False)
        e.add_field(name="Channel", value=message.channel.mention, inline=True)
        e.add_field(name="By", value=actor or "Author / Unknown (no audit entry)", inline=True)
        if message.content:
            e.add_field(name="Content", value=message.content[:1024], inline=False)
        await self._send(message.guild, e, message.channel.id)

    async def _raw_message_allowed(self, payload, switch):
        if payload.guild_id is None or getattr(payload, "cached_message", None) is not None:
            return None
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or await self.bot.cog_disabled_in_guild(self, guild):
            return None
        conf = await self._settings(guild)
        if not conf["message"][switch] or await self._is_exempt(
            guild, payload.channel_id, "message"
        ):
            return None
        if (guild.id, payload.message_id) in self._own_log_ids:
            return None
        author = getattr(payload, "data", {}).get("author")
        if author and (author.get("bot") or author.get("id") == str(self.bot.user.id)):
            return None
        # Uncached deletes do not identify an author. Avoid logging our own destinations.
        destinations = {
            conf["log_channel"],
            *conf["overrides"].values(),
            *conf["features"]["routes"].values(),
        }
        if author is None and payload.channel_id in destinations:
            return None
        return guild

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if "content" not in payload.data:
            return
        guild = await self._raw_message_allowed(payload, "edit")
        if guild is None:
            return
        author = payload.data.get("author", {})
        embed = await self._E(guild, "Message edited outside cache", etype="message_edited")
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>")
        embed.add_field(name="Message ID", value=str(payload.message_id))
        embed.add_field(name="Author", value=author.get("id", "Unavailable in this event"))
        embed.add_field(name="Before", value="Unavailable, message was not cached.", inline=False)
        embed.add_field(name="After", value=payload.data["content"] or "<empty>", inline=False)
        embed.add_field(
            name="Jump",
            value=f"https://discord.com/channels/{guild.id}/{payload.channel_id}/{payload.message_id}",
            inline=False,
        )
        await self._send(guild, embed, payload.channel_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        guild = await self._raw_message_allowed(payload, "delete")
        if guild is None:
            return
        embed = await self._E(guild, "Message deleted outside cache", etype="message_deleted")
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>")
        embed.add_field(name="Message ID", value=str(payload.message_id))
        embed.add_field(
            name="Author and content", value="Unavailable, message was not cached.", inline=False
        )
        await self._send(guild, embed, payload.channel_id)

    @commands.Cog.listener()
    @guild_enabled
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        g = await self._settings(guild)
        if not g["message"]["bulk_delete"]:
            return
        if await self._is_exempt(guild, payload.channel_id, "message"):
            return
        ch = guild.get_channel_or_thread(payload.channel_id)
        actor = await self._audit_actor_recent(
            guild,
            [
                discord.AuditLogAction.message_bulk_delete,
                discord.AuditLogAction.message_delete,
            ],
            channel_id=payload.channel_id,
            lookback_s=60,
        )
        e = await self._E(
            guild,
            "Bulk delete",
            description=f"{len(payload.message_ids)} messages",
            etype="bulk_delete",
        )
        if isinstance(ch, discord.TextChannel):
            e.add_field(name="Channel", value=ch.mention, inline=True)
        e.add_field(name="By", value=actor or "Unknown (no audit entry)", inline=True)
        await self._send(guild, e, payload.channel_id)

    @commands.Cog.listener()
    @guild_enabled
    async def on_guild_channel_pins_update(self, channel: discord.abc.GuildChannel, last_pin):
        guild = channel.guild
        g = await self._settings(guild)
        if not g["message"]["pins"]:
            return
        if await self._is_exempt(guild, channel.id, "message"):
            return
        actor = await self._audit_actor_recent(
            guild,
            [
                getattr(discord.AuditLogAction, "message_pin", None),
                getattr(discord.AuditLogAction, "message_unpin", None),
            ],
            channel_id=getattr(channel, "id", None),
            lookback_s=60,
        )
        e = await self._E(
            guild,
            "Pins updated",
            description=f"#{getattr(channel, 'name', 'unknown')}",
            etype="pins_updated",
        )
        if last_pin:
            try:
                e.add_field(
                    name="Last Pin",
                    value=discord.utils.format_dt(last_pin, style="R"),
                    inline=True,
                )
            except Exception:
                pass
        e.add_field(name="By", value=actor or "Unknown (no audit entry)", inline=True)
        await self._send(guild, e, channel.id)

    # reactions
    @commands.Cog.listener()
    @guild_enabled
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        g = await self._settings(guild)
        if not g["reactions"]["add"]:
            return
        if await self._is_exempt(guild, payload.channel_id, "message"):
            return
        if self._should_suppress(
            f"react_add:{payload.guild_id}:{payload.channel_id}:{payload.message_id}:{str(payload.emoji)}:{payload.user_id}",
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
    @guild_enabled
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        g = await self._settings(guild)
        if not g["reactions"]["remove"]:
            return
        if await self._is_exempt(guild, payload.channel_id, "message"):
            return
        if self._should_suppress(
            f"react_rm:{payload.guild_id}:{payload.channel_id}:{payload.message_id}:{str(payload.emoji)}:{payload.user_id}",
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
    @guild_enabled
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        g = await self._settings(guild)
        if not g["reactions"]["clear"]:
            return
        if await self._is_exempt(guild, payload.channel_id, "message"):
            return
        jump = f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}"
        e = await self._E(guild, "Reactions cleared", etype="reaction_cleared")
        e.add_field(name="Message", value=f"[jump]({jump})", inline=True)
        e.add_field(
            name="By",
            value="Unknown (Discord does not audit reaction clears)",
            inline=True,
        )
        await self._send(guild, e, payload.channel_id)

    # server structure
    @commands.Cog.listener()
    @guild_enabled
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        g = await self._settings(channel.guild)
        if g["server"]["channel_create"] and not await self._is_exempt(
            channel.guild, channel.id, "server"
        ):
            actor = await self._audit_actor_recent(
                channel.guild,
                discord.AuditLogAction.channel_create,
                target_id=getattr(channel, "id", None),
            )
            e = await self._E(
                channel.guild,
                "Channel created",
                description=channel.mention,
                etype="channel_created",
            )
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(channel.guild, e, getattr(channel, "id", None))

    @commands.Cog.listener()
    @guild_enabled
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        g = await self._settings(channel.guild)
        if g["server"]["channel_delete"] and not await self._is_exempt(
            channel.guild, channel.id, "server"
        ):
            actor = await self._audit_actor_recent(
                channel.guild,
                discord.AuditLogAction.channel_delete,
                target_id=getattr(channel, "id", None),
            )
            e = await self._E(
                channel.guild,
                "Channel deleted",
                description=f"#{channel.name}",
                etype="channel_deleted",
            )
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(channel.guild, e, getattr(channel, "id", None))

    @commands.Cog.listener()
    @guild_enabled
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ):
        g = await self._settings(after.guild)
        if g["server"]["channel_update"] and not await self._is_exempt(
            after.guild, after.id, "server"
        ):
            changes = attribute_changes(
                before,
                after,
                (
                    "name",
                    "topic",
                    "nsfw",
                    "slowmode_delay",
                    "bitrate",
                    "user_limit",
                    "rtc_region",
                ),
            )
            overwrites = overwrite_changes(before, after)
            if not changes and not overwrites:
                return

            actor = await self._audit_actor_recent(
                after.guild,
                [
                    discord.AuditLogAction.channel_update,
                    discord.AuditLogAction.overwrite_create,
                    discord.AuditLogAction.overwrite_update,
                    discord.AuditLogAction.overwrite_delete,
                ],
                target_id=after.id,
            )
            e = await self._E(
                after.guild,
                "Channel updated",
                description="\n".join(changes),
                etype="channel_updated",
            )
            e.add_field(name="Channel", value=after.mention, inline=True)
            e.add_field(name="Recent audit actor", value=actor or "Unknown", inline=True)
            for target, changes in overwrites:
                e.add_field(name=target, value=changes, inline=False)
            await self._send(after.guild, e, after.id, permission_change=bool(overwrites))

    @commands.Cog.listener()
    @guild_enabled
    async def on_guild_role_create(self, role: discord.Role):
        g = await self._settings(role.guild)
        if g["server"]["role_create"]:
            actor = await self._audit_actor_recent(
                role.guild,
                discord.AuditLogAction.role_create,
                target_id=getattr(role, "id", None),
            )
            e = await self._E(
                role.guild,
                "Role created",
                description=role.mention,
                etype="role_created",
            )
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(role.guild, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_guild_role_delete(self, role: discord.Role):
        g = await self._settings(role.guild)
        if g["server"]["role_delete"]:
            actor = await self._audit_actor_recent(
                role.guild,
                discord.AuditLogAction.role_delete,
                target_id=getattr(role, "id", None),
            )
            e = await self._E(
                role.guild, "Role deleted", description=role.name, etype="role_deleted"
            )
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(role.guild, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        g = await self._settings(after.guild)
        if g["server"]["role_update"]:
            changes = attribute_changes(
                before, after, ("name", "color", "hoist", "mentionable", "position")
            )
            changes.extend(permission_changes(before.permissions, after.permissions))
            if not changes:
                return
            actor = await self._audit_actor_recent(
                after.guild,
                discord.AuditLogAction.role_update,
                target_id=getattr(after, "id", None),
            )
            e = await self._E(
                after.guild,
                "Role updated",
                description=after.mention + "\n" + "\n".join(changes),
                etype="role_updated",
            )
            e.add_field(name="Recent audit actor", value=actor or "Unknown", inline=True)
            await self._send(
                after.guild,
                e,
                permission_change=bool(permission_changes(before.permissions, after.permissions)),
            )

    @commands.Cog.listener()
    @guild_enabled
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        g = await self._settings(after)
        if g["server"]["server_update"]:
            changes = attribute_changes(
                before,
                after,
                (
                    "name",
                    "description",
                    "verification_level",
                    "explicit_content_filter",
                    "afk_timeout",
                    "afk_channel",
                    "system_channel",
                    "default_notifications",
                    "premium_progress_bar_enabled",
                    "icon",
                    "banner",
                    "splash",
                ),
            )
            if not changes:
                return
            actor = await self._audit_actor_recent(
                after, discord.AuditLogAction.guild_update, lookback_s=60
            )
            e = await self._E(
                after, "Server updated", description="\n".join(changes), etype="server_updated"
            )
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(after, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        g = await self._settings(guild)
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
    @guild_enabled
    async def on_guild_stickers_update(self, guild: discord.Guild, before, after):
        g = await self._settings(guild)
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
    @guild_enabled
    async def on_guild_integrations_update(self, guild: discord.Guild):
        g = await self._settings(guild)
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
    @guild_enabled
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel):
        g = await self._settings(channel.guild)
        if g["server"]["webhooks_update"] and not await self._is_exempt(
            channel.guild, channel.id, "server"
        ):
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
                channel.guild,
                "Webhooks updated",
                description=f"#{getattr(channel, 'name', 'unknown')}",
                etype="webhooks_updated",
            )
            e.add_field(name="By", value=actor or "Unknown (no audit entry)", inline=True)
            await self._send(channel.guild, e, channel.id)

    @commands.Cog.listener()
    @guild_enabled
    async def on_invite_create(self, invite: discord.Invite):
        guild = self.bot.get_guild(getattr(invite.guild, "id", None))
        if guild is None:
            return
        g = await self._settings(guild)
        if g["invites"]["create"]:
            e = await self._E(guild, "Invite created", etype="invite_created")
            e.add_field(name="Code", value=invite.code, inline=True)
            if invite.channel:
                e.add_field(name="Channel", value=invite.channel.mention, inline=True)
            inv = getattr(invite, "inviter", None)
            e.add_field(
                name="By",
                value=(f"{inv} ({inv.id})" if inv else "Unknown"),
                inline=True,
            )
            await self._send(guild, e, getattr(invite.channel, "id", None))

    @commands.Cog.listener()
    @guild_enabled
    async def on_invite_delete(self, invite: discord.Invite):
        guild = self.bot.get_guild(getattr(invite.guild, "id", None))
        if guild is None:
            return
        g = await self._settings(guild)
        if g["invites"]["delete"]:
            actor = await self._audit_actor_recent(
                guild,
                getattr(discord.AuditLogAction, "invite_delete", None),
                lookback_s=60,
            )
            e = await self._E(guild, "Invite deleted", etype="invite_deleted")
            e.add_field(name="Code", value=invite.code, inline=True)
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(guild, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_member_join(self, member: discord.Member):
        g = await self._settings(member.guild)
        if g["member"]["join"]:
            e = await self._E(
                member.guild,
                "Member joined",
                description=f"{member} ({member.id})",
                etype="member_joined",
            )
            e.add_field(name="By", value=f"{member} ({member.id})", inline=True)
            await self._send(member.guild, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_member_remove(self, member: discord.Member):
        g = await self._settings(member.guild)
        if g["member"]["leave"]:
            # IMPROVED: Check for KICK
            kick_actor = await self._audit_actor_recent(
                member.guild,
                discord.AuditLogAction.kick,
                target_id=member.id,
                lookback_s=10,
            )

            if kick_actor:
                e = await self._E(
                    member.guild,
                    "Member kicked",
                    description=f"{member} ({member.id})",
                    etype="member_kicked",
                )
                e.add_field(name="By", value=kick_actor, inline=True)
            else:
                e = await self._E(
                    member.guild,
                    "Member left",
                    description=f"{member} ({member.id})",
                    etype="member_left",
                )

            await self._send(member.guild, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        g = await self._settings(after.guild)

        # Nicknames
        if before.nick != after.nick and g["member"]["nick_changed"]:
            actor = await self._audit_actor_recent(
                after.guild, discord.AuditLogAction.member_update, target_id=after.id
            )
            e = await self._E(after.guild, "Nickname changed", etype="nick_changed")
            e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
            e.add_field(name="Before", value=before.nick or "None", inline=True)
            e.add_field(name="After", value=after.nick or "None", inline=True)
            e.add_field(name="By", value=actor or f"{after} (self / unknown)", inline=True)
            await self._send(after.guild, e)

        # Roles - IMPROVED
        if set(before.roles) != set(after.roles) and g["member"]["roles_changed"]:
            actor = await self._audit_actor_recent(
                after.guild,
                discord.AuditLogAction.member_role_update,
                target_id=after.id,
            )

            added = set(after.roles) - set(before.roles)
            removed = set(before.roles) - set(after.roles)

            desc = []
            if added:
                desc.append(f"**Added:** {', '.join([r.mention for r in added])}")
            if removed:
                desc.append(f"**Removed:** {', '.join([r.mention for r in removed])}")

            e = await self._E(
                after.guild,
                "Roles changed",
                description="\n".join(desc),
                etype="roles_changed",
            )
            e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(after.guild, e)

        # Timeout
        if g["member"]["timeout"] and before.timed_out_until != after.timed_out_until:
            actor = await self._audit_actor_recent(
                after.guild, discord.AuditLogAction.member_update, target_id=after.id
            )
            e = await self._E(after.guild, "Timeout updated", etype="timeout_updated")
            e.add_field(name="User", value=f"{after} ({after.id})", inline=False)
            e.add_field(name="Until", value=str(after.timed_out_until), inline=True)
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(after.guild, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        g = await self._settings(guild)
        if g["member"]["ban"]:
            actor = await self._audit_actor_recent(
                guild, discord.AuditLogAction.ban, target_id=getattr(user, "id", None)
            )
            e = await self._E(
                guild,
                "User banned",
                description=f"{user} ({user.id})",
                etype="user_banned",
            )
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(guild, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        g = await self._settings(guild)
        if g["member"]["unban"]:
            actor = await self._audit_actor_recent(
                guild, discord.AuditLogAction.unban, target_id=getattr(user, "id", None)
            )
            e = await self._E(
                guild,
                "User unbanned",
                description=f"{user} ({user.id})",
                etype="user_unbanned",
            )
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(guild, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        g = await self._settings(member.guild)
        ch = await self._log_channel(member.guild)
        if not ch:
            return
        rate = await self._rate_seconds(member.guild)

        # Join
        if before.channel is None and after.channel is not None and g["voice"]["join"]:
            e = await self._E(
                member.guild,
                "Voice join",
                description=f"{member} → {after.channel.mention}",
                etype="voice_join",
            )
            await self._send(member.guild, e)
            return

        # Leave
        if before.channel is not None and after.channel is None and g["voice"]["leave"]:
            actor = await self._audit_actor_recent(
                member.guild,
                discord.AuditLogAction.member_disconnect,
                target_id=member.id,
                lookback_s=5,
            )
            e = await self._E(
                member.guild,
                "Voice leave",
                description=f"{member} ← {before.channel.mention}",
                etype="voice_leave",
            )
            e.add_field(name="By", value=actor or f"{member} (self)", inline=True)
            await self._send(member.guild, e)
            return

        # Move
        if (
            before.channel
            and after.channel
            and before.channel.id != after.channel.id
            and g["voice"]["move"]
        ):
            actor = await self._audit_actor_recent(
                member.guild,
                discord.AuditLogAction.member_move,
                target_id=member.id,
                lookback_s=5,
            )
            e = await self._E(
                member.guild,
                "Voice move",
                description=f"{member}: {before.channel.mention} → {after.channel.mention}",
                etype="voice_move",
            )
            e.add_field(name="By", value=actor or f"{member} (self)", inline=True)
            await self._send(member.guild, e)

        # Mute (Fixed Logic)
        if g["voice"]["mute"]:
            # Self Mute
            if before.self_mute != after.self_mute:
                if not self._should_suppress(f"v_smute:{member.guild.id}:{member.id}", rate):
                    state = "Self Muted" if after.self_mute else "Self Unmuted"
                    e = await self._E(
                        member.guild,
                        "Voice State",
                        description=f"**{state}**",
                        etype="voice_mute",
                    )
                    e.add_field(name="User", value=str(member), inline=True)
                    e.add_field(name="By", value=f"{member} (self)", inline=True)
                    await self._send(member.guild, e)
            # Server Mute
            if before.mute != after.mute:
                if not self._should_suppress(f"v_mute:{member.guild.id}:{member.id}", rate):
                    state = "Server Muted" if after.mute else "Server Unmuted"
                    actor = await self._audit_actor_recent(
                        member.guild,
                        discord.AuditLogAction.member_update,
                        target_id=member.id,
                        lookback_s=10,
                    )
                    e = await self._E(
                        member.guild,
                        "Voice State",
                        description=f"**{state}**",
                        etype="voice_mute",
                    )
                    e.add_field(name="User", value=str(member), inline=True)
                    e.add_field(
                        name="By",
                        value=actor or "Unknown (Audit log miss)",
                        inline=True,
                    )
                    await self._send(member.guild, e)

        # Deaf (Fixed Logic)
        if g["voice"]["deaf"]:
            # Self Deaf
            if before.self_deaf != after.self_deaf:
                if not self._should_suppress(f"v_sdeaf:{member.guild.id}:{member.id}", rate):
                    state = "Self Deafened" if after.self_deaf else "Self Undeafened"
                    e = await self._E(
                        member.guild,
                        "Voice State",
                        description=f"**{state}**",
                        etype="voice_deaf",
                    )
                    e.add_field(name="User", value=str(member), inline=True)
                    e.add_field(name="By", value=f"{member} (self)", inline=True)
                    await self._send(member.guild, e)
            # Server Deaf
            if before.deaf != after.deaf:
                if not self._should_suppress(f"v_deaf:{member.guild.id}:{member.id}", rate):
                    state = "Server Deafened" if after.deaf else "Server Undeafened"
                    actor = await self._audit_actor_recent(
                        member.guild,
                        discord.AuditLogAction.member_update,
                        target_id=member.id,
                        lookback_s=10,
                    )
                    e = await self._E(
                        member.guild,
                        "Voice State",
                        description=f"**{state}**",
                        etype="voice_deaf",
                    )
                    e.add_field(name="User", value=str(member), inline=True)
                    e.add_field(
                        name="By",
                        value=actor or "Unknown (Audit log miss)",
                        inline=True,
                    )
                    await self._send(member.guild, e)

        if g["voice"]["video"] and (before.self_video != after.self_video):
            if not self._should_suppress(f"v_video:{member.guild.id}:{member.id}", rate):
                e = await self._E(
                    member.guild,
                    "Video state change",
                    description=f"{member} → {'on' if after.self_video else 'off'}",
                    etype="voice_video",
                )
                e.add_field(name="By", value=f"{member} (self)", inline=True)
                await self._send(member.guild, e)
        if g["voice"]["stream"] and (before.self_stream != after.self_stream):
            if not self._should_suppress(f"v_stream:{member.guild.id}:{member.id}", rate):
                e = await self._E(
                    member.guild,
                    "Stream state change",
                    description=f"{member} → {'on' if after.self_stream else 'off'}",
                    etype="voice_stream",
                )
                e.add_field(name="By", value=f"{member} (self)", inline=True)
                await self._send(member.guild, e)

    # scheduled events
    @commands.Cog.listener()
    @guild_enabled
    async def on_scheduled_event_create(self, event: discord.ScheduledEvent):
        g = await self._settings(event.guild)
        if g["sched"]["create"]:
            actor = await self._audit_actor_recent(
                event.guild,
                getattr(discord.AuditLogAction, "scheduled_event_create", None),
                target_id=getattr(event, "id", None),
            )
            e = await self._E(
                event.guild,
                "Scheduled event created",
                description=event.name,
                etype="sched_created",
            )
            e.add_field(
                name="By",
                value=actor or getattr(event, "creator", None) or "Unknown",
                inline=True,
            )
            await self._send(event.guild, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_scheduled_event_update(
        self, before: discord.ScheduledEvent, after: discord.ScheduledEvent
    ):
        g = await self._settings(after.guild)
        if g["sched"]["update"]:
            actor = await self._audit_actor_recent(
                after.guild,
                getattr(discord.AuditLogAction, "scheduled_event_update", None),
                target_id=getattr(after, "id", None),
            )
            e = await self._E(
                after.guild,
                "Scheduled event updated",
                description=after.name,
                etype="sched_updated",
            )
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(after.guild, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent):
        g = await self._settings(event.guild)
        if g["sched"]["delete"]:
            actor = await self._audit_actor_recent(
                event.guild,
                getattr(discord.AuditLogAction, "scheduled_event_delete", None),
                target_id=getattr(event, "id", None),
            )
            e = await self._E(
                event.guild,
                "Scheduled event deleted",
                description=event.name,
                etype="sched_deleted",
            )
            e.add_field(name="By", value=actor or "Unknown", inline=True)
            await self._send(event.guild, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_scheduled_event_user_add(self, event: discord.ScheduledEvent, user: discord.User):
        g = await self._settings(event.guild)
        if g["sched"]["user_add"]:
            e = await self._E(
                event.guild,
                "Event RSVP added",
                description=f"{user} → {event.name}",
                etype="sched_user_add",
            )
            e.add_field(name="By", value=f"{user} ({user.id})", inline=True)
            await self._send(event.guild, e)

    @commands.Cog.listener()
    @guild_enabled
    async def on_scheduled_event_user_remove(
        self, event: discord.ScheduledEvent, user: discord.User
    ):
        g = await self._settings(event.guild)
        if g["sched"]["user_remove"]:
            e = await self._E(
                event.guild,
                "Event RSVP removed",
                description=f"{user} ✕ {event.name}",
                etype="sched_user_rem",
            )
            e.add_field(name="By", value=f"{user} ({user.id})", inline=True)
            await self._send(event.guild, e)

    # commands
    @commands.Cog.listener()
    @guild_enabled
    async def on_command_completion(self, ctx: commands.Context):
        guild = getattr(ctx, "guild", None)
        if not guild:
            return
        g = await self._settings(guild)
        if not g["commands"]["this_bot"]:
            return
        e = await self._E(guild, "Bot command ran", etype="cmd_thisbot")
        e.add_field(
            name="Command",
            value=ctx.command.qualified_name if ctx.command else "unknown",
            inline=True,
        )
        e.add_field(name="User", value=f"{ctx.author} ({ctx.author.id})", inline=True)
        e.add_field(name="Channel", value=getattr(ctx.channel, "mention", "DM"), inline=True)
        await self._send(guild, e, getattr(ctx.channel, "id", None))

    @commands.Cog.listener()
    @guild_enabled
    async def on_message(self, message: discord.Message):
        if not (
            message.guild
            and message.author
            and message.author.bot
            and self.bot.user
            and message.author.id != self.bot.user.id
        ):
            return
        g = await self._settings(message.guild)
        if not g["commands"]["other_bots"]:
            return
        content = message.content or ""
        if not self._cmd_prefix_re.match(content):
            return
        if self._should_suppress(
            f"otherbotcmd:{message.guild.id}:{message.author.id}",
            await self._rate_seconds(message.guild),
        ):
            return
        e = await self._E(message.guild, "Other bot command ran", etype="cmd_otherbot")
        e.add_field(name="Bot", value=f"{message.author} ({message.author.id})", inline=True)
        e.add_field(name="Channel", value=message.channel.mention, inline=True)
        if content:
            e.add_field(name="Content", value=content[:300], inline=False)
        await self._send(message.guild, e, message.channel.id)

    async def cog_before_invoke(self, ctx):
        await prepare_hybrid(ctx)
        self._settings_cache.pop(ctx.guild.id, None)

    async def cog_after_invoke(self, ctx):
        finish_configuration_audit(ctx)
        self._settings_cache.pop(ctx.guild.id, None)

    async def _migrate_removed_presence(self):
        """Remove the retired member presence switch without changing other settings."""
        for guild_id, saved in (await self.config.all_guilds()).items():
            if "presence" in saved.get("member", {}):
                async with self.config.guild_from_id(guild_id).member() as member:
                    member.pop("presence", None)
        self._settings_cache.clear()

    async def cog_load(self):
        self._closing = False
        await self._migrate_removed_presence()
        await self._migrate_case_sources()
        self._history_task = asyncio.create_task(
            self._history_maintenance(), name="logplus-history-retention"
        )
        self._moderation_task = asyncio.create_task(self._moderation_loop())

    async def cog_unload(self):
        self._closing = True
        tasks = tuple(self._alert_tasks) + (
            (self._moderation_task,) if self._moderation_task else ()
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._alert_tasks.clear()
        self._alert_windows.clear()
        self._alert_sent.clear()
        if self._history_task:
            self._history_task.cancel()
            await asyncio.gather(self._history_task, return_exceptions=True)
            self._history_task = None
        for gid in list(self._retry_tasks):
            await self._cancel_retries(gid)
        self._retry_queues.clear()
        self._delivery_status.clear()
        self._own_log_ids.clear()
        await close_views(self)
        self._settings_cache.clear()
        self._audit_cache.clear()
        self._audit_locks.clear()
        self._last_event_at.clear()

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
            group = self.config.guild(guild)
            keys = [*DEFAULTS_GUILD, "features", "history_settings", "alert_settings"]
            values = await asyncio.gather(*(group.get_attr(key)() for key in keys))
            settings = dict(zip(keys, values))
            self._settings_cache[guild.id] = (now, settings)
            return settings

    @staticmethod
    def _fit_embed(embed):
        def units(text):
            return len((text or "").encode("utf-16-le")) // 2

        def clip(text, limit):
            return (
                (text or "")
                .encode("utf-16-le")[: max(0, limit) * 2]
                .decode("utf-16-le", errors="ignore")
            )

        embed.title = clip(embed.title, 256) or None
        if embed.footer.text:
            embed.set_footer(text=clip(embed.footer.text, 2048), icon_url=embed.footer.icon_url)
        if embed.author.name:
            embed.set_author(
                name=clip(embed.author.name, 256),
                url=embed.author.url,
                icon_url=embed.author.icon_url,
            )
        remaining = 6000 - units(embed.title) - units(embed.footer.text) - units(embed.author.name)
        embed.description = clip(embed.description, min(4096, remaining)) or None
        remaining -= units(embed.description)
        fields = list(embed.fields)[:25]
        embed.clear_fields()
        for field in fields:
            if remaining < 2:
                break
            name = clip(field.name, min(256, remaining - 1)) or "\u200b"
            remaining -= units(name)
            value = clip(field.value, min(1024, remaining)) or "\u200b"
            remaining -= units(value)
            embed.add_field(name=name, value=value, inline=field.inline)
        return embed

    async def _thread_event(self, thread, key, title, event_type, description=None):
        settings = await self._settings(thread.guild)
        if not settings["server"][key] or await self._is_exempt(thread.guild, thread.id, "server"):
            return
        actor = await self._audit_actor_recent(
            thread.guild,
            getattr(discord.AuditLogAction, key, None),
            target_id=thread.id,
        )
        embed = await self._E(
            thread.guild,
            title,
            description=description or thread.name,
            etype=event_type,
        )
        embed.add_field(name="Thread", value=thread.mention)
        embed.add_field(name="By", value=actor or "Unknown")
        await self._send(thread.guild, embed, thread.id)

    @commands.Cog.listener()
    @guild_enabled
    async def on_thread_create(self, thread):
        await self._thread_event(thread, "thread_create", "Thread created", "thread_created")

    @commands.Cog.listener()
    @guild_enabled
    async def on_thread_delete(self, thread):
        await self._thread_event(thread, "thread_delete", "Thread deleted", "thread_deleted")

    @commands.Cog.listener()
    @guild_enabled
    async def on_thread_update(self, before, after):
        changes = [
            f"{key}: {getattr(before, key)} to {getattr(after, key)}"
            for key in ("name", "archived", "locked", "slowmode_delay")
            if getattr(before, key) != getattr(after, key)
        ]
        if changes:
            await self._thread_event(
                after,
                "thread_update",
                "Thread updated",
                "thread_updated",
                "\n".join(changes),
            )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        await self._cancel_retries(guild.id)
        self._retry_queues.pop(guild.id, None)
        self._delivery_status.pop(guild.id, None)
        for key in list(self._own_log_ids):
            if key[0] == guild.id:
                self._own_log_ids.pop(key, None)
        self._settings_cache.pop(guild.id, None)
        self._settings_locks.pop(guild.id, None)
        self._audit_cache.pop(guild.id, None)
        self._audit_locks.pop(guild.id, None)
        for key in list(self._last_event_at):
            if f":{guild.id}:" in key:
                self._last_event_at.pop(key, None)

    async def red_delete_data_for_user(self, *, requester, user_id):
        await self._incident_user_data(user_id, delete=True)
        await super().red_delete_data_for_user(requester=requester, user_id=user_id)

    async def red_get_data_for_user(self, *, user_id):
        data = await super().red_get_data_for_user(user_id=user_id)
        data.update(await self._incident_export(user_id))
        return data
