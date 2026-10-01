"""Channel inheritance, persistent expiry, protected text, and late reload guards."""

import asyncio
import time
from unittest.mock import AsyncMock

import discord
from conftest import make_channel, make_context, make_member, make_message

from owoplus import OwoPlus
from owoplus.features import channel_features


async def test_channel_styles_expire_across_reload_and_threads_inherit(bot, guild):
    cog = OwoPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    thread = make_channel(guild, 457, discord.Thread)
    thread.parent, thread.parent_id = channel, channel.id
    ctx = make_context(guild, channel, member)
    await cog.owo_style_set.callback(cog, ctx, channel=channel, style="pirate", minutes=0)
    await cog.owo_style_set.callback(cog, ctx, channel=thread, style="robot", minutes=1)
    features = await OwoPlus(bot).config.guild(guild).features()
    assert channel_features(thread, features)["style"] == "robot"
    assert channel_features(thread, features, time.time() + 61)["style"] == "pirate"
    await cog.owo_style_clear.callback(cog, ctx, channel=thread)
    assert channel_features(thread, await cog.config.guild(guild).features())["style"] == "pirate"
    await cog.on_guild_channel_delete(channel)
    assert not (await cog.config.guild(guild).features())["channel_styles"]


async def test_styles_keep_links_mentions_emoji_and_code_intact(bot, guild, monkeypatch):
    monkeypatch.setattr("owoplus.cog.random.randrange", lambda n: 1)
    cog = OwoPlus(bot)
    features = await cog.config.guild(guild).features()
    text = "hello friend `hello` ```friend``` https://site.invalid/hello <@123456789012345678> <:hello:456>"
    for style, expected in (("pirate", "ahoy matey"), ("robot", "GREETINGS HUMAN COMPANION")):
        output = await cog._render_async(text, "full", False, {**features, "style": style})
        assert expected in output
        for protected in (
            "`hello`",
            "```friend```",
            "https://site.invalid/hello",
            "<@123456789012345678>",
            "<:hello:456>",
        ):
            assert protected in output
        assert (
            cog._choose_mode(
                make_member(guild),
                "https://site.invalid/hello",
                {"one_in": 1000000, "features": {**features, "style": style}},
            )
            == "none"
        )
    features["words"] = {"hello": "howdy", "friend": None}
    assert (
        await cog._render_async("hello friend", "keys", False, {**features, "style": "pirate"})
        == "howdy friend"
    )


async def test_automatic_styles_obey_optouts_and_do_not_repost_after_expiry_or_unload(bot, guild):
    cog = OwoPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    ctx = make_context(guild, channel, member)
    await cog.config.guild(guild).enabled.set(True)
    await cog.owo_style_set.callback(cog, ctx, channel=channel, style="pirate", minutes=1)
    cog._repost = AsyncMock(return_value=True)
    await cog.on_message(make_message(member, channel, content="hello friend"))
    assert cog._repost.await_args.args[1] == "ahoy matey"
    await cog.owooptout.callback(cog, ctx, enabled=True)
    await cog.on_message(make_message(member, channel, content="hello friend"))
    assert cog._repost.await_count == 1
    await cog.owooptout.callback(cog, ctx, enabled=False)
    started, release = asyncio.Event(), asyncio.Event()

    async def render(*args):
        started.set()
        await release.wait()
        return "ahoy matey"

    cog._render_async = render
    task = asyncio.create_task(
        cog.on_message(make_message(member, channel, content="hello friend"))
    )
    await started.wait()
    async with cog.config.guild(guild).features() as features:
        features["channel_styles"][str(channel.id)]["expires"] = time.time() - 1
    cog._settings_cache.clear()
    release.set()
    await task
    assert cog._repost.await_count == 1
    await cog.cog_unload()
    assert not await cog._should_process(make_message(member, channel))


async def test_manual_style_does_not_delete_or_repost(bot, guild):
    cog = OwoPlus(bot)
    ctx = make_context(guild, make_channel(guild), make_member(guild))
    cog._repost = AsyncMock()
    await cog.stylize.callback(cog, ctx, style="robot", text="hello")
    cog._repost.assert_not_awaited()
    assert "GREETINGS" in ctx.send.await_args.kwargs["embed"].description
