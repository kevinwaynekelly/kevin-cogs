"""One optional dashboard for the five independently installed cogs."""

import asyncio
import io
import json
import logging
import shutil
from contextlib import AsyncExitStack
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version
from typing import Optional, Union

import discord
from redbot.core import Config, app_commands, commands

from .audit import AuditCommands
from .command_support import (
    check_command,
    configuration_action,
    finish_configuration_audit,
    prepare_hybrid,
)
from .interactive import close_views, component_context, component_error
from .maintenance import HUB_DEFAULTS, MaintenanceCommands
from .presentation import Presentation
from .readiness import FEATURES, ReadinessCommands
from .schema import (
    FIELDS,
    MAX_FILE,
    TARGETS,
    merge_fields,
    parse_backup,
    select_fields,
    validate_fields,
)

log = logging.getLogger(__name__)


class DashboardView(discord.ui.View):
    def __init__(self, hub, ctx, names):
        super().__init__(timeout=180)
        self.hub, self.guild_id, self.owner_id = hub, ctx.guild.id, ctx.author.id
        self.message = None
        selector = discord.ui.Select(
            placeholder="Choose a cog to configure",
            options=[
                discord.SelectOption(label=name.removesuffix("Plus"), value=name) for name in names
            ],
        )

        async def choose(interaction):
            try:
                await self.checked(interaction, "settings")
                name = selector.values[0]
                cog = hub.bot.get_cog(name)
                if cog is None:
                    raise commands.CheckFailure("That cog is unloaded. Open a new settings panel.")
                ctx = await component_context(
                    cog, interaction, TARGETS[name][1], owner_id=self.owner_id
                )
                command = hub.bot.get_command(TARGETS[name][1])
                await command.callback(cog, ctx)
            except commands.CommandError as error:
                await component_error(interaction, error)

        selector.callback = choose
        self.add_item(selector)
        hub._views.add(self)

    async def checked(self, interaction, path):
        if (
            self.is_finished()
            or self.hub._closing
            or interaction.guild is None
            or interaction.guild.id != self.guild_id
        ):
            raise commands.CheckFailure("This settings panel expired or belongs to another server.")
        return await component_context(self.hub, interaction, path, owner_id=self.owner_id)

    async def on_timeout(self):
        self.hub._views.discard(self)
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        await component_error(interaction, error)


class RestoreView(DashboardView):
    def __init__(self, hub, ctx, bundle, selected):
        super().__init__(hub, ctx, selected)
        self.clear_items()
        self.bundle, self.selected = deepcopy(bundle), selected
        self.lock = asyncio.Lock()
        self.applied = False
        confirm = discord.ui.Button(
            label="Restore these settings", style=discord.ButtonStyle.danger
        )

        async def restore(interaction):
            try:
                ctx = await self.checked(interaction, "settings restore")
                async with self.lock:
                    if self.applied:
                        raise commands.CheckFailure("This backup was already restored.")
                    selected = await hub._validate_bundle(ctx, self.bundle)
                    if any(selected[name] is not self.selected[name] for name in selected):
                        raise commands.CheckFailure(
                            "A cog was reloaded. Preview the restore again."
                        )
                    async with configuration_action(hub, ctx):
                        warnings = await hub._apply_bundle(ctx, self.bundle, selected)
                    self.applied = True
                    self.stop()
                    await self.on_timeout()
                await interaction.followup.send(
                    "Server settings restored. Member records were preserved."
                    + (
                        " Refresh these setup panels again: " + ", ".join(warnings)
                        if warnings
                        else ""
                    ),
                    ephemeral=True,
                )
            except commands.CommandError as error:
                await component_error(interaction, error)

        confirm.callback = restore
        self.add_item(confirm)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

        async def cancel_restore(interaction):
            try:
                await self.checked(interaction, "settings restore")
                async with self.lock:
                    self.applied = True
                    self.stop()
                    await self.on_timeout()
                await interaction.followup.send("Restore cancelled.", ephemeral=True)
            except commands.CommandError as error:
                await component_error(interaction, error)

        cancel.callback = cancel_restore
        self.add_item(cancel)


