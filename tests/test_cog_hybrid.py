"""Exercise renamed groups, shortcuts, and slash checks with Red's real runtime."""

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import make_channel, make_member
from discord.app_commands.commands import validate_name
from redbot.core import commands
from redbot.core._cli import parse_cli_flags
from redbot.core._events import init_events
from test_audio_hybrid import red_command_runtime as audio_command_fixture

from communityplus import CommunityPlus
from communityplus.command_support import check_command
from levelplus import LevelPlus
from logplus import LogPlus
from logplus.cog import EVENT_SWITCHES
from owoplus import OwoPlus

red_command_runtime = audio_command_fixture
COGS = (CommunityPlus, LevelPlus, LogPlus, OwoPlus)
SHORTCUTS = {
    "rank": "level show",
    "leaderboard": "level leaderboard",
    "levellookup": "level lookup",
    "seen": "community seen",
    "seendetail": "community seendetail",
    "activity": "community stats",
    "seenlist": "community seenlist",
    "logstatus": "log",
    "logchannel": "log channel",
    "lograte": "log rate",
}


@pytest.fixture
async def command_runtime(red_command_runtime, monkeypatch):
    bot, audio, member, invoke = red_command_runtime
    # Red installs this handler at startup; retain it for permission/error behavior.
    init_events(bot, parse_cli_flags([]))
    monkeypatch.setattr(bot, "_delete_delay", AsyncMock())
    loaded = []
    try:
        for cls in COGS:
            cog = cls(bot)
            await bot.add_cog(cog)
            loaded.append(cog)
        yield bot, loaded, member, invoke
    finally:
        for cog in reversed(loaded):
            await bot.remove_cog(cog.qualified_name)


async def invoke_slash(bot, invoke, monkeypatch, path, **options):
    """Run the actual hybrid prepare/convert/check/hook/error/callback pipeline."""
    ctx = await invoke("!help")
    command = bot.get_command(path)
    if command is None and path.endswith(" status"):
        command = bot.get_command(path.removesuffix(" status"))
        app = command.app_command.get_command("status")
    else:
        app = command.app_command
    ctx.command = command
    ctx.command_failed = False
    ctx.prefix = "/"
    ctx.send.reset_mock()
    done = []
    ctx.defer = AsyncMock(side_effect=lambda: done.append(True))
    interaction = SimpleNamespace(
        client=bot,
        command=app,
        namespace=SimpleNamespace(**options),
        response=SimpleNamespace(is_done=lambda: bool(done)),
    )
    ctx.interaction = interaction
    with monkeypatch.context() as patch:
        patch.setattr(bot, "get_context", AsyncMock(return_value=ctx))
        await asyncio.wait_for(app._invoke_with_namespace(interaction, interaction.namespace), 2)
    return ctx


