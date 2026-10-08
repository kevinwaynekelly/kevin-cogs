import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from conftest import make_channel, make_context, make_member

from logplus import LogPlus


async def configured_cog(bot, guild):
    cog = LogPlus(bot)
    channel = make_channel(guild)
    await cog.config.guild(guild).log_channel.set(channel.id)
    cog._send = AsyncMock()
    return cog, channel


async def test_hourly_summary_has_status_activity_duration_and_member_identity(bot, guild):
    cog, _ = await configured_cog(bot, guild)
    member = make_member(guild)
    member.activities = [discord.Game("The Witcher")]
    await cog._presence_cycle(0)
    member.status = discord.Status.idle
    member.activities = []
    await cog._presence_cycle(1800)
    cog._send.assert_not_awaited()
    await cog._presence_cycle(3600)
    cog._send.assert_awaited_once()
    _, embed = cog._send.await_args.args
    fields = {field.name: field.value for field in embed.fields}
    assert str(member.id) in fields["Member"]
    assert "Online: 30m 0s" in fields["Presence"]
    assert "Idle: 30m 0s" in fields["Presence"]
    assert fields["Status changes"] == "1"
    assert fields["Activities"] == "Playing The Witcher: 30m 0s"
    assert embed.event_type == "presence_summary"
    assert cog._send.await_args.kwargs == {"category": "member"}
    await cog._presence_cycle(3600)
    assert cog._send.await_count == 1


async def test_activity_only_presence_updates_are_buffered_not_posted(bot, guild, monkeypatch):
    cog, _ = await configured_cog(bot, guild)
    member = make_member(guild)
    await cog._presence_cycle(900)
    before = SimpleNamespace(status=member.status)
    member.activities = [discord.Game("Minecraft")]
    monkeypatch.setattr("logplus.cog.time.time", lambda: 1800)
    await cog.on_presence_update(before, member)
    cog._send.assert_not_awaited()
    await cog._presence_cycle(3600)
    embed = cog._send.await_args.args[1]
    assert "Partial hour" in embed.footer.text
    assert (
        next(f.value for f in embed.fields if f.name == "Activities") == "Playing Minecraft: 30m 0s"
    )


async def test_disabled_collection_and_missing_destination_discard_buffers(bot, guild):
    cog, _ = await configured_cog(bot, guild)
    make_member(guild)
    await cog._presence_cycle(0)
    await LogPlus.event.callback(cog, make_context(guild), "member.presence", False)
    assert len(cog._presence_tracker) == 0
    await cog.cog_after_invoke(make_context(guild))
    await cog._presence_cycle(3600)
    cog._send.assert_not_awaited()
    await LogPlus.event.callback(cog, make_context(guild), "member.presence", True)
    await cog.cog_after_invoke(make_context(guild))
    await cog._presence_cycle(4000)
    await cog.config.guild(guild).log_channel.set(None)
    cog._settings_cache.clear()
    await cog._presence_cycle(7200)
    assert len(cog._presence_tracker) == 0
    cog._send.assert_not_awaited()


async def test_disconnect_does_not_account_unobserved_gap(bot, guild, monkeypatch):
    cog, _ = await configured_cog(bot, guild)
    make_member(guild)
    await cog._presence_cycle(0)
    await cog.on_disconnect()
    await cog._presence_cycle(1800)
    assert len(cog._presence_tracker) == 0
    monkeypatch.setattr("logplus.cog.time.time", lambda: 2700)
    await cog.on_resumed()
    await cog._presence_cycle(3600)
    embed = cog._send.await_args.args[1]
    assert next(f.value for f in embed.fields if f.name == "Presence") == "Online: 15m 0s"


async def test_member_route_and_delivery_switch_are_applied_to_summary(bot, guild):
    cog, channel = await configured_cog(bot, guild)
    destination = make_channel(guild, channel.id + 1)
    await cog.config.guild(guild).features.routes.set({"member": destination.id})
    cog._settings_cache.clear()
    cog._send = LogPlus._send.__get__(cog)
    destination.send.return_value = SimpleNamespace(id=42)
    make_member(guild)
    await cog._presence_cycle(0)
    await cog._presence_cycle(3600)
    channel.send.assert_not_awaited()
    destination.send.assert_awaited_once()
    embed = destination.send.await_args.kwargs["embed"]
    record = cog._pending_log(embed, None, "member")
    await cog.config.guild(guild).member.presence.set(False)
    cog._settings_cache.clear()
    assert await cog._delivery_route(guild, record) is None


async def test_privacy_export_deletion_and_unload_clear_pending_presence(bot, guild):
    cog, _ = await configured_cog(bot, guild)
    member = make_member(guild)
    await cog._presence_cycle(1200)
    data = await cog.red_get_data_for_user(user_id=member.id)
    assert json.load(data["logplus-presence.json"])[str(guild.id)]["user_id"] == member.id
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    assert not await cog.red_get_data_for_user(user_id=member.id)
    cog._presence_tracker.observe(member, 1800)
    await cog.cog_load()
    task = cog._presence_task
    await asyncio.sleep(0)
    await cog.cog_unload()
    assert task.done()
    assert cog._presence_task is None
    assert len(cog._presence_tracker) == 0


async def test_no_presence_intent_or_red_disabled_guild_cannot_collect(bot, guild):
    cog, _ = await configured_cog(bot, guild)
    make_member(guild)
    bot.intents = discord.Intents.none()
    await cog._presence_cycle(0)
    assert len(cog._presence_tracker) == 0
    bot.intents.presences = True
    await cog._presence_cycle(1800)
    assert len(cog._presence_tracker) == 1
    bot.cog_disabled_in_guild.return_value = True
    await cog._presence_cycle(3600)
    cog._send.assert_not_awaited()
    assert len(cog._presence_tracker) == 0


async def test_privacy_deletion_cancels_inflight_summary_before_history_or_send(bot, guild):
    cog, channel = await configured_cog(bot, guild)
    member = make_member(guild)
    await cog.config.guild(guild).history_settings.enabled.set(True)
    cog._settings_cache.clear()
    cog._send = LogPlus._send.__get__(cog)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_observer(*args):
        started.set()
        await release.wait()

    cog._observe_log = blocked_observer
    await cog._presence_cycle(0)
    cycle = asyncio.create_task(cog._presence_cycle(3600))
    await asyncio.wait_for(started.wait(), 1)
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    release.set()
    await asyncio.wait_for(cycle, 1)
    channel.send.assert_not_awaited()
    assert await cog.config.guild(guild).history_records() == []
    assert not cog._presence_send_tasks


async def test_delayed_presence_callback_cannot_recreate_departed_or_disconnected_member(
    bot, guild
):
    cog, _ = await configured_cog(bot, guild)
    member = make_member(guild)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_check(*args):
        entered.set()
        await release.wait()
        return True

    cog._presence_enabled = blocked_check
    callback = asyncio.create_task(cog.on_presence_update(member, member))
    await entered.wait()
    guild.members.clear()
    release.set()
    await callback
    assert len(cog._presence_tracker) == 0
    guild.members.append(member)
    entered.clear()
    release.clear()
    callback = asyncio.create_task(cog.on_presence_update(member, member))
    await entered.wait()
    await cog.on_disconnect()
    release.set()
    await callback
    assert len(cog._presence_tracker) == 0
