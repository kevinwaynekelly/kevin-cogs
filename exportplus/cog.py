"""Administrator-requested, private server chat exports with owned bounded jobs."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import discord
from redbot.core import commands
from redbot.core.data_manager import cog_data_path

from .command_support import check_command
from .constants import (
    JOB_TIMEOUT,
    MAX_EXPORT_BYTES,
    MAX_JOBS,
    MAX_RETAINED,
    MAX_STORAGE_BYTES,
    RETENTION,
)
from .history import accessible, discover, scan_channel
from .presentation import Presentation
from .transcript import ExportLimit, TranscriptWriter, parse_date, stamp

log = logging.getLogger(__name__)
ExportChannel = Union[
    discord.TextChannel,
    discord.Thread,
    discord.ForumChannel,
    discord.VoiceChannel,
    discord.StageChannel,
]


@dataclass
class ExportJob:
    ctx: object
    root: Path
    after: object
    before: object
    include_bots: bool
    include_threads: bool
    scope: object = None
    task: object = None
    io_task: object = None
    writer: object = None
    progress: object = None
    check_access: object = None
    channels: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    volumes: list = field(default_factory=list)
    messages: int = 0
    complete: bool = True
    current: object = None
    state: str = "running"
    created: float = field(default_factory=time.time)
    finished: float = 0
    last_progress: float = 0
    delivered: int = 0
    error: str = ""
    transfer_lock: object = field(default_factory=asyncio.Lock)
    transfers: set = field(default_factory=set)
    privacy_lock: object = field(default_factory=asyncio.Lock)
    excluded_users: set = field(default_factory=set)

    @property
    def guild_id(self):
        return self.ctx.guild.id

    @property
    def owner_id(self):
        return self.ctx.author.id

    def warn(self, text):
        self.complete = False
        if len(self.warnings) < 200:
            self.warnings.append(text)
        elif len(self.warnings) == 200:
            self.warnings.append(
                "Additional warnings omitted; channel statuses remain in the index."
            )

    def manifest(self):
        return {
            "schema": 1,
            "server": self.ctx.guild.name,
            "server_id": str(self.guild_id),
            "created_at": stamp(datetime.fromtimestamp(self.created, timezone.utc)),
            "after": stamp(self.after),
            "before": stamp(self.before),
            "include_bots": self.include_bots,
            "include_threads": self.include_threads,
            "scope": str(self.scope.id) if self.scope else "server",
            "complete": self.complete,
            "messages": self.messages,
            "channels": self.channels,
            "warnings": self.warnings,
        }


class ExportPlus(commands.Cog):
    """Export accessible server chats into readable files for ChatGPT."""

    def __init__(self, bot):
        self.bot = bot
        self._presentation = Presentation("ExportPlus", "export")
        self._root = cog_data_path(self) / "exports"
        self._jobs = {}
        self._lock = asyncio.Lock()
        self._maintenance = None
        self._closing = False

    async def cog_load(self):
        # No job resumes after reload; discarded orphan directories cannot leak
        # into a new requester's download and never accumulate indefinitely.
        shutil.rmtree(self._root, ignore_errors=True)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._maintenance = asyncio.create_task(self._maintain(), name="ExportPlus retention")

    async def cog_unload(self):
        self._closing = True
        if self._maintenance:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
        async with self._lock:
            jobs = list(self._jobs.values())
            for job in jobs:
                await self._erase(job)
            self._jobs.clear()
            shutil.rmtree(self._root, ignore_errors=True)

    async def _maintain(self):
        await self.bot.wait_until_red_ready()
        while not self._closing:
            await asyncio.sleep(600)
            async with self._lock:
                await self._prune()

    async def _erase(self, job):
        tasks = {
            task
            for task in [job.task, *job.transfers]
            if task and task is not asyncio.current_task()
        }
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        shutil.rmtree(job.root, ignore_errors=True)
        self._jobs.pop(job.guild_id, None)

    async def _prune(self):
        for job in list(self._jobs.values()):
            if job.finished and time.time() - job.finished >= RETENTION:
                await self._erase(job)

    async def cog_before_invoke(self, ctx):
        if getattr(ctx, "interaction", None) is not None:
            await check_command(ctx, ctx.command)
            if not ctx.interaction.response.is_done():
                await ctx.defer(ephemeral=True)

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    async def _private(self, job, content, **kwargs):
        return await asyncio.wait_for(
            self._presentation.send(
                job.ctx.author,
                content,
                title="Chat export",
                theme_guild=job.ctx.guild,
                theme_bot=self.bot,
                **kwargs,
            ),
            60,
        )

    async def _authorize(self, job, *, files=False):
        if self._closing:
            raise commands.CheckFailure("ExportPlus is unloading.")
        if self._jobs.get(job.guild_id) is not job:
            raise commands.CheckFailure("This export was cleared, expired or replaced.")
        guild = self.bot.get_guild(job.guild_id)
        member = guild.get_member(job.owner_id) if guild else None
        if member is None or await self.bot.cog_disabled_in_guild(self, guild):
            raise commands.CheckFailure("The export requester or cog is no longer available.")
        ctx = copy(job.ctx)
        ctx.author = member
        await check_command(ctx, job.ctx.command)
        job.ctx.author = member
        if files:
            for row in job.channels:
                if not row["messages"]:
                    continue
                channel = guild.get_channel_or_thread(int(row["id"]))
                if channel is None:
                    try:
                        channel = await asyncio.wait_for(guild.fetch_channel(int(row["id"])), 20)
                    except (discord.HTTPException, asyncio.TimeoutError):
                        raise commands.CheckFailure(
                            "A source channel is no longer accessible."
                        ) from None
                if not await accessible(channel, member, guild.me):
                    raise commands.CheckFailure(
                        "History access changed. Clear this export and start a fresh one."
                    )

    async def _progress(self, job, *, force=False):
        if not job.progress or not force and time.monotonic() - job.last_progress < 30:
            return
        job.last_progress = time.monotonic()
        embed = self._presentation.embed(
            "Chat export",
            f"State: {job.state}\nMessages: {job.messages:,}\nChannels/threads checked: {len(job.channels):,}\n"
            f"Use `!export status` for progress or `!export cancel` to stop.",
            tone="success" if job.state == "ready" else "info",
        )
        embed = self._presentation.apply_theme(embed, bot=self.bot, guild=job.ctx.guild)
        try:
            await asyncio.wait_for(job.progress.edit(embed=embed), 15)
        except discord.NotFound:
            pass
        except (discord.HTTPException, asyncio.TimeoutError) as error:
            log.warning(
                "Chat export progress notification failed",
                extra={
                    "notification_error": type(error).__name__,
                    "notification_stage": "export_progress",
                    "notification_guild_id": job.guild_id,
                },
            )

    async def _start(self, ctx, *, scope=None, after=None, before=None, bots=True, threads=True):
        if not self.bot.intents.message_content:
            raise commands.CheckFailure(
                "Enable Message Content in Discord's Developer Portal and Red before exporting chats."
            )
        try:
            after, before = parse_date(after), parse_date(before)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from None
        cutoff = discord.utils.utcnow()
        before = min(before, cutoff) if before else cutoff
        if after and after >= before:
            raise commands.BadArgument("The after date must be earlier than the before date.")
        if scope and scope.guild.id != ctx.guild.id:
            raise commands.BadArgument("Choose a channel from this server.")
        if scope and not await accessible(scope, ctx.author, ctx.guild.me):
            raise commands.CheckFailure("You and the bot need access to this channel's history.")
        if isinstance(scope, discord.ForumChannel) and not threads:
            raise commands.BadArgument("Forum exports need threads enabled to include their posts.")
        async with self._lock:
            await self._prune()
            existing = self._jobs.get(ctx.guild.id)
            if existing and existing.task and not existing.task.done():
                raise commands.CheckFailure("An export is already running in this server.")
            if existing:
                if existing.owner_id != ctx.author.id:
                    raise commands.CheckFailure(
                        "Another administrator's export is retained. Wait for expiry or ask them to clear it."
                    )
                await self._erase(existing)
            running = sum(bool(job.task and not job.task.done()) for job in self._jobs.values())
            used = sum(
                path.stat().st_size
                for job in self._jobs.values()
                if job.task is None or job.task.done()
                for path in job.root.rglob("*")
                if path.is_file()
            )
            if (
                running >= MAX_JOBS
                or len(self._jobs) >= MAX_RETAINED
                or used + (running + 1) * MAX_EXPORT_BYTES * 2 > MAX_STORAGE_BYTES
            ):
                raise commands.CheckFailure(
                    "Export storage or workers are busy. Clear an old export or try later."
                )
            root = self._root / f"{ctx.guild.id}-{uuid.uuid4().hex}"
            job = ExportJob(ctx, root, after, before, bots, threads, scope)
            try:
                job.progress = await self._private(
                    job,
                    f"Export requested for **{discord.utils.escape_markdown(ctx.guild.name)}**. Files will be sent here. Starting history scan.",
                )
            except (discord.HTTPException, asyncio.TimeoutError):
                raise commands.CheckFailure(
                    "I could not DM you. Allow direct messages from this server and try again."
                ) from None
            job.writer = TranscriptWriter(root, ctx.guild.name)
            job.check_access = lambda: self._check_progress(job)
            self._jobs[ctx.guild.id] = job
            job.task = asyncio.create_task(self._run(job), name=f"ExportPlus {ctx.guild.id}")
        await self._reply(
            ctx,
            "Export started. Your files and progress will arrive by DM. Use `!export status` or `!export cancel`.",
            tone="success",
        )

    async def _check_progress(self, job):
        await self._authorize(job)
        await self._progress(job)

    async def _scan(self, job):
        guild = job.ctx.guild
        async for channel in discover(
            job, guild, job.ctx.author, scope=job.scope, threads=job.include_threads
        ):
            await self._check_progress(job)
            await scan_channel(job, channel, guild, job.ctx.author)

    async def _run(self, job):
        try:
            try:
                await asyncio.wait_for(self._scan(job), JOB_TIMEOUT)
            except (ExportLimit, asyncio.TimeoutError) as error:
                job.warn(
                    str(error)
                    if isinstance(error, ExportLimit)
                    else "The four-hour export deadline was reached. Use a narrower date or channel filter."
                )
                for row in job.channels:
                    if row["status"] == "pending":
                        row["status"] = "partial: export deadline reached"
            await self._authorize(job, files=True)
            async with job.privacy_lock:
                job.state = "packaging"
                job.io_task = asyncio.create_task(
                    asyncio.to_thread(job.writer.finish, job.manifest())
                )
                # Cancellation waits for the compressor before removing its directory.
                job.volumes = await asyncio.shield(job.io_task)
            job.state, job.finished = "ready", time.time()
            await self._progress(job, force=True)
            await self._deliver(job)
        except asyncio.CancelledError:
            if job.io_task:
                await asyncio.gather(job.io_task, return_exceptions=True)
            shutil.rmtree(job.root, ignore_errors=True)
            job.state, job.finished = "cancelled", time.time()
            raise
        except (commands.CommandError, discord.HTTPException, asyncio.TimeoutError) as error:
            if not isinstance(error, commands.CommandError):
                log.warning(
                    "Chat export or private delivery failed",
                    extra={
                        "notification_error": type(error).__name__,
                        "notification_stage": "export_delivery",
                        "notification_guild_id": job.guild_id,
                    },
                )
            if job.state == "ready":
                job.error = f"Private delivery failed ({type(error).__name__}). Use !export download to retry."
            else:
                shutil.rmtree(job.root, ignore_errors=True)
                job.state, job.finished = "failed", time.time()
                job.error = (
                    "Access changed or Discord could not finish the request. Start a fresh export."
                )
            await self._notify_failure(job)
        except Exception as error:
            shutil.rmtree(job.root, ignore_errors=True)
            job.state, job.finished = "failed", time.time()
            job.error = (
                f"Export failed ({type(error).__name__}). Check bot disk space and try again."
            )
            log.warning(
                "Chat export job failed",
                extra={
                    "notification_error": type(error).__name__,
                    "notification_stage": "export_job",
                    "notification_guild_id": job.guild_id,
                },
            )
            await self._notify_failure(job)

    async def _notify_failure(self, job):
        try:
            await self._private(job, job.error, tone="warning")
        except (discord.HTTPException, asyncio.TimeoutError) as error:
            log.warning(
                "Chat export failure notification delivery failed",
                extra={
                    "notification_error": type(error).__name__,
                    "notification_stage": "export_failure_notice",
                    "notification_guild_id": job.guild_id,
                },
            )

    async def _deliver(self, job, *, part=0, text=False):
        task = asyncio.current_task()
        job.transfers.add(task)
        try:
            async with job.transfer_lock:
                await self._send_files(job, part=part, text=text)
                if not text and not part:
                    job.error = ""
        finally:
            job.transfers.discard(task)

    async def _send_files(self, job, *, part=0, text=False):
        await self._authorize(job, files=True)
        paths = sorted(job.root.glob("chat-*.txt")) if text else job.volumes
        if part:
            if not 1 <= part <= len(paths):
                raise commands.BadArgument(f"Choose a part from 1 to {len(paths)}.")
            paths = [paths[part - 1]]
        for path in paths:
            await self._authorize(job, files=True)
            upload = discord.File(path, filename=path.name)
            try:
                await self._private(
                    job,
                    f"**{path.name}** · {job.messages:,} messages exported. "
                    + ("Some history is missing. Check INDEX.txt. " if not job.complete else "")
                    + (
                        "Upload this text file directly to ChatGPT."
                        if text
                        else "Extract the ZIP and upload INDEX.txt plus the chat text files to ChatGPT. Use `!export text 1` for a direct text attachment."
                    ),
                    file=upload,
                    tone="success" if job.complete else "warning",
                )
            finally:
                upload.close()
            if not text:
                job.delivered += 1

    async def _job(self, ctx):
        async with self._lock:
            await self._prune()
            job = self._jobs.get(ctx.guild.id)
        if job is None or job.owner_id != ctx.author.id:
            raise commands.CheckFailure(
                "You have no retained export in this server. Use export server to start one."
            )
        return job

    @commands.hybrid_group(name="export", invoke_without_command=True, fallback="status")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def export(self, ctx):
        """Export server chats privately into readable files for ChatGPT."""
        async with self._lock:
            await self._prune()
            job = self._jobs.get(ctx.guild.id)
        if job and job.owner_id == ctx.author.id:
            await self._show_status(ctx, job)
        else:
            await self._reply(
                ctx,
                "Use `!export server` for this server's accessible chats, or `!export channel #chat` for one channel. Files arrive by DM. Use `!export help` for date filters and controls.",
            )

    @export.command(name="help")
    async def export_help(self, ctx):
        """Show export commands, filters and private delivery details."""
        await self._presentation.help(ctx)

    @export.command(name="server")
    async def export_server(
        self,
        ctx,
        after: Optional[str] = None,
        before: Optional[str] = None,
        bots: bool = True,
        threads: bool = True,
    ):
        """Export accessible server chats. ISO after is inclusive; before is exclusive, UTC."""
        await self._start(ctx, after=after, before=before, bots=bots, threads=threads)

    @export.command(name="channel")
    async def export_channel(
        self,
        ctx,
        channel: ExportChannel,
        after: Optional[str] = None,
        before: Optional[str] = None,
        bots: bool = True,
        threads: bool = True,
    ):
        """Export one channel, thread or forum; optionally filter dates and bot messages."""
        await self._start(
            ctx, scope=channel, after=after, before=before, bots=bots, threads=threads
        )

    async def _show_status(self, ctx, job):
        await self._reply(
            ctx,
            f"State: {job.state}\nMessages: {job.messages:,}\nChannels/threads checked: {len(job.channels):,}\n"
            f"ZIP parts: {len(job.volumes)}\nText parts: {len(list(job.root.glob('chat-*.txt')))}\n"
            + (
                "Some history was skipped or incomplete; read INDEX.txt.\n"
                if not job.complete
                else ""
            )
            + (job.error + "\n" if job.error else "")
            + "Files are cached for 24 hours or until cleared/reloaded. Use `!export download`, `!export text 1`, or `!export clear`.",
        )

    @export.command(name="progress", aliases=["status"])
    async def export_progress(self, ctx):
        """Show your export progress, completion and retained file counts."""
        await self._show_status(ctx, await self._job(ctx))

    @export.command(name="cancel")
    async def export_cancel(self, ctx):
        """Cancel your running export and erase its partial files."""
        job = await self._job(ctx)
        async with self._lock:
            await self._erase(job)
        await self._reply(ctx, "Export cancelled and its temporary files erased.", tone="success")

    @export.command(name="download")
    async def export_download(self, ctx, part: int = 0):
        """DM your completed ZIP export again; zero sends all archive parts."""
        job = await self._job(ctx)
        if job.state != "ready":
            raise commands.CheckFailure(
                "Your export is not ready. Use export status to check progress."
            )
        if part < 0:
            raise commands.BadArgument("Use zero for all parts or a positive part number.")
        try:
            await self._deliver(job, part=part)
        except (discord.HTTPException, asyncio.TimeoutError):
            raise commands.CheckFailure(
                "Private delivery failed. Allow DMs and try again."
            ) from None
        await self._reply(ctx, "Export files sent to your DMs.", tone="success")

    @export.command(name="text")
    async def export_text(self, ctx, part: int = 1):
        """DM one readable text part for direct upload to ChatGPT."""
        job = await self._job(ctx)
        if job.state != "ready":
            raise commands.CheckFailure("Your export is not ready.")
        if part < 1:
            raise commands.BadArgument("Text part numbers start at one.")
        try:
            await self._deliver(job, part=part, text=True)
        except (discord.HTTPException, asyncio.TimeoutError):
            raise commands.CheckFailure(
                "Private delivery failed. Allow DMs and try again."
            ) from None
        await self._reply(ctx, "Chat text sent to your DMs.", tone="success")

    @export.command(name="clear")
    async def export_clear(self, ctx):
        """Erase your cached export, cancelling a running scan if needed."""
        await self.export_cancel.callback(self, ctx)

    async def red_get_data_for_user(self, *, user_id: int):
        """Return only messages authored by the requested user, not a full chat archive."""
        import io

        async with self._lock:
            await self._prune()
            output = io.BytesIO()
            for job in self._jobs.values():
                if job.state != "ready":
                    continue
                for path in sorted(job.root.glob("messages-*.jsonl")):
                    with path.open(encoding="utf-8") as source:
                        for line in source:
                            row = json.loads(line)
                            if row["author"]["id"] == str(user_id):
                                output.write(line.encode())
            if not output.tell():
                return {}
            output.seek(0)
            return {"retained-chat-messages.jsonl": output}

    async def red_delete_data_for_user(self, *, requester: str, user_id: int):
        async with self._lock:
            for job in list(self._jobs.values()):
                # Pause append/packaging, then cancel all affected scans and transfers.
                async with job.privacy_lock:
                    active = bool(job.task and not job.task.done())
                    if active:
                        # In-flight scans must never append a deleted author's later rows.
                        # Bound transient deletion barriers, cancelling only this job if full.
                        if len(job.excluded_users) >= 1024:
                            await self._erase(job)
                            continue
                        job.excluded_users.add(user_id)
                    affected = job.owner_id == user_id
                    if not affected and job.writer is not None:
                        try:
                            affected = await asyncio.to_thread(job.writer.contains_author, user_id)
                        except (OSError, ValueError, KeyError, TypeError):
                            # An unreadable archive cannot prove it contains no user data.
                            affected = True
                    if affected:
                        await self._erase(job)