async def test_all_cogs_register_with_core_and_serialize_slash_payloads(command_runtime):
    bot, loaded, member, invoke = command_runtime
    assert bot.get_command("leave").cog.qualified_name == "Core"
    assert bot.get_command("com") is None
    assert bot.get_command("logplus") is None
    assert bot.get_command("owoplus") is None
    assert bot.get_command("stats") is bot.get_command("activity")
    assert bot.get_command("lb") is bot.get_command("leaderboard")
    for cog in loaded:
        for command in cog.walk_commands():
            assert "plus" not in command.name
            assert all("plus" not in alias for alias in command.aliases)
    assert set(SHORTCUTS) <= set(bot.tree._disabled_global_commands)
    all_roots = {**bot.tree._global_commands, **bot.tree._disabled_global_commands}
    assert len(all_roots) <= 100
    counts = {}
    for app in all_roots.values():
        if isinstance(app, discord.app_commands.Group):
            for leaf in app.walk_commands():
                if isinstance(leaf, discord.app_commands.Command) and leaf.binding:
                    cog_name = leaf.binding.qualified_name
                    counts[cog_name] = counts.get(cog_name, 0) + 1
        elif app.binding:
            cog_name = app.binding.qualified_name
            counts[cog_name] = counts.get(cog_name, 0) + 1
    assert {
        key: counts.get(key, 0)
        for key in (
            "AudioPlus",
            "CommunityPlus",
            "LevelPlus",
            "LogPlus",
            "OwoPlus",
        )
    } == {"AudioPlus": 36, "CommunityPlus": 51, "LevelPlus": 59, "LogPlus": 23, "OwoPlus": 33}

    def check_options(payload, depth=0):
        # Discord.py does not validate unrenamed callback parameter names at registration.
        # Inspect the final upload payload, including scalar options and localizations.
        validate_name(payload["name"])
        for name in payload.get("name_localizations", {}).values():
            validate_name(name)
        assert len(payload.get("options", [])) <= 25
        assert 1 <= len(payload["description"]) <= 100
        for option in payload.get("options", []):
            if option["type"] in (1, 2):
                assert depth < 2
            check_options(option, depth + 1)

    endpoints = []
    for name, app in bot.tree._disabled_global_commands.items():
        check_options(app.to_dict(bot.tree))
        if name in {"community", "level", "log", "owo", *SHORTCUTS}:
            assert app.guild_only
            endpoints.extend(
                child.qualified_name
                for child in app.walk_commands()
                if isinstance(child, discord.app_commands.Command)
            ) if isinstance(app, discord.app_commands.Group) else endpoints.append(name)
    assert len(endpoints) >= 90
    for root in ("community", "level", "log", "owo"):
        assert bot.get_command(root).app_command.get_command("status")
    for path in (
        "community sticky ignore add",
        "level formula linear base",
        "level restrict nochannels add",
        "level restrict noroles add",
        "log toggle voice join",
        "community restore",
        "community invites",
        "level xp setid",
        "level xp removeid",
        "level name setid",
        "level name get",
    ):
        command = bot.get_command(path)
        assert command is not None
        assert not getattr(command, "app_command", None)


@pytest.mark.parametrize("slash", [False, True])
@pytest.mark.parametrize("owner", [False, True])
async def test_formula_calibration_accepts_lowercase_slash_options_and_keeps_prefix_checks(
    command_runtime, monkeypatch, slash, owner
):
    bot, loaded, member, invoke = command_runtime
    if owner:
        bot.owner_ids.add(member.id)
    group = bot.get_cog("LevelPlus").config.guild(member.guild)
    before = await group.all()
    if slash:
        ctx = await invoke_slash(
            bot,
            invoke,
            monkeypatch,
            "level formula calibrate",
            level1=1,
            xp1=100,
            level2=2,
            xp2=250,
        )
        assert ctx.defer.await_count == int(owner)
    else:
        ctx = await invoke("!level formula calibrate 1 100 2 250")
    assert ctx.command_failed is (not owner)
    if owner:
        assert await group.linear() == {"base": 100.0, "inc": 50.0}
        assert await group.curve() == "linear"
        assert await group.multiplier() == before["multiplier"]
        assert "Calibrated linear curve" in ctx.send.await_args.kwargs["embed"].description
    else:
        assert await group.all() == before


@pytest.mark.parametrize("path", ["rank", "leaderboard", "level status", "level show"])
async def test_regular_members_can_use_member_slash_commands(command_runtime, monkeypatch, path):
    bot, loaded, member, invoke = command_runtime
    ctx = await invoke_slash(bot, invoke, monkeypatch, path)
    assert not ctx.command_failed
    ctx.defer.assert_awaited_once()
    assert ctx.send.await_count >= 1


async def test_slash_member_and_channel_options_convert_and_select_the_target(
    command_runtime, monkeypatch
):
    bot, loaded, member, invoke = command_runtime
    target = make_member(member.guild, user_id=223456789012345678, name="Another member")
    level = bot.get_cog("LevelPlus")
    await level.config.guild(member.guild).xp.set({str(target.id): 123})
    ctx = await invoke_slash(bot, invoke, monkeypatch, "rank", member=target)
    assert not ctx.command_failed
    output = ctx.send.await_args.kwargs["embed"]
    assert output.description == target.mention
    assert next(field.value for field in output.fields if field.name == "Total XP") == "123"
    bot.owner_ids.add(member.id)
    channel = make_channel(member.guild, channel_id=323456789012345678)
    ctx = await invoke_slash(
        bot, invoke, monkeypatch, "logchannel", channel=SimpleNamespace(resolve=lambda: channel)
    )
    assert not ctx.command_failed
    assert await bot.get_cog("LogPlus").config.guild(member.guild).log_channel() == channel.id
    ctx = await invoke("!logchannel")
    assert not ctx.command_failed
    assert channel.mention in ctx.send.await_args.kwargs["embed"].description


