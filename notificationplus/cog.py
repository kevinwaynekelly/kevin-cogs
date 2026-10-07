"""Owner-only suite failure alerts handed off to Unraid's notification system."""

import asyncio
import logging

from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path

from .constants import MAX_PENDING
from .detection import SuiteErrors
from .outbox import Outbox, completed_task, identifier, safe_exception
from .presentation import Presentation

log = logging.getLogger(__name__)


class NotificationPlus(commands.Cog):
    """Send suite failures to an Unraid host script using existing email settings."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=702035015, force_registration=True)
        self.config.register_global(enabled=False)
        self._presentation = Presentation("NotificationPlus", "notifications")
        self._outbox = Outbox(cog_data_path(self) / "alerts" / "unraid.json")
        self._pending = asyncio.Queue(maxsize=MAX_PENDING)
        self._handler = None
        self._worker = None
        self._closing = False
        self._stopping = False
        self._generation = 0
        self._configuration = asyncio.Lock()
        self._changes = set()
        self._reporting = 0
        self._direct_dropped = 0

    async def cog_load(self):
        await self._outbox.load(bool(await self.config.enabled()))
        self._worker = asyncio.create_task(self._consume(), name="SuiteFailureNotifications")
        self._handler = SuiteErrors(self, asyncio.get_running_loop())
        logging.getLogger().addHandler(self._handler)

    async def cog_unload(self):
        self._stopping = True
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            # Stop admission first; already admitted events may finish normally.
            self._handler.accepting = False
        try:
            await asyncio.wait_for(self._pending.join(), timeout=5)
        except asyncio.TimeoutError:
            pass
        if self._changes:
            await asyncio.gather(*self._changes, return_exceptions=True)
        self._closing = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        while not self._pending.empty():
            self._pending.get_nowait()
            self._pending.task_done()
            if self._handler is not None:
                self._handler.completed()
        if self._handler is not None:
            self._handler.close()
        # Shielded writes complete while holding this lock, including when their
        # command/worker was cancelled during unload.
        async with self._outbox.lock:
            pass

    async def _consume(self):
        while True:
            event = await self._pending.get()
            try:
                if event[4] == self._generation:
                    await self.report(event[0], event[1], guild_id=event[2], error=event[3])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error("Could not queue suite failure alert (%s)", safe_exception(error))
            finally:
                self._pending.task_done()
                if self._handler is not None:
                    self._handler.completed()

    async def report(
        self, cogname, stage, guild_id=None, error=None, kind="notification", cause=None
    ):
        """Queue a sanitized failure; intended for suite-owned failure handlers."""
        if self._closing or self._outbox.state is None:
            return False
        if self._reporting >= MAX_PENDING:
            self._direct_dropped += 1
            return False
        self._reporting += 1
        try:
            return await self._outbox.report(
                cogname, stage, guild_id, error=error, kind=kind, cause=cause
            )
        finally:
            self._reporting -= 1

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        original = getattr(error, "original", error)
        # Invalid usage and denied permission are routine user-facing responses.
        if isinstance(original, commands.CommandError) or isinstance(
            original, asyncio.CancelledError
        ):
            return
        command = getattr(ctx, "command", None)
        owner = getattr(getattr(command, "cog", None), "qualified_name", None)
        if owner in {"Core", "CogManagerUI", "Downloader"}:
            wrapper = "DownloaderPlus" if owner == "Downloader" else "CorePlus"
            if self.bot.get_cog(wrapper) is None:
                return
            owner = wrapper
        guild_id = getattr(getattr(ctx, "guild", None), "id", None)
        await self.report(
            owner,
            identifier(getattr(command, "qualified_name", None)),
            guild_id,
            error=original,
            kind="command",
        )

    async def cog_check(self, ctx):
        if self._closing or self._stopping:
            raise commands.CheckFailure("NotificationPlus is unloading.")
        if not await self.bot.is_owner(ctx.author):
            raise commands.NotOwner("Only the bot owner can manage Unraid failure notifications.")
        return True

    async def cog_before_invoke(self, ctx):
        await self.cog_check(ctx)
        interaction = getattr(ctx, "interaction", None)
        if interaction is not None and not interaction.response.is_done():
            await ctx.defer(ephemeral=True)

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def red_get_data_for_user(self, *, user_id):
        return {}

    async def red_delete_data_for_user(self, *, requester, user_id):
        # The outbox intentionally contains no individual user identifiers.
        return None

    @commands.hybrid_group(name="notifications", fallback="status", invoke_without_command=True)
    @commands.is_owner()
    async def notifications(self, ctx):
        """Inspect and enable suite error alerts through Unraid's email system."""
        state = await self._outbox.snapshot()
        last = state["events"][-1] if state["events"] else None
        lines = [
            f"**Failure alerts** · {'Enabled' if state['enabled'] else 'Disabled'}",
            f"**Container outbox** · `{self._outbox.path}`",
            f"**Retained alerts** · {len(state['events'])} / 64, up to seven days",
            "**Repeat suppression** · Ten minutes per matching failure",
            f"**Last queued** · {last['cog'] + ' · ' + last['at'] if last else 'None'}",
            f"**Latest write error** · {self._outbox.last_error or 'None'}",
            f"**Dropped busy log events** · {self._handler.dropped if self._handler else 0}",
            f"**Dropped busy direct reports** · {self._direct_dropped}",
            "Run Aria's 3_pull_github_repo, then schedule 1_cog_failure_alerts every minute "
            "and enable Alert email notifications. The bridge discovers red-discordbot:/data; "
            "use the outbox path above for a custom layout. "
            "A queued alert does not confirm host processing or email delivery.",
        ]
        await self._presentation.send(ctx, "\n".join(lines), title="Status")

    async def _configure(self, ctx, enabled):
        async with self._configuration:
            await self.cog_check(ctx)
            change = asyncio.create_task(self._change_enabled(enabled))
            self._changes.add(change)
            try:
                await completed_task(change)
            finally:
                self._changes.discard(change)
        await self._presentation.send(
            ctx,
            "Failure alerts enabled. Schedule Aria's 1_cog_failure_alerts every minute "
            "to deliver them through Unraid's saved Alert recipients."
            if enabled
            else "Failure alerts disabled and retained queued events cleared.",
            title="Enabled" if enabled else "Disabled",
            tone="success",
        )

    async def _change_enabled(self, enabled):
        previous = bool(await self.config.enabled())
        await self.config.enabled.set(enabled)
        if not await self._outbox.configure(enabled):
            await self.config.enabled.set(previous)
            raise commands.CommandError(
                "Could not write the persistent notification outbox. Check filesystem permissions."
            )
        self._generation += 1

    @notifications.command(name="enable")
    async def notifications_enable(self, ctx):
        """Enable sanitized failure alerts; the Unraid host bridge must be scheduled."""
        await self._configure(ctx, True)

    @notifications.command(name="disable")
    async def notifications_disable(self, ctx):
        """Disable failure alerts and clear the queued operational records."""
        await self._configure(ctx, False)

    @notifications.command(name="test")
    async def notifications_test(self, ctx):
        """Queue a test for the host bridge without claiming email delivery."""
        if not await self.report("NotificationPlus", "owner test", kind="test"):
            raise commands.BadArgument(
                "Enable notifications first and check the outbox write status."
            )
        await self._presentation.send(
            ctx,
            "Test queued. The Unraid host bridge sends it on its next scheduled run. "
            "Check Unraid's notification log and your email to verify delivery.",
            title="Test queued",
            tone="success",
        )
