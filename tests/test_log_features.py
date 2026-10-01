"""Diffs, routing precedence, bounded unsent-page retries, and raw events."""

import asyncio
import json
from copy import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import forbidden, make_channel, make_context, make_member

from logplus import LogPlus
from logplus.constants import EVENT_STYLE
from logplus.delivery import EVENT_SWITCH
from logplus.diffs import overwrite_changes, permission_changes


async def test_retry_uses_current_theme_without_losing_original_tone(bot, guild):
    cog = LogPlus(bot)
    channel = make_channel(guild)
    await cog.config.guild(guild).log_channel.set(channel.id)
    bot._kevin_cogs_themes = {guild.id: {"colors": {"error": 0x123456}, "footer": "Scarlet"}}
    card = cog._presentation.embed("Failure", "details", tone="error")
    record = cog._pending_log(card, None)
    channel.send.side_effect = forbidden()
    with pytest.raises(discord.Forbidden):
        await cog._deliver_log(guild, record)
    assert record.parts[0]["embed"].color.value == card.color.value
    bot._kevin_cogs_themes[guild.id] = {"colors": {"error": 0x654321}, "footer": "New brand"}
    channel.send.side_effect = None
    assert await cog._deliver_log(guild, record)
    card = channel.send.call_args.kwargs["embed"]
    assert card.color.value == 0x654321 and card.footer.text == "New brand"


def test_permissions_and_overwrites_distinguish_allow_deny_and_inherit():
    old = discord.Permissions(view_channel=True)
    new = discord.Permissions(send_messages=True)
    assert permission_changes(old, new) == [
        "**Allowed** · send messages",
        "**Removed** · view channel",
    ]
    role = SimpleNamespace(id=101)
    target = Mock()
    target.id = role.id
    before = SimpleNamespace(
        overwrites={target: discord.PermissionOverwrite(view_channel=True, send_messages=False)}
    )
    after = SimpleNamespace(overwrites={target: discord.PermissionOverwrite(view_channel=False)})
    diffs = overwrite_changes(before, after)
    assert "view channel · Allow → Deny" in diffs[0][1]
    assert "send messages · Deny → Inherit" in diffs[0][1]
    assert "Deny → Inherit" in overwrite_changes(after, SimpleNamespace(overwrites={}))[0][1]
    assert set(EVENT_STYLE) <= set(EVENT_SWITCH)


async def test_permission_only_updates_have_details_and_unchanged_events_are_skipped(bot, guild):
    cog = LogPlus(bot)
    before = make_channel(guild)
    before.overwrites = {}
    before.topic, before.nsfw, before.slowmode_delay = None, False, 0
    after = copy(before)
    role = Mock()
    role.id = 101
    after.overwrites = {role: discord.PermissionOverwrite(send_messages=False)}
    cog._send = AsyncMock()
    cog._audit_actor_recent = AsyncMock(return_value="Kevin (111)")
    await cog.on_guild_channel_update(before, after)
    embed = cog._send.await_args.args[1]
    assert "send messages · Inherit → Deny" in embed.fields[-1].value
    assert embed.event_type == "channel_updated"
    await cog.on_guild_channel_update(after, after)
    assert cog._send.await_count == 1
    before_role = discord.Role(
        guild=guild,
        state=Mock(),
        data={"id": "101", "name": "Member", "permissions": "0", "color": 0, "position": 1},
    )
    after_role = copy(before_role)
    after_role._permissions = discord.Permissions(manage_roles=True).value
    await cog.on_guild_role_update(before_role, after_role)
    assert "manage roles" in cog._send.await_args.args[1].description