@pytest.mark.parametrize("owner", [False, True])
async def test_direct_rank_and_leaderboard_parse_for_regular_members(command_runtime, owner):
    bot, loaded, member, invoke = command_runtime
    if owner:
        bot.owner_ids.add(member.id)
    level = bot.get_cog("LevelPlus")
    await level.config.guild(member.guild).xp.set({str(member.id): 42})
    for text in ("!rank", "!level show", "!leaderboard 5", "!lb 5", "!levellookup Kevin"):
        ctx = await invoke(text)
        assert ctx.valid and not ctx.command_failed
        assert ctx.send.await_count >= 1
        assert ctx.command.cog is level
    output = (await invoke("!rank")).send.await_args.kwargs["embed"]
    assert next(field.value for field in output.fields if field.name == "Total XP") == "42"


@pytest.mark.parametrize("slash", [False, True])
async def test_feature_member_commands_and_admin_settings_keep_permissions(
    command_runtime, monkeypatch, slash
):
    bot, loaded, member, invoke = command_runtime
    if slash:
        ctx = await invoke_slash(bot, invoke, monkeypatch, "owooptout", enabled=True)
    else:
        ctx = await invoke("!owooptout true")
    assert not ctx.command_failed
    owo = bot.get_cog("OwoPlus")
    assert (await owo.config.guild(member.guild).features.optouts())[str(member.id)]
    if slash:
        ctx = await invoke_slash(bot, invoke, monkeypatch, "roles")
    else:
        ctx = await invoke("!roles")
    assert not ctx.command_failed
    cases = [
        ("community tracking", {"enabled": False}, "!community tracking false"),
        ("level guard minwords", {"count": 2}, "!level guard minwords 2"),
        ("log delivery", {"retry": False}, "!log delivery false"),
        ("owo intensity", {"value": 2}, "!owo intensity 2"),
    ]
    for path, options, text in cases:
        ctx = (
            await invoke_slash(bot, invoke, monkeypatch, path, **options)
            if slash
            else await invoke(text)
        )
        assert ctx.command_failed, path
    assert await bot.get_cog("CommunityPlus").config.guild(member.guild).seen.enabled()
    assert await bot.get_cog("LevelPlus").config.guild(member.guild).xp_features.min_words() == 0
    assert await bot.get_cog("LogPlus").config.guild(member.guild).features.retry()
    assert await owo.config.guild(member.guild).features.intensity() == 0
    bot.owner_ids.add(member.id)
    for path, options, text in cases:
        ctx = (
            await invoke_slash(bot, invoke, monkeypatch, path, **options)
            if slash
            else await invoke(text)
        )
        assert not ctx.command_failed, path
    assert not await bot.get_cog("CommunityPlus").config.guild(member.guild).seen.enabled()
    assert await bot.get_cog("LevelPlus").config.guild(member.guild).xp_features.min_words() == 2
    assert not await bot.get_cog("LogPlus").config.guild(member.guild).features.retry()
    assert await owo.config.guild(member.guild).features.intensity() == 2


@pytest.mark.parametrize("permission", ["none", "manage_guild", "owner"])
async def test_admin_shortcuts_and_renamed_groups_keep_permissions(command_runtime, permission):
    bot, loaded, member, invoke = command_runtime
    if permission == "owner":
        bot.owner_ids.add(member.id)
    elif permission == "manage_guild":
        ctx = await invoke("!help")
        ctx.channel.permissions_for.side_effect = lambda target: (
            discord.Permissions.all()
            if target is member.guild.me
            else discord.Permissions(manage_guild=True)
        )
    for text in (
        "!community",
        "!community welcome disable",
        "!seen",
        "!seendetail",
        "!activity",
        "!stats",
        "!seenlist 10",
        "!log",
        "!logstatus",
        "!logchannel",
        "!lograte 0.5",
        "!owo enable",
    ):
        ctx = await invoke(text)
        assert ctx.valid
        assert ctx.command_failed is (permission == "none"), text
        if permission != "none":
            assert ctx.send.await_count >= 1, text
    log = bot.get_cog("LogPlus")
    assert await log.config.guild(member.guild).rate.seconds() == (
        2.0 if permission == "none" else 0.5
    )


