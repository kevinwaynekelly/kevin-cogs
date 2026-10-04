import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from conftest import make_channel, make_context, make_guild, make_member, make_message

from communityplus import CommunityPlus


async def test_concurrent_activity_counters_are_atomic(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    await asyncio.gather(*(cog.on_message(make_message(member, channel)) for _ in range(30)))
    data = await cog.config.member(member).all()
    assert data["stats"]["messages"] == 30
    assert data["seen"]["message_ch"] == channel.id


async def test_presence_timestamp_changes_only_with_status(bot, guild, monkeypatch):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    monkeypatch.setattr(cog, "_now_ts", lambda: 100)
    await cog._handle_presence_update_logic(member, member)
    initial = await cog.config.member(member).all()
    monkeypatch.setattr(cog, "_now_ts", lambda: 200)
    await cog._handle_presence_update_logic(member, member)
    later = await cog.config.member(member).all()
    assert later["seen"]["presence"]["since"] == initial["seen"]["presence"]["since"] == 100
    assert later["stats"]["status_changes"]["online"] == 1
    member.status = discord.Status.offline
    await cog._handle_presence_update_logic(member, member)
    assert (await cog.config.member(member).all())["seen"]["presence"]["last_offline"] == 200


async def test_activity_start_is_counted_once(bot, guild):
    cog = CommunityPlus(bot)
    before = make_member(guild)
    after = make_member(guild, before.id + 1)
    after.activities = [SimpleNamespace(type=discord.ActivityType.playing, name="Game")]
    await cog._handle_presence_update_logic(before, after)
    await cog._handle_presence_update_logic(after, after)
    data = await cog.config.member(after).all()
    assert data["stats"]["game_launches"] == 1
    assert data["activity_names"] == {"Game": 1}


async def test_large_saved_game_history_survives_reload_new_activity_and_privacy_export(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    names = {f"Game {index}": index + 1 for index in range(150)}
    long_title = "A" * 300
    names[long_title] = 10
    await cog.config.member(member).activity_names.set(names)
    await cog.config.member(member).stats.game_launches.set(12345)
    await cog._restore_solo_timers()
    assert await cog.config.member(member).activity_names() == names
    before = SimpleNamespace(activities=[])
    member.activities = [SimpleNamespace(type=discord.ActivityType.playing, name=long_title)]
    await cog.on_presence_update(before, member)
    expected = {**names, long_title: 11}
    assert await cog.config.member(member).activity_names() == expected
    assert await cog.config.member(member).stats.game_launches() == 12346
    exported = json.load((await cog.red_get_data_for_user(user_id=member.id))["communityplus.json"])
    assert exported[str(guild.id)]["activity_names"] == expected
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    assert await cog.red_get_data_for_user(user_id=member.id) == {}


async def test_same_user_has_separate_guild_timers_and_unload_cancels_them(bot, guild):
    cog = CommunityPlus(bot)
    other = make_guild(guild.id + 1)
    bot.guilds.append(other)
    for current in (guild, other):
        member = make_member(current)
        channel = make_channel(current, kind=discord.VoiceChannel)
        member.voice = SimpleNamespace(channel=channel)
        channel.members = [member]
        await cog._schedule_solo_disconnect(member, 300)
    assert len(cog._solo_tasks) == 2
    tasks = list(cog._solo_tasks.values())
    await cog.cog_unload()
    assert all(task.cancelled() for task in tasks)
    assert not cog._solo_tasks


async def test_refresh_does_not_reset_existing_solo_deadline(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild, kind=discord.VoiceChannel)
    member.voice = SimpleNamespace(channel=channel)
    channel.members = [member]
    await cog._schedule_solo_disconnect(member, 300)
    task = cog._solo_tasks[(guild.id, member.id)]
    await cog._schedule_solo_disconnect(member, 300)
    assert cog._solo_tasks[(guild.id, member.id)] is task
    await cog.cog_unload()


async def test_timer_checks_channel_and_disabled_state_at_deadline(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    first = make_channel(guild, kind=discord.VoiceChannel)
    second = make_channel(guild, first.id + 1, discord.VoiceChannel)
    first.members = second.members = [member]
    member.voice = SimpleNamespace(channel=first)
    await cog.config.guild(guild).vcsolo.enabled.set(True)
    await cog._schedule_solo_disconnect(member, 0)
    task = cog._solo_tasks[(guild.id, member.id)]
    member.voice.channel = second
    await task
    member.move_to.assert_not_awaited()
    await cog._schedule_solo_disconnect(member, 0)
    bot.cog_disabled_in_guild.return_value = True
    await cog._solo_tasks[(guild.id, member.id)]
    member.move_to.assert_not_awaited()


async def test_enabled_solo_timer_disconnects_and_disable_cancels(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild, kind=discord.VoiceChannel)
    member.voice = SimpleNamespace(channel=channel)
    channel.members = [member]
    await cog.config.guild(guild).vcsolo.enabled.set(True)
    await cog._schedule_solo_disconnect(member, 0)
    await cog._solo_tasks[(guild.id, member.id)]
    member.move_to.assert_awaited_once_with(None, reason="Solo VC timeout")
    await cog._schedule_solo_disconnect(member, 300)
    task = cog._solo_tasks[(guild.id, member.id)]
    await CommunityPlus.cvc_dis.callback(cog, make_context(guild))
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert not cog._solo_tasks


async def test_unload_cancels_startup_before_red_is_ready(bot, guild):
    cog = CommunityPlus(bot)
    ready = asyncio.Event()
    bot.wait_until_red_ready = ready.wait
    await cog.cog_load()
    task = cog._startup_task
    await cog.cog_unload()
    assert task.cancelled()


async def test_seenlist_paginates_and_data_hooks_remove_all_records(bot, guild):
    cog = CommunityPlus(bot)
    for number in range(60):
        make_member(guild, 123456789012345678 + number, name="a" * 32)
    ctx = make_context(guild)
    await CommunityPlus.com_seenlist.callback(cog, ctx, limit=60)
    assert ctx.send.await_count > 1
    assert all(
        len(call.kwargs["embed"].description.encode("utf-16-le")) // 2 <= 4096
        for call in ctx.send.await_args_list
    )
    member = guild.members[0]
    await cog._record_activity(member, "message", 456, {"messages": 1})
    exported = await cog.red_get_data_for_user(user_id=member.id)
    assert "communityplus.json" in exported
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    assert await cog.red_get_data_for_user(user_id=member.id) == {}


async def test_disabled_seen_does_not_collect_voice_activity(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    cog._record_activity = AsyncMock()
    await cog.config.guild(guild).seen.enabled.set(False)
    before = SimpleNamespace(channel=None, self_stream=False, self_video=False)
    after = SimpleNamespace(channel=None, self_stream=True, self_video=False)
    await cog.on_voice_state_update(member, before, after)
    cog._record_activity.assert_not_awaited()


async def test_sticky_purge_and_activity_updates_use_consistent_locks(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    await cog.config.member(member).sticky_roles.set([111, 222])
    await asyncio.gather(
        CommunityPlus.cst_purge.callback(cog, make_context(guild), member),
        *(cog._record_activity(member, "message", 456, {"messages": 1}) for _ in range(30)),
    )
    data = await cog.config.member(member).all()
    assert data["sticky_roles"] == []
    assert data["stats"]["messages"] == 30
