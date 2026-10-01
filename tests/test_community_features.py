"""Role safety, persistent controls, voice accounting, and weekly summaries."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import forbidden, make_channel, make_context, make_member, make_message
from redbot.core import commands

from communityplus import CommunityPlus
from communityplus.features import RoleMenuView, voice_days, week_key


def add_roles(guild):
    def role(uid, position, permissions=0):
        return discord.Role(
            guild=guild,
            state=Mock(),
            data={
                "id": str(uid),
                "name": f"Role {uid}",
                "position": position,
                "permissions": str(permissions),
                "color": 0,
                "managed": False,
            },
        )

    safe, other, unsafe, top = role(101, 1), role(102, 2), role(103, 3, 8), role(104, 10)
    guild.me.top_role = top
    guild.roles = [safe, other, unsafe, top]
    guild.get_role.side_effect = lambda uid: next((r for r in guild.roles if r.id == uid), None)
    return safe, other, unsafe, top


async def test_role_menu_applies_only_current_safe_roles_and_survives_reload(bot, guild):
    cog = CommunityPlus(bot)
    safe, other, unsafe, top = add_roles(guild)
    member = make_member(guild)
    member.roles = [other]
    member.add_roles.side_effect = lambda *roles, **kw: member.roles.extend(roles)
    member.remove_roles = AsyncMock(
        side_effect=lambda *roles, **kw: [member.roles.remove(r) for r in roles]
    )
    ctx = make_context(guild, author=member)
    await cog.rolemenu_add.callback(cog, ctx, safe)
    for role in (unsafe, top):
        with pytest.raises(commands.BadArgument):
            await cog.rolemenu_add.callback(cog, ctx, role)
    await cog._apply_self_roles(member, {safe.id})
    assert set(member.roles) == {safe, other}
    await cog._apply_self_roles(member, set())
    assert member.roles == [other]
    with pytest.raises(commands.BadArgument):
        await cog._apply_self_roles(member, {unsafe.id})
    # A role made privileged after setup is never assigned by an old menu.
    safe._permissions = discord.Permissions(administrator=True).value
    with pytest.raises(commands.BadArgument):
        await cog._apply_self_roles(member, {safe.id})
    safe._permissions = 0
    channel = make_channel(guild)
    channel.send.return_value = SimpleNamespace(id=333)
    await cog.rolemenu_post.callback(cog, ctx, channel)
    old = cog._role_views[333]
    assert old.is_persistent()
    await cog.cog_unload()
    assert old.is_finished()
    replacement = CommunityPlus(bot)
    bot.add_view = Mock()
    await replacement._restore_role_menus(guild)
    bot.add_view.assert_called_once_with(replacement._role_views[333], message_id=333)
    await replacement.rolemenu_unpost.callback(replacement, ctx, "333")
    assert not await replacement.config.guild(guild).features.role_menus()
    await replacement.cog_unload()


async def test_role_click_checks_clicker_disabled_state_and_guild(bot, guild, monkeypatch):
    cog = CommunityPlus(bot)
    safe, *_ = add_roles(guild)
    member = make_member(guild)
    view = RoleMenuView(cog, guild, [safe], message_id=333)
    interaction = SimpleNamespace(
        guild_id=guild.id,
        user=member,
        response=SimpleNamespace(is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    checked = AsyncMock(return_value=SimpleNamespace(author=member))
    monkeypatch.setattr("communityplus.features.component_context", checked)
    cog._apply_self_roles = AsyncMock()
    selector = view.children[0]
    selector._values = [str(safe.id)]
    await selector.callback(interaction)
    checked.assert_awaited_once_with(cog, interaction, "roles", owner_id=None)
    cog._apply_self_roles.assert_awaited_once_with(member, {safe.id})
    checked.side_effect = commands.DisabledCommand("roles disabled")
    await selector.callback(interaction)
    assert cog._apply_self_roles.await_count == 1
    checked.reset_mock()
    interaction.guild_id += 1
    await selector.callback(interaction)
    checked.assert_not_awaited()
    view.stop()


async def test_voice_duration_checkpoints_split_dates_and_delete_data(bot, guild, monkeypatch):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild, kind=discord.VoiceChannel)
    channel.members = [member]
    member.voice = SimpleNamespace(channel=channel)
    clock = [datetime(2026, 10, 1, 4, 59, 30, tzinfo=timezone.utc).timestamp(), 100.0]
    monkeypatch.setattr("communityplus.features.time.time", lambda: clock[0])
    monkeypatch.setattr("communityplus.features.time.monotonic", lambda: clock[1])
    await cog._track_voice(member, channel)
    clock[0] += 90
    clock[1] += 90
    await asyncio.gather(*(cog._track_voice(member, channel) for _ in range(5)))
    data = await cog.config.member(member).participation()
    assert data["voice_seconds"] == 90
    assert data["days"]["2026-09-30"]["voice_seconds"] == 30
    assert data["days"]["2026-10-01"]["voice_seconds"] == 60
    await cog.on_message(make_message(member, make_channel(guild)))
    exported = json.load((await cog.red_get_data_for_user(user_id=member.id))["communityplus.json"])
    assert exported[str(guild.id)]["participation"]["voice_seconds"] == 90
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    assert not await cog.red_get_data_for_user(user_id=member.id)
    assert (guild.id, member.id) not in cog._voice_sessions
    await cog.tracking.callback(cog, make_context(guild), False)
    clock[0] += 3600
    clock[1] += 3600
    await cog._track_voice(member, channel)
    assert not await cog.red_get_data_for_user(user_id=member.id)


def test_voice_day_splitting_handles_daylight_saving():
    start = datetime(2026, 11, 1, 5, tzinfo=timezone.utc).timestamp()
    assert list(voice_days(start, start + 25 * 3600, "America/Chicago")) == [
        ("2026-11-01", 25 * 3600)
    ]


async def test_solo_exemptions_warnings_and_dm_switch_keep_deadline(bot, guild, monkeypatch):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild, kind=discord.VoiceChannel)
    channel.members = [member]
    member.voice = SimpleNamespace(channel=channel)
    await cog.config.guild(guild).features.solo_channels.set([channel.id])
    await cog._schedule_solo_disconnect(member, 0)
    await cog._solo_tasks[(guild.id, member.id)]
    member.move_to.assert_not_awaited()
    await cog.config.guild(guild).features.solo_channels.set([])
    await cog.config.guild(guild).features.warning_seconds.set(10)
    cog._settings_cache.clear()
    clock = [100.0]
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("communityplus.cog.asyncio.sleep", sleep)
    monkeypatch.setattr("communityplus.cog.time.monotonic", lambda: clock[0])

    async def dm(*args, **kwargs):
        clock[0] += 2  # Sending the warning must not extend the deadline.

    cog._presentation.send = AsyncMock(side_effect=dm)
    await cog._schedule_solo_disconnect(member, 60)
    await cog._solo_tasks[(guild.id, member.id)]
    assert sleeps == [50, 8]
    assert member.move_to.await_count == 1
    assert cog._presentation.send.await_count == 2
    await cog.solo_notify.callback(cog, make_context(guild), False)
    cog._presentation.send.reset_mock()
    await cog._schedule_solo_disconnect(member, 60)
    await cog._solo_tasks[(guild.id, member.id)]
    cog._presentation.send.assert_not_awaited()


async def test_weekly_digest_persists_cursor_retries_failure_and_bounds_days(
    bot, guild, monkeypatch
):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    clock = [datetime(2026, 10, 5, 15, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr("communityplus.features.time.time", lambda: clock[0])
    group = cog.config.guild(guild)
    await group.features.summary.channel.set(channel.id)
    await cog.summary_enable.callback(cog, make_context(guild), True)
    await cog._digest_tick(guild)
    channel.send.assert_not_awaited()
    # Newly enabled summaries wait until the next weekly boundary.
    clock[0] += 7 * 86400
    await cog._record_activity(member, "message", 456, {"messages": 2})
    for day in range(40):
        clock[0] += 86400
        await cog._record_activity(member, "message", 456, {"messages": 1})
    assert len((await cog.config.member(member).participation())["days"]) == 35
    channel.send.side_effect = forbidden()
    cursor = await group.features.summary.last_week()
    await cog._digest_tick(guild)
    assert await group.features.summary.last_week() == cursor
    channel.send.side_effect = None
    await cog._digest_tick(guild)
    assert await group.features.summary.last_week() != cursor
    replacement = CommunityPlus(bot)
    channel.send.reset_mock()
    await replacement._digest_tick(guild)
    channel.send.assert_not_awaited()
    conf = await group.features.summary()
    assert week_key(clock[0], conf)[1]
    embed = await replacement._summary_embed(guild)
    assert embed.fields[0].value == "7"
    assert embed.fields[2].value == "1"


async def test_tracking_and_background_tasks_stop_on_unload(bot, guild):
    cog = CommunityPlus(bot)
    await cog.cog_load()
    tasks = [cog._startup_task, cog._maintenance_task]
    await cog.cog_unload()
    assert all(task.cancelled() for task in tasks)
