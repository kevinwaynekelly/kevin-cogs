"""External emoji capture with real Config and mocked Discord/CDN boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import make_channel, make_member, make_message
from redbot.core import commands
from test_audio_hybrid import red_command_runtime as audio_fixture
from test_cog_hybrid import command_runtime as cog_fixture
from test_cog_hybrid import invoke_slash

from emojistealerplus import EmojiStealerPlus
from emojistealerplus.cog import emoji_name
from emojistealerplus.constants import MAX_IMAGE

command_runtime = cog_fixture
red_command_runtime = audio_fixture
SOURCE = discord.PartialEmoji(name="dance", id=111111111111111111)
PNG = b"\x89PNG\r\n\x1a\nimage"


@pytest.fixture
async def emoji_cog(bot, guild):
    cog = EmojiStealerPlus(bot)
    await cog.cog_load()
    guild.emojis = []
    guild.emoji_limit = 50
    guild.fetch_emojis = AsyncMock(side_effect=lambda: list(guild.emojis))

    async def create(**kwargs):
        emoji = SimpleNamespace(
            id=222222222222222222 + len(guild.emojis),
            name=kwargs["name"],
            animated=kwargs["image"].startswith(b"GIF"),
        )
        guild.emojis.append(emoji)
        return emoji

    guild.create_custom_emoji = AsyncMock(side_effect=create)
    cog._download = AsyncMock(return_value=PNG)
    try:
        yield cog
    finally:
        await cog.cog_unload()


async def test_concurrent_captures_and_identical_image_ids_do_not_duplicate(emoji_cog, guild):
    results = await asyncio.gather(*(emoji_cog._copy(guild, SOURCE) for _ in range(5)))
    assert sum(created for result, created in results) == 1
    other = discord.PartialEmoji(name="sameimage", id=333333333333333333)
    result, created = await emoji_cog._copy(guild, other)
    assert not created and result.id == results[0][0].id
    assert guild.create_custom_emoji.await_count == 1
    assert len(await emoji_cog.config.guild(guild).copied()) == 2


async def test_deleted_copy_can_be_recreated(emoji_cog, guild):
    await emoji_cog._copy(guild, SOURCE)
    guild.emojis.clear()
    await emoji_cog._copy(guild, SOURCE)
    assert guild.create_custom_emoji.await_count == 2


async def test_animated_slots_and_name_collisions(emoji_cog, guild):
    guild.emoji_limit = 1
    guild.emojis = [SimpleNamespace(id=777, name="dance", animated=False)]
    emoji_cog._download.return_value = b"GIF89aanimation"
    animated = discord.PartialEmoji(name="dance", id=SOURCE.id, animated=True)
    result, created = await emoji_cog._copy(guild, animated)
    assert created and result.animated and result.name != "dance"
    assert len(result.name) <= 32
    with pytest.raises(commands.BadArgument, match="free slots"):
        await emoji_cog._copy(guild, discord.PartialEmoji(name="other", id=999999999999999999))


async def test_missing_permission_pause_disable_and_unicode(emoji_cog, guild, bot):
    guild.me.guild_permissions = discord.Permissions.none()
    with pytest.raises(commands.CheckFailure, match="Create Expressions"):
        await emoji_cog._copy(guild, SOURCE)
    guild.me.guild_permissions = discord.Permissions.all()
    await emoji_cog._set_capture(guild, "enabled", False)
    with pytest.raises(commands.CheckFailure):
        await emoji_cog._copy(guild, SOURCE, automatic=True)
    await emoji_cog._copy(guild, SOURCE)
    bot.cog_disabled_in_guild.return_value = True
    with pytest.raises(commands.CheckFailure):
        await emoji_cog._copy(guild, SOURCE)
    with pytest.raises(commands.BadArgument):
        await emoji_cog._copy(guild, discord.PartialEmoji(name="😀"))


async def test_messages_edits_reactions_and_channel_scope(emoji_cog, guild):
    channel = make_channel(guild)
    member = make_member(guild)
    message = make_message(member, channel, content=f"😀 {SOURCE} {SOURCE}")
    emoji_cog._run = AsyncMock()
    await emoji_cog.on_message(message)
    assert emoji_cog._queue.qsize() == 1
    await emoji_cog.on_message_edit(SimpleNamespace(content="old"), message)
    assert emoji_cog._queue.qsize() == 1
    payload = SimpleNamespace(
        guild_id=guild.id, channel_id=channel.id, user_id=member.id, member=member, emoji=SOURCE
    )
    await emoji_cog.on_raw_reaction_add(payload)
    assert emoji_cog._queue.qsize() == 1
    await emoji_cog._set_capture(guild, "channel", 999)
    other = discord.PartialEmoji(name="new", id=444444444444444444)
    await emoji_cog._queue_emoji(guild, channel, other)
    assert emoji_cog._queue.qsize() == 1
    member.bot = True
    await emoji_cog.on_message(make_message(member, channel, content=str(other)))
    assert emoji_cog._queue.qsize() == 1
    await asyncio.sleep(0)


async def test_worker_and_http_session_stop_on_unload(emoji_cog, guild):
    session = emoji_cog._session
    await emoji_cog._queue_emoji(guild, make_channel(guild), SOURCE)
    await emoji_cog._queue.join()
    task = emoji_cog._worker
    await emoji_cog.cog_unload()
    assert task.done() and session.closed and not emoji_cog._pending
    assert guild.create_custom_emoji.await_count == 1


@pytest.mark.parametrize(
    "body,animated,valid",
    [
        (PNG, False, True),
        (b"GIF89a123", True, True),
        (b"html", False, False),
        (PNG, True, False),
        (PNG + b"x" * MAX_IMAGE, False, False),
    ],
)
async def test_cdn_stream_image_validation_and_bounds(emoji_cog, body, animated, valid):
    async def chunks(size):
        for start in range(0, len(body), size):
            yield body[start : start + size]

    response = SimpleNamespace(status=200, content=SimpleNamespace(iter_chunked=chunks))
    manager = Mock()
    manager.__aenter__ = AsyncMock(return_value=response)
    manager.__aexit__ = AsyncMock(return_value=False)
    session = SimpleNamespace(get=Mock(return_value=manager), close=AsyncMock())
    await emoji_cog._session.close()
    emoji_cog._session = session
    del emoji_cog._download
    emoji = discord.PartialEmoji(name="dance", id=SOURCE.id, animated=animated)
    if valid:
        assert await emoji_cog._download(emoji) == body
    else:
        with pytest.raises(commands.BadArgument):
            await emoji_cog._download(emoji)
    assert session.get.call_args.kwargs["allow_redirects"] is False
    assert session.get.call_args.args[0].startswith("https://cdn.discordapp.com/emojis/")


async def test_real_prefix_slash_registration_and_permission_checks(command_runtime, monkeypatch):
    bot, loaded, member, invoke = command_runtime
    cog = EmojiStealerPlus(bot)
    await bot.add_cog(cog)
    bot.owner_ids.add(member.id)
    try:
        await invoke("!emoji enabled False")
        assert not (await cog.config.guild(member.guild).capture())["enabled"]
        await invoke_slash(bot, invoke, monkeypatch, "emoji reactions", enabled=False)
        assert not (await cog.config.guild(member.guild).capture())["reactions"]
        bot.owner_ids.discard(member.id)
        member.guild_permissions.manage_guild = False
        member.guild_permissions.administrator = False
        ctx = await invoke("!emoji enabled True")
        assert (
            ctx.command_failed and not (await cog.config.guild(member.guild).capture())["enabled"]
        )
        assert len({**bot.tree._global_commands, **bot.tree._disabled_global_commands}) <= 100
    finally:
        await bot.remove_cog("EmojiStealerPlus")


def test_names_remain_valid_and_unique():
    assert emoji_name("a", SOURCE.id, set()) == "emoji_a"
    names = {"dance", "dance_11111111"}
    result = emoji_name("dance", SOURCE.id, names)
    assert result not in names and len(result) <= 32
