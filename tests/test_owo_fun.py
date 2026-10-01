"""Author-only Undo, explicit haiku storage, voting deadlines and bounded styles."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import forbidden, make_channel, make_context, make_member, make_message
from redbot.core import commands

from owoplus import OwoPlus
from owoplus.fun import POETRY_LIMIT, style_words

POEM = "the sun is so bright the sky is so blue and clear we go home at night"


async def test_custom_styles_keep_protected_content_and_delete_overrides(bot, guild):
    cog = OwoPlus(bot)
    ctx = make_context(guild, make_channel(guild), make_member(guild))
    await cog.customstyle_create.callback(
        cog, ctx, "space", replacements='{"hello":"greetings","friend":"pilot"}'
    )
    await cog.customstyle_decorate.callback(cog, ctx, "space", "[SPACE] ", " END", True)
    await cog.stylize.callback(
        cog, ctx, "space", text="hello friend <@123> https://example.invalid/Hello `hello`"
    )
    result = ctx.send.call_args.kwargs["embed"].description
    assert "[SPACE] GREETINGS PILOT" in result
    assert "<@123>" in result and "https://example.invalid/Hello" in result and "`hello`" in result
    await cog.owo_style_set.callback(cog, ctx, ctx.channel, "space", 0)
    await cog.customstyle_delete.callback(cog, ctx, "space")
    assert not (await cog.config.guild(guild).features())["channel_styles"]
    with pytest.raises(commands.BadArgument):
        style_words('{"hello":"hi","Hello":"bye"}')
    with pytest.raises(commands.BadArgument):
        style_words('{"hello":"line\\nbreak"}')


async def test_undo_author_checks_attachment_preservation_and_expiry(bot, guild):
    cog = OwoPlus(bot)
    member, channel = make_member(guild), make_channel(guild)
    message = make_message(member, channel, content="original")
    posted = SimpleNamespace(content="changed", edit=AsyncMock(), delete=AsyncMock())
    hook = SimpleNamespace(send=AsyncMock(return_value=posted))
    cog._ensure_webhook = AsyncMock(return_value=hook)
    try:
        assert await cog._repost(message, "changed")
        assert "owoplus.json" in await cog.red_get_data_for_user(user_id=member.id)
        view = cog._undos[message.id]
        stranger = make_context(guild, channel, make_member(guild, 789))
        with pytest.raises(commands.CheckFailure):
            await view.restore(stranger)
        await view.restore(make_context(guild, channel, member))
        assert posted.edit.call_args.kwargs["content"] == "original"
        assert "attachments" not in posted.edit.call_args.kwargs
        posted.delete.assert_not_awaited()
        assert not cog._undos and not view.raw
        await cog._attach_undo(message, hook, [posted])
        view = cog._undos[message.id]
        view.expires = time.monotonic() - 1
        with pytest.raises(commands.BadArgument, match="expired"):
            await view.restore(make_context(guild, channel, member))
        assert not cog._undos and not view.raw
    finally:
        await cog.cog_unload()


@pytest.mark.parametrize("failure", ["control", "cancel_after_delete"])
async def test_failed_undo_control_never_removes_successful_repost(bot, guild, failure):
    cog = OwoPlus(bot)
    message = make_message(make_member(guild), make_channel(guild))
    posted = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
    cog._ensure_webhook = AsyncMock(
        return_value=SimpleNamespace(send=AsyncMock(return_value=posted))
    )
    if failure == "control":
        message.channel.send.side_effect = forbidden()
        assert await cog._repost(message, "changed")
    else:
        cog._attach_undo = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await cog._repost(message, "changed")
    message.delete.assert_awaited_once()
    posted.delete.assert_not_awaited()
    assert not cog._undos
    await cog.cog_unload()


async def test_undo_partial_failure_rolls_back_and_can_retry(bot, guild):
    cog = OwoPlus(bot)
    member, channel = make_member(guild), make_channel(guild)
    message = make_message(member, channel, content="a" * 2500)
    first = SimpleNamespace(content="x" * 2000, edit=AsyncMock(), delete=AsyncMock())
    second = SimpleNamespace(
        content="y", edit=AsyncMock(side_effect=forbidden()), delete=AsyncMock()
    )
    try:
        await cog._attach_undo(message, SimpleNamespace(send=AsyncMock()), [first, second])
        view = cog._undos[message.id]
        with pytest.raises(commands.BadArgument, match="retry"):
            await view.restore(make_context(guild, channel, member))
        assert first.edit.call_args.kwargs["content"] == first.content
        first.delete.assert_not_awaited()
        assert view.raw == message.content
        second.edit.side_effect = None
        await view.restore(make_context(guild, channel, member))
        assert not cog._undos
    finally:
        await cog.cog_unload()


async def test_hall_requires_approval_and_deletion_removes_user_data(bot, guild):
    cog = OwoPlus(bot)
    member, channel = make_member(guild), make_channel(guild)
    ctx = make_context(guild, channel, member)
    member.guild_permissions = discord.Permissions.none()
    await cog.haikuhall_submit.callback(cog, ctx, text=POEM)
    key = next(iter((await cog.config.guild(guild).poetry())["hall"]))
    await cog.haikuhall.callback(cog, ctx)
    assert "No approved" in ctx.send.call_args.kwargs["embed"].description
    await cog.haikuhall_approve.callback(cog, ctx, key, True)
    await cog.haikuhall.callback(cog, ctx)
    assert key in ctx.send.call_args.kwargs["embed"].description
    assert "owoplus.json" in await cog.red_get_data_for_user(user_id=member.id)
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    assert not (await cog.config.guild(guild).poetry())["hall"]
    assert await cog.red_get_data_for_user(user_id=member.id) == {}


async def test_contest_votes_are_unique_deadlines_close_and_announce_once(bot, guild):
    cog = OwoPlus(bot)
    channel = make_channel(guild)
    author = make_member(guild)
    other, voter = make_member(guild, 222), make_member(guild, 333)
    ctx = make_context(guild, channel, author)
    await cog.haikucontest_create.callback(cog, ctx, 1, title="Autumn")
    group = cog.config.guild(guild).poetry
    key = next(iter((await group())["contests"]))
    await cog.haikucontest_submit.callback(cog, ctx, key, text=POEM)
    await cog.haikucontest_submit.callback(cog, make_context(guild, channel, other), key, text=POEM)
    entries = (await group())["contests"][key]["entries"]
    first, second = list(entries)
    with pytest.raises(commands.BadArgument):
        await cog.haikucontest_vote.callback(cog, ctx, key, first)
    vctx = make_context(guild, channel, voter)
    await cog.haikucontest_vote.callback(cog, vctx, key, first)
    await cog.haikucontest_vote.callback(cog, vctx, key, second)
    assert len((await group())["contests"][key]["votes"]) == 1
    async with group() as data:
        data["contests"][key]["ends"] = time.time() - 1
    with pytest.raises(commands.BadArgument, match="deadline"):
        await cog.haikucontest_vote.callback(cog, vctx, key, first)
    await cog._poetry_tick(guild)
    await cog._poetry_tick(guild)
    contest = (await group())["contests"][key]
    assert contest["closed"] and contest["winner"] == second and contest["announced"]
    channel.send.assert_awaited_once()
    await cog.red_delete_data_for_user(requester="user", user_id=other.id)
    contest = (await group())["contests"][key]
    assert second not in contest["entries"] and not contest["votes"] and contest["winner"] is None
    await cog._prune_poetry(now=time.time() + 91 * 86400)
    assert not (await group())["contests"]


async def test_poetry_budget_rejects_and_rolls_back_new_entry(bot, guild):
    cog = OwoPlus(bot)
    channel, member = make_channel(guild), make_member(guild)
    group = cog.config.guild(guild).poetry
    async with group() as data:
        data["hall"]["old"] = {
            "author": 2,
            "approved": True,
            "approver": None,
            "at": time.time(),
            "text": "x" * (POETRY_LIMIT - 100),
        }
    with pytest.raises(commands.BadArgument, match="full"):
        await cog.haikuhall_submit.callback(cog, make_context(guild, channel, member), text=POEM)
    assert list((await group())["hall"]) == ["old"]
