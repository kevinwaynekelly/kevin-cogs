"""Scope, personal opt-outs, code preservation, corrections, and cooldown rollback."""

import json
from unittest.mock import AsyncMock

import discord
from conftest import make_channel, make_context, make_guild, make_member, make_message

from owoplus import OwoPlus


async def test_scopes_inherit_threads_and_optouts_survive_reload(bot, guild):
    cog = OwoPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    thread = make_channel(guild, 457, discord.Thread)
    thread.parent, thread.parent_id = channel, channel.id
    ctx = make_context(guild, channel, member)
    group = cog.config.guild(guild)
    await group.enabled.set(True)
    await cog.owo_channels_mode.callback(cog, ctx, mode="allowlist")
    assert not await cog._should_process(make_message(member, thread))
    await cog.owo_channels_allow.callback(cog, ctx, channel=channel)
    assert await cog._should_process(make_message(member, thread))
    await cog.owooptout.callback(cog, ctx)
    assert not await OwoPlus(bot)._should_process(make_message(member, thread))
    data = json.load((await cog.red_get_data_for_user(user_id=member.id))["owoplus.json"])
    assert data[str(guild.id)]["optout"]
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    assert await cog._should_process(make_message(member, thread))
    await cog.owo_channels_exclude.callback(cog, ctx, channel=channel)
    assert not await cog._should_process(make_message(member, thread))


async def test_custom_words_preserve_code_and_builtin_keywords_can_be_disabled(bot, guild):
    cog = OwoPlus(bot)
    member = make_member(guild)
    ctx = make_context(guild, make_channel(guild), member)
    await cog.owo_words_add.callback(cog, ctx, original="hello", replacement="hiya")
    await cog.owo_words_remove.callback(cog, ctx, original="bro")
    features = await cog.config.guild(guild).features()
    output = await cog._render_async(
        "Hello bro `hello bro`\n```hello bro```", "keys", False, features
    )
    assert "Hiya bro" in output and "`hello bro`" in output and "```hello bro```" in output
    other = make_guild(124)
    fresh = await cog.config.guild(other).features()
    assert "bwo" in await cog._render_async("bro", "keys", False, fresh)
    await cog.owo_keywords.callback(cog, ctx, enabled=False)
    conf = await cog._settings(guild)
    assert await cog._render_async("bro hello", "keys", False, conf["features"]) == "bro hello"


async def test_syllable_overrides_are_server_local_and_manual_commands_do_not_repost(bot, guild):
    cog = OwoPlus(bot)
    member = make_member(guild)
    ctx = make_context(guild, make_channel(guild), member)
    await cog.owo_syllables_set.callback(cog, ctx, word="foo", count=5)
    await cog.owo_syllables_set.callback(cog, ctx, word="bar", count=7)
    features = await cog.config.guild(guild).features()
    assert "🌸" in await cog._render_async("foo bar foo", "none", True, features)
    fresh = await cog.config.guild(make_guild(124)).features()
    assert await cog._render_async("foo bar foo", "none", True, fresh) == "foo bar foo"
    cog._repost = AsyncMock()
    await cog.haiku_command.callback(cog, ctx, text="foo bar foo")
    await cog.owoify.callback(cog, ctx, text="hello world")
    cog._repost.assert_not_awaited()


async def test_cooldowns_suppress_concurrent_reposts_and_failed_repost_releases_slot(bot, guild):
    cog = OwoPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    group = cog.config.guild(guild)
    await group.enabled.set(True)
    await group.features.cooldown.set(60)
    cog._repost = AsyncMock(return_value=True)
    await cog.on_message(make_message(member, channel, content="bro"))
    await cog.on_message(make_message(member, channel, content="dude"))
    cog._repost.assert_awaited_once()
    cog._transform_times.clear()
    cog._repost.side_effect = [False, True]
    await cog.on_message(make_message(member, channel, content="bro"))
    await cog.on_message(make_message(member, channel, content="dude"))
    assert cog._repost.await_count == 3
