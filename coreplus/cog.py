"""Themed Red management, slash entry points and a global help formatter."""

import platform
from datetime import datetime, timezone

import discord
from redbot.core import commands, version_info

from .command_support import finish_configuration_audit, prepare_hybrid
from .help import CoreHelpFormatter
from .management import invoke_native, style_replies, words
from .presentation import Presentation


class CorePlus(commands.Cog):
    """Browse Scarlet's commands and manage Red with matching themed controls.

    CorePlus supplies the shared help menu and selected slash controls. Red Core
    and CogManagerUI remain responsible for their original commands and settings.
    """

    native_cog_names = {"Core", "CogManagerUI"}
    native_help_paths = {
        "core info": "info",
        "core uptime": "uptime",
        "core invite": "invite",
        "core cogs": "cogs",
        "core load": "load",
        "core unload": "unload",
        "core reload": "reload",
        "core slash": "slash list",
        "core slash enable": "slash enablecog",
        "core slash disable": "slash disablecog",
        "core slash sync": "slash sync",
    }

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self._presentation = Presentation("CorePlus", "core")
        self._views = set()
        self._formatter = CoreHelpFormatter(self)
        self._closing = False

    async def cog_load(self):
        # Red refuses to replace another non-default formatter. Fail cleanly
        # rather than silently displacing another cog's help system.
        self.bot.set_help_formatter(self._formatter)
        self.bot.before_invoke(self.style_native_reply)

    async def cog_unload(self):
        self._closing = True
        self.bot.remove_before_invoke_hook(self.style_native_reply)
        # Hybrid removal normally uses the prefix name. This command's public
        # slash name is /help, so remove that owned definition explicitly.
        tree = self.bot.tree
        app = tree.get_command("help") or getattr(tree, "_disabled_global_commands", {}).get("help")
        if app is self.help_menu.app_command:
            tree.remove_command("help")
        if getattr(self.bot, "_help_formatter", None) is self._formatter:
            self.bot.reset_help_formatter()
        for view in tuple(self._views):
            view.stop()
            await view.on_timeout()
        self._views.clear()

    async def style_native_reply(self, ctx):
        name = getattr(getattr(ctx.command, "cog", None), "qualified_name", None)
        if name not in {"Core", "CogManagerUI"} or self._closing:
            return
        # Leave Red's always-available licensing and data-rights responses intact.
        if ctx.command.qualified_name.split()[0] in {"licenseinfo", "mydata"}:
            return
        if not await self.bot.cog_disabled_in_guild(self, ctx.guild):
            style_replies(ctx, self._presentation)

    async def cog_before_invoke(self, ctx):
        await prepare_hybrid(ctx)

    async def cog_after_invoke(self, ctx):
        finish_configuration_audit(ctx)

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def red_get_data_for_user(self, *, user_id):
        return {}

    async def red_delete_data_for_user(self, *, requester, user_id):
        for view in tuple(self._views):
            if view.owner_id == user_id:
                view.stop()
                await view.on_timeout()

    async def _native(self, ctx, path, arguments=()):
        return await invoke_native(self, ctx, path, arguments, expected={"Core", "CogManagerUI"})

    @commands.hybrid_command(name="helpme")
    async def help_menu(self, ctx, *, query: str = ""):
        """Browse categories, command usage and available slash controls.

        Use `[p]help`, `[p]helpme <command or category>`, or `/help`.
        Only currently available commands are shown. Controls belong to the
        requester and expire after three minutes.
        """
        await self._formatter.send_help(ctx, query or None, from_help_command=True)

    @commands.hybrid_group(name="core", invoke_without_command=True, fallback="status")
    async def core(self, ctx):
        """Show bot status and open themed Red management controls."""
        start = getattr(self.bot, "uptime", None)
        since = "Not available yet"
        if isinstance(start, datetime):
            since = discord.utils.format_dt(start.replace(tzinfo=timezone.utc), "R")
        latency = getattr(self.bot, "latency", 0.0)
        latency_text = (
            f"{latency * 1000:.0f} ms" if isinstance(latency, (int, float)) else "Unknown"
        )
        embed = self._presentation.embed("Bot status", "Use `/help` to browse available commands.")
        embed.add_field(name="Red / Discord.py", value=f"{version_info} / {discord.__version__}")
        embed.add_field(name="Python", value=platform.python_version())
        embed.add_field(name="Gateway latency", value=latency_text)
        embed.add_field(name="Started", value=since)
        embed.add_field(name="Loaded cogs", value=str(len(self.bot.cogs)))
        embed.add_field(name="Help", value="Category menu with checked prefix and slash usage")
        await self._presentation.send(ctx, embed=embed)

    @core.command(name="help")
    async def core_help(self, ctx):
        """Browse bot-management commands and their permissions."""
        await self._formatter.send_help(ctx, "CorePlus", from_help_command=True)

    @core.command(name="info")
    async def core_info(self, ctx):
        """Show Red's bot information with the shared theme."""
        await self._native(ctx, "info")

    @core.command(name="uptime")
    async def core_uptime(self, ctx):
        """Show how long the bot has been running."""
        await self._native(ctx, "uptime")

    @core.command(name="invite")
    async def core_invite(self, ctx):
        """Show the configured bot invitation, respecting Red's invite policy."""
        await self._native(ctx, "invite")

    @core.command(name="cogs")
    @commands.is_owner()
    async def core_cogs(self, ctx):
        """List loaded and available cog packages as the bot owner."""
        await self._native(ctx, "cogs")

    @core.command(name="load")
    @commands.is_owner()
    async def core_load(self, ctx, *, packages: str):
        """Load one or more installed packages, separated by spaces."""
        await self._native(ctx, "load", words(packages))

    @core.command(name="unload")
    @commands.is_owner()
    async def core_unload(self, ctx, *, packages: str):
        """Unload one or more packages without uninstalling their files."""
        await self._native(ctx, "unload", words(packages))

    @core.command(name="reload")
    @commands.is_owner()
    async def core_reload(self, ctx, *, packages: str):
        """Reload one or more packages after updating their code."""
        await self._native(ctx, "reload", words(packages))

    @core.group(name="slash", invoke_without_command=True, fallback="list")
    @commands.is_owner()
    async def core_slash(self, ctx):
        """List Red's enabled and disabled slash commands."""
        await self._native(ctx, "slash list")

    @core_slash.command(name="enable")
    async def slash_enable(self, ctx, *, packages: str):
        """Enable slash commands for one or more loaded cog packages."""
        await self._native(ctx, "slash enablecog", words(packages))

    @core_slash.command(name="disable")
    async def slash_disable(self, ctx, *, packages: str):
        """Disable slash commands for one or more cog packages."""
        await self._native(ctx, "slash disablecog", words(packages))

    @core_slash.command(name="sync")
    async def slash_sync(self, ctx):
        """Publish slash changes to Discord with Red's sync cooldown."""
        await self._native(ctx, "slash sync")


# The prefix name avoids replacing Red's existing !help command. Its hybrid
# application command has the intuitive /help name and retains Red's pipeline.
CorePlus.help_menu.app_command.name = "help"
