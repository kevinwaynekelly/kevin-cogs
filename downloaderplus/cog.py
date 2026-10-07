"""Themed owner controls, authenticated triggers and daily Red Downloader updates."""

import asyncio
import io
import json
import logging
import secrets
import time
from copy import deepcopy

import discord
from discord.ext.commands.view import StringView
from redbot.core import Config, commands

from .command_support import check_command, finish_configuration_audit, prepare_hybrid
from .constants import DAILY_DEFAULTS, DISCORD_DEFAULTS, SYNC_DEFAULTS, WEBHOOK_DEFAULTS
from .daily import DailyUpdates, next_daily
from .discord_trigger import DiscordTrigger
from .management import invoke_native, native_command, style_replies, words
from .presentation import Presentation
from .privacy import ConfigurationBarrier, configuration_request
from .slash_sync import sync_enabled
from .trigger_page import trigger_url
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
        self.config.register_global(
            webhook=deepcopy(WEBHOOK_DEFAULTS),
            daily=deepcopy(DAILY_DEFAULTS),
            slash_sync=deepcopy(SYNC_DEFAULTS),
            discord_trigger=deepcopy(DISCORD_DEFAULTS),
        )
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
        self._daily = DailyUpdates(
            self.config, self._daily_update, ready=getattr(bot, "wait_until_red_ready", None)
        )
        self._discord_trigger = DiscordTrigger(
            self.config, self._discord_update, ready=getattr(bot, "wait_until_red_ready", None)
        )
        self._privacy = ConfigurationBarrier()

    async def cog_load(self):
        self.bot.before_invoke(self.style_native_reply)
        try:
            await self._discord_trigger.start()
        except Exception as error:
            log.error(
                "Discord update trigger could not start.",
                extra={
                    "notification_error": type(error).__name__,
                    "notification_stage": "Discord update trigger",
                },
            )
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

        try:
            await self._daily.start()
        except Exception as error:
            log.error(
                "Daily update scheduler could not start.",
                extra={
                    "notification_error": type(error).__name__,
                    "notification_stage": "Daily update",
                },
            )

    async def cog_unload(self):
        self._closing = True
        self.bot.remove_before_invoke_hook(self.style_native_reply)
        await self._privacy.close()
        await self._webhook.close()
        await self._daily.close()
        await self._discord_trigger.close()

    @commands.Cog.listener()
    async def on_message(self, message):
        if not self._closing:
            await self._discord_trigger.accept(message)

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
        data = {}
        for name in ("webhook", "daily", "discord_trigger"):
            policy = await getattr(self.config, name)()
            if policy["owner_id"] == user_id:
                data[name] = {key: policy[key] for key in ("owner_id", "channel_id", "enabled")}
                if name == "discord_trigger":
                    data[name].update(webhook_id=policy["webhook_id"], guild_id=policy["guild_id"])
        if not data:
            return {}
        return {"downloaderplus.json": io.BytesIO(json.dumps(data, indent=2).encode())}

    async def red_delete_data_for_user(self, *, requester, user_id):
        async with self._privacy.deletion(user_id) as erase:
            if not erase:
                return
            async with self._configuration_lock:
                for name, defaults, service in (
                    ("webhook", WEBHOOK_DEFAULTS, self._webhook),
                    ("daily", DAILY_DEFAULTS, self._daily),
                    ("discord_trigger", DISCORD_DEFAULTS, self._discord_trigger),
                ):
                    async with getattr(self.config, name)() as policy:
                        owned = policy["owner_id"] == user_id
                        if owned:
                            policy.clear()
                            policy.update(deepcopy(defaults))
                    if owned:
                        await service.close()

    async def _native(self, ctx, path, arguments=()):
        async with self._operation_lock:
            return await invoke_native(self, ctx, path, arguments, expected={"Downloader"})

    def _webhook_repos(self):
        source = self.bot.get_cog("Downloader")
        return source._repo_manager.repos if source else ()

    async def _webhook_context(self, policy, path, text, *, defer_reload=False, control=None):
        channel = self.bot.get_channel(policy["channel_id"])
        guild = getattr(channel, "guild", None)
        owner = guild.get_member(policy["owner_id"]) if guild else None
        if owner is None or not await self.bot.is_owner(owner):
            raise commands.CheckFailure("The update's bot owner or result channel is unavailable.")
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
        # Self-reload resets the old command objects' permission readiness.
        # Resolve current controls so final sync checks the replacement cog.
        for path in ((control or self.webhook).qualified_name, "download update"):
            await check_command(ctx, native_command(self.bot, path, {"DownloaderPlus"}))
        await check_command(ctx, command)
        return ctx

    async def _webhook_update(self, policy):
        return await self._automatic_update(policy, self.webhook)

    async def _daily_update(self, policy):
        return await self._automatic_update(policy, self.daily)

    async def _discord_update(self, policy):
        return await self._automatic_update(policy, self.discord_trigger)

    async def _automatic_update(self, policy, control):
        async with self._operation_lock:
            ctx = await self._webhook_context(
                policy, "cog update", "update True", defer_reload=True, control=control
            )
            await self.bot.invoke(ctx)
            if ctx.command_failed:
                raise RuntimeError("Native Downloader rejected the automatic update.")
        status = "failed" if ctx.update_failed else "complete"
        detail = (
            "Native Downloader reported an update failure; check the result channel and Red logs."
            if ctx.update_failed
            else "Repositories and unpinned cogs updated; enabled slash commands synced."
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
            async with self._operation_lock:
                if packages:
                    reload_ctx = await self._webhook_context(
                        policy, "reload", " ".join(packages), control=control
                    )
                    await self.bot.invoke(reload_ctx)
                    if reload_ctx.command_failed or reload_ctx.update_failed:
                        raise RuntimeError("Native Core reported an automatic reload failure.")
                sync_ctx = await self._webhook_context(
                    policy, "slash sync", "sync", control=control
                )
                summary = await sync_enabled(self.bot, self.config, sync_ctx)
                await self._presentation.send(sync_ctx, summary, tone="success")

        return status, detail, reload

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
        """Inspect private URL and signed GitHub updates for all repos and cogs."""
        policy = await self.config.webhook()
        result_channel = f"<#{policy['channel_id']}>" if policy["channel_id"] else "Not configured"
        repos = sorted(
            f"{repository_name(repo.url)} · {repo.branch}"
            for repo in self._webhook_repos()
            if repository_name(repo.url)
        )
        embed = self._presentation.embed(
            "Update webhook",
            f"**Automation** · {'Enabled' if policy['enabled'] else 'Disabled'}\n"
            f"**Listener** · {'Running' if self._webhook.runner else 'Stopped'}\n"
            f"**Bind** · `{policy['bind']}:{policy['port']}`\n"
            "**Paths** · `/github` (signed push) · `/update` (private trigger)\n"
            f"**Result channel** · {result_channel}\n"
            f"**Queued** · {'Yes' if policy['pending'] else 'No'}\n"
            "Matching pushes refresh every repository and update unpinned installed cogs. "
            "Changed loaded cogs reload automatically, then enabled slash commands sync.",
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
    async def webhook_setup(self, ctx, port: int = 8766, bind: str = "0.0.0.0", base_url: str = ""):
        """Prepare the update listener and privately send its trigger link and GitHub secret."""
        secret = secrets.token_hex(32)
        try:
            listener(bind, port)
            link = trigger_url(base_url, port, secret)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        await self._source(ctx)
        try:
            await ctx.author.send(
                "DownloaderPlus private update link (anyone with it can request an update):\n"
                f"<{link}>\nOpening this link queues an update after the listener is enabled. "
                "The default address works on your LAN. Use a reachable HTTPS base URL for remote access.\n\n"
                "Optional GitHub webhook secret (keep private):\n"
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
            "Private update link and GitHub secret sent by DM. Run `download webhook enable` to start the listener.",
            tone="success",
        )

    @webhook.command(name="enable")
    @commands.guild_only()
    @configuration_request
    async def webhook_enable(self, ctx):
        """Enable the private URL and signed pushes; send update results in this channel."""
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
            "Webhook updates enabled for your private link and signed GitHub pushes. Update, reload and slash sync results go here.",
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

    @webhook.command(name="link")
    @configuration_request
    async def webhook_link(self, ctx, base_url: str = ""):
        """DM the private update URL; optionally use your HTTPS reverse proxy address."""
        policy = await self.config.webhook()
        if not policy["secret"]:
            raise commands.BadArgument("Run download webhook setup first.")
        try:
            link = trigger_url(base_url, policy["port"], policy["secret"])
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        try:
            await ctx.author.send(
                f"Private DownloaderPlus update link: <{link}>\n"
                "Opening it requests an update of every repository and unpinned cog, "
                "then reloads changes and syncs enabled slash commands. Keep this link private.\n"
                f"Listener: {'enabled' if policy['enabled'] else 'disabled; run download webhook enable'}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            raise commands.BadArgument("Allow DMs from this bot, then retry.") from exc
        await self._presentation.send(ctx, "Private update link sent by DM.", tone="success")

    @download.group(name="daily", invoke_without_command=True, fallback="status")
    async def daily(self, ctx):
        """Show the daily repository/cog update schedule and latest result."""
        policy = await self.config.daily()
        channel = f"<#{policy['channel_id']}>" if policy["channel_id"] else "Not configured"
        due = f"<t:{policy['next_run']}:F>" if policy["enabled"] else "Disabled"
        embed = self._presentation.embed(
            "Daily updates",
            f"**Automation** · {'Enabled' if policy['enabled'] else 'Disabled'}\n"
            f"**Schedule** · {policy['time']} · {policy['timezone']}\n"
            f"**Next update** · {due}\n**Result channel** · {channel}\n"
            "Refresh all repositories, update unpinned cogs, reload changes and sync enabled slash commands.",
        )
        if policy["last_result"]:
            result = policy["last_result"]
            embed.add_field(
                name="Last update", value=f"{result['status']} · {result['detail']}", inline=False
            )
        await self._presentation.send(ctx, embed=embed)

    @daily.command(name="enable")
    @commands.guild_only()
    @configuration_request
    async def daily_enable(self, ctx, clock: str = "", timezone: str = ""):
        """Enable daily updates here; defaults to 04:00 America/Chicago."""
        policy = await self.config.daily()
        clock, timezone = clock or policy["time"], timezone or policy["timezone"]
        try:
            due = next_daily(time.time(), clock, timezone)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
        await self._source(ctx)
        await check_command(ctx, native_command(self.bot, "cog update", {"Downloader"}))
        await check_command(ctx, native_command(self.bot, "slash sync", {"Core"}))
        await self._daily.close()
        policy.update(
            enabled=True,
            time=clock,
            timezone=timezone,
            owner_id=ctx.author.id,
            channel_id=ctx.channel.id,
            generation=secrets.token_hex(16),
            next_run=due,
        )
        await self.config.daily.set(policy)
        await self._daily.start()
        await self._presentation.send(
            ctx,
            f"Daily updates enabled at **{clock} {timezone}**. Next: <t:{due}:F>. "
            "Update, reload and slash sync results will appear here.",
            tone="success",
        )

    @daily.command(name="disable")
    @configuration_request
    async def daily_disable(self, ctx):
        """Disable daily updates and cancel the scheduled worker."""
        async with self.config.daily() as policy:
            policy["enabled"] = False
        await self._daily.close()
        await self._presentation.send(ctx, "Daily updates disabled.", tone="success")

    @download.group(name="discord", invoke_without_command=True, fallback="status")
    async def discord_trigger(self, ctx):
        """Show the approved Discord webhook that can request updates."""
        policy = await self.config.discord_trigger()
        channel = f"<#{policy['channel_id']}>" if policy["channel_id"] else "Not configured"
        embed = self._presentation.embed(
            "Discord update trigger",
            f"**Automation** · {'Enabled' if policy['enabled'] else 'Disabled'}\n"
            f"**Webhook ID** · {policy['webhook_id'] or 'Not configured'}\n"
            f"**Channel** · {channel}\n"
            f"**Queued** · {'Yes' if policy['pending'] else 'No'}\n"
            "Send `updateall` or `!updateall` through the approved webhook to update repositories "
            "and unpinned cogs, reload changes and sync enabled slash commands. "
            "Results appear in the configured channel. No public bot port is required.",
        )
        if policy["last_result"]:
            result = policy["last_result"]
            embed.add_field(
                name="Last update", value=f"{result['status']} · {result['detail']}", inline=False
            )
        await self._presentation.send(ctx, embed=embed)

    @discord_trigger.command(name="enable")
    @commands.guild_only()
    @configuration_request
    async def discord_enable(self, ctx, webhook_id: str = ""):
        """Approve an existing incoming Discord webhook in this text channel."""
        if not isinstance(ctx.channel, discord.TextChannel):
            raise commands.BadArgument(
                "Run this in the webhook's server text channel, outside a thread."
            )
        if not self.bot.intents.message_content or not self.bot.intents.guild_messages:
            raise commands.BadArgument(
                "Enable Message Content and server message intents for Red, then restart it."
            )
        permissions = ctx.channel.permissions_for(ctx.guild.me)
        if (
            not permissions.view_channel
            or not permissions.send_messages
            or not permissions.manage_webhooks
        ):
            raise commands.BadArgument(
                "The bot needs View Channel, Send Messages and Manage Webhooks in this channel."
            )
        policy = await self.config.discord_trigger()
        value = webhook_id.strip() or str(policy["webhook_id"] or "")
        if (
            len(value) > 20
            or not value.isascii()
            or not value.isdecimal()
            or not 0 < int(value) < 2**64
        ):
            raise commands.BadArgument("Supply the webhook's numeric ID, not its private URL.")
        await self._source(ctx)
        await check_command(ctx, native_command(self.bot, "cog update", {"Downloader"}))
        await check_command(ctx, native_command(self.bot, "slash sync", {"Core"}))
        try:
            hook = await self.bot.fetch_webhook(int(value))
        except discord.HTTPException:
            raise commands.BadArgument(
                "Could not inspect that webhook. Check its ID and the bot's Manage Webhooks permission."
            ) from None
        if (
            hook.type != discord.WebhookType.incoming
            or hook.guild_id != ctx.guild.id
            or hook.channel_id != ctx.channel.id
        ):
            raise commands.BadArgument(
                "Choose an incoming webhook that belongs to this server and text channel."
            )
        same_binding = (
            policy["webhook_id"] == hook.id
            and policy["guild_id"] == ctx.guild.id
            and policy["channel_id"] == ctx.channel.id
        )
        await self._discord_trigger.close()
        replacement = deepcopy(DISCORD_DEFAULTS)
        replacement.update(
            enabled=True,
            webhook_id=hook.id,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            owner_id=ctx.author.id,
            generation=secrets.token_hex(16),
            last_message_id=policy["last_message_id"] if same_binding else 0,
            last_result=policy["last_result"] if same_binding else {},
        )
        try:
            await self.config.discord_trigger.set(replacement)
        finally:
            # Re-read durable settings if a write fails, preserving the old worker.
            await self._discord_trigger.start()
        await self._presentation.send(
            ctx,
            "Discord webhook updates enabled here. Post `updateall` through that webhook; "
            "the bot will update repositories and unpinned cogs, reload changes and sync enabled slash commands.",
            tone="success",
        )

    @discord_trigger.command(name="disable")
    @configuration_request
    async def discord_disable(self, ctx):
        """Stop updates from the approved Discord webhook without deleting it."""
        async with self.config.discord_trigger() as policy:
            policy.update(enabled=False, pending=False, generation=secrets.token_hex(16))
        await self._discord_trigger.close()
        await self._presentation.send(ctx, "Discord webhook updates disabled.", tone="success")

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
        """Update all repositories and unpinned cogs, reload changes and sync slash commands.

        Uses one native Red update pass, including repositories without installed
        cogs. Pinned packages remain pinned. No webhook setup is required.
        """
        async with self._operation_lock:
            child = await invoke_native(self, ctx, "cog update", ["True"], expected={"Downloader"})
            if not child.command_failed:
                summary = await sync_enabled(self.bot, self.config, ctx)
                await self._presentation.send(ctx, summary, tone="success")

    @commands.hybrid_command(name="updateall")
    @commands.is_owner()
    async def updateall(self, ctx):
        """Update all repositories and unpinned cogs, reload changes and sync slash commands."""
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
