"""ZIP contents, bounds, server isolation, current checks and seven-cog integration."""

import json
import time
import zipfile
from copy import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import make_channel
from discord.app_commands.commands import validate_name
from redbot.core import commands
from test_cog_hybrid import invoke_slash
from test_readiness import native_report
from test_settingshub import command_runtime as command_fixture
from test_settingshub import hub_runtime as hub_fixture
from test_settingshub import red_command_runtime as red_fixture

from emojistealerplus import EmojiStealerPlus
from settingshub.command_support import configuration_action
from settingshub.support import ERROR_TTL, support_diagnostics

command_runtime = command_fixture
hub_runtime = hub_fixture
red_command_runtime = red_fixture


async def test_support_archive_contains_only_whitelisted_diagnostics_and_no_personal_content(
    hub_runtime,
):
    bot, hub, member, invoke = hub_runtime
    audio = bot.get_cog("AudioPlus")
    audio._resolver.search = AsyncMock()
    audio.diagnostic_report = AsyncMock(
        return_value=native_report(voice_error="secret https://user:password@host.invalid/path")
    )
    ctx = await invoke("!settings")
    source = copy(ctx)
    source.command = bot.get_command("play")
    source.message.content = "!play extremely private search terms"
    await hub.on_command_error(
        source,
        commands.CommandInvokeError(
            RuntimeError("password=secret https://host.invalid private content")
        ),
    )
    ctx = await invoke("!settings support")
    assert not ctx.command_failed
    upload = ctx.send.call_args.kwargs["file"]
    assert upload.filename == "cog-support.zip"
    with zipfile.ZipFile(upload.fp) as archive:
        assert set(archive.namelist()) == {
            "README.txt",
            "diagnostics.json",
            "readiness.json",
            "recent-errors.json",
        }
        data = b"".join(archive.read(name) for name in archive.namelist())
        for forbidden in (
            b"secret",
            b"password",
            b"host.invalid",
            b"private search",
            str(member.id).encode(),
            b"voice_error",
            b"/workspace/",
        ):
            assert forbidden not in data
        errors = json.loads(archive.read("recent-errors.json"))
        assert len(errors) == 1 and errors[0]["error_type"] == "RuntimeError"
        assert errors[0]["command"] == "play"
        diagnostics = json.loads(archive.read("diagnostics.json"))
        assert not diagnostics["cogs"]["AudioPlus"]["native_player"]["voice_ready"]
        assert len(diagnostics["cogs"]["AudioPlus"]["installed_source_sha256"]) == 64
        assert sum(info.file_size for info in archive.infolist()) < 256 * 1024
    audio._resolver.search.assert_not_awaited()


async def test_error_capture_is_bounded_expires_and_ignores_expected_errors_and_other_servers(
    hub_runtime,
):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    ctx.command = bot.get_command("rank")
    for error in (
        commands.BadArgument("private"),
        commands.CheckFailure("denied"),
        commands.DisabledCommand(),
        commands.CommandError("Expected"),
    ):
        await hub.on_command_error(ctx, error)
    assert not hub._support_errors
    for _ in range(105):
        await hub.on_command_error(ctx, RuntimeError("do not export"))
    assert len(hub._support_errors) == 100
    hub._support_errors[0]["at"] = int(time.time()) - ERROR_TTL - 1
    hub._prune_support()
    assert len(hub._support_errors) == 99
    hub._support_errors[-1]["guild_id"] = 987
    bot.get_cog("AudioPlus").diagnostic_report = AsyncMock(return_value=native_report())
    report = await hub._support_report(ctx)
    assert len(report["recent_errors"]) == 98
    bot.get_command("level setup").disable_in(member.guild)
    report = await hub._support_report(ctx)
    assert not report["recent_errors"] and "messagexp" not in report["readiness"]
    await hub.cog_unload()
    assert not hub._support_errors


async def test_support_survives_broken_audio_diagnostics_and_slash_keeps_permissions(
    hub_runtime, monkeypatch
):
    bot, hub, member, invoke = hub_runtime
    bot.get_cog("AudioPlus").diagnostic_report = AsyncMock(
        side_effect=OSError("private /data/password")
    )
    ctx = await invoke_slash(bot, invoke, monkeypatch, "settings support")
    assert not ctx.command_failed
    with zipfile.ZipFile(ctx.send.call_args.kwargs["file"].fp) as archive:
        diagnostics = json.loads(archive.read("diagnostics.json"))
        assert diagnostics["cogs"]["AudioPlus"]["diagnostic_error"] == "OSError"
        assert b"password" not in archive.read("readiness.json")
    bot.owner_ids.discard(member.id)
    ctx = await invoke_slash(bot, invoke, monkeypatch, "settings support")
    assert ctx.command_failed and not any("file" in call.kwargs for call in ctx.send.call_args_list)


