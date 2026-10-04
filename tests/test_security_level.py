"""Formatting, CSV output, guild isolation and retained XP privacy regressions."""

import csv
import io
import json
from unittest.mock import Mock

import pytest
from conftest import make_channel, make_context, make_guild, make_member
from redbot.core import commands
from test_audio_hybrid import red_command_runtime as audio_fixture
from test_cog_hybrid import command_runtime as cog_fixture

from levelplus import LevelPlus
from levelplus.templates import render_template

command_runtime = cog_fixture
red_command_runtime = audio_fixture

UNSAFE_TEMPLATES = (
    "{user.name:1000000000}",
    "{user.xp:01000000000d}",
    "{user.xp:.1000000000f}",
    "{user.name:{user.xp}}",
    "{user.__class__.__mro__[1]}",
    "{user.name:513}",
    "{user.name:١٠٠٠٠٠٠٠٠٠}",
    "{user.name!r:99999999999999999999999999999999}",
)


@pytest.mark.parametrize("template", UNSAFE_TEMPLATES)
def test_unsafe_specs_are_rejected_before_a_field_is_formatted(template, monkeypatch):
    format_field = Mock(side_effect=AssertionError("An unsafe template reached formatting"))
    monkeypatch.setattr("levelplus.templates.FORMATTER.format_field", format_field)
    with pytest.raises(ValueError):
        render_template(template, mention="@Member", name="Member", level=1, xp=1)
    format_field.assert_not_called()


@pytest.mark.parametrize("template", UNSAFE_TEMPLATES)
@pytest.mark.parametrize("preview", [False, True])
async def test_saved_unsafe_templates_fall_back_in_live_notices_and_previews(
    bot, guild, monkeypatch, template, preview
):
    cog = LevelPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    await cog.config.guild(guild).levelup.template.set(template)
    await cog.config.guild(guild).levelup.channel_id.set(channel.id)
    format_field = Mock(side_effect=AssertionError("Unsafe formatting was attempted"))
    monkeypatch.setattr("levelplus.templates.FORMATTER.format_field", format_field)
    if preview:
        await LevelPlus.level_testmsg.callback(
            cog, make_context(guild, channel, member), member=member
        )
    else:
        await cog.maybe_announce_levelup(guild, member, 0, 1)
    assert "has reached level" in channel.send.await_args.args[0]
    assert len(channel.send.await_args.args[0]) < 200
    format_field.assert_not_called()


@pytest.mark.parametrize("template", UNSAFE_TEMPLATES + ("x" * 501, "{user.name:wrong}"))
async def test_template_setter_rejects_unsafe_input_without_changing_config(bot, guild, template):
    cog = LevelPlus(bot)
    group = cog.config.guild(guild)
    before = await group.levelup.template()
    with pytest.raises(commands.BadArgument):
        await LevelPlus.levelup_template.callback(cog, make_context(guild), text=template)
    assert await group.levelup.template() == before


def test_normal_format_specs_and_escaped_braces_still_work_and_output_is_bounded():
    rendered = render_template(
        "{{Level}} {user.level:03d}: {user.name!r} has {user.xp:,d} XP {user.mention}",
        mention="<@123>",
        name="Kevin",
        level=7,
        xp=12345,
    )
    assert rendered == "{Level} 007: 'Kevin' has 12,345 XP <@123>"
    with pytest.raises(ValueError, match="2000"):
        render_template("{user.name:512}" * 5, mention="@Member", name="Member", level=1, xp=1)


async def test_real_command_parser_rejects_huge_widths_and_saves_a_valid_template(command_runtime):
    bot, _, member, invoke = command_runtime
    bot.owner_ids.add(member.id)
    cog = bot.get_cog("LevelPlus")
    before = await cog.config.guild(member.guild).levelup.template()
    ctx = await invoke("!level levelup template {user.name:1000000000}")
    assert ctx.command_failed
    assert await cog.config.guild(member.guild).levelup.template() == before
    ctx = await invoke("!level levelup template {user.mention} now has {user.xp:,d} XP")
    assert not ctx.command_failed
    assert (
        await cog.config.guild(member.guild).levelup.template()
        == "{user.mention} now has {user.xp:,d} XP"
    )


@pytest.mark.parametrize("preview", [False, True])
async def test_display_names_and_templates_cannot_ping_roles_everyone_or_other_members(
    bot, guild, preview
):
    cog = LevelPlus(bot)
    member = make_member(guild, name="@everyone @here <@987654321012345678> <@&888>")
    channel = make_channel(guild)
    group = cog.config.guild(guild)
    await group.levelup.channel_id.set(channel.id)
    await group.levelup.template.set("{user.mention} {user.name} @everyone <@&777> <@555>")
    if preview:
        await LevelPlus.level_testmsg.callback(
            cog, make_context(guild, channel, member), member=member
        )
    else:
        await cog.maybe_announce_levelup(guild, member, 0, 1)
    message = channel.send.await_args
    assert member.mention in message.args[0]
    assert "@\u200beveryone" in message.args[0]
    assert "@\u200bhere" in message.args[0]
    mentions = message.kwargs["allowed_mentions"].to_dict()
    assert mentions == {"users": [member.id], "parse": []}
    assert message.kwargs["allowed_mentions"].replied_user is False


