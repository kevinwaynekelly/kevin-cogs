"""Configured/candidate channels, role safety, dependencies and no settings writes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import make_channel, make_guild
from redbot.core import commands
from test_cog_hybrid import invoke_slash
from test_settingshub import command_runtime as command_fixture
from test_settingshub import hub_runtime as hub_fixture
from test_settingshub import red_command_runtime as red_fixture

command_runtime = command_fixture
hub_runtime = hub_fixture
red_command_runtime = red_fixture


def member_role(guild, *, position=1, permissions=0, managed=False):
    role = discord.Role(
        guild=guild,
        state=Mock(),
        data={
            "id": "333",
            "name": "Members",
            "permissions": str(permissions),
            "position": position,
            "managed": managed,
        },
    )
    top = discord.Role(
        guild=guild,
        state=Mock(),
        data={"id": "999", "name": "Scarlet", "permissions": "0", "position": 10},
    )
    guild.me.top_role = top
    guild.get_role.side_effect = lambda rid: role if rid == role.id else None
    return role


async def test_welcome_checks_actual_destination_permissions_and_is_read_only(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    bot._connection._intents.members = True
    ctx = await invoke("!settings")
    channel = make_channel(member.guild, 900)
    cog = bot.get_cog("CommunityPlus")
    before = await cog.config.guild(member.guild).all()
    report = await hub._feature_readiness(ctx, "welcome", channel=channel)
    assert report.ready and channel.mention in report.text()
    channel.permissions_for.return_value = discord.Permissions(view_channel=True)
    report = await hub._feature_readiness(ctx, "welcome", channel=channel)
    assert not report.ready and "Send Messages" in report.text()
    assert await cog.config.guild(member.guild).all() == before


async def test_missing_configured_channel_and_text_fallback_are_distinguished(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    bot._connection._intents.members = True
    ctx = await invoke("!settings")
    report = await hub._feature_readiness(ctx, "welcome")
    assert not report.ready and "Select or configure" in report.text()
    channel = make_channel(member.guild, 900)
    channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, send_messages=True
    )
    report = await hub._feature_readiness(ctx, "welcome", channel=channel)
    assert report.ready and "text fallback" in report.text() and "Ready with notes" in report.text()


@pytest.mark.parametrize(
    "position, permissions, managed", [(10, 0, False), (1, 8, False), (1, 0, True)]
)
async def test_unsafe_unassignable_roles_fail_before_any_grant(
    hub_runtime, position, permissions, managed
):
    bot, hub, member, invoke = hub_runtime
    bot._connection._intents.members = True
    ctx = await invoke("!settings")
    role = member_role(member.guild, position=position, permissions=permissions, managed=managed)
    report = await hub._feature_readiness(ctx, "autorole", role=role)
    assert not report.ready
    member.add_roles.assert_not_awaited()
    assert (await bot.get_cog("CommunityPlus").config.guild(member.guild).autorole())[
        "role_id"
    ] is None


async def test_safe_candidate_role_and_missing_manage_roles(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    bot._connection._intents.members = True
    ctx = await invoke("!settings")
    role = member_role(member.guild)
    assert (await hub._feature_readiness(ctx, "autorole", role=role)).ready
    member.guild.me.guild_permissions = discord.Permissions.none()
    assert not (await hub._feature_readiness(ctx, "autorole", role=role)).ready


async def test_voice_rooms_check_hub_and_destination_category(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    bot._connection._intents.voice_states = True
    ctx = await invoke("!settings")
    voice = make_channel(member.guild, 901, kind=discord.VoiceChannel)
    category = make_channel(member.guild, 902, kind=discord.CategoryChannel)
    voice.category = category
    report = await hub._feature_readiness(ctx, "voicerooms", voice=voice)
    assert report.ready
    category.permissions_for.return_value = discord.Permissions(view_channel=True, connect=True)
    report = await hub._feature_readiness(ctx, "voicerooms", voice=voice)
    assert not report.ready and category.mention in report.text()


def native_report(**overrides):
    return {
        "voice_error": None,
        "ffmpeg": "ffmpeg 5.1",
        "packages": {"yt-dlp": "2026.8.19", "yt-dlp-ejs": "0.8.0"},
        "runtimes": ["Node"],
        **overrides,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"voice_error": "Missing native API DAVE_PROTOCOL_VERSION"},
        {"runtimes": []},
        {"ffmpeg": "missing"},
    ],
)
async def test_audio_inspects_native_apis_and_runtime_without_connecting(hub_runtime, overrides):
    bot, hub, member, invoke = hub_runtime
    bot._connection._intents.voice_states = True
    ctx = await invoke("!settings")
    voice = make_channel(member.guild, 901, kind=discord.VoiceChannel)
    audio = bot.get_cog("AudioPlus")
    audio.diagnostic_report = AsyncMock(return_value=native_report(**overrides))
    audio._connect_voice = AsyncMock()
    report = await hub._feature_readiness(ctx, "playback", voice=voice)
    assert report.ready == (not overrides)
    audio._connect_voice.assert_not_awaited()
    assert not audio._players


async def test_full_voice_channel_fails_without_capacity_bypass(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    bot._connection._intents.voice_states = True
    ctx = await invoke("!settings")
    voice = make_channel(member.guild, 901, kind=discord.VoiceChannel)
    voice.user_limit = 1
    voice.members = [member]
    voice.permissions_for.return_value = discord.Permissions(
        view_channel=True, connect=True, speak=True
    )
    bot.get_cog("AudioPlus").diagnostic_report = AsyncMock(return_value=native_report())
    report = await hub._feature_readiness(ctx, "playback", voice=voice)
    assert not report.ready and "Channel capacity" in report.text()


async def test_logging_checks_configured_route_destinations(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    good = make_channel(member.guild, 901)
    bad = make_channel(member.guild, 902)
    bad.permissions_for.return_value = discord.Permissions.none()
    log = bot.get_cog("LogPlus")
    await log.config.guild(member.guild).log_channel.set(good.id)
    await log.config.guild(member.guild).features.routes.set({"voice": bad.id})
    report = await hub._feature_readiness(ctx, "logging")
    assert not report.ready and bad.mention in report.text()


async def test_transformations_check_scope_and_webhook_permissions(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    bot._connection._intents.message_content = True
    ctx = await invoke("!settings")
    channel = make_channel(member.guild, 901)
    cog = bot.get_cog("OwoPlus")
    report = await hub._feature_readiness(ctx, "transformations", channel=channel)
    assert report.ready
    await cog.config.guild(member.guild).features.excluded.set([channel.id])
    report = await hub._feature_readiness(ctx, "transformations", channel=channel)
    assert not report.ready and "Transformation scope" in report.text()
    await cog.config.guild(member.guild).features.excluded.set([])
    channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, send_messages=True
    )
    report = await hub._feature_readiness(ctx, "transformations", channel=channel)
    assert not report.ready and "Manage Webhooks" in report.text()


async def test_cross_server_candidates_are_rejected(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    bot._connection._intents.members = True
    ctx = await invoke("!settings")
    other = make_guild(456)
    channel = make_channel(other)
    assert not (await hub._feature_readiness(ctx, "welcome", channel=channel)).ready
    role = member_role(other)
    assert not (await hub._feature_readiness(ctx, "autorole", role=role)).ready


async def test_ready_preserves_source_permissions_and_disabled_state(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    bot.get_command("community setup").disable_in(member.guild)
    with pytest.raises(commands.DisabledCommand):
        await hub._feature_readiness(ctx, "welcome")


async def test_slash_readiness_converts_candidate_role_and_omitted_options(
    hub_runtime, monkeypatch
):
    bot, hub, member, invoke = hub_runtime
    bot._connection._intents.members = True
    role = member_role(member.guild)
    ctx = await invoke_slash(
        bot,
        invoke,
        monkeypatch,
        "settings ready",
        feature="autorole",
        role=role,
    )
    assert not ctx.command_failed
    assert "Role hierarchy" in ctx.send.call_args.kwargs["embed"].description
    assert not await hub._audit_records(member.guild.id)


async def test_all_features_report_missing_setup_without_mutating_policy(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    bot.get_cog("AudioPlus").diagnostic_report = AsyncMock(return_value=native_report())
    before = {
        name: await cog.config.guild(member.guild).all() for name, cog in hub._loaded().items()
    }
    ctx = await invoke("!settings ready")
    assert not ctx.command_failed
    text = "\n".join(call.kwargs["embed"].description for call in ctx.send.call_args_list)
    assert "Temporary voice rooms" in text and "Music playback" in text
    assert "Needs attention" in text
    after = {
        name: await cog.config.guild(member.guild).all() for name, cog in hub._loaded().items()
    }
    assert before == after


async def test_slash_readiness_converts_candidate_text_channel(hub_runtime, monkeypatch):
    bot, hub, member, invoke = hub_runtime
    bot._connection._intents.members = True
    channel = make_channel(member.guild, 902)
    option = SimpleNamespace(resolve=lambda: channel)
    ctx = await invoke_slash(
        bot, invoke, monkeypatch, "settings ready", feature="welcome", channel=option
    )
    assert not ctx.command_failed
    assert channel.mention in ctx.send.call_args.kwargs["embed"].description
