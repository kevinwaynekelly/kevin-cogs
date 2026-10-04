"""Exercise management overlays with real Red parsing and mocked Discord I/O."""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from redbot.core import commands
from redbot.core.commands.help import RedHelpFormatter
from test_audio_hybrid import red_command_runtime as command_fixture
from test_cog_hybrid import invoke_slash
from test_presentation import assert_limits

from coreplus import CorePlus
from coreplus.management import quoted, style_replies, words

red_command_runtime = command_fixture


def sends(ctx):
    return getattr(ctx, "_kevin_management_sender", ctx.send)


@pytest.fixture
async def core_runtime(red_command_runtime, monkeypatch):
    bot, audio, member, invoke = red_command_runtime
    bot._uptime = datetime.utcnow() - timedelta(hours=2)
    monkeypatch.setattr(bot, "on_command_error", AsyncMock())
    cog = CorePlus(bot)
    await bot.add_cog(cog)
    try:
        yield bot, cog, member, invoke
    finally:
        await bot.remove_cog("CorePlus")


async def test_core_registration_keeps_native_commands_and_restores_help(core_runtime):
    bot, cog, member, invoke = core_runtime
    native = {name: bot.get_command(name) for name in ("help", "load", "repo", "invite")}
    assert bot._help_formatter is cog._formatter
    assert bot.get_command("helpme").app_command.name == "help"
    assert bot.tree._disabled_global_commands["help"].wrapped is cog.help_menu
    assert {name: bot.get_command(name) for name in native} == native
    hook_count = len(bot._red_before_invoke_objs)
    ctx = await invoke("!help")
    assert not ctx.command_failed
    assert len(cog._views) == 1
    await bot.remove_cog("CorePlus")
    assert len(bot._red_before_invoke_objs) == hook_count - 1
    assert type(bot._help_formatter) is RedHelpFormatter
    assert not cog._views
    assert {name: bot.get_command(name) for name in native} == native
    assert "help" not in bot.tree._disabled_global_commands
    replacement = CorePlus(bot)
    await bot.add_cog(replacement)
    assert len(bot._red_before_invoke_objs) == hook_count


async def test_global_help_is_one_themed_category_card(core_runtime):
    bot, cog, member, invoke = core_runtime
    ctx = await invoke("!help")
    assert sends(ctx).await_count == 1
    card = sends(ctx).call_args.kwargs["embed"]
    assert card.title == "CorePlus · Scarlet help"
    assert "/help" in card.description
    assert {field.name for field in card.fields} >= {"CorePlus", "AudioPlus"}
    assert not {"Core", "CogManagerUI"}.intersection(field.name for field in card.fields)
    assert_limits(card)
    view = sends(ctx).call_args.kwargs["view"]
    assert isinstance(view.children[0], discord.ui.Select)
    assert view.owner_id == member.id and view.timeout == 180


@pytest.mark.parametrize("slash", [False, True])
async def test_help_details_and_aliases_use_real_pipeline(core_runtime, monkeypatch, slash):
    bot, cog, member, invoke = core_runtime
    if slash:
        ctx = await invoke_slash(bot, invoke, monkeypatch, "helpme", query="play")
        ctx.defer.assert_awaited_once()
    else:
        ctx = await invoke("!help p")
    assert not ctx.command_failed
    card = sends(ctx).call_args.kwargs["embed"]
    assert card.title == "CorePlus · play"
    assert any("!play" in field.value for field in card.fields)
    assert any("/play" in field.value for field in card.fields)
    assert any("p" in field.value for field in card.fields if field.name == "Text aliases")
    assert_limits(card)


async def test_help_checks_native_parent_and_shortcut_disables(core_runtime):
    bot, cog, member, invoke = core_runtime
    ctx = await invoke("!help")
    groups = await cog._formatter.categories(ctx)
    core_names = {command.name for command in groups["CorePlus"]}
    assert "load" not in core_names and "reload" not in core_names
    bot.owner_ids.add(member.id)
    groups = await cog._formatter.categories(ctx)
    assert "load" in {command.name for command in groups["CorePlus"]}
    bot.get_command("audio play").disable_in(member.guild)
    groups = await cog._formatter.categories(ctx)
    assert "play" not in {command.name for command in groups["AudioPlus"]}
    bot.get_command("load").disable_in(member.guild)
    groups, payloads = await cog._formatter.cards(ctx, "core")
    fields = [field.name for payload in payloads for field in payload["embed"].fields]
    assert "load" not in fields
    assert ctx.command is bot.get_command("help")