@pytest.mark.parametrize(
    "path",
    [
        "community welcome disable",
        "level message enable",
        "log clearchannel",
        "owo enable",
        "seen",
        "activity",
        "lograte",
    ],
)
async def test_slash_admin_paths_reject_ordinary_members(command_runtime, monkeypatch, path):
    bot, loaded, member, invoke = command_runtime
    ctx = await invoke_slash(bot, invoke, monkeypatch, path)
    assert ctx.command_failed
    ctx.defer.assert_not_awaited()
    assert await bot.get_cog("CommunityPlus").config.guild(member.guild).welcome.enabled()
    assert await bot.get_cog("OwoPlus").config.guild(member.guild).enabled() is False


@pytest.mark.parametrize(
    "path,options",
    [
        ("rank", {}),
        ("leaderboard", {"top": 5}),
        ("levellookup", {"query": "Kevin"}),
        ("seen", {}),
        ("activity", {}),
        ("seenlist", {"limit": 10}),
        ("community welcome disable", {}),
        ("level message enable", {"enabled": False}),
        ("lograte", {"seconds": 0.25}),
        ("log event", {"event": "sched.create", "enabled": False}),
        ("owo preview", {"text": "hello there"}),
    ],
)
async def test_slash_options_use_existing_callbacks_and_defer(
    command_runtime, monkeypatch, path, options
):
    bot, loaded, member, invoke = command_runtime
    bot.owner_ids.add(member.id)
    ctx = await invoke_slash(bot, invoke, monkeypatch, path, **options)
    assert not ctx.command_failed, path
    ctx.defer.assert_awaited_once()
    assert ctx.send.await_count >= 1
    if path == "community welcome disable":
        assert (
            await bot.get_cog("CommunityPlus").config.guild(member.guild).welcome.enabled() is False
        )
    elif path == "level message enable":
        assert await bot.get_cog("LevelPlus").config.guild(member.guild).message.enabled() is False
    elif path == "lograte":
        assert await bot.get_cog("LogPlus").config.guild(member.guild).rate.seconds() == 0.25
    elif path == "log event":
        assert await bot.get_cog("LogPlus").config.guild(member.guild).sched.create() is False


@pytest.mark.parametrize("target", ["level", "level show"])
@pytest.mark.parametrize("slash", [False, True])
async def test_shortcuts_honor_disabled_group_or_original_command(
    command_runtime, monkeypatch, target, slash
):
    bot, loaded, member, invoke = command_runtime
    getter = bot.get_cog("LevelPlus")._get_xp = AsyncMock(return_value=42)
    bot.get_command(target).disable_in(member.guild)
    if slash:
        ctx = await invoke_slash(bot, invoke, monkeypatch, "rank")
    else:
        ctx = await invoke("!rank")
    assert ctx.command_failed
    getter.assert_not_awaited()
    assert ctx.command is bot.get_command("rank")


@pytest.mark.parametrize("target", ["level", "level show", "LevelPlus"])
@pytest.mark.parametrize("path", ["rank", "level show"])
@pytest.mark.parametrize("slash", [False, True])
async def test_saved_red_denials_apply_to_original_and_shortcut_paths(
    command_runtime, monkeypatch, target, path, slash
):
    bot, loaded, member, invoke = command_runtime
    entry = bot.get_cog(target) or bot.get_command(target)
    entry.deny_to(member.id, member.guild.id)
    getter = bot.get_cog("LevelPlus")._get_xp = AsyncMock(return_value=42)
    ctx = await invoke_slash(bot, invoke, monkeypatch, path) if slash else await invoke(f"!{path}")
    assert ctx.command_failed
    getter.assert_not_awaited()
    command = bot.get_command(path)
    if slash or path == "rank":
        assert ctx.command is command
    else:
        assert ctx.command in [command, *command.parents]


