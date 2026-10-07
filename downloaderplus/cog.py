"""Themed owner controls and signed GitHub updates backed by Red Downloader."""

import asyncio
import io
import json
import logging
import secrets
from copy import deepcopy

import discord
from discord.ext.commands.view import StringView
from redbot.core import Config, commands

from .command_support import check_command, finish_configuration_audit, prepare_hybrid
from .constants import WEBHOOK_DEFAULTS
from .management import invoke_native, native_command, style_replies, words
from .presentation import Presentation
from .privacy import ConfigurationBarrier, configuration_request
from .webhook import GitHubWebhook, listener, repository_name

log = logging.getLogger("downloaderplus.cog")


class WebhookMessage(discord.Message):
    """A genuine Discord message type for Red checks, with no posted message."""

    async def delete(self, **kwargs):
        return None

    @property
    def jump_url(self):
        return ""


class WebhookContext(commands.Context):
    """Run Red's parser and defer its reload until the update status is saved."""

    async def invoke(self, command, /, *args, **kwargs):
        if getattr(command.cog, "qualified_name", "") == "Core" and command.name == "reload":
            await check_command(self, command)
            self.reload_packages.update(args)
            return None
        return await super().invoke(command, *args, **kwargs)


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
        "download updateall": "cog update",
        "updateall": "cog update",
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
        self.config = Config.get_conf(self, identifier=702035017, force_registration=True)
        self.config.register_global(webhook=deepcopy(WEBHOOK_DEFAULTS))
        self._presentation = Presentation("DownloaderPlus", "download")
        self._closing = False
        # Keep automatic and overlay updates serialized across a self-reload.
        self._operation_lock = bot.__dict__.setdefault(
            "_kevin_downloader_update_lock", asyncio.Lock()
        )
        self._configuration_lock = bot.__dict__.setdefault(
            "_kevin_downloader_webhook_lock", asyncio.Lock()
        )
        self._webhook = GitHubWebhook(self.config, self._webhook_repos, self._webhook_update)
        self._privacy = ConfigurationBarrier()

    async def cog_load(self):
        self.bot.before_invoke(self.style_native_reply)
        try:
            await self._webhook.start()
        except Exception as error:
            self._webhook.error = "Listener failed to start; check the bind address and port."
            log.error(
                "GitHub webhook listener could not start.",
                extra={
                    "notification_error": type(error).__name__,
                    "notification_stage": "Webhook listener",
                },
            )

    async def cog_unload(self):
        self._closing = True
        self.bot.remove_before_invoke_hook(self.style_native_reply)
        await self._privacy.close()
        await self._webhook.close()

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
        policy = await self.config.webhook()
        if policy["owner_id"] != user_id:
            return {}
        data = {key: policy[key] for key in ("owner_id", "channel_id", "enabled")}
        return {"downloaderplus.json": io.BytesIO(json.dumps(data, indent=2).encode())}

    async def red_delete_data_for_user(self, *, requester, user_id):
        async with self._privacy.deletion(user_id) as erase:
            if not erase:
                return
            async with self._configuration_lock:
                async with self.config.webhook() as policy:
                    if policy["owner_id"] != user_id:
                        return
                    policy.clear()
                    policy.update(deepcopy(WEBHOOK_DEFAULTS))
                await self._webhook.close()

    async def _native(self, ctx, path, arguments=()):
        async with self._operation_lock:
            return await invoke_native(self, ctx, path, arguments, expected={"Downloader"})

    def _webhook_repos(self):
        source = self.bot.get_cog("Downloader")
        return source._repo_manager.repos if source else ()

    async def _webhook_context(self, policy, path, text, *, defer_reload=False):
        channel = self.bot.get_channel(policy["channel_id"])
        guild = getattr(channel, "guild", None)
        owner = guild.get_member(policy["owner_id"]) if guild else None
        if owner is None or not await self.bot.is_owner(owner):
            raise commands.CheckFailure("The webhook's bot owner or result channel is unavailable.")
        command = native_command(self.bot, path, {"Downloader" if path == "cog update" else "Core"})

        message = WebhookMessage(
            state=self.bot._connection,
            channel=channel,
            data={
                "id": str(discord.utils.time_snowflake(discord.utils.utcnow())),
                "type": 0,
                "content": f"!{command.root_parent.name if command.root_parent else command.name} {text}",
                "author": {
                    "id": str(owner.id),
                    "username": owner.name,
                    "discriminator": "0",
                    "avatar": None,
                },
                "mentions": [],
                "mention_roles": [],
            },
        )
        message.author = owner
        root = command.root_parent or command
        cls = WebhookContext if defer_reload else commands.Context
        ctx = cls(
            message=message,
            bot=self.bot,
            view=StringView(text),
            prefix="!",
            command=root,
            invoked_with=root.name,
            assume_yes=True,
        )
        ctx.reload_packages = set()
        ctx.update_failed = False

        async def send(content=None, **kwargs):
            # Native failures are often reports rather than raised exceptions.
            # Retain only a boolean, never repository URLs or arbitrary messages.
            embed = kwargs.get("embed")
            texts = [str(content or "")]
            if embed is not None:
                texts.extend([embed.title or "", embed.description or ""])
                texts.extend(field.value for field in embed.fields)
            text = " ".join(texts).casefold()
            if any(
                token in text
                for token in (
                    "failed",
                    "there was an error",
                    "unable to",
                    "cannot ",
                    "could not",
                    "not found in any cog path",
                )
            ):
                ctx.update_failed = True
            return await channel.send(content, **kwargs)

        ctx.send = send
        style_replies(ctx, self._presentation)
        await check_command(ctx, self.webhook)
        await check_command(ctx, self.update)
        await check_command(ctx, command)
        return ctx

    async def _webhook_update(self, policy):
        async with self._operation_lock:
            ctx = await self._webhook_context(
                policy, "cog update", "update True", defer_reload=True
            )
            await self.bot.invoke(ctx)
            if ctx.command_failed:
                raise RuntimeError("Native Downloader rejected the automatic update.")
        status = "failed" if ctx.update_failed else "complete"
        detail = (
            "Native Downloader reported an update failure; check the result channel and Red logs."
            if ctx.update_failed
            else "All repositories refreshed; unpinned installed cogs checked and updated."
        )
        if ctx.update_failed:
            log.error(
                "Automatic Downloader update reported a repository or dependency failure.",
                extra={
                    "notification_stage": "Repository update",
                    "notification_guild_id": ctx.guild.id,
                },
            )

        async def reload():
            # Run the actual Core parser/checks after completion is durable. During
            # a self-reload the old listener closes before the new cog binds.
            packages = sorted(
                ctx.reload_packages, key=lambda name: (name == "downloaderplus", name)
            )
            if not packages:
                return
            async with self._operation_lock:
                reload_ctx = await self._webhook_context(policy, "reload", " ".join(packages))
                await self.bot.invoke(reload_ctx)
                if reload_ctx.command_failed or reload_ctx.update_failed:
                    raise RuntimeError("Native Core reported an automatic reload failure.")

        return status, detail, reload if ctx.reload_packages else None

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
            "`/download available` · `/download install` · `/updateall`",
            inline=False,
        )
        await self._presentation.send(ctx, embed=embed)

    @download.command(name="help")
    async def download_help(self, ctx):
        """Browse cog installation and repository commands."""
        await ctx.send_help(self.download)

    @download.group(name="webhook", invoke_without_command=True, fallback="status")
    async def webhook(self, ctx):
        """Inspect signed GitHub push updates for all installed repos and cogs."""
        policy = await self.config.webhook()
        result_channel = f"<#{policy['channel_id']}>" if policy["channel_id"] else "Not configured"
        repos = sorted(
            f"{repository_name(repo.url)} · {repo.branch}"
            for repo in self._webhook_repos()
            if repository_name(repo.url)
        )
        embed = self._presentation.embed(
            "GitHub webhook",
            f"**Automation** · {'Enabled' if policy['enabled'] else 'Disabled'}\n"
            f"**Listener** · {'Running' if self._webhook.runner else 'Stopped'}\n"
            f"**Bind** · `{policy['bind']}:{policy['port']}/github`\n"
            f"**Result channel** · {result_channel}\n"
            f"**Queued** · {'Yes' if policy['pending'] else 'No'}\n"
            "Matching pushes refresh every repository and update unpinned installed cogs. "
            "Changed loaded cogs reload automatically.",
        )
        embed.add_field(
            name="Accepted repositories / branches", value="\n".join(repos) or "None", inline=False
        )
        result = policy["last_result"]
        if result:
            embed.add_field(
                name="Last update", value=f"{result['status']} · {result['detail']}", inline=False
            )
        if self._webhook.error:
            embed.add_field(name="Listener error", value=self._webhook.error, inline=False)
        await self._presentation.send(ctx, embed=embed)

    @webhook.command(name="setup")
    @commands.guild_only()
    @configuration_request
    async def webhook_setup(self, ctx, port: int = 8766, bind: str = "0.0.0.0"):
        """Prepare a GitHub listener and DM its secret; enable it after adding the hook."""
        try:
            listener(bind, port)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        await self._source(ctx)
        secret = secrets.token_hex(32)
        try:
            await ctx.author.send(
                "DownloaderPlus GitHub webhook secret (keep private):\n"
                f"```\n{secret}\n```\n"
                "Use `application/json`, subscribe to push events, and enable SSL verification. "
                "The payload URL is your public HTTPS endpoint forwarding to `/github` on "
                f"Red's port {port}. Then run `download webhook enable` in the result channel.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            raise commands.BadArgument(
                "Allow DMs from this bot, then retry webhook setup."
            ) from exc
        await self._webhook.close()
        policy = deepcopy(WEBHOOK_DEFAULTS)
        policy.update(
            secret=secret, bind=bind, port=port, owner_id=ctx.author.id, channel_id=ctx.channel.id
        )
        await self.config.webhook.set(policy)
        self._webhook.error = ""
        await self._presentation.send(
            ctx,
            "Secret sent privately. Add the GitHub webhook, then run `download webhook enable`.",
            tone="success",
        )

    @webhook.command(name="enable")
    @commands.guild_only()
    @configuration_request
    async def webhook_enable(self, ctx):
        """Enable authenticated push updates; send native update results in this channel."""
        policy = await self.config.webhook()
        if not policy["secret"]:
            raise commands.BadArgument("Run download webhook setup first.")
        await self._source(ctx)
        await check_command(ctx, native_command(self.bot, "cog update", {"Downloader"}))
        await self._webhook.close()
        async with self.config.webhook() as policy:
            policy.update(enabled=True, owner_id=ctx.author.id, channel_id=ctx.channel.id)
        try:
            await self._webhook.start()
        except Exception as error:
            async with self.config.webhook() as policy:
                policy["enabled"] = False
            log.error(
                "GitHub webhook listener could not start.",
                extra={
                    "notification_error": type(error).__name__,
                    "notification_stage": "Webhook listener",
                    "notification_guild_id": ctx.guild.id,
                },
            )
            raise commands.BadArgument(
                "Listener failed to start. Check Red logs and the port mapping."
            ) from None
        self._webhook.error = ""
        await self._presentation.send(
            ctx,
            "Webhook updates enabled. Only signed pushes to an installed GitHub repository's tracked branch trigger updates.",
            tone="success",
        )

    @webhook.command(name="disable")
    @configuration_request
    async def webhook_disable(self, ctx):
        """Stop the webhook listener and cancel pending automatic updates."""
        async with self.config.webhook() as policy:
            policy.update(enabled=False, pending=False)
        await self._webhook.close()
        await self._presentation.send(ctx, "Webhook updates disabled.", tone="success")

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

    @download.command(name="updateall")
    async def download_updateall(self, ctx):
        """Refresh every repository, update unpinned cogs and reload changed cogs.

        Uses one native Red update pass, including repositories without installed
        cogs. Pinned packages remain pinned. No webhook setup is required.
        """
        await self._native(ctx, "cog update", ["True"])

    @commands.hybrid_command(name="updateall")
    @commands.is_owner()
    async def updateall(self, ctx):
        """Update all repositories and unpinned installed cogs, then reload changes."""
        await check_command(ctx, self.download_updateall)
        await self.download_updateall.callback(self, ctx)

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
