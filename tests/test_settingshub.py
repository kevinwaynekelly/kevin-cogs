"""Shared dashboard, strict backups, current checks, and transactional rollback."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import make_context, make_guild, make_member, make_message
from discord.app_commands.commands import validate_name
from redbot.core import commands
from redbot.core.config import Value
from test_audio_hybrid import red_command_runtime as audio_fixture
from test_cog_hybrid import command_runtime as cog_fixture
from test_cog_hybrid import invoke_slash

from settingshub import SettingsHub
from settingshub.cog import DashboardView, RestoreView
from settingshub.schema import parse_backup

command_runtime = cog_fixture
red_command_runtime = audio_fixture


@pytest.fixture
async def hub_runtime(command_runtime):
    bot, loaded, member, invoke = command_runtime
    hub = SettingsHub(bot)
    await bot.add_cog(hub)
    bot.owner_ids.add(member.id)
    try:
        yield bot, hub, member, invoke
    finally:
        await bot.remove_cog("SettingsHub")


async def test_all_six_cogs_fit_slash_limits_and_validate_every_option(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    roots = {**bot.tree._global_commands, **bot.tree._disabled_global_commands}
    assert len(roots) <= 100

    def check(payload, depth=0):
        validate_name(payload["name"])
        assert 1 <= len(payload["description"]) <= 100
        options = payload.get("options", [])
        assert len(options) <= 25
        for option in options:
            if option["type"] in (1, 2):
                assert depth < 2
            check(option, depth + 1)

    for root in roots.values():
        check(root.to_dict(bot.tree))


async def test_backups_exclude_personal_data_and_restore_preserves_records(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    level = bot.get_cog("LevelPlus")
    owo = bot.get_cog("OwoPlus")
    community = bot.get_cog("CommunityPlus")
    log = bot.get_cog("LogPlus")
    await level.config.guild(member.guild).xp.set({str(member.id): 123})
    await level.config.guild(member.guild).milestones.set(
        {str(member.id): {"badges": {"first": 1}}}
    )
    await owo.config.guild(member.guild).features.optouts.set({str(member.id): True})
    await community.config.guild(member.guild).features.role_menus.set(
        [{"channel": 123, "message": 456}]
    )
    await community.config.guild(member.guild).features.summary.last_week.set("keep")
    await log.config.guild(member.guild).history_records.set([])
    bundle = await hub._backup_bundle(ctx)
    raw = json.dumps(bundle).encode()
    assert str(member.id).encode() not in raw
    for key in (
        "milestones",
        "optouts",
        "role_menus",
        "last_week",
        "playlists",
        "favorites",
        "password",
        "recipient",
        "history_records",
        "social",
        "boosts",
    ):
        assert ('"' + key + '"').encode() not in raw
    assert "xp" not in bundle["cogs"]["LevelPlus"]
    community._refresh_role_menus = AsyncMock()
    bundle["cogs"]["OwoPlus"]["enabled"] = True
    bundle["cogs"]["LevelPlus"]["message"]["enabled"] = False
    bundle["cogs"]["CommunityPlus"]["vcsolo"]["enabled"] = False
    selected = await hub._validate_bundle(ctx, parse_backup(json.dumps(bundle).encode()))
    await hub._apply_bundle(ctx, bundle, selected)
    assert await owo.config.guild(member.guild).enabled()
    assert not await level.config.guild(member.guild).message.enabled()
    assert await level.config.guild(member.guild).xp() == {str(member.id): 123}
    assert await owo.config.guild(member.guild).features.optouts() == {str(member.id): True}
    assert await community.config.guild(member.guild).features.summary.last_week() == "keep"
    assert await community.config.guild(member.guild).features.role_menus() == [
        {"channel": 123, "message": 456}
    ]


async def test_map_restore_replaces_dictionary_and_keeps_omitted_members(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    bundle = await hub._backup_bundle(ctx)
    bundle["cogs"] = {"OwoPlus": bundle["cogs"]["OwoPlus"]}
    cog = bot.get_cog("OwoPlus")
    await cog.config.guild(member.guild).features.words.set({"hello": "old"})
    selected = await hub._validate_bundle(ctx, bundle)
    await hub._apply_bundle(ctx, bundle, selected)
    assert not await cog.config.guild(member.guild).features.words()


async def test_custom_styles_restore_without_poetry_or_undo_records(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    await invoke('!customstyle create space {"hello":"greetings"}')
    ctx = await invoke("!settings")
    bundle = await hub._backup_bundle(ctx)
    bundle["cogs"] = {"OwoPlus": bundle["cogs"]["OwoPlus"]}
    assert "poetry" not in bundle["cogs"]["OwoPlus"]
    assert bundle["cogs"]["OwoPlus"]["features"]["custom_styles"]["space"]["words"] == {
        "hello": "greetings"
    }
    selected = await hub._validate_bundle(ctx, bundle)
    await hub._apply_bundle(ctx, bundle, selected)
    invalid = deepcopy(bundle)
    invalid["cogs"]["OwoPlus"]["features"]["channel_styles"] = {
        str(ctx.channel.id): {"style": "unknown", "expires": 0}
    }
    with pytest.raises(commands.BadArgument):
        await hub._validate_bundle(ctx, invalid)


async def test_reward_role_backup_uses_role_id_to_level_mapping(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    guild = member.guild
    role = discord.Role(
        guild=guild,
        state=Mock(),
        data={"id": "12345678901234567", "name": "Level reward", "permissions": "0", "position": 1},
    )
    guild.me.top_role = discord.Role(
        guild=guild,
        state=Mock(),
        data={"id": "999", "name": "Scarlet", "permissions": "0", "position": 100},
    )
    guild.get_role.side_effect = lambda rid: role if rid == role.id else None
    level = bot.get_cog("LevelPlus")
    await level.config.guild(guild).rewards.roles.set({str(role.id): 5})
    ctx = await invoke("!settings")
    bundle = await hub._backup_bundle(ctx)
    bundle["cogs"] = {"LevelPlus": bundle["cogs"]["LevelPlus"]}
    selected = await hub._validate_bundle(ctx, bundle)
    await level.config.guild(guild).rewards.roles.set({})
    await hub._apply_bundle(ctx, bundle, selected)
    assert await level.config.guild(guild).rewards.roles() == {str(role.id): 5}
    bundle["cogs"]["LevelPlus"]["rewards"]["roles"][str(role.id)] = 100001
    with pytest.raises(commands.BadArgument):
        await hub._validate_bundle(ctx, bundle)


async def test_restore_refreshes_live_audio_normalization_policy(hub_runtime):
    import asyncio

    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    bundle = await hub._backup_bundle(ctx)
    bundle["cogs"] = {"AudioPlus": bundle["cogs"]["AudioPlus"]}
    bundle["cogs"]["AudioPlus"]["continuity"]["normalize"] = True
    selected = await hub._validate_bundle(ctx, bundle)
    cog = bot.get_cog("AudioPlus")
    player = SimpleNamespace(
        lock=asyncio.Lock(), normalize=False, _autoplay_generation=0, _balance_queue=Mock()
    )
    cog._get_player = lambda guild: player
    await hub._apply_bundle(ctx, bundle, selected)
    assert player.normalize


@pytest.mark.parametrize(
    "raw",
    [b'{"schema":1,"schema":1}', b'{"schema":NaN}', b"[]", b"{}", b"x" * (256 * 1024 + 1), b"\xff"],
)
def test_backup_parser_rejects_invalid_or_unbounded_files(raw):
    with pytest.raises(commands.BadArgument):
        parse_backup(raw)


@pytest.mark.parametrize(
    "mutation",
    ["server", "unknown", "type", "nan", "range", "role", "channel", "word", "extra_map"],
)
async def test_invalid_restore_never_mutates_configuration(hub_runtime, mutation):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    bundle = await hub._backup_bundle(ctx)
    values = bundle["cogs"]
    if mutation == "server":
        bundle["guild_id"] += 1
    elif mutation == "unknown":
        values["AudioPlus"]["password"] = "secret"
    elif mutation == "type":
        values["OwoPlus"]["enabled"] = 1
    elif mutation == "nan":
        values["LevelPlus"]["multiplier"] = float("nan")
    elif mutation == "range":
        values["LogPlus"]["history_settings"]["days"] = 999
    elif mutation == "role":
        values["AudioPlus"]["music"]["dj_role"] = 123
        member.guild.get_role.return_value = None
    elif mutation == "channel":
        values["LogPlus"]["log_channel"] = 789
    elif mutation == "word":
        values["OwoPlus"]["features"]["words"] = {"bad word": "bad"}
    else:
        values["OwoPlus"]["features"]["syllables"] = {"hello": {"unexpected": True}}
    with pytest.raises(commands.BadArgument):
        await hub._validate_bundle(ctx, bundle)
    assert not await bot.get_cog("OwoPlus").config.guild(member.guild).enabled()


async def test_failed_restore_rolls_back_applied_cogs(hub_runtime, monkeypatch):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    original = await hub._backup_bundle(ctx)
    bundle = deepcopy(original)
    bundle["cogs"]["AudioPlus"]["music"]["autoplay"] = True
    bundle["cogs"]["OwoPlus"]["enabled"] = True
    selected = await hub._validate_bundle(ctx, bundle)
    setter = Value.set
    calls = 0

    async def fail_once(self, value):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("Storage unavailable")
        return await setter(self, value)

    monkeypatch.setattr(Value, "set", fail_once)
    with pytest.raises(OSError):
        await hub._apply_bundle(ctx, bundle, selected)
    assert await hub._backup_bundle(ctx) == original


async def test_prefix_and_slash_permissions_and_attachment_preview(hub_runtime, monkeypatch):
    bot, hub, member, invoke = hub_runtime
    bot.owner_ids.discard(member.id)
    ctx = await invoke("!settings health")
    assert ctx.command_failed
    ctx = await invoke_slash(bot, invoke, monkeypatch, "settings health")
    assert ctx.command_failed
    ctx.defer.assert_not_awaited()
    bot.owner_ids.add(member.id)
    ctx = await invoke("!settings backup")
    file = ctx.send.await_args.kwargs["file"]
    raw = file.fp.getvalue()
    attachment = SimpleNamespace(size=len(raw), read=AsyncMock(return_value=raw))
    ctx = await invoke("!settings")
    await hub.restore.callback(hub, ctx, file=attachment)
    assert "Ready to restore" in ctx.send.await_args.kwargs["embed"].description
    view = ctx.send.await_args.kwargs["view"]
    assert isinstance(view, RestoreView) and not view.applied
    bot.get_cog("OwoPlus").owoplus.enabled = False
    with pytest.raises(commands.DisabledCommand):
        await hub._validate_bundle(ctx, parse_backup(raw))


async def test_dashboard_rejects_other_server_requester_reload_and_erases_previews(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    view = ctx.send.await_args.kwargs["view"]
    message = make_message(make_member(member.guild, 444), ctx.channel)
    message._state = bot._connection
    interaction = SimpleNamespace(
        guild=member.guild,
        user=member,
        message=message,
        response=SimpleNamespace(is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    selector = view.children[0]
    selector._values = ["OwoPlus"]
    interaction.guild = make_guild(member.guild.id + 1)
    await selector.callback(interaction)
    assert "another server" in interaction.followup.send.await_args.args[0]
    interaction.guild = member.guild
    interaction.user = message.author
    await selector.callback(interaction)
    assert "another member" in interaction.followup.send.await_args.args[0]
    interaction.user = member
    await bot.remove_cog("OwoPlus")
    await selector.callback(interaction)
    assert "unloaded" in interaction.followup.send.await_args.args[0]
    await hub.red_delete_data_for_user(requester="user", user_id=member.id)
    assert not hub._views
    assert await hub.red_get_data_for_user(user_id=member.id) == {}


async def test_optional_hub_registers_without_other_cogs_and_cleans_views(bot, guild):
    hub = SettingsHub(bot)
    bot.get_cog = lambda name: None
    ctx = make_context(guild, author=make_member(guild))
    await hub.settings.callback(hub, ctx)
    assert "Load AudioPlus" in ctx.send.await_args.kwargs["embed"].description
    view = DashboardView(hub, ctx, ["AudioPlus"])
    await hub.cog_unload()
    assert view.is_finished() and not hub._views


async def test_hub_slash_payload_and_independent_helpers(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    app = bot.tree._disabled_global_commands["settings"]
    assert len(app.commands) == 7
    assert {command.name for command in app.commands} == {
        "panel",
        "health",
        "backup",
        "restore",
        "diagnostics",
        "history",
        "ready",
    }
    from pathlib import Path

    for file in ("presentation.py", "command_support.py", "interactive.py"):
        assert Path("settingshub", file).read_text() == Path("audioplus", file).read_text()


async def test_restore_button_checks_permissions_again_and_applies_only_once(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    bundle = await hub._backup_bundle(ctx)
    bundle["cogs"] = {"OwoPlus": bundle["cogs"]["OwoPlus"]}
    bundle["cogs"]["OwoPlus"]["enabled"] = True
    selected = await hub._validate_bundle(ctx, bundle)
    view = RestoreView(hub, ctx, bundle, selected)
    message = make_message(member, ctx.channel)
    message._state = bot._connection
    interaction = SimpleNamespace(
        guild=member.guild,
        user=member,
        message=message,
        response=SimpleNamespace(is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    button = view.children[0]
    bot.owner_ids.discard(member.id)
    await button.callback(interaction)
    assert not view.applied and not await selected["OwoPlus"].config.guild(member.guild).enabled()
    bot.owner_ids.add(member.id)
    await button.callback(interaction)
    assert view.applied and await selected["OwoPlus"].config.guild(member.guild).enabled()
    await selected["OwoPlus"].config.guild(member.guild).enabled.set(False)
    await button.callback(interaction)
    assert not await selected["OwoPlus"].config.guild(member.guild).enabled()


async def test_restore_rejects_privileged_grant_roles_but_allows_safe_current_roles(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    bundle = await hub._backup_bundle(ctx)

    def role(uid, permissions, position):
        return discord.Role(
            guild=member.guild,
            state=bot._connection,
            data={
                "id": str(uid),
                "name": "Role",
                "permissions": str(permissions),
                "position": position,
                "color": 0,
                "managed": False,
            },
        )

    member.guild.me.top_role = role(104, 0, 10)
    safe = role(101, 0, 1)
    unsafe = role(102, discord.Permissions(manage_messages=True).value, 2)
    member.guild.get_role.side_effect = {safe.id: safe, unsafe.id: unsafe}.get
    bundle["cogs"]["CommunityPlus"]["features"]["self_roles"] = [unsafe.id]
    with pytest.raises(commands.BadArgument):
        await hub._validate_bundle(ctx, bundle)
    bundle["cogs"]["CommunityPlus"]["features"]["self_roles"] = [safe.id]
    bundle["cogs"]["LevelPlus"]["rewards"]["roles"] = {str(safe.id): 10}
    assert len(await hub._validate_bundle(ctx, bundle)) == 5


async def test_dashboard_opens_original_setup_and_checks_disabled_source(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    view = ctx.send.await_args.kwargs["view"]
    message = make_message(member, ctx.channel)
    message._state = bot._connection
    interaction = SimpleNamespace(
        guild=member.guild,
        user=member,
        message=message,
        response=SimpleNamespace(is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    view.children[0]._values = ["OwoPlus"]
    cog = bot.get_cog("OwoPlus")
    await view.children[0].callback(interaction)
    assert len(cog._views) == 1
    cog.owoplus.enabled = False
    await view.children[0].callback(interaction)
    assert len(cog._views) == 1
    assert "disabled" in interaction.followup.send.await_args.args[0]


async def test_real_attachment_conversion_for_text_and_slash_restore(hub_runtime, monkeypatch):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    bundle = await hub._backup_bundle(ctx)
    raw = json.dumps(bundle).encode()
    file = Mock(spec=discord.Attachment)
    file.size = len(raw)
    file.read = AsyncMock(return_value=raw)
    message = make_message(member, ctx.channel, content="!settings restore", attachments=[file])
    message._state = bot._connection
    ctx = await bot.get_context(message)
    await bot.invoke(ctx)
    assert not ctx.command_failed
    assert isinstance(ctx.send.await_args.kwargs["view"], RestoreView)
    ctx = await invoke_slash(bot, invoke, monkeypatch, "settings restore", file=file)
    assert not ctx.command_failed
    ctx.defer.assert_awaited_once()
    assert isinstance(ctx.send.await_args.kwargs["view"], RestoreView)


async def test_failed_dashboard_send_does_not_retain_an_unposted_view(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    await hub.red_delete_data_for_user(requester="user", user_id=member.id)
    ctx.send.side_effect = OSError("Transport unavailable")
    with pytest.raises(OSError):
        await hub.settings.callback(hub, ctx)
    assert not hub._views


async def test_post_restore_refresh_failure_reports_saved_configuration(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    bundle = await hub._backup_bundle(ctx)
    community = bot.get_cog("CommunityPlus")
    community._refresh_role_menus = AsyncMock(side_effect=OSError("Transport unavailable"))
    bundle["cogs"]["OwoPlus"]["enabled"] = True
    selected = await hub._validate_bundle(ctx, bundle)
    warnings = await hub._apply_bundle(ctx, bundle, selected)
    assert warnings == ["CommunityPlus"]
    assert await bot.get_cog("OwoPlus").config.guild(member.guild).enabled()