@pytest.mark.parametrize(
    "alias",
    (
        '=HYPERLINK("https://example.invalid", "click")',
        "+SUM(1,2)",
        "-1+1",
        "@SUM(1,2)",
        " \t\r\n=1+1",
        "\x00 \x1b\t=1+1",
        "\ufeff\u200b@SUM(1,2)",
    ),
)
async def test_csv_exports_formula_aliases_as_literal_cells_without_altering_config(
    bot, guild, alias
):
    cog = LevelPlus(bot)
    uid = "123456789012345678"
    group = cog.config.guild(guild)
    await group.xp.set({uid: 9007199254740993})
    await group.names.set({uid: alias})
    ctx = make_context(guild)
    await LevelPlus.xp_export.callback(cog, ctx)
    output = ctx.send.await_args.kwargs["file"].fp.read().decode("utf-8")
    rows = list(csv.reader(io.StringIO(output)))
    assert rows == [
        ["user_id", "xp", "alias"],
        [uid, "9007199254740993", "'" + alias.replace("\x00", "\\0")],
    ]
    assert (await group.names())[uid] == alias


@pytest.mark.parametrize("alias", ("Kelly, Kevin", "ordinary", "'a quoted name", "42", ""))
async def test_ordinary_aliases_survive_csv_import_and_export_unchanged(bot, guild, alias):
    cog = LevelPlus(bot)
    uid = "123456789012345678"
    raw = io.StringIO()
    csv.writer(raw).writerow([uid, 42, alias])
    ctx = make_context(guild)
    await LevelPlus.xp_import_csv.callback(cog, ctx, raw=raw.getvalue())
    await LevelPlus.xp_export.callback(cog, ctx)
    output = ctx.send.await_args.kwargs["file"].fp.read().decode("utf-8")
    assert list(csv.reader(io.StringIO(output)))[1] == [uid, "42", alias]


@pytest.mark.parametrize("earned", [0, 25])
async def test_daily_earned_record_is_exported_even_when_all_other_user_records_are_absent(
    bot, guild, earned
):
    cog = LevelPlus(bot)
    uid = 123456789012345678
    group = cog.config.guild(guild)
    await group.earned_today.set({"day": "2026-10-04", "xp": {str(uid): earned, "555": 99}})
    data = json.load((await cog.red_get_data_for_user(user_id=uid))["levelplus.json"])
    assert data[str(guild.id)]["daily_earned"] == earned
    assert data[str(guild.id)]["daily_earned_day"] == "2026-10-04"
    assert "555" not in json.dumps(data)
    await cog.red_delete_data_for_user(requester="user", user_id=uid)
    assert not await cog.red_get_data_for_user(user_id=uid)
    assert (await group.earned_today())["xp"] == {"555": 99}


async def test_public_name_lookup_never_searches_users_cached_from_another_guild(bot, guild):
    cog = LevelPlus(bot)
    other = make_member(make_guild(guild.id + 1), name="Private Guild Alias")
    bot.users.append(other)
    ctx = make_context(guild)
    await LevelPlus.level_lookup.callback(cog, ctx, query="Private Guild")
    output = ctx.send.await_args.kwargs["embed"].description
    assert output == "No matches."
    await LevelPlus.level_lookup.callback(cog, ctx, query=str(other.id))
    output = ctx.send.await_args.kwargs["embed"].description
    assert "unknown" in output and "Private Guild Alias" not in output
    current = make_member(guild, other.id + 1, name="Visible Guild Alias")
    await LevelPlus.level_lookup.callback(cog, ctx, query="Visible Guild")
    output = ctx.send.await_args.kwargs["embed"].description
    assert str(current.id) in output and "Visible Guild Alias" in output


async def test_importlines_name_resolution_is_guild_scoped_but_explicit_ids_still_work(bot, guild):
    cog = LevelPlus(bot)
    other = make_member(make_guild(guild.id + 1), name="Private Guild Alias")
    bot.users.append(other)
    await LevelPlus.xp_import_lines.callback(
        cog, make_context(guild), lines="Private Guild Alias,10"
    )
    assert await cog.config.guild(guild).xp() == {}
    await LevelPlus.xp_import_lines.callback(cog, make_context(guild), lines=f"{other.id},10")
    assert await cog.config.guild(guild).xp() == {str(other.id): 10}
    assert await cog.config.guild(guild).names() == {str(other.id): str(other.id)}
