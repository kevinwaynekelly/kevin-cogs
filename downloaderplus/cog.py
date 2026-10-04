"""Themed owner controls backed by Red's existing Downloader engine."""

from redbot.core import commands

from .command_support import check_command, finish_configuration_audit, prepare_hybrid
from .management import invoke_native, native_command, style_replies, words
from .presentation import Presentation


class DownloaderPlus(commands.Cog):
    """Install, update and inspect cogs with themed owner-only controls.

    Bundled Downloader keeps the repositories, installed records, converters,
    dependency installation, pinning and update/reload behavior.
    """

    native_cog_names = {"Downloader"}
    native_help_paths = {
        "download": "repo list",
        "download repos": "repo list",
        "download repos add": "repo add",
        "download repos remove": "repo delete",
        "download repos info": "repo info",
        "download repos update": "repo update",
        "download available": "cog list",
        "download installed": "cog list",
        "download info": "cog info",
        "download install": "cog install",
        "download uninstall": "cog uninstall",
        "download update": "cog update",
        "download checkupdates": "cog checkforupdates",
        "download pin": "cog pin",
        "download unpin": "cog unpin",
        "download pinned": "cog listpinned",
        "download version": "cog updatetoversion",
        "download find": "findcog",
    }

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self._presentation = Presentation("DownloaderPlus", "download")
        self._closing = False

    async def cog_load(self):
        self.bot.before_invoke(self.style_native_reply)

    def cog_unload(self):
        self._closing = True
        self.bot.remove_before_invoke_hook(self.style_native_reply)

    async def style_native_reply(self, ctx):
        if (
            self._closing
            or getattr(getattr(ctx.command, "cog", None), "qualified_name", None) != "Downloader"
        ):
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
        return

    async def _native(self, ctx, path, arguments=()):
        return await invoke_native(self, ctx, path, arguments, expected={"Downloader"})

    async def _source(self, ctx):
        command = native_command(self.bot, "cog list", {"Downloader"})
        await check_command(ctx, command)
        await command.cog.cog_before_invoke(ctx)
        return command.cog

    @commands.hybrid_group(name="download", invoke_without_command=True, fallback="status")
    @commands.is_owner()
    async def download(self, ctx):
        """Show cog installation status and owner-only Downloader controls."""
        source = await self._source(ctx)
        installed = await source.installed_cogs()
        repos = source._repo_manager.repos
        embed = self._presentation.embed("Cog manager", "Manage installed cogs and repositories.")
        embed.add_field(name="Repositories", value=str(len(repos)))
        embed.add_field(name="Installed cogs", value=str(len(installed)))
        embed.add_field(name="Pinned cogs", value=str(sum(bool(cog.pinned) for cog in installed)))
        embed.add_field(
            name="Common controls",
            value="`/download repos list` · `/download installed`\n"
            "`/download available` · `/download install` · `/download update`",
            inline=False,
        )
        await self._presentation.send(ctx, embed=embed)

    @download.command(name="help")
    async def download_help(self, ctx):
        """Browse cog installation and repository commands."""
        await ctx.send_help(self.download)

    @download.group(name="repos", invoke_without_command=True, fallback="list")
    async def repos(self, ctx):
        """List configured cog repositories."""
        await self._native(ctx, "repo list")

    @repos.command(name="add")
    async def repos_add(self, ctx, name: str, url: str, branch: str = ""):
        """Add a repository using Red's existing installation agreement."""
        args = [name, url, *([branch] if branch else [])]
        await self._native(ctx, "repo add", args)

    @repos.command(name="remove")
    async def repos_remove(self, ctx, *, names: str):
        """Remove named repositories using Red's native removal behavior."""
        await self._native(ctx, "repo delete", words(names))

    @repos.command(name="info")
    async def repos_info(self, ctx, name: str):
        """Show repository author, description and branch information."""
        await self._native(ctx, "repo info", [name])

    @repos.command(name="update")
    async def repos_update(self, ctx, *, names: str = ""):
        """Refresh selected repository copies, or all copies if omitted.

        This refreshes repository copies only. Use download update to update
        the installed cog files.
        """
        await self._native(ctx, "repo update", words(names, required=False))

    @download.command(name="available")
    async def available(self, ctx, repo: str):
        """List the installed and available cogs in one repository."""
        await self._native(ctx, "cog list", [repo])

    @download.command(name="installed")
    async def installed(self, ctx):
        """List installed cogs, their repositories and pinned state."""
        source = await self._source(ctx)
        installed = sorted(await source.installed_cogs(), key=lambda item: item.name.lower())
        embed = self._presentation.embed("Installed cogs", f"{len(installed)} installed packages.")
        for item in installed:
            embed.add_field(
                name=item.name,
                value=f"**Repository** · {item.repo_name or 'Unavailable'}\n"
                f"**Updates** · {'Pinned' if item.pinned else 'Enabled'}",
                inline=False,
            )
        await self._presentation.send(ctx, embed=embed)

    @download.command(name="info")
    async def info(self, ctx, repo: str, package: str):
        """Show a cog's description and compatibility information."""
        await self._native(ctx, "cog info", [repo, package])

    @download.command(name="install")
    async def install(self, ctx, repo: str, *, packages: str):
        """Install space-separated packages from one configured repository.

        Installation does not automatically load packages. Use core load or
        Red's native load command afterward.
        """
        await self._native(ctx, "cog install", [repo, *words(packages)])

    @download.command(name="uninstall")
    async def uninstall(self, ctx, *, packages: str):
        """Uninstall named packages using Red's existing unload/file handling."""
        await self._native(ctx, "cog uninstall", words(packages))

    @download.command(name="update")
    async def update(self, ctx, reload: bool = True, *, packages: str = ""):
        """Update selected cogs or all unpinned cogs, reloading by default.

        Example: `[p]download update True audioplus communityplus`.
        Slash fields select packages and whether to reload. Red's pinning,
        failed-dependency reports and native reload behavior are preserved.
        """
        await self._native(ctx, "cog update", [str(reload), *words(packages, required=False)])

    @download.command(name="checkupdates")
    async def checkupdates(self, ctx):
        """Check available updates without installing them."""
        await self._native(ctx, "cog checkforupdates")

    @download.command(name="pin")
    async def pin(self, ctx, *, packages: str):
        """Pin installed packages so normal updates skip them."""
        await self._native(ctx, "cog pin", words(packages))

    @download.command(name="unpin")
    async def unpin(self, ctx, *, packages: str):
        """Allow normal updates for previously pinned packages."""
        await self._native(ctx, "cog unpin", words(packages))

    @download.command(name="pinned")
    async def pinned(self, ctx):
        """List cogs currently excluded from normal updates."""
        await self._native(ctx, "cog listpinned")

    @download.command(name="version")
    async def version(
        self, ctx, repo: str, revision: str, reload: bool = True, *, packages: str = ""
    ):
        """Update installed cogs from a specific repository revision."""
        await self._native(
            ctx,
            "cog updatetoversion",
            [str(reload), repo, revision, *words(packages, required=False)],
        )

    @download.command(name="find")
    async def find(self, ctx, *, command: str):
        """Find the installed cog and repository behind a command."""
        await self._native(ctx, "findcog", [command])
