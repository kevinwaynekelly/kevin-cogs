import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import forbidden, make_channel, make_context, make_member, make_message

from levelplus import LevelPlus
from levelplus.levels import cumulative_xp


@pytest.mark.parametrize("multiplier", [0.1, 1.0, 2.0, 10.0])
@pytest.mark.parametrize("anchors", [(1, 100, 2, 250), (2, 250, 1, 100)])
async def test_calibration_fits_thresholds_with_active_multiplier(bot, guild, multiplier, anchors):
    cog = LevelPlus(bot)
    group = cog.config.guild(guild)
    await group.multiplier.set(multiplier)
    await group.curve.set("constant")
    await LevelPlus.formula_calibrate.callback(cog, make_context(guild), *anchors)
    coefficients = await group.linear()
    assert await group.multiplier() == multiplier
    assert await group.curve() == "linear"
    for level, xp in zip(anchors[::2], anchors[1::2]):
        assert cumulative_xp(level, "linear", multiplier, **coefficients) == xp


@pytest.mark.parametrize(
    "anchors",
    [
        (1, 10**308, 2, 10**308),
        (1, 10**400, 2, 3 * 10**400),
        (1, 9007199254740993, 2, 18014398509481986),
        (1, -1, 2, 100),
        (1, 100, 2, -1),
    ],
)
async def test_invalid_calibration_preserves_saved_settings(bot, guild, anchors):
    cog = LevelPlus(bot)
    group = cog.config.guild(guild)
    await group.curve.set("exponential")
    before = await group.all()
    ctx = make_context(guild)
    await LevelPlus.formula_calibrate.callback(cog, ctx, *anchors)
    assert await group.all() == before
    ctx.send.assert_awaited_once()
    assert "Calibrated" not in ctx.send.await_args.kwargs["embed"].description


@pytest.mark.parametrize("multiplier", [0.0, -1.0, float("inf"), float("nan")])
async def test_calibration_rejects_invalid_saved_multiplier(bot, guild, multiplier):
    cog = LevelPlus(bot)
    group = cog.config.guild(guild)
    await group.multiplier.set(multiplier)
    before = await group.linear()
    ctx = make_context(guild)
    await LevelPlus.formula_calibrate.callback(cog, ctx, 1, 100, 2, 250)
    assert await group.linear() == before
    assert "positive multiplier" in ctx.send.await_args.kwargs["embed"].description


async def test_concurrent_xp_updates_and_saved_alias(bot, guild):
    cog = LevelPlus(bot)
    member = make_member(guild)
    await cog.config.guild(guild).names.set({str(member.id): "Custom alias"})
    await asyncio.gather(*(cog._add_xp(guild, member, 3) for _ in range(50)))
    await cog._remember_name(guild, member)
    assert await cog._get_xp(guild, member.id) == 150
    assert (await cog.config.guild(guild).names())[str(member.id)] == "Custom alias"


async def test_settings_do_not_load_user_maps_and_cache_is_invalidated(bot, guild):
    cog = LevelPlus(bot)
    await cog.config.guild(guild).xp.set({"123": 100})
    settings = await cog._settings(guild)
    assert "xp" not in settings and "names" not in settings
    await cog.config.guild(guild).message.enabled.set(False)
    assert await cog._settings(guild) is settings
    await cog.cog_after_invoke(make_context(guild))
    assert not (await cog._settings(guild))["message"]["enabled"]


async def test_csv_quoted_alias_and_integer_precision(bot, guild):
    cog = LevelPlus(bot)
    ctx = make_context(guild)
    uid = "123456789012345678"
    await LevelPlus.xp_import_csv.callback(
        cog, ctx, raw=f'user_id,xp,alias\n{uid},9007199254740993,"Kelly, Kevin"\n'
    )
    assert await cog._get_xp(guild, int(uid)) == 9007199254740993
    assert (await cog.config.guild(guild).names())[uid] == "Kelly, Kevin"


async def test_bad_csv_is_not_partially_imported(bot, guild):
    cog = LevelPlus(bot)
    ctx = make_context(guild)
    await LevelPlus.xp_import_csv.callback(
        cog, ctx, raw='123456789012345678,20,valid\n123456789012345679,30,"unfinished'
    )
    assert await cog.config.guild(guild).xp() == {}