async def test_help_category_pages_do_not_cut_command_descriptions(core_runtime):
    bot, cog, member, invoke = core_runtime
    ctx = await invoke("!help AudioPlus")
    groups, payloads = await cog._formatter.cards(ctx, "AudioPlus")
    fields = [field for payload in payloads for field in payload["embed"].fields]
    assert len(payloads) > 1
    assert {field.name for field in fields} == {command.name for command in groups["AudioPlus"]}
    for command in groups["AudioPlus"]:
        field = next(field for field in fields if field.name == command.name)
        assert command.format_shortdoc_for_context(ctx) in field.value
    assert sends(ctx).await_count == 1


async def test_help_text_fallback_and_server_theme(core_runtime, monkeypatch):
    bot, cog, member, invoke = core_runtime
    bot._kevin_cogs_themes = {member.guild.id: {"colors": {"info": 0x123456}, "footer": "Scarlet"}}
    ctx = await invoke("!help")
    assert sends(ctx).call_args.kwargs["embed"].color.value == 0x123456
    assert sends(ctx).call_args.kwargs["embed"].footer.text.startswith("Scarlet")
    monkeypatch.setattr(bot, "embed_requested", AsyncMock(return_value=False))
    _, payloads = await cog._formatter.cards(ctx, "AudioPlus")
    assert all(payload["embed"] is None for payload in payloads)
    assert all(len(payload["content"].encode("utf-16-le")) // 2 <= 2000 for payload in payloads)
    assert "play" in "".join(payload["content"] for payload in payloads)


@pytest.mark.parametrize("slash", [False, True])
async def test_native_uptime_works_through_prefix_and_slash(core_runtime, monkeypatch, slash):
    bot, cog, member, invoke = core_runtime
    ctx = (
        await invoke_slash(bot, invoke, monkeypatch, "core uptime")
        if slash
        else await invoke("!core uptime")
    )
    assert not ctx.command_failed
    card = sends(ctx).call_args.kwargs["embed"]
    assert card.title == "CorePlus · Uptime"
    assert "2 hours" in card.description
    assert_limits(card)


async def test_native_legacy_reply_is_styled_once_and_disable_is_respected(core_runtime):
    bot, cog, member, invoke = core_runtime
    ctx = await invoke("!uptime")
    assert sends(ctx).call_args.kwargs["embed"].title == "CorePlus · Uptime"
    await cog.style_native_reply(ctx)
    await ctx.send("hello")
    assert sends(ctx).call_args.kwargs["embed"].description == "hello"
    await bot._disabled_cog_cache.disable_cog_in_guild(cog.qualified_name, member.guild.id)
    ctx = await invoke("!uptime")
    assert not hasattr(ctx, "_kevin_management_sender")
    assert "2 hours" in ctx.send.call_args.args[0]


@pytest.mark.parametrize("owner", [False, True])
@pytest.mark.parametrize("slash", [False, True])
async def test_core_loading_preserves_checks_parser_and_native_hooks(
    core_runtime, monkeypatch, owner, slash
):
    bot, cog, member, invoke = core_runtime
    if owner:
        bot.owner_ids.add(member.id)
    source = bot.get_command("load")
    callback = AsyncMock()
    monkeypatch.setattr(source, "_callback", callback)
    before = AsyncMock()
    source.before_invoke(before)
    ctx = (
        await invoke_slash(bot, invoke, monkeypatch, "core load", packages="levelplus logplus")
        if slash
        else await invoke("!core load levelplus logplus")
    )
    if owner:
        assert not ctx.command_failed
        callback.assert_awaited_once()
        assert callback.call_args.args[2:] == ("levelplus", "logplus")
        before.assert_awaited_once()
        source.disable_in(member.guild)
        await invoke("!core load levelplus")
        assert callback.await_count == 1
    else:
        assert ctx.command_failed
        callback.assert_not_awaited()


async def test_native_sync_keeps_original_cooldown_and_handler(core_runtime, monkeypatch):
    bot, cog, member, invoke = core_runtime
    bot.owner_ids.add(member.id)
    monkeypatch.setattr(bot.tree, "sync", AsyncMock(return_value=[]))
    ctx = await invoke("!core")
    ctx.channel._state = bot._connection
    monkeypatch.setattr(bot.http, "send_typing", AsyncMock())
    ctx = await invoke("!core slash sync")
    assert not ctx.command_failed, bot.on_command_error.call_args
    await invoke("!core slash sync")
    bot.tree.sync.assert_awaited_once()
    assert bot.get_command("slash sync").has_error_handler()


async def test_replies_keep_files_mentions_filters_and_original_sender(core_runtime):
    bot, cog, member, invoke = core_runtime
    ctx = await invoke("!help")
    raw = sends(ctx)
    raw.reset_mock()
    style_replies(ctx, cog._presentation)
    await ctx.send("before @everyone", filter=lambda text: text.replace("before", "after"))
    assert raw.call_args.kwargs["embed"].description == "after @everyone"
    assert not raw.call_args.kwargs["allowed_mentions"].everyone
    file = SimpleNamespace(filename="result.txt")
    await ctx.send(file=file)
    assert raw.call_args.kwargs["file"] is file
    assert "embed" not in raw.call_args.kwargs


def test_arguments_round_trip_without_dispatching_another_command():
    from discord.ext.commands.view import StringView

    values = ["kevin-cogs", "two words", 'one "quote"', "back\\slash", "$(echo nope)", ""]
    for value in values:
        assert StringView(quoted(value)).get_quoted_word() == value
    assert words('levelplus "logplus"') == ["levelplus", "logplus"]
    for invalid in ("line\nbreak", "trailing\\", "x" * 2001):
        with pytest.raises(commands.BadArgument):
            quoted(invalid)
    with pytest.raises(commands.BadArgument):
        words(" ")


async def test_help_views_recheck_identity_permissions_and_expire(core_runtime):
    bot, cog, member, invoke = core_runtime
    ctx = await invoke("!help")
    view = sends(ctx).call_args.kwargs["view"]
    interaction = SimpleNamespace(
        user=member,
        guild=member.guild,
        channel_id=ctx.channel.id,
        message=ctx.message,
        response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    interaction.message.edit = AsyncMock()
    await view.change(interaction, target="AudioPlus")
    assert view.target == "AudioPlus"
    interaction.message.edit.assert_awaited_once()
    await view.change(interaction, action="next")
    assert view.page == 1
    interaction.user = SimpleNamespace(id=member.id + 1)
    await view.change(interaction, action="home")
    assert view.target == "AudioPlus"
    assert "belongs to another member" in interaction.followup.send.call_args.args[0]
    interaction.user = member
    cog.help_menu.disable_in(member.guild)
    await view.change(interaction, action="home")
    assert view.target == "AudioPlus"
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    assert not cog._views
    assert await cog.red_get_data_for_user(user_id=member.id) == {}


async def test_core_helper_copies_and_all_packages_fit_discord(core_runtime, monkeypatch, tmp_path):
    import importlib

    from discord.app_commands.commands import validate_name

    bot, core, member, invoke = core_runtime
    for helper in ("presentation.py", "command_support.py", "interactive.py"):
        assert Path("coreplus", helper).read_bytes() == Path("audioplus", helper).read_bytes()
    loaded = []
    for package in ("introplus", "exportplus"):
        monkeypatch.setattr(
            f"{package}.cog.cog_data_path", lambda cog: tmp_path / type(cog).__name__
        )
    try:
        for package, name in (
            ("communityplus", "CommunityPlus"),
            ("levelplus", "LevelPlus"),
            ("logplus", "LogPlus"),
            ("owoplus", "OwoPlus"),
            ("emojistealerplus", "EmojiStealerPlus"),
            ("exportplus", "ExportPlus"),
            ("backupplus", "BackupPlus"),
            ("introplus", "IntroPlus"),
            ("presenceplus", "PresencePlus"),
            ("settingshub", "SettingsHub"),
        ):
            cog = getattr(importlib.import_module(package), name)(bot)
            await bot.add_cog(cog)
            loaded.append(cog)
        roots = {**bot.tree._disabled_global_commands, **bot.tree._global_commands}
        actions = 0
        core_actions = 0

        def validate(payload, depth=0):
            validate_name(payload["name"])
            assert 1 <= len(payload["description"]) <= 100
            assert len(payload.get("options", [])) <= 25
            for option in payload.get("options", []):
                if option["type"] in (1, 2):
                    assert depth < 2
                validate(option, depth + 1)

        for app in roots.values():
            validate(app.to_dict(bot.tree))
            leaves = app.walk_commands() if isinstance(app, discord.app_commands.Group) else [app]
            for leaf in leaves:
                if isinstance(leaf, discord.app_commands.Command):
                    actions += 1
                    if leaf.binding is core:
                        core_actions += 1
        assert (len(roots), actions, core_actions) == (88, 417, 14)
        print(f"{len(roots)} roots / {actions} actions")
    finally:
        for cog in reversed(loaded):
            await bot.remove_cog(cog.qualified_name)