class SettingsHub(ReadinessCommands, AuditCommands, MaintenanceCommands, commands.Cog):
    """Shared setup, health, and server settings backup."""

    def __init__(self, bot):
        self.bot = bot
        self._presentation = Presentation("Settings", "settings")
        self._views = set()
        self._closing = False
        self.config = Config.get_conf(self, identifier=702034990, force_registration=True)
        self.config.register_guild(**HUB_DEFAULTS)
        self._maintenance_task = None
        self._maintenance_log = log
        self._init_audit()

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    async def _send_view(self, ctx, view, content, **kwargs):
        try:
            view.message = await self._reply(ctx, content, view=view, **kwargs)
        except BaseException:
            view.stop()
            await view.on_timeout()
            raise

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def cog_before_invoke(self, ctx):
        await prepare_hybrid(ctx)

    async def cog_after_invoke(self, ctx):
        finish_configuration_audit(ctx)

    async def cog_load(self):
        for cog in [self, *self._loaded().values()]:
            self._observe_config(cog)
        for guild in self.bot.guilds:
            await self._cache_theme(guild.id)
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def cog_unload(self):
        self._closing = True
        self._close_audit()
        if self._maintenance_task:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
        if isinstance(getattr(self.bot, "_kevin_cogs_themes", None), dict):
            self.bot._kevin_cogs_themes.clear()
        await close_views(self)

    def _loaded(self):
        return {name: cog for name in TARGETS if (cog := self.bot.get_cog(name)) is not None}

    async def _source_check(self, ctx, cog):
        if (
            self._closing
            or self.bot.get_cog(cog.qualified_name) is not cog
            or getattr(cog, "_closing", False)
            or await self.bot.cog_disabled_in_guild(cog, ctx.guild)
        ):
            raise commands.CheckFailure(f"{cog.qualified_name} is unloaded or disabled here.")
        command = self.bot.get_command(TARGETS[cog.qualified_name][1])
        if command is None or command.cog is not cog:
            raise commands.CheckFailure("The cog's setup command is unavailable.")
        await check_command(ctx, command)

    async def _backup_bundle(self, ctx):
        cogs = self._loaded()
        if not cogs:
            raise commands.BadArgument("Load at least one of Kevin's five cogs first.")
        records = {}
        for name, cog in cogs.items():
            # Omit cogs the caller cannot currently configure.
            try:
                await self._source_check(ctx, cog)
            except (commands.CheckFailure, commands.DisabledCommand):
                continue
            group = cog.config.guild(ctx.guild)
            values = await asyncio.gather(*(group.get_attr(key)() for key in FIELDS[name]))
            records[name] = select_fields(name, dict(zip(FIELDS[name], values)))
        if not records:
            raise commands.CheckFailure("No loaded cog settings are available to you.")
        return {"schema": 1, "guild_id": ctx.guild.id, "cogs": records}

    async def _validate_bundle(self, ctx, bundle):
        if bundle["guild_id"] != ctx.guild.id:
            raise commands.BadArgument(
                "This backup belongs to another server. Restore it in its original server."
            )
        selected = {}
        for name, values in bundle["cogs"].items():
            cog = self.bot.get_cog(name)
            if cog is None:
                raise commands.BadArgument(f"Load {name} before restoring its settings.")
            await self._source_check(ctx, cog)
            expected = select_fields(name, cog.config._defaults[Config.GUILD])
            try:
                validate_fields(ctx.guild, expected, values)
            except (TypeError, ValueError, OverflowError, RecursionError) as error:
                raise commands.BadArgument(
                    f"Invalid {name} configuration in the backup."
                ) from error
            selected[name] = cog
        return selected

    async def _apply_bundle(self, ctx, bundle, selected):
        snapshots, changed = {}, []
        async with AsyncExitStack() as stack:
            for name in sorted(selected):
                group = selected[name].config.guild(ctx.guild)
                keys = sorted(FIELDS[name])
                if name == "LogPlus":
                    keys = sorted([*keys, "history_records"])
                for key in keys:
                    await stack.enter_async_context(group.get_attr(key).get_lock())
            # Revalidate after acquiring locks, including live role/channel policies.
            current = await self._validate_bundle(ctx, bundle)
            if any(current[name] is not selected[name] for name in selected):
                raise commands.CheckFailure("A cog was reloaded during restore. Preview it again.")
            for name, cog in selected.items():
                group = cog.config.guild(ctx.guild)
                snapshots[name] = {key: await group.get_attr(key)() for key in FIELDS[name]}
            try:
                for name, cog in selected.items():
                    group = cog.config.guild(ctx.guild)
                    merged = merge_fields(snapshots[name], bundle["cogs"][name])
                    for key, value in merged.items():
                        if (
                            self._closing
                            or self.bot.get_cog(name) is not cog
                            or getattr(cog, "_closing", False)
                        ):
                            raise commands.CheckFailure("A cog was unloaded during restore.")
                        changed.append((name, key))
                        await group.get_attr(key).set(value)
            except BaseException:
                failures = []
                for name, key in reversed(changed):
                    try:
                        await (
                            selected[name]
                            .config.guild(ctx.guild)
                            .get_attr(key)
                            .set(snapshots[name][key])
                        )
                    except Exception:
                        failures.append(f"{name}.{key}")
                if failures:
                    raise commands.CommandError(
                        "Restore failed and some settings could not be rolled back: "
                        + ", ".join(failures)
                    ) from None
                raise
            finally:
                for cog in selected.values():
                    getattr(cog, "_settings_cache", {}).pop(ctx.guild.id, None)
        # Operational refreshes happen after releasing Config locks.
        warnings = []
        for name, cog in selected.items():
            if self.bot.get_cog(name) is not cog or getattr(cog, "_closing", False):
                warnings.append(name)
                continue
            try:
                if name == "CommunityPlus":
                    await cog._reset_solo(ctx.guild)
                    await cog._refresh_role_menus(ctx.guild)
                elif name == "AudioPlus":
                    player = cog._get_player(ctx.guild)
                    if player:
                        music = await cog.config.guild(ctx.guild).music()
                        continuity = await cog.config.guild(ctx.guild).continuity()
                        async with player.lock:
                            player.fair_queue, player.autoplay = (
                                music["fair_queue"],
                                music["autoplay"],
                            )
                            player._autoplay_generation += 1
                            player.normalize = continuity["normalize"]
                            player._balance_queue()
                elif name == "LogPlus":
                    await cog._history_query(ctx.guild, days=90)
            except Exception:
                log.exception("Settings restored but %s refresh failed", name)
                warnings.append(name)
        return warnings

    @commands.hybrid_group(name="settings", invoke_without_command=True, fallback="panel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def settings(self, ctx):
        """Open setup controls for Kevin's loaded cogs."""
        names = self._loaded()
        if not names:
            return await self._reply(
                ctx, "Load AudioPlus, CommunityPlus, LevelPlus, LogPlus, or OwoPlus first."
            )
        view = DashboardView(self, ctx, names)
        await self._send_view(
            ctx,
            view,
            "Choose a cog to open its guided setup. Controls use your current permissions. Use settings health, backup, or restore for maintenance.",
            title="Server settings",
        )

    @settings.group(name="history", invoke_without_command=True, fallback="list")
    async def configuration_history(self, ctx, page: int = 1):
        """Browse who changed server settings and their old and new values."""
        await self._show_configuration_history(ctx, page)

    @settings.command(name="ready")
    @app_commands.choices(
        feature=[
            app_commands.Choice(name="All configured features", value="all"),
            *[app_commands.Choice(name=label, value=name) for name, (_, label) in FEATURES.items()],
        ]
    )
    async def ready(
        self,
        ctx,
        feature: str = "all",
        channel: Optional[discord.TextChannel] = None,
        voice: Optional[Union[discord.VoiceChannel, discord.StageChannel]] = None,
        role: Optional[discord.Role] = None,
    ):
        """Check a feature's configured or candidate channels, roles and prerequisites."""
        await self._readiness_reply(ctx, feature.lower(), channel=channel, voice=voice, role=role)

    @configuration_history.command(name="show")
    async def configuration_history_show(self, ctx, identifier: str):
        """Show before and after values for a retained change ID."""
        await self._show_configuration_change(ctx, identifier)

    @configuration_history.command(name="enabled")
    async def configuration_history_enabled(self, ctx, enabled: bool, days: int = 30):
        """Enable or pause change collection, with retention from 1 to 90 days."""
        if not 1 <= days <= 90:
            raise commands.BadArgument("Choose retention from 1 to 90 days.")
        section = self.config.guild(ctx.guild).audit_policy
        async with section.get_lock():
            await section.set({"enabled": enabled, "days": days})
        await self._audit_records(ctx.guild.id)
        await self._reply(
            ctx, f"Configuration history collection: {enabled}. Retention: {days} days."
        )

    @configuration_history.command(name="clear")
    async def configuration_history_clear(self, ctx):
        """Erase retained changes for cogs you can currently configure."""
        visible = {r["id"] for r in await self._visible_audit(ctx)}
        section = self.config.guild(ctx.guild).configuration_history
        async with section.get_lock():
            await section.set([r for r in await section() if r["id"] not in visible])
        await self._reply(ctx, "Accessible configuration history was cleared.", tone="success")

    @configuration_history.command(name="export")
    @commands.bot_has_permissions(attach_files=True)
    async def configuration_history_export(self, ctx):
        """Download retained changes for cogs you can currently configure."""
        await self._export_configuration_history(ctx)

    @settings.command(name="health")
    async def health(self, ctx):
        """Inspect loaded cogs, local dependencies, and delivery."""
        lines = []
        for name in TARGETS:
            cog = self.bot.get_cog(name)
            state = (
                "Unloaded"
                if cog is None
                else (
                    "Disabled here"
                    if await self.bot.cog_disabled_in_guild(cog, ctx.guild)
                    else "Loaded"
                )
            )
            lines.append(f"**{name.removesuffix('Plus')}** · {state}")
            if name == "LogPlus" and cog:
                status = cog._delivery_status[ctx.guild.id]
                lines.append(
                    f"Pending log events: {len(cog._retry_queues.get(ctx.guild.id, []))} · Delivered: {status['delivered']} · Failed sends: {status['failures']}"
                )
        lines.append("**Local music prerequisites**")
        for package in ("yt-dlp", "yt-dlp-ejs", "PyNaCl", "davey"):
            try:
                state = version(package)
            except PackageNotFoundError:
                state = "Missing"
            lines.append(f"{package}: {state}")
        lines.append(
            f"FFmpeg: {'Found' if shutil.which('ffmpeg') else 'Missing'} · JavaScript runtime: {'Found' if any(shutil.which(binary) for binary in ('deno', 'node', 'qjs')) else 'Missing'}"
        )
        permissions = ctx.channel.permissions_for(ctx.guild.me)
        missing = [
            label
            for key, label in (
                ("send_messages", "Send Messages"),
                ("embed_links", "Embed Links"),
                ("attach_files", "Attach Files"),
                ("manage_webhooks", "Manage Webhooks"),
                ("manage_messages", "Manage Messages"),
            )
            if not getattr(permissions, key)
        ]
        lines.append("Missing permissions in this channel: " + (", ".join(missing) or "None"))
        lines.append(
            "These checks do not test live voice or YouTube extraction. Run audiostatus or the opt-in audio monitor for playback checks."
        )
        await self._reply(ctx, "\n".join(lines), title="Health")

    @settings.command(name="diagnostics")
    @commands.bot_has_permissions(attach_files=True)
    async def diagnostic_export(self, ctx):
        """Download dependency, permission, cog and failure diagnostics."""
        await self._download_diagnostics(ctx)

    @settings.command(name="backup")
    @commands.bot_has_permissions(attach_files=True)
    async def backup(self, ctx):
        """Download server settings without member data or secrets."""
        bundle = await self._backup_bundle(ctx)
        raw = json.dumps(bundle, ensure_ascii=False, indent=2).encode()
        if len(raw) > MAX_FILE:
            raise commands.BadArgument(
                "This settings backup exceeds the 256 KiB limit. Reduce custom dictionaries or channel lists first."
            )
        await self._reply(
            ctx,
            f"Backed up settings for {len(bundle['cogs'])} cogs. Restore in this server with settings restore and this file.",
            file=discord.File(io.BytesIO(raw), filename=f"settings-{ctx.guild.id}.json"),
        )

    @settings.command(name="restore")
    async def restore(self, ctx, file: discord.Attachment):
        """Validate an attached backup and preview its restore."""
        if file.size > MAX_FILE:
            raise commands.BadArgument("Use a JSON backup no larger than 256 KiB.")
        try:
            raw = await asyncio.wait_for(file.read(), 10)
        except (discord.HTTPException, asyncio.TimeoutError) as error:
            raise commands.BadArgument(
                "Could not read that attachment. Upload it again."
            ) from error
        bundle = parse_backup(raw)
        selected = await self._validate_bundle(ctx, bundle)
        view = RestoreView(self, ctx, bundle, selected)
        await self._send_view(
            ctx,
            view,
            "Ready to restore: "
            + ", ".join(selected)
            + ".\nThis replaces the included server settings and keeps member records, histories, active polls/events, role-menu messages, and runtime cursors. Temporary XP boosts and global audio settings are excluded. Review the backup, then use the button within three minutes.",
            title="Restore preview",
            tone="warning",
        )

    async def red_get_data_for_user(self, *, user_id):
        data = await self._audit_user_data(user_id)
        return {"settingshub.json": io.BytesIO(json.dumps(data, indent=2).encode())} if data else {}

    async def red_delete_data_for_user(self, *, requester, user_id):
        await self._audit_user_data(user_id, delete=True)
        for view in tuple(self._views):
            if view.owner_id == user_id:
                view.stop()
                await view.on_timeout()
