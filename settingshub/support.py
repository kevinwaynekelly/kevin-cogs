"""Bounded support archives built from explicit nonpersonal diagnostic fields."""

import io
import json
import re
import time
import zipfile
from collections import deque

import discord
from redbot.core import commands

from .readiness import FEATURES
from .schema import MAX_FILE, TARGETS

ERROR_TTL = 86400
PACKAGE = re.compile(r"[a-zA-Z0-9.+_-]{1,64}\Z")
ERROR_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")


def safe_version(value):
    return value if isinstance(value, str) and PACKAGE.fullmatch(value) else "unavailable"


def support_diagnostics(raw):
    """Whitelist values; raw error text, URLs, settings and paths never enter a bundle."""
    result = {
        "schema": 1,
        "at": raw["at"],
        "guild_id": raw["guild_id"],
        "packages": {},
        "permissions": {},
        "cogs": {},
    }
    result["packages"] = {
        name: safe_version(value)
        for name, value in raw["packages"].items()
        if name in {"Red-DiscordBot", "discord.py", "yt-dlp", "yt-dlp-ejs", "PyNaCl", "davey"}
    }
    result["permissions"] = {
        name: bool(value)
        for name, value in raw["permissions"].items()
        if name in discord.Permissions.VALID_FLAGS
    }
    for name, item in raw["cogs"].items():
        if name not in {*TARGETS, "SettingsHub"}:
            continue
        digest = item.get("installed_source_sha256", "")
        entry = {
            "loaded": bool(item.get("loaded")),
            "disabled": bool(item.get("disabled")),
            "installed_source_sha256": digest
            if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest)
            else "unavailable",
            "source_files": max(0, min(1000, item.get("source_files", 0)))
            if type(item.get("source_files")) is int
            else 0,
        }
        if "diagnostic_error" in item:
            entry["diagnostic_error"] = safe_version(item["diagnostic_error"])
        if name == "AudioPlus" and isinstance(native := item.get("native_player"), dict):
            player = {
                "ready": bool(native.get("ready")),
                "voice_ready": not bool(native.get("voice_error")),
                "ffmpeg_available": native.get("ffmpeg")
                not in (None, "missing", "unavailable", "timed out"),
                "packages": {
                    key: safe_version(value)
                    for key, value in native.get("packages", {}).items()
                    if key in {"discord.py", "yt-dlp", "yt-dlp-ejs", "PyNaCl", "davey"}
                },
                "runtimes": [
                    key for key in native.get("runtimes", []) if key in {"Deno", "Node", "QuickJS"}
                ],
            }
            if isinstance(last := native.get("last_playback_check"), dict):
                player["last_playback_check"] = {
                    key: value
                    for key, value in last.items()
                    if key in {"at", "ok", "success"} and type(value) in {bool, int}
                }
            entry["native_player"] = player
        if name == "LogPlus" and isinstance(delivery := item.get("delivery"), dict):
            entry["delivery"] = {
                key: max(0, value)
                for key, value in delivery.items()
                if key in {"delivered", "recovered", "dropped", "failed"} and type(value) is int
            }
            entry["delivery"]["error_present"] = bool(delivery.get("last_error"))
        result["cogs"][name] = entry
    return result


class SupportBundles:
    def _init_support(self):
        self._support_errors = deque(maxlen=100)

    def _prune_support(self):
        now = time.time()
        while self._support_errors and self._support_errors[0]["at"] < now - ERROR_TTL:
            self._support_errors.popleft()

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if self._closing or not ctx.guild or await self.bot.cog_disabled_in_guild(self, ctx.guild):
            return
        command = getattr(ctx, "command", None)
        cog = getattr(command, "cog", None)
        if (
            cog is None
            or cog.qualified_name not in {*TARGETS, "SettingsHub"}
            or self.bot.get_cog(cog.qualified_name) is not cog
            or self.bot.get_command(command.qualified_name) is not command
        ):
            return
        error = getattr(error, "original", error)
        if (
            isinstance(
                error,
                (
                    commands.UserInputError,
                    commands.CheckFailure,
                    commands.DisabledCommand,
                    commands.CommandOnCooldown,
                ),
            )
            or type(error) is commands.CommandError
        ):
            return
        name = type(error).__name__
        path = command.qualified_name
        if not ERROR_NAME.fullmatch(name) or not re.fullmatch(r"[a-z0-9_ -]{1,100}", path):
            return
        self._prune_support()
        self._support_errors.append(
            {
                "guild_id": ctx.guild.id,
                "at": int(time.time()),
                "cog": cog.qualified_name,
                "command": path,
                "error_type": name,
            }
        )

    async def _support_report(self, ctx):
        self._prune_support()
        diagnostics = support_diagnostics(await self._diagnostic_bundle(ctx))
        readiness = {}
        for key in FEATURES:
            try:
                report = await self._feature_readiness(ctx, key)
                # Names and flags are controlled by the checker. Details can contain exception
                # messages from dependencies, so they are intentionally not exported.
                readiness[key] = {
                    "ready": report.ready,
                    "checks": [
                        {
                            "name": check["name"][:100],
                            "ok": check["ok"],
                            "required": check["required"],
                        }
                        for check in report.checks[:100]
                    ],
                }
            except (commands.CheckFailure, commands.DisabledCommand):
                continue
            except Exception as error:
                readiness[key] = {
                    "ready": False,
                    "error_type": type(error).__name__
                    if ERROR_NAME.fullmatch(type(error).__name__)
                    else "UnknownError",
                }
        visible = {"SettingsHub"}
        for name, cog in self._loaded().items():
            try:
                await self._source_check(ctx, cog)
            except (commands.CheckFailure, commands.DisabledCommand):
                continue
            visible.add(name)
        errors = [
            {key: value for key, value in row.items() if key != "guild_id"}
            for row in self._support_errors
            if row["guild_id"] == ctx.guild.id and row["cog"] in visible
        ]
        return {"diagnostics": diagnostics, "readiness": readiness, "recent_errors": errors}

    async def _download_support(self, ctx):
        report = await self._support_report(ctx)
        lines = [
            "Kevin's Cogs support bundle",
            f"Server ID: {ctx.guild.id}",
            "",
            "Checks inspect local prerequisites and permissions. No live voice or YouTube probe was run.",
            "Recent errors contain only time, registered command name and exception type.",
            "No member records, command arguments, message contents, settings, credentials or raw logs are included.",
            "",
            "Readiness:",
        ]
        for key, item in report["readiness"].items():
            lines.append(f"{key}: {'ready' if item['ready'] else 'needs attention'}")
            for check in item.get("checks", []):
                lines.append(f"  {'OK' if check['ok'] else 'MISSING'} {check['name']}")
        lines.extend(
            [
                "",
                "Useful commands:",
                "settings ready <feature>",
                "settings health",
                "audiostatus",
                "audiocheck now (explicit live playback check)",
                "event <event_id>",
                "emoji",
            ]
        )
        files = {
            "README.txt": "\n".join(lines).encode(),
            **{
                name.replace("_", "-") + ".json": json.dumps(
                    value, ensure_ascii=False, indent=2
                ).encode()
                for name, value in report.items()
            },
        }
        if sum(map(len, files.values())) > MAX_FILE:
            raise commands.CommandError(
                "The support report exceeds 256 KiB. Try individual settings diagnostics and settings ready reports."
            )
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        stream.seek(0)
        await self._reply(
            ctx,
            "Support bundle attached. It contains local checks and bounded recent error types. No live playback probe was run.",
            file=discord.File(stream, filename="cog-support.zip"),
        )
