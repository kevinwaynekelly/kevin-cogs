"""Shared themes, permission-aware discovery and bounded configuration checkpoints."""

import asyncio
import hashlib
import io
import json
import time
from contextlib import AsyncExitStack
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version

import discord
from redbot.core import commands

from .command_support import check_command
from .presentation import COLORS, clip
from .schema import FIELDS, MAX_FILE, TARGETS, select_fields

HUB_DEFAULTS = {
    "theme": {"colors": dict(COLORS), "footer": "Kevin's Cogs"},
    "snapshots": {"enabled": False, "hours": 6, "last_at": 0, "records": []},
}


def differences(before, after, path=""):
    """List changed setting paths without echoing potentially large values."""
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            result.extend(differences(before.get(key), after.get(key), f"{path}.{key}".strip(".")))
        return result
    return [path] if before != after else []


class MaintenanceCommands:
    async def _cache_theme(self, guild_id):
        themes = getattr(self.bot, "_kevin_cogs_themes", None)
        if not isinstance(themes, dict):
            themes = self.bot._kevin_cogs_themes = {}
        themes[guild_id] = await self.config.guild_from_id(guild_id).theme()

    async def _capture_settings(self, guild):
        records = {}
        # Explicitly enabled automatic snapshots run as a server configuration job.
        # Member records, secrets, disabled/unloaded cogs and runtime data stay excluded.
        for name, cog in self._loaded().items():
            if await self.bot.cog_disabled_in_guild(cog, guild):
                continue
            group = cog.config.guild(guild)
            async with AsyncExitStack() as stack:
                for key in sorted(FIELDS[name]):
                    await stack.enter_async_context(group.get_attr(key).get_lock())
                values = {key: await group.get_attr(key)() for key in FIELDS[name]}
            records[name] = select_fields(name, values)
        return {"schema": 1, "guild_id": guild.id, "cogs": records}

    async def _store_snapshot(self, guild, bundle, *, now=None):
        now = time.time() if now is None else now
        raw = json.dumps(bundle, sort_keys=True, ensure_ascii=False).encode()
        if len(raw) > MAX_FILE or not bundle["cogs"]:
            return False
        digest = hashlib.sha256(raw).hexdigest()
        section = self.config.guild(guild).snapshots
        async with section.get_lock():
            state = await section()
            state["last_at"] = int(now)
            records = state["records"]
            if records and records[-1]["hash"] == digest:
                await section.set(state)
                return False
            identifier = str(max(int(now * 1000), int(records[-1]["id"]) + 1 if records else 1))
            records.append({"id": identifier, "at": int(now), "hash": digest, "bundle": bundle})
            state["records"] = records[-10:]
            await section.set(state)
        return True

    async def _snapshot_tick(self, *, now=None):
        now = time.time() if now is None else now
        for guild in self.bot.guilds:
            if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
                continue
            state = await self.config.guild(guild).snapshots()
            if state["enabled"] and now - state["last_at"] >= state["hours"] * 3600:
                bundle = await self._capture_settings(guild)
                # A disable during capture must stop this checkpoint.
                if (await self.config.guild(guild).snapshots())["enabled"] and not self._closing:
                    await self._store_snapshot(guild, bundle, now=now)

    async def _maintenance_loop(self):
        await self.bot.wait_until_red_ready()
        while not self._closing:
            try:
                await self._snapshot_tick()
            except Exception:
                self._maintenance_log.exception("Settings snapshot maintenance failed")
            await asyncio.sleep(60)

    @commands.hybrid_group(name="theme", invoke_without_command=True, fallback="show")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def theme(self, ctx):
        """Customize common colors and footer for Kevin's cogs."""
        theme = await self.config.guild(ctx.guild).theme()
        await self._reply(
            ctx,
            "\n".join(f"**{key.title()}** · #{value:06X}" for key, value in theme["colors"].items())
            + "\n**Footer** · "
            + theme["footer"],
            title="Server theme",
        )

    @theme.command(name="color")
    async def theme_color(self, ctx, tone: str, color: str):
        """Set info, success, warning or error to a six-digit hex color."""
        import re

        tone = tone.lower()
        if tone not in COLORS or not re.fullmatch(r"#?[0-9a-fA-F]{6}", color):
            raise commands.BadArgument(
                "Choose info, success, warning or error and a color such as #818CF8."
            )
        section = self.config.guild(ctx.guild).theme
        async with section.get_lock():
            state = await section()
            state["colors"][tone] = int(color.lstrip("#"), 16)
            await section.set(state)
        await self._cache_theme(ctx.guild.id)
        await self._reply(ctx, "Theme color saved.", tone=tone)

    @theme.command(name="footer")
    async def theme_footer(self, ctx, *, text: str):
        """Set the common footer brand, up to 80 characters."""
        if not 1 <= len(text.strip()) <= 80 or any(ord(c) < 32 for c in text):
            raise commands.BadArgument("Use 1 to 80 characters on one line.")
        section = self.config.guild(ctx.guild).theme
        async with section.get_lock():
            state = await section()
            state["footer"] = text.strip()
            await section.set(state)
        await self._cache_theme(ctx.guild.id)
        await self._reply(ctx, "Footer saved.")

    @theme.command(name="reset")
    async def theme_reset(self, ctx):
        """Restore the standard theme."""
        section = self.config.guild(ctx.guild).theme
        async with section.get_lock():
            await section.set(deepcopy(HUB_DEFAULTS["theme"]))
        await self._cache_theme(ctx.guild.id)
        await self._reply(ctx, "Standard theme restored.")

    @commands.hybrid_command(name="commandbrowser")
    @commands.guild_only()
    async def command_browser(self, ctx, *, query: str = ""):
        """Find available cog commands and see their usage. Example: commandbrowser voice."""
        if len(query) > 80:
            raise commands.BadArgument("Keep the search under 80 characters.")
        matches = []
        for command in sorted(self.bot.walk_commands(), key=lambda c: c.qualified_name):
            if (
                not command.cog
                or command.cog.qualified_name not in {*TARGETS, "SettingsHub"}
                or command.hidden
            ):
                continue
            description = command.short_doc or command.help or "Open this command's help."
            if query.casefold() not in (command.qualified_name + " " + description).casefold():
                continue
            if await self.bot.cog_disabled_in_guild(command.cog, ctx.guild):
                continue
            try:
                await check_command(ctx, command)
            except commands.CommandError:
                continue
            usage = f"{ctx.clean_prefix}{command.qualified_name} {command.signature}".strip()
            matches.append(
                f"**{command.qualified_name}**\n{clip(description, 180)}\nUsage: `{clip(usage, 300)}`"
            )
            if len(matches) == 25:
                break
        await self._reply(
            ctx,
            "\n\n".join(matches) or "No available commands match. Try a shorter search.",
            title="Command browser",
        )

    @commands.hybrid_group(name="snapshots", invoke_without_command=True, fallback="list")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def snapshots(self, ctx):
        """List up to ten saved configuration checkpoints."""
        state = await self.config.guild(ctx.guild).snapshots()
        lines = [
            f"Automatic: {'on' if state['enabled'] else 'off'} · Interval: {state['hours']} hours"
        ]
        lines.extend(
            f"`{r['id']}` · <t:{r['at']}:f> · {len(r['bundle']['cogs'])} cogs"
            for r in reversed(state["records"])
        )
        await self._reply(ctx, "\n".join(lines), title="Configuration snapshots")

    @snapshots.command(name="auto")
    async def snapshots_auto(self, ctx, enabled: bool, hours: int = 6):
        """Enable or disable automatic snapshots, every 1 to 168 hours."""
        if not 1 <= hours <= 168:
            raise commands.BadArgument("Choose an interval of 1 to 168 hours.")
        section = self.config.guild(ctx.guild).snapshots
        async with section.get_lock():
            state = await section()
            state.update(enabled=enabled, hours=hours, last_at=0)
            await section.set(state)
        await self._reply(ctx, "Automatic snapshot policy saved.")

    @snapshots.command(name="create")
    async def snapshots_create(self, ctx):
        """Save a checkpoint of settings you can currently administer."""
        created = await self._store_snapshot(ctx.guild, await self._backup_bundle(ctx))
        await self._reply(
            ctx,
            "Snapshot saved."
            if created
            else "Settings match the latest snapshot or exceed the backup limit.",
        )

    async def _find_snapshot(self, ctx, identifier):
        if len(identifier) > 30:
            raise commands.BadArgument("Use an ID from snapshots list.")
        records = (await self.config.guild(ctx.guild).snapshots())["records"]
        record = next((r for r in records if r["id"] == identifier), None)
        if record is None:
            raise commands.BadArgument("That snapshot expired or does not exist.")
        await self._validate_bundle(ctx, record["bundle"])
        return record

    @snapshots.command(name="diff")
    async def snapshots_diff(self, ctx, identifier: str):
        """Compare a checkpoint with your current eligible configuration."""
        record = await self._find_snapshot(ctx, identifier)
        paths = differences(record["bundle"]["cogs"], (await self._backup_bundle(ctx))["cogs"])
        await self._reply(
            ctx,
            "\n".join(f"`{p}`" for p in paths[:100]) or "No settings changed.",
            title="Changed settings",
        )

    @snapshots.command(name="restore")
    async def snapshots_restore(self, ctx, identifier: str):
        """Preview a validated same-server checkpoint restore."""
        from .cog import RestoreView

        record = await self._find_snapshot(ctx, identifier)
        selected = await self._validate_bundle(ctx, record["bundle"])
        view = RestoreView(self, ctx, record["bundle"], selected)
        await self._send_view(
            ctx,
            view,
            "Restore this configuration checkpoint? Member and operational records are preserved.",
            title="Snapshot restore",
            tone="warning",
        )

    @snapshots.command(name="delete")
    async def snapshots_delete(self, ctx, identifier: str = "all"):
        """Delete one checkpoint, or all saved checkpoints."""
        section = self.config.guild(ctx.guild).snapshots
        async with section.get_lock():
            state = await section()
            state["records"] = (
                []
                if identifier == "all"
                else [r for r in state["records"] if r["id"] != identifier]
            )
            await section.set(state)
        await self._reply(ctx, "Snapshot records removed.")

    async def _diagnostic_bundle(self, ctx):
        packages = {}
        for name in ("Red-DiscordBot", "discord.py", "yt-dlp", "yt-dlp-ejs", "PyNaCl", "davey"):
            try:
                packages[name] = version(name)
            except PackageNotFoundError:
                packages[name] = "missing"
        report = {
            "schema": 1,
            "at": int(time.time()),
            "guild_id": ctx.guild.id,
            "packages": packages,
            "permissions": dict(ctx.channel.permissions_for(ctx.guild.me)),
            "cogs": {},
        }
        for name, cog in self._loaded().items():
            entry = {
                "loaded": True,
                "disabled": await self.bot.cog_disabled_in_guild(cog, ctx.guild),
                "module": type(cog).__module__,
            }
            if name == "AudioPlus":
                entry["native_player"] = await cog.diagnostic_report(ctx.guild.id)
            if name == "LogPlus":
                entry["delivery"] = dict(cog._delivery_status[ctx.guild.id])
            report["cogs"][name] = entry
        return report

    async def _download_diagnostics(self, ctx):
        report = await self._diagnostic_bundle(ctx)
        await self._reply(
            ctx,
            "Diagnostic report attached. Package and permission checks do not test live playback.",
            file=discord.File(
                io.BytesIO(json.dumps(report, indent=2).encode()), filename="cog-diagnostics.json"
            ),
        )
