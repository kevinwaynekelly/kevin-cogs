"""Capacity promotion, recurrence, explicit onboarding and birthday consent."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import make_channel, make_context, make_member
from redbot.core import commands

from communityplus import CommunityPlus
from communityplus.social import attendance_choice


def test_capacity_preserves_order_and_promotes_after_cancellation():
    record = {"capacity": 1, "rsvps": {}}
    attendance_choice(record, "a", "yes")
    attendance_choice(record, "b", "yes")
    attendance_choice(record, "c", "yes")
    assert record["rsvps"] == {"a": "yes", "b": "wait", "c": "wait"}
    attendance_choice(record, "a", "no")
    assert record["rsvps"]["b"] == "yes"
    assert record["rsvps"]["c"] == "wait"


async def test_recurrence_skips_missed_occurrences_without_replaying_attendance(bot, guild):
    cog = CommunityPlus(bot)
    now = 2000000
    cog._now_ts = lambda: now
    record = {
        "title": "weekly",
        "at": now - 86400 * 40,
        "closed": False,
        "repeat_days": 7,
        "capacity": 2,
        "rsvps": {"1": "yes"},
        "remind": {"1": True},
        "notified": ["1"],
        "announced": True,
        "reminder_at": now - 86400 * 40 - 900,
        "message": None,
        "channel": 1,
    }
    await cog.config.guild(guild).social.events.set({"test": record})
    cog._social_refresh = AsyncMock()
    await cog._social_tick(guild)
    updated = await cog.config.guild(guild).social.events.get_raw("test")
    assert updated["at"] > now and updated["at"] <= now + 7 * 86400
    assert not updated["closed"] and not updated["rsvps"] and not updated["remind"]
    assert updated["at"] - updated["reminder_at"] == 900


async def test_onboarding_revalidates_role_and_stores_rules_digest(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    ctx = make_context(guild, author=member)
    role = Mock(spec=discord.Role)
    guild.me.top_role = Mock(spec=discord.Role)
    role.id = 321
    role.is_default.return_value = False
    role.managed = False
    role.permissions = discord.Permissions.none()
    role.__ge__ = Mock(return_value=False)
    guild.get_role.side_effect = lambda rid: role if rid == role.id else None
    member.add_roles = AsyncMock()
    await cog.onboarding_configure.callback(cog, ctx, role, rules="Be kind.")
    await cog.onboarding_accept.callback(cog, ctx)
    member.add_roles.assert_awaited_once()
    record = await cog.config.member(member).celebrations()
    assert len(record["accepted"]["rules_hash"]) == 64
    role.permissions.administrator = True
    with pytest.raises(commands.BadArgument):
        await cog.onboarding_accept.callback(cog, ctx)
    assert member.add_roles.await_count == 1


async def test_birthdays_require_opt_in_and_announce_once_without_birth_year(bot, guild):
    cog = CommunityPlus(bot)
    channel = make_channel(guild)
    member = make_member(guild)
    ctx = make_context(guild, channel, member)
    await cog.birthday_configure.callback(cog, ctx, channel, timezone_name="UTC", hour=9)
    now = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    await cog._birthday_tick(guild, now=now)
    channel.send.assert_not_awaited()
    await cog.birthday_set.callback(cog, ctx, 10, 1)
    cog._birthday_slots.clear()
    await cog._birthday_tick(guild, now=now)
    channel.send.assert_awaited_once()
    state = await cog.config.member(member).celebrations()
    assert state["birthday"] == [10, 1] and state["last_year"] == 2026
    await cog._birthday_tick(guild, now=datetime(2026, 10, 1, 11, tzinfo=timezone.utc))
    assert channel.send.await_count == 1
    await cog.birthday_remove.callback(cog, ctx)
    assert (await cog.config.member(member).celebrations())["birthday"] is None


async def test_room_owners_are_checked_and_fresh_empty_rooms_survive_gateway_delay(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    ctx = make_context(guild, author=member)
    room = Mock(spec=discord.VoiceChannel)
    room.id, room.members = 55, []
    room.delete = AsyncMock()
    member.guild_permissions = discord.Permissions.none()
    member.voice = SimpleNamespace(channel=room)
    guild.get_channel.side_effect = lambda cid: room if cid == room.id else None
    await cog.config.guild(guild).voice_rooms.set(
        {"55": {"owner": member.id + 1, "created": cog._now_ts()}}
    )
    with pytest.raises(commands.CheckFailure):
        await cog._owned_room(ctx)
    await cog._clean_rooms(guild)
    room.delete.assert_not_awaited()
    await cog.config.guild(guild).voice_rooms.set(
        {"55": {"owner": member.id, "created": cog._now_ts() - 60}}
    )
    assert await cog._owned_room(ctx) is room
    await cog._clean_rooms(guild)
    room.delete.assert_awaited_once()
    assert not await cog.config.guild(guild).voice_rooms()