async def test_category_routes_follow_source_and_thread_parent_overrides(bot, guild):
    cog = LogPlus(bot)
    default, category, specific = (make_channel(guild, uid) for uid in (500, 501, 502))
    source = make_channel(guild, 600)
    thread = make_channel(guild, 601, discord.Thread)
    thread.parent_id = source.id
    conf = cog.config.guild(guild)
    await conf.log_channel.set(default.id)
    await cog.route_category.callback(cog, make_context(guild), "member", category)
    await conf.overrides.set({str(source.id): specific.id})
    cog._settings_cache.clear()
    assert await cog._log_channel(guild, None, "member") is category
    assert await cog._log_channel(guild, source.id, "member") is specific
    assert await cog._log_channel(guild, thread.id, "member") is specific
    assert await cog._log_channel(guild, None, "voice") is default
    embed = await cog._E(guild, "Member joined", etype="member_joined")
    await cog._send(guild, embed)
    category.send.assert_awaited_once()
    await cog.ignore_add.callback(cog, make_context(guild), source, "all")
    assert await cog._is_exempt(guild, thread.id, "message")
    assert await cog._is_exempt(guild, thread.id, "server")
    await asyncio.gather(
        cog._flip(make_context(guild), "message", "edit"),
        cog.ignore_remove.callback(cog, make_context(guild), source, "message"),
    )
    assert not await conf.message.edit()
    assert not await cog._is_exempt(guild, thread.id, "message")
    assert await cog._is_exempt(guild, thread.id, "server")


async def test_failed_page_retry_sends_only_unsent_parts_and_reroutes(bot, guild):
    cog = LogPlus(bot)
    original, replacement = make_channel(guild), make_channel(guild, 457)
    await cog.config.guild(guild).log_channel.set(original.id)
    gate = asyncio.Event()

    async def pause_retry(gid):
        await gate.wait()

    cog._retry_logs = pause_retry
    embed = await cog._E(guild, "Delete", "x" * 7000, etype="message_deleted")
    original.send.side_effect = [SimpleNamespace(id=1), forbidden()]
    await cog._send(guild, embed, 555)
    assert original.send.await_count == 2
    record = cog._retry_queues[guild.id][0]
    assert len(record.parts) == 2
    assert cog._delivery_status[guild.id]["failures"] == 1
    assert record.parts[0]["embed"].description == "x" * 3000
    await cog.config.guild(guild).log_channel.set(replacement.id)
    cog._settings_cache.clear()
    record.retries = 1
    assert await cog._deliver_log(guild, record)
    assert replacement.send.await_count == 2
    assert cog._delivery_status[guild.id]["recovered"] == 1
    assert not record.parts
    await cog.cog_unload()


async def test_retry_worker_caps_attempts_drops_expired_and_respects_disable(
    bot, guild, monkeypatch
):
    cog = LogPlus(bot)
    channel = make_channel(guild)
    await cog.config.guild(guild).log_channel.set(channel.id)
    channel.send.side_effect = forbidden()
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("logplus.delivery.asyncio.sleep", sleep)
    await cog._send(guild, await cog._E(guild, "Joined", etype="member_joined"))
    task = cog._retry_tasks[guild.id]
    await task
    assert delays == [2, 4, 8]
    assert channel.send.await_count == 4
    assert not cog._retry_queues and not cog._retry_tasks
    assert cog._delivery_status[guild.id]["dropped"] == 1
    record = cog._pending_log(await cog._E(guild, "Edited", etype="message_edited"), 456)
    record.created -= 301
    cog._enqueue_log(guild, record)
    await cog._retry_tasks[guild.id]
    assert channel.send.await_count == 4
    record = cog._pending_log(await cog._E(guild, "Edited", etype="message_edited"), 456)
    await cog.config.guild(guild).message.edit.set(False)
    cog._settings_cache.clear()
    cog._enqueue_log(guild, record)
    await cog._retry_tasks[guild.id]
    assert channel.send.await_count == 4
    record = cog._pending_log(await cog._E(guild, "Joined", etype="member_joined"), None)
    cog._enqueue_log(guild, record)
    bot.cog_disabled_in_guild.return_value = True
    await cog._retry_tasks[guild.id]
    assert channel.send.await_count == 4
    await cog.cog_unload()


