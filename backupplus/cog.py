"""Named structure backups, private review, and explicitly confirmed restores."""

import asyncio
import hmac
import io
import json
import logging
import re
import secrets
import time
from collections import defaultdict
from copy import copy, deepcopy
from typing import Optional

import discord
from redbot.core import Config, commands

from .command_support import check_command
from .constants import (
    API_TIMEOUT,
    DEFAULTS_GUILD,
    MAX_AUTO,
    MAX_FILE,
    MAX_MANUAL,
    MAX_SAFETY,
    MAX_STATE,
    PREVIEW_TTL,
    RESTORE_TIMEOUT,
)
from .presentation import Presentation
from .restore import build_plan, execute, target_id
from .snapshot import capture, encode, fingerprint, name_key, parse, portable, validate

log = logging.getLogger(__name__)


class BackupPlus(commands.Cog):
    """Back up server roles, channels and permissions; preview and restore structure."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=702035011, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._presentation = Presentation("BackupPlus", "backup")
        self._locks = defaultdict(asyncio.Lock)
        self._previews = {}
        self._tasks = set()
        self._active = {}
        self._automatic = None
        self._closing = False

    async def cog_load(self):
        self._automatic = asyncio.create_task(
            self._maintain(), name="BackupPlus automatic snapshots"
        )

    async def cog_unload(self):
        self._closing = True
        self._previews.clear()
        tasks = {
            task
            for task in [self._automatic, *self._tasks]
            if task and task is not asyncio.current_task()
        }
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        self._tasks.clear()
        self._locks.clear()

    async def cog_before_invoke(self, ctx):
        await check_command(ctx, ctx.command)
        if getattr(ctx, "interaction", None) is not None and not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=True)
        await self._authorize(ctx)
        self._tasks.add(asyncio.current_task())

    async def cog_after_invoke(self, ctx):
        self._tasks.discard(asyncio.current_task())

    async def cog_command_error(self, ctx, error):
        self._tasks.discard(asyncio.current_task())
        await self._presentation.command_error(ctx, error)

    async def _authorize(self, ctx, *, bot_permissions=False):
        guild = ctx.guild
        if self._closing or not guild or await self.bot.cog_disabled_in_guild(self, guild):
            raise commands.CheckFailure("BackupPlus is unavailable in this server.")
        member = guild.get_member(ctx.author.id)
        if member is None:
            member = await asyncio.wait_for(guild.fetch_member(ctx.author.id), API_TIMEOUT)
        if member.id != guild.owner_id and not member.guild_permissions.administrator:
            raise commands.CheckFailure(
                "Server backups require Discord Administrator permission or the server owner."
            )
        ctx.author = member
        if bot_permissions and (
            not guild.me
            or not guild.me.guild_permissions.manage_roles
            or not guild.me.guild_permissions.manage_channels
        ):
            raise commands.CheckFailure("The bot needs Manage Roles and Manage Channels.")

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    async def _private(self, ctx, content, **kwargs):
        await self._authorize(ctx)
        try:
            return await asyncio.wait_for(
                self._presentation.send(
                    ctx.author,
                    content,
                    title="Server backup",
                    theme_guild=ctx.guild,
                    theme_bot=self.bot,
                    **kwargs,
                ),
                60,
            )
        except (discord.HTTPException, asyncio.TimeoutError):
            raise commands.CheckFailure(
                "I could not DM you. Allow DMs from this server and try again."
            ) from None

    async def _fetch(self, guild):
        roles, channels = await asyncio.wait_for(
            asyncio.gather(guild.fetch_roles(), guild.fetch_channels()), API_TIMEOUT
        )
        return (
            capture(guild, roles, channels),
            {str(role.id): role for role in roles},
            {str(channel.id): channel for channel in channels},
        )

    async def _save(self, guild, state):
        if len(encode(state)) > MAX_STATE:
            raise commands.BadArgument(
                "Server backup storage is full. Delete an old snapshot first."
            )
        section = self.config.guild(guild).state
        async with section.get_lock():
            await section.set(state)

    def _record(self, data, category):
        return {
            "data": data,
            "category": category,
            "mappings": {"roles": {}, "channels": {}},
            "pending": {},
        }

    async def _store(self, guild, state, name, data, *, category="manual", protected=None):
        updated = deepcopy(state)
        snapshots = updated["snapshots"]
        if category == "manual":
            if name in snapshots:
                raise commands.BadArgument(
                    "That name already exists. Choose another name or delete it first."
                )
            if sum(record["category"] == "manual" for record in snapshots.values()) >= MAX_MANUAL:
                raise commands.BadArgument(
                    "Five manual backups are already stored. Delete one first."
                )
        else:
            limit = MAX_AUTO if category == "auto" else MAX_SAFETY
            candidates = sorted(
                (
                    key
                    for key, record in snapshots.items()
                    if record["category"] == category and key != protected
                ),
                key=lambda key: snapshots[key]["data"]["created_at"],
            )
            while sum(record["category"] == category for record in snapshots.values()) >= limit:
                if not candidates:
                    raise commands.BadArgument("Cannot rotate a snapshot currently being restored.")
                snapshots.pop(candidates.pop(0))
        snapshots[name] = self._record(data, category)
        await self._save(guild, updated)
        return updated

    async def _get(self, guild, name):
        name = name_key(name, reserved=True)
        state = await self.config.guild(guild).state()
        record = state["snapshots"].get(name)
        if record is None:
            raise commands.BadArgument("That backup does not exist. Use backup list.")
        validate(record["data"], guild.id)
        return name, state, record

    def _prune_previews(self):
        now = time.monotonic()
        self._previews = {
            key: value for key, value in self._previews.items() if value["expires"] > now
        }

    def _live_hash(self, guild, live):
        return fingerprint(
            {
                "roles": live["roles"],
                "channels": live["channels"],
                "warnings": live["warnings"],
                "features": sorted(guild.features),
                "bitrate_limit": guild.bitrate_limit,
                "bot_roles": sorted(str(role.id) for role in guild.me.roles),
            }
        )

    async def _prepare(self, ctx, name):
        await self._authorize(ctx, bot_permissions=True)
        name, state, record = await self._get(ctx.guild, name)
        live, roles, channels = await self._fetch(ctx.guild)
        plan = build_plan(
            ctx.guild, record["data"], live, deepcopy(record["mappings"]), record["pending"]
        )
        return name, state, record, plan, roles, channels

    @commands.hybrid_group(name="backup", invoke_without_command=True, fallback="status")
    @commands.guild_only()
    @commands.admin_or_permissions(administrator=True)
    async def backup(self, ctx):
        """Back up server roles, channels and permissions; review a restore before applying."""
        state = await self.config.guild(ctx.guild).state()
        automatic = f"Every {state['auto_hours']} hours" if state["auto_hours"] else "Off"
        result = state["last_restore"]
        await self._reply(
            ctx,
            f"Stored snapshots: **{len(state['snapshots'])}**\nAutomatic backups: **{automatic}**\n"
            + (
                f"Last restore: **{result['state']}**, {result['completed']}/{result['total']} operations.\n"
                if result
                else ""
            )
            + (f"Last automatic error: **{state['last_error']}**.\n" if state["last_error"] else "")
            + "Use `backup create <name>`, `backup list`, or `backup help`. Detailed snapshots and restore previews arrive privately by DM.",
        )

    @backup.command(name="help")
    async def backup_help(self, ctx):
        """Show backup commands, limits and restore requirements."""
        await self._presentation.help(ctx)

    @backup.command(name="create")
    async def backup_create(self, ctx, name: str):
        """Save a named snapshot of server roles, channels and permission overwrites."""
        name = name_key(name)
        async with self._locks[ctx.guild.id]:
            await self._authorize(ctx, bot_permissions=True)
            state = await self.config.guild(ctx.guild).state()
            data, _, _ = await self._fetch(ctx.guild)
            await self._authorize(ctx, bot_permissions=True)
            await self._store(ctx.guild, state, name, data)
        await self._reply(
            ctx,
            f"Saved **{name}** with {len(data['roles'])} roles and {len(data['channels'])} channels. Use `backup preview {name}` or `backup download {name}`."
            + (
                " Some channel types were omitted; use backup show for details."
                if data["warnings"]
                else ""
            ),
            tone="success",
        )

    @backup.command(name="list")
    async def backup_list(self, ctx):
        """List this server's manual, automatic and pre-restore snapshots."""
        state = await self.config.guild(ctx.guild).state()
        rows = sorted(
            state["snapshots"].items(), key=lambda item: item[1]["data"]["created_at"], reverse=True
        )
        await self._reply(
            ctx,
            "\n".join(
                f"**{name}** · {record['category']} · <t:{int(record['data']['created_at'])}:R> · {len(record['data']['roles'])} roles / {len(record['data']['channels'])} channels"
                for name, record in rows
            )
            or "No snapshots yet. Use backup create <name>.",
        )

    @backup.command(name="show")
    async def backup_show(self, ctx, name: str):
        """Privately inspect snapshot scope, capture time and restore recovery state."""
        name, _, record = await self._get(ctx.guild, name)
        data = record["data"]
        await self._private(
            ctx,
            f"**{name}** · <t:{int(data['created_at'])}:F>\nRoles: {len(data['roles'])}\nChannels: {len(data['channels'])}\nPermission overwrites: {sum(len(row['overwrites']) for row in data['channels'])}\nCategory: {record['category']}\nUncertain create requests: {len(record['pending'])}\n"
            + "\n".join(data["warnings"])
            + "\nMessages, member role assignments, emoji images, webhooks and server-wide settings are outside this snapshot's scope.",
        )
        await self._reply(ctx, "Backup details sent to your DMs.", tone="success")

    @backup.command(name="download")
    async def backup_download(self, ctx, name: str):
        """DM a portable JSON snapshot; restoration remains limited to this server."""
        name, _, record = await self._get(ctx.guild, name)
        upload = discord.File(
            io.BytesIO(encode(portable(record["data"], record["mappings"]))),
            filename=f"server-backup-{name}.json",
        )
        try:
            await self._private(
                ctx,
                f"**{name}** · Server structure snapshot. Attach this file to `backup import <new-name>` to retain another copy on this server.",
                file=upload,
            )
        finally:
            upload.close()
        await self._reply(ctx, "Snapshot sent to your DMs.", tone="success")

    @backup.command(name="import")
    async def backup_import(self, ctx, name: str, attachment: Optional[discord.Attachment] = None):
        """Validate and save an attached same-server JSON backup without changing Discord."""
        name = name_key(name)
        if attachment is None:
            attachment = next(iter(ctx.message.attachments), None)
        if attachment is None or attachment.size > MAX_FILE:
            raise commands.BadArgument("Attach a BackupPlus JSON file no larger than 2 MiB.")
        raw = await asyncio.wait_for(attachment.read(), API_TIMEOUT)
        data = parse(raw, ctx.guild.id)
        async with self._locks[ctx.guild.id]:
            await self._authorize(ctx)
            state = await self.config.guild(ctx.guild).state()
            await self._store(ctx.guild, state, name, data)
        await self._reply(
            ctx,
            f"Imported **{name}**. Use `backup preview {name}` to inspect changes.",
            tone="success",
        )

    @backup.command(name="preview")
    async def backup_preview(self, ctx, name: str):
        """DM exact restore changes and a requester-bound confirmation valid for ten minutes."""
        async with self._locks[ctx.guild.id]:
            name, _, record, plan, _, _ = await self._prepare(ctx, name)
            self._prune_previews()
            if len(self._previews) >= 200:
                raise commands.CheckFailure(
                    "Restore previews are busy. Try again after an old preview expires."
                )
            token = secrets.token_hex(4)
            preview = {
                "name": name,
                "token": token,
                "record": fingerprint(record),
                "live": self._live_hash(ctx.guild, plan.live),
                "expires": time.monotonic() + PREVIEW_TTL,
            }
            text = f"**{name}** · {len(plan.actions)} planned operations.\n" + "\n".join(
                f"• {action['action'].title()} {action['kind']}: {discord.utils.escape_markdown(action['name'])}"
                for action in plan.actions[:15]
            )
            if plan.blockers:
                text += "\n\n**Restore blocked**\n" + "\n".join(plan.blockers)
            else:
                text += f"\n\nReview the attached complete plan, then use `backup restore {name} {token}` within ten minutes. A fresh pre-restore snapshot is saved first."
            if plan.warnings:
                text += f"\nWarnings: {len(plan.warnings)}; read the attached report."
            report = json.dumps(plan.report(), ensure_ascii=False, indent=2).encode("utf-8")
            if len(report) > 7 * 1024 * 1024:
                report = encode(plan.report())
            upload = discord.File(io.BytesIO(report), filename=f"restore-plan-{name}.json")
            try:
                await self._private(
                    ctx, text, file=upload, tone="warning" if plan.blockers else "info"
                )
            finally:
                upload.close()
            if not plan.blockers:
                self._previews[(ctx.guild.id, ctx.author.id)] = preview
        await self._reply(ctx, "Restore preview sent to your DMs.", tone="success")

    @backup.command(name="restore")
    async def backup_restore(self, ctx, name: str, token: str):
        """Apply a reviewed same-server snapshot; no deletes or member-role assignments."""
        self._prune_previews()
        key = (ctx.guild.id, ctx.author.id)
        preview = self._previews.get(key)
        if (
            preview is None
            or preview["name"] != name.lower()
            or not re.fullmatch(r"[a-f0-9]{8}", token)
            or not hmac.compare_digest(preview["token"], token)
        ):
            raise commands.CheckFailure(
                "Use backup preview <name> first and supply its current confirmation code."
            )
        async with self._locks[ctx.guild.id]:
            name, state, record, plan, roles, channels = await self._prepare(ctx, name)
            if (
                self._previews.get(key) is not preview
                or preview["expires"] <= time.monotonic()
                or preview["record"] != fingerprint(record)
                or preview["live"] != self._live_hash(ctx.guild, plan.live)
            ):
                raise commands.CheckFailure(
                    "The server, snapshot or preview changed. Run backup preview again."
                )
            if plan.blockers:
                raise commands.CheckFailure(
                    "Restore prerequisites changed. Run backup preview again."
                )
            self._previews.pop(key, None)
            if not plan.actions:
                await self._reply(
                    ctx, "The server already matches this backup's supported scope.", tone="success"
                )
                return
            safety = "before-" + secrets.token_hex(6)
            state = await self._store(
                ctx.guild, state, safety, plan.live, category="safety", protected=name
            )
            record = state["snapshots"][name]
            # The executor and persistent recovery record share the same ID mapping.
            plan.mappings = record["mappings"]
            result = {
                "name": name,
                "created_at": time.time(),
                "state": "running",
                "completed": 0,
                "total": len(plan.actions),
                "error": "",
                "safety": safety,
            }
            state["last_restore"] = result
            await self._save(ctx.guild, state)
            self._active[ctx.guild.id] = asyncio.current_task()

            async def before():
                await self._authorize(ctx, bot_permissions=True)
                checked = copy(ctx)
                await check_command(checked, self.backup_restore)

            async def remember(kind, source, identifier, *, uncertain=False, clear=False):
                marker = kind + ":" + source
                if clear:
                    record["pending"].pop(marker, None)
                elif uncertain:
                    record["pending"][marker] = True
                else:
                    record["mappings"]["roles" if kind == "role" else "channels"][source] = (
                        identifier
                    )
                    record["pending"].pop(marker, None)
                await self._save(ctx.guild, state)

            async def progress(action):
                result["completed"] += 1

            try:
                await asyncio.wait_for(
                    execute(
                        ctx.guild,
                        plan,
                        roles,
                        channels,
                        before,
                        remember,
                        progress,
                        reason=f"BackupPlus {name} requested by {ctx.author.id}",
                    ),
                    RESTORE_TIMEOUT,
                )
                result["state"] = "complete"
            except asyncio.CancelledError:
                result["state"], result["error"] = "cancelled", "CancelledError"
                raise
            except Exception as error:
                result["state"], result["error"] = "partial", type(error).__name__
                log.warning(
                    "BackupPlus restore stopped in guild %s (%s)",
                    ctx.guild.id,
                    type(error).__name__,
                )
            finally:
                self._active.pop(ctx.guild.id, None)
                await self._save(ctx.guild, state)
            await self._reply(
                ctx,
                f"Restore **{result['state']}**: {result['completed']}/{result['total']} operations. Pre-restore snapshot: **{safety}**."
                + (
                    f" Stopped with {result['error']}. Run backup preview to review remaining work; use backup bind if a create request has an uncertain result."
                    if result["error"]
                    else ""
                ),
                tone="success" if result["state"] == "complete" else "warning",
            )

    @backup.command(name="delete")
    async def backup_delete(self, ctx, name: str):
        """Delete one stored snapshot without deleting any Discord objects."""
        async with self._locks[ctx.guild.id]:
            name, state, _ = await self._get(ctx.guild, name)
            state["snapshots"].pop(name)
            await self._save(ctx.guild, state)
            self._previews = {
                key: preview
                for key, preview in self._previews.items()
                if key[0] != ctx.guild.id or preview["name"] != name
            }
        await self._reply(ctx, f"Deleted stored snapshot **{name}**.", tone="success")

    @backup.command(name="auto")
    async def backup_auto(self, ctx, hours: int = 0):
        """Set an automatic backup interval of 6–168 hours; zero disables it."""
        if hours != 0 and not 6 <= hours <= 168:
            raise commands.BadArgument("Use 6–168 hours, or zero to disable automatic backups.")
        async with self._locks[ctx.guild.id]:
            state = await self.config.guild(ctx.guild).state()
            state.update(auto_hours=hours, last_attempt=time.time(), last_error="")
            await self._save(ctx.guild, state)
        await self._reply(
            ctx,
            f"Automatic structure backups set to every **{hours} hours**. The first runs after that interval; the latest three are retained."
            if hours
            else "Automatic backups disabled.",
            tone="success",
        )

    @backup.command(name="bind")
    async def backup_bind(self, ctx, name: str, kind: str, source_id: str, target_id_value: str):
        """Bind a backed-up role/channel to an existing same-server object for recovery."""
        if kind not in {"role", "channel"}:
            raise commands.BadArgument("Choose role or channel.")
        async with self._locks[ctx.guild.id]:
            await self._authorize(ctx, bot_permissions=True)
            name, state, record = await self._get(ctx.guild, name)
            live, _, _ = await self._fetch(ctx.guild)
            group = "roles" if kind == "role" else "channels"
            source = next((row for row in record["data"][group] if row["id"] == source_id), None)
            target = next((row for row in live[group] if row["id"] == target_id_value), None)
            if (
                source is None
                or target is None
                or (
                    kind == "role"
                    and (
                        source["managed"]
                        or target["managed"]
                        or source_id == str(ctx.guild.id)
                        or target_id_value == str(ctx.guild.id)
                    )
                )
                or (kind == "channel" and source["kind"] != target["kind"])
            ):
                raise commands.BadArgument(
                    "Choose matching supported objects in this server; default/managed roles cannot be rebound."
                )
            other_targets = {
                target_id(record["mappings"], group, row["id"])
                for row in record["data"][group]
                if row["id"] != source_id
            }
            if target_id_value in other_targets:
                raise commands.BadArgument("Another backed-up object already uses that target.")
            record["mappings"][group][source_id] = target_id_value
            record["pending"].pop(kind + ":" + source_id, None)
            await self._save(ctx.guild, state)
        await self._reply(
            ctx,
            "Recovery mapping saved. Run backup preview again before restoring.",
            tone="success",
        )

    @backup.command(name="cancel")
    async def backup_cancel(self, ctx):
        """Stop this server's running restore; completed Discord changes remain."""
        task = self._active.get(ctx.guild.id)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self._reply(
                ctx,
                "Restore cancelled. Use backup status and preview to inspect remaining work.",
                tone="success",
            )
        else:
            await self._reply(ctx, "No restore is running.")

    async def _maintain(self):
        await self.bot.wait_until_red_ready()
        while not self._closing:
            await self._automatic_once()
            self._prune_previews()
            await asyncio.sleep(60)

    async def _automatic_once(self):
        for guild_id, saved in (await self.config.all_guilds()).items():
            settings = saved.get("state", {})
            guild = self.bot.get_guild(guild_id)
            if (
                not guild
                or not settings.get("auto_hours")
                or time.time() - settings.get("last_attempt", 0) < settings["auto_hours"] * 3600
            ):
                continue
            async with self._locks[guild.id]:
                state = await self.config.guild(guild).state()
                if (
                    self._closing
                    or not state["auto_hours"]
                    or time.time() - state["last_attempt"] < state["auto_hours"] * 3600
                    or await self.bot.cog_disabled_in_guild(self, guild)
                ):
                    continue
                state["last_attempt"] = time.time()
                await self._save(guild, state)
                try:
                    if (
                        not guild.me
                        or not guild.me.guild_permissions.manage_roles
                        or not guild.me.guild_permissions.manage_channels
                    ):
                        raise commands.CheckFailure("Missing bot permissions")
                    data, _, _ = await self._fetch(guild)
                    if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
                        continue
                    state["last_error"] = ""
                    name = "auto-" + secrets.token_hex(6)
                    await self._store(guild, state, name, data, category="auto")
                except Exception as error:
                    state["last_error"] = type(error).__name__
                    await self._save(guild, state)
                    log.warning(
                        "BackupPlus automatic snapshot failed in guild %s (%s)",
                        guild.id,
                        type(error).__name__,
                    )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        task = self._active.get(guild.id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._locks[guild.id]:
            await self.config.guild(guild).clear()
        self._previews = {key: value for key, value in self._previews.items() if key[0] != guild.id}

    async def red_get_data_for_user(self, *, user_id):
        rows = []
        for guild_id, state in (await self.config.all_guilds()).items():
            for name, record in state.get("state", {}).get("snapshots", {}).items():
                for channel in record["data"]["channels"]:
                    for item in channel["overwrites"]:
                        if item["kind"] == "member" and item["id"] == str(user_id):
                            rows.append(
                                {
                                    "guild_id": str(guild_id),
                                    "backup": name,
                                    "channel_id": channel["id"],
                                    **item,
                                }
                            )
        return {"backupplus-overwrites.json": io.BytesIO(encode(rows))} if rows else {}

    async def red_delete_data_for_user(self, *, requester, user_id):
        self._previews.clear()
        tasks = {task for task in self._active.values() if task is not asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for guild_id in await self.config.all_guilds():
            async with self._locks[guild_id]:
                state = await self.config.guild_from_id(guild_id).state()
                state["snapshots"] = {
                    name: record
                    for name, record in state["snapshots"].items()
                    if not any(
                        item["kind"] == "member" and item["id"] == str(user_id)
                        for channel in record["data"]["channels"]
                        for item in channel["overwrites"]
                    )
                }
                await self._save(discord.Object(guild_id), state)