async def test_importlines_rejects_ambiguous_names(bot, guild):
    cog = LevelPlus(bot)
    a = make_member(guild, name="Same")
    make_member(guild, a.id + 1, name="Same")
    await LevelPlus.xp_import_lines.callback(
        cog, make_context(guild), lines=f"Same,20\n{a.id},9007199254740993"
    )
    assert await cog.config.guild(guild).xp() == {str(a.id): 9007199254740993}


async def test_reaction_fetch_failure_still_awards_reactor(bot, guild):
    cog = LevelPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    channel.fetch_message.side_effect = forbidden()
    payload = SimpleNamespace(
        guild_id=guild.id, channel_id=channel.id, user_id=member.id, member=member, message_id=10
    )
    await cog.on_raw_reaction_add(payload)
    assert await cog._get_xp(guild, member.id) == 25


async def test_reactions_skip_bot_author_and_share_event_cooldown(bot, guild):
    cog = LevelPlus(bot)
    reactor = make_member(guild)
    author = make_member(guild, reactor.id + 1, bot=True)
    channel = make_channel(guild)
    message = make_message(author, channel)
    bot.cached_messages.append(message)
    payload = SimpleNamespace(
        guild_id=guild.id,
        channel_id=channel.id,
        user_id=reactor.id,
        member=reactor,
        message_id=message.id,
    )
    await cog.on_raw_reaction_add(payload)
    await cog.on_raw_reaction_add(payload)
    assert await cog._get_xp(guild, reactor.id) == 25
    assert await cog._get_xp(guild, author.id) == 0


async def test_thread_parent_and_role_exclusions(bot, guild):
    cog = LevelPlus(bot)
    member = make_member(guild)
    parent = make_channel(guild, kind=discord.ForumChannel)
    thread = make_channel(guild, parent.id + 1, discord.Thread)
    thread.parent_id, thread.parent = parent.id, parent
    settings = await cog._settings(guild)
    assert cog._eligible(member, thread, settings)
    settings["restrictions"]["forum_xp"] = False
    assert not cog._eligible(member, thread, settings)
    settings["restrictions"]["forum_xp"] = True
    settings["restrictions"]["no_channels"] = [parent.id]
    assert not cog._eligible(member, thread, settings)
    settings["restrictions"]["no_channels"] = []
    settings["restrictions"]["no_roles"] = [111]
    member.roles = [SimpleNamespace(id=111)]
    assert not cog._eligible(member, thread, settings)


async def test_components_do_not_award_slash_xp_and_disabled_cog_stays_idle(bot, guild):
    cog = LevelPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    interaction = SimpleNamespace(
        type=discord.InteractionType.component, guild=guild, user=member, channel=channel
    )
    await cog.on_interaction(interaction)
    bot.cog_disabled_in_guild.return_value = True
    await cog.on_message(make_message(member, channel))
    assert await cog.config.guild(guild).xp() == {}


async def test_voice_and_message_awards_do_not_overwrite_each_other(bot, guild):
    cog = LevelPlus(bot)
    member = make_member(guild)
    voice = make_channel(guild, kind=discord.VoiceChannel)
    voice.members = [member]
    guild.voice_channels = [voice]
    member.voice = SimpleNamespace(channel=voice)
    await cog.config.guild(guild).voice.min.set(15)
    await cog.config.guild(guild).voice.max.set(15)
    await asyncio.gather(cog.voice_tick.coro(cog), cog._add_xp(guild, member, 7))
    assert await cog._get_xp(guild, member.id) == 22


async def test_voice_empty_tick_does_not_write_and_user_deletion_clears_alias(bot, guild):
    cog = LevelPlus(bot)
    member = make_member(guild)
    group = cog.config.guild(guild)
    await cog._set_xp(guild, member.id, 30)
    await cog._remember_name(guild, member)
    cog.maybe_announce_levelup = AsyncMock()
    await cog.voice_tick.coro(cog)
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=member.id)
    assert await group.xp() == {} and await group.names() == {}
    assert await cog.red_get_data_for_user(user_id=member.id) == {}


async def test_long_leaderboard_is_paginated(bot, guild):
    cog = LevelPlus(bot)
    await cog.config.guild(guild).xp.set({str(123456789012345678 + i): i + 1 for i in range(50)})
    await cog.config.guild(guild).names.set(
        {str(123456789012345678 + i): "😀" * 100 for i in range(50)}
    )
    ctx = make_context(guild)
    await LevelPlus.leaderboard.callback(cog, ctx, top=50)
    assert ctx.send.await_count > 1
    assert all(
        len(call.kwargs["embed"].description.encode("utf-16-le")) // 2 <= 4096
        for call in ctx.send.await_args_list
    )