@pytest.mark.parametrize("root", ["community", "level", "log", "owo"])
async def test_group_checks_reject_dms_and_restore_context(command_runtime, root):
    bot, loaded, member, invoke = command_runtime
    ctx = await invoke("!rank")
    original_command, original_state = ctx.command, ctx.permission_state
    original_interaction = ctx.interaction = SimpleNamespace()
    ctx.message.guild = None
    ctx.guild = None
    with pytest.raises(commands.NoPrivateMessage):
        await check_command(ctx, bot.get_command(root))
    assert ctx.command is original_command
    assert ctx.permission_state is original_state
    assert ctx.interaction is original_interaction


@pytest.mark.parametrize("path", ["community status", "log status", "owo status"])
async def test_administrator_slash_status_panels(command_runtime, monkeypatch, path):
    bot, loaded, member, invoke = command_runtime
    bot.owner_ids.add(member.id)
    ctx = await invoke_slash(bot, invoke, monkeypatch, path)
    assert not ctx.command_failed
    ctx.defer.assert_awaited_once()
    assert ctx.send.await_count >= 1


@pytest.mark.parametrize(
    "text",
    [
        "!level formula linear base 100",
        "!level restrict nochannels list",
        "!level restrict noroles list",
        "!community sticky ignore list",
        "!log toggle voice join",
    ],
)
async def test_deeper_prefix_branches_still_run(command_runtime, text):
    bot, loaded, member, invoke = command_runtime
    bot.owner_ids.add(member.id)
    ctx = await invoke(text)
    assert not ctx.command_failed
    assert ctx.send.await_count >= 1


async def test_log_event_inspection_validation_and_autocomplete(command_runtime):
    bot, loaded, member, invoke = command_runtime
    bot.owner_ids.add(member.id)
    cog = bot.get_cog("LogPlus")
    before = await cog.config.guild(member.guild).all()
    ctx = await invoke("!log event voice.join")
    assert not ctx.command_failed
    assert await cog.config.guild(member.guild).all() == before
    ctx = await invoke("!log event unknown.key false")
    assert not ctx.command_failed
    assert "Unknown event" in ctx.send.await_args.kwargs["embed"].description
    assert await cog.config.guild(member.guild).all() == before
    ctx = await invoke("!log event voice.join false")
    assert not ctx.command_failed
    assert await cog.config.guild(member.guild).voice.join() is False
    assert len(await cog.event_autocomplete(None, "")) == 25
    choices = await cog.event_autocomplete(None, "sched.")
    assert {choice.value for choice in choices} == {
        key for key in EVENT_SWITCHES if key.startswith("sched.")
    }


async def test_log_event_setter_shares_locks_with_existing_toggles(command_runtime):
    bot, loaded, member, invoke = command_runtime
    bot.owner_ids.add(member.id)
    ctx = await invoke("!logstatus")
    cog = bot.get_cog("LogPlus")
    await asyncio.gather(
        *(cog.event.callback(cog, ctx, event="message.delete", enabled=False) for _ in range(20)),
        *(cog._flip(ctx, "message", "edit") for _ in range(20)),
    )
    settings = await cog.config.guild(member.guild).message()
    assert settings["delete"] is False
    assert settings["edit"] is True


async def test_removing_and_reloading_cogs_replaces_all_commands(command_runtime):
    bot, loaded, member, invoke = command_runtime
    original_leave = bot.get_command("leave")
    for cog in loaded:
        roots = {command.name for command in cog.get_commands()}
        await bot.remove_cog(cog.qualified_name)
        assert not roots.intersection(bot.tree._disabled_global_commands)
        assert all(bot.get_command(name) is None for name in roots)
        replacement = type(cog)(bot)
        await bot.add_cog(replacement)
        assert all(bot.get_command(name).cog is replacement for name in roots)
    bot.owner_ids.add(member.id)
    for text in ("!rank", "!activity", "!logstatus", "!owo"):
        assert not (await invoke(text)).command_failed
    assert bot.get_command("leave") is original_leave


def test_permission_helpers_are_vendored_identically():
    sources = [
        Path(
            importlib.import_module(f"{cls.__module__.split('.')[0]}.command_support").__file__
        ).read_text()
        for cls in COGS
    ]
    assert len(set(sources)) == 1