async def test_all_seven_cogs_register_valid_slash_options_and_share_helpers(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    emoji = EmojiStealerPlus(bot)
    await bot.add_cog(emoji)
    try:
        roots = {**bot.tree._global_commands, **bot.tree._disabled_global_commands}
        assert len(roots) == 82
        counts = {}
        for root in roots.values():
            leaves = (
                root.walk_commands() if isinstance(root, discord.app_commands.Group) else [root]
            )
            for leaf in leaves:
                if isinstance(leaf, discord.app_commands.Command) and leaf.binding:
                    name = leaf.binding.qualified_name
                    counts[name] = counts.get(name, 0) + 1
        assert counts == {
            "AudioPlus": 62,
            "CommunityPlus": 80,
            "LevelPlus": 74,
            "LogPlus": 41,
            "OwoPlus": 55,
            "EmojiStealerPlus": 6,
            "SettingsHub": 23,
        }

        def check(payload, depth=0):
            validate_name(payload["name"])
            assert 1 <= len(payload["description"]) <= 100
            assert len(payload.get("options", [])) <= 25
            for option in payload.get("options", []):
                if option["type"] in (1, 2):
                    assert depth < 2
                check(option, depth + 1)

        for root in roots.values():
            check(root.to_dict(bot.tree))
        for file in ("presentation.py", "interactive.py", "command_support.py"):
            expected = Path("audioplus", file).read_text()
            assert all(
                Path(name.lower(), file).read_text() == expected
                for name in [*hub._loaded(), "SettingsHub"]
            )
        ctx = await invoke("!settings")
        bundle = await hub._backup_bundle(ctx)
        assert bundle["cogs"]["EmojiStealerPlus"] == {
            "capture": {"enabled": True, "reactions": True, "notify": False, "channel": None}
        }
        assert "copied" not in bundle["cogs"]["EmojiStealerPlus"]
        bundle["cogs"] = {"EmojiStealerPlus": bundle["cogs"]["EmojiStealerPlus"]}
        bundle["cogs"]["EmojiStealerPlus"]["capture"]["enabled"] = False
        selected = await hub._validate_bundle(ctx, bundle)
        async with configuration_action(hub, ctx):
            await hub._apply_bundle(ctx, bundle, selected)
        assert not (await emoji.config.guild(member.guild).capture())["enabled"]
        assert any(
            row["cog"] == "EmojiStealerPlus" for row in await hub._audit_records(member.guild.id)
        )
    finally:
        await bot.remove_cog("EmojiStealerPlus")


async def test_native_and_emoji_readiness_check_exact_current_permissions_without_writes(
    hub_runtime,
):
    bot, hub, member, invoke = hub_runtime
    emoji = EmojiStealerPlus(bot)
    await bot.add_cog(emoji)
    try:
        bot._connection._intents.message_content = True
        bot._connection._intents.guild_messages = True
        bot._connection._intents.guild_reactions = True
        bot._connection._intents.guild_scheduled_events = True
        ctx = await invoke("!settings")
        channel = make_channel(member.guild, 810)
        channel.permissions_for.return_value = discord.Permissions(view_channel=True)
        before = await emoji.config.guild(member.guild).all()
        report = await hub._feature_readiness(ctx, "emojis", channel=channel)
        assert report.ready
        member.guild.me.guild_permissions.create_expressions = False
        assert not (await hub._feature_readiness(ctx, "emojis", channel=channel)).ready
        assert await emoji.config.guild(member.guild).all() == before
        voice = make_channel(member.guild, 811, kind=discord.VoiceChannel)
        report = await hub._feature_readiness(ctx, "nativeevents", voice=voice)
        assert report.ready
        voice.permissions_for.return_value.connect = False
        report = await hub._feature_readiness(ctx, "nativeevents", voice=voice)
        assert not report.ready and "Connect" in report.text()
        member.guild.create_scheduled_event.assert_not_called()
    finally:
        await bot.remove_cog("EmojiStealerPlus")


def test_support_whitelist_never_serializes_raw_errors_paths_unknown_fields_or_urls():
    raw = {
        "at": 123,
        "guild_id": 456,
        "packages": {"discord.py": "2.7.1", "secret": "token"},
        "permissions": {"view_channel": True, "password": "secret"},
        "cogs": {
            "AudioPlus": {
                "loaded": True,
                "disabled": False,
                "installed_source_sha256": "a" * 64,
                "source_files": 5,
                "password": "secret",
                "native_player": native_report(
                    voice_error="secret",
                    runtimes=["Deno", "https://token.invalid"],
                    ffmpeg="/secret/path",
                    last_playback_check={
                        "at": 100,
                        "ok": False,
                        "details": "private",
                        "video": "https://private.invalid",
                    },
                ),
            }
        },
    }
    sanitized = support_diagnostics(raw)
    text = json.dumps(sanitized)
    assert "secret" not in text and "private" not in text and "token" not in text
    assert sanitized["cogs"]["AudioPlus"]["native_player"]["runtimes"] == ["Deno"]


async def test_older_permission_objects_report_missing_create_events_without_crashing(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    member.guild.me.guild_permissions = SimpleNamespace(manage_events=True)
    report = await hub._feature_readiness(ctx, "nativeevents")
    assert not report.ready and "Create Events" in report.text()
    community = bot.get_cog("CommunityPlus")
    with pytest.raises(commands.CheckFailure, match="Create Events"):
        community._native_permissions(member.guild, None)
