"""Persistent poll/event behavior using Red storage and mocked Discord transport."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import forbidden, make_channel, make_context, make_member
from redbot.core import commands

from communityplus import CommunityPlus
from communityplus.social import RETENTION, event_time, social_embed


@pytest.fixture
async def social(bot, guild):
    cog = CommunityPlus(bot)
    cog._now_ts = lambda: 1000
    ctx = make_context(guild, make_channel(guild), make_member(guild))
    message = SimpleNamespace(id=555, edit=AsyncMock())
    ctx.send.return_value = message
    ctx.channel.fetch_message.return_value = message
    yield cog, ctx
    await cog.cog_unload()


async def create_poll(cog, ctx):
    await cog.poll_create.callback(cog, ctx, "Which game?", "One|Two", 1)
    return next(iter(await cog.config.guild(ctx.guild).social.polls()))


async def create_event(cog, ctx):
    await cog.event_create.callback(cog, ctx, "Game night", "1600", 15)
    return next(iter(await cog.config.guild(ctx.guild).social.events()))


async def test_poll_refresh_keeps_server_theme(social):
    cog, ctx = social
    ctx.bot = cog.bot
    cog.bot._kevin_cogs_themes = {ctx.guild.id: {"colors": {"info": 0x123456}, "footer": "Scarlet"}}
    key = await create_poll(cog, ctx)
    await cog._social_choice(ctx, "polls", key, 1)
    await cog._social_refresh(ctx.guild, "polls", key)
    card = ctx.channel.fetch_message.return_value.edit.call_args.kwargs["embed"]
    assert card.color.value == 0x123456 and card.footer.text.startswith("Scarlet")


async def test_one_vote_per_member_changes_without_duplicating_and_expiry_rejects(social):
    cog, ctx = social
    key = await create_poll(cog, ctx)
    await asyncio.gather(
        *(cog._social_choice(ctx, "polls", key, choice) for choice in (1, 2, 1, 2))
    )
    row = await cog._social_record(ctx.guild, "polls", key)
    assert row["votes"] == {str(ctx.author.id): 2}
    with pytest.raises(commands.BadArgument):
        await cog._social_choice(ctx, "polls", key, 3)
    cog._now_ts = lambda: row["at"]
    with pytest.raises(commands.BadArgument):
        await cog._social_choice(ctx, "polls", key, 1)
    assert (
        next(
            field for field in social_embed(cog, "polls", key, row).fields if field.name == "Status"
        ).value
        == "Closed"
    )
    await cog._social_tick(ctx.guild)
    assert (await cog._social_record(ctx.guild, "polls", key))["closed"]
    assert not cog._social_views


async def test_participant_and_record_limits_and_failed_post_rollback(social):
    cog, ctx = social
    key = await create_poll(cog, ctx)
    async with cog.config.guild(ctx.guild).social() as data:
        data["polls"][key]["votes"] = {str(uid): 1 for uid in range(1000)}
    with pytest.raises(commands.BadArgument):
        await cog._social_choice(ctx, "polls", key, 1)
    for _ in range(9):
        await cog.poll_create.callback(cog, ctx, "Other?", "Yes|No", 1)
    with pytest.raises(commands.BadArgument):
        await cog.poll_create.callback(cog, ctx, "Too many?", "Yes|No", 1)
    await cog.poll_close.callback(cog, ctx, key)
    ctx.send.side_effect = forbidden()
    with pytest.raises(Exception):
        await cog.poll_create.callback(cog, ctx, "Failed", "Yes|No", 1)
    assert len(await cog.config.guild(ctx.guild).social.polls()) == 10


async def test_rsvp_is_independent_of_opt_in_reminders_and_success_is_not_resent(social):
    cog, ctx = social
    key = await create_event(cog, ctx)
    await cog.event_rsvp.callback(cog, ctx, key, "YES")
    await cog._social_tick(ctx.guild)
    ctx.author.send.assert_not_awaited()
    await cog.event_remind.callback(cog, ctx, key, True)
    await cog._social_tick(ctx.guild)
    await cog._social_tick(ctx.guild)
    assert ctx.author.send.await_count == 1
    row = await cog._social_record(ctx.guild, "events", key)
    assert row["rsvps"] == {str(ctx.author.id): "yes"}
    assert row["notified"] == [str(ctx.author.id)] and row["announced"]


async def test_reminder_failure_retries_and_cancel_optout_and_disable_stop_delivery(social):
    cog, ctx = social
    key = await create_event(cog, ctx)
    await cog.event_remind.callback(cog, ctx, key, True)
    ctx.author.send.side_effect = forbidden()
    await cog._social_tick(ctx.guild)
    assert not (await cog._social_record(ctx.guild, "events", key))["notified"]
    cog.bot.cog_disabled_in_guild.return_value = True
    await cog._social_tick(ctx.guild)
    assert ctx.author.send.await_count == 1
    cog.bot.cog_disabled_in_guild.return_value = False
    await cog.event_remind.callback(cog, ctx, key, False)
    await cog._social_tick(ctx.guild)
    assert ctx.author.send.await_count == 1
    await cog.event_remind.callback(cog, ctx, key, True)
    ctx.author.send.side_effect = None
    await cog._social_tick(ctx.guild)
    assert ctx.author.send.await_count == 2
    await cog.event_cancel.callback(cog, ctx, key)
    await cog._social_tick(ctx.guild)
    assert ctx.author.send.await_count == 2


async def test_panels_restore_and_data_hooks_erase_only_one_member(social):
    cog, ctx = social
    poll_id, event_id = await create_poll(cog, ctx), await create_event(cog, ctx)
    await cog._social_choice(ctx, "polls", poll_id, 1)
    await cog.event_remind.callback(cog, ctx, event_id, True)
    other = make_member(ctx.guild, 9999)
    other_ctx = make_context(ctx.guild, ctx.channel, other)
    await cog._social_choice(other_ctx, "polls", poll_id, 2)
    export = json.load(
        (await cog.red_get_data_for_user(user_id=ctx.author.id))["communityplus.json"]
    )
    assert export["social"][str(ctx.guild.id)][f"polls:{poll_id}"]["votes"] == 1
    for view in cog._social_views.values():
        view.stop()
    cog._social_views.clear()
    cog.bot.add_view = Mock()
    await cog._restore_social(ctx.guild)
    assert cog.bot.add_view.call_count == 2 and all(
        view.is_persistent() for view in cog._social_views.values()
    )
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=ctx.author.id)
    assert not await cog._social_user_data(ctx.author.id)
    assert (await cog._social_record(ctx.guild, "polls", poll_id))["votes"] == {str(other.id): 2}
    cog._now_ts = lambda: 5000 + RETENTION
    await cog._social_tick(ctx.guild)
    assert not await cog.config.guild(ctx.guild).social.polls()
    assert not await cog.config.guild(ctx.guild).social.events()
    assert not cog._social_views and not cog._social_locks


async def test_unload_cancels_owned_reminder_delivery(social):
    cog, ctx = social
    key = await create_event(cog, ctx)
    await cog.event_remind.callback(cog, ctx, key, True)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    ctx.author.send = send
    cog._maintenance_task = asyncio.create_task(cog._social_tick(ctx.guild))
    await started.wait()
    await cog.cog_unload()
    assert cancelled.is_set() and cog._maintenance_task.cancelled() and not cog._social_views


@pytest.mark.parametrize("requested,permitted", [(True, True), (False, True), (True, False)])
async def test_updated_panels_keep_embed_preferences_and_plain_text_fallback(
    social, requested, permitted
):
    cog, ctx = social
    ctx.embed_requested = AsyncMock(return_value=requested)
    permissions = discord.Permissions.all()
    permissions.embed_links = permitted
    ctx.channel.permissions_for.return_value = permissions
    key = await create_poll(cog, ctx)
    await cog._social_choice(ctx, "polls", key, 1)
    update = ctx.channel.fetch_message.return_value.edit.await_args.kwargs
    assert bool(update["embed"]) is (requested and permitted)
    if not requested or not permitted:
        assert "1 vote(s)" in update["content"] and len(update["content"]) <= 2000


@pytest.mark.parametrize(
    "value", ["2026-10-03T19:00", "bad date", "0", "1000", "999999999999999999999999"]
)
def test_event_dates_require_future_offset_or_timestamp(value):
    with pytest.raises(commands.BadArgument):
        event_time(value, 1000)


def test_event_date_offsets_and_unix_times_match():
    stamp = event_time("2026-10-03T19:00-05:00", 1790841600)
    assert stamp == event_time(str(stamp), 1790841600)