async def test_queue_bounds_data_hooks_and_unload_cancel_workers(bot, guild):
    cog = LogPlus(bot)
    member = make_member(guild)
    embed = await cog._E(guild, "Member", member.mention, etype="member_joined")
    for _ in range(102):
        cog._enqueue_log(guild, cog._pending_log(embed, None))
    assert len(cog._retry_queues[guild.id]) == 100
    assert cog._delivery_status[guild.id]["dropped"] == 2
    export = json.load((await cog.red_get_data_for_user(user_id=member.id))["logplus-pending.json"])
    assert len(export[str(guild.id)]) == 100
    task = cog._retry_tasks[guild.id]
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    assert task.cancelled()
    assert not await cog.red_get_data_for_user(user_id=member.id)
    assert not cog._retry_queues
    record = cog._pending_log(embed, None)
    record.size = 3 * 1024 * 1024
    cog._enqueue_log(guild, record)
    assert not cog._retry_queues[guild.id]
    record.size = 100
    cog._enqueue_log(guild, record)
    task = cog._retry_tasks[guild.id]
    await cog.cog_unload()
    assert task.cancelled()
    assert not cog._retry_tasks and not cog._retry_queues
    cog._enqueue_log(guild, record)
    assert not cog._retry_tasks


async def test_text_fallback_preserves_acknowledged_chunks_on_failure(bot, guild):
    cog = LogPlus(bot)
    channel = make_channel(guild)
    channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, send_messages=True
    )
    await cog.config.guild(guild).log_channel.set(channel.id)
    embed = await cog._E(guild, "Deleted", "x" * 3000, etype="message_deleted")
    record = cog._pending_log(embed, 555)
    channel.send.side_effect = [SimpleNamespace(id=1), forbidden()]
    try:
        await cog._deliver_log(guild, record)
    except discord.Forbidden:
        pass
    assert len(record.parts) == 1 and len(record.parts[0]["content"]) < 2000
    channel.send.side_effect = None
    assert await cog._deliver_log(guild, record)
    assert channel.send.await_count == 3
    assert all("embed" not in call.kwargs for call in channel.send.await_args_list)


async def test_disable_between_pages_stops_the_rest_of_a_delivery(bot, guild):
    cog = LogPlus(bot)
    channel = make_channel(guild)
    await cog.config.guild(guild).log_channel.set(channel.id)

    async def send(**kwargs):
        bot.cog_disabled_in_guild.return_value = True
        return SimpleNamespace(id=1)

    channel.send.side_effect = send
    record = cog._pending_log(
        await cog._E(guild, "Message", "x" * 7000, etype="message_deleted"), 555
    )
    assert not await cog._deliver_log(guild, record)
    assert channel.send.await_count == 1 and len(record.parts) == 2


async def test_inflight_failure_after_unload_does_not_start_a_retry(bot, guild):
    cog = LogPlus(bot)
    channel = make_channel(guild)
    await cog.config.guild(guild).log_channel.set(channel.id)
    started, release = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        started.set()
        await release.wait()
        raise forbidden()

    channel.send.side_effect = send
    task = asyncio.create_task(
        cog._send(guild, await cog._E(guild, "Joined", etype="member_joined"))
    )
    await started.wait()
    await cog.cog_unload()
    release.set()
    await task
    assert not cog._retry_tasks and not cog._retry_queues and not cog._delivery_status


async def test_raw_edits_and_deletes_cover_uncached_only_and_skip_log_destinations(bot, guild):
    cog = LogPlus(bot)
    channel = make_channel(guild)
    cog._send = AsyncMock()
    payload = SimpleNamespace(
        guild_id=guild.id,
        channel_id=channel.id,
        message_id=42,
        cached_message=None,
        data={"content": "new text"},
    )
    await cog.on_raw_message_edit(payload)
    await cog.on_raw_message_delete(payload)
    assert cog._send.await_count == 2
    edit = cog._send.await_args_list[0].args[1]
    assert "Unavailable" in edit.fields[3].value
    assert edit.fields[4].value == "new text"
    payload.cached_message = object()
    await cog.on_raw_message_edit(payload)
    await cog.on_raw_message_delete(payload)
    payload.cached_message = None
    payload.data = {"embeds": []}
    await cog.on_raw_message_edit(payload)
    assert cog._send.await_count == 2
    await cog.config.guild(guild).log_channel.set(channel.id)
    cog._settings_cache.clear()
    await cog.on_raw_message_delete(payload)
    assert cog._send.await_count == 2
    payload.channel_id += 1
    bot.cog_disabled_in_guild.return_value = True
    await cog.on_raw_message_delete(payload)
    assert cog._send.await_count == 2
