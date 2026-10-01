"""Earned XP limits, time boundaries, independent seasons, roles, and data hooks."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from conftest import make_channel, make_context, make_member, make_message

from levelplus import LevelPlus
from levelplus.features import day_at, period_totals


async def test_caps_are_atomic_across_sources_and_survive_reload(bot, guild):
    cog = LevelPlus(bot)
    member = make_member(guild)
    group = cog.config.guild(guild)
    await group.xp_features.daily_cap.set(100)
    await asyncio.gather(
        *(
            cog._add_xp(guild, member, 7, source=source)
            for source in ["message", "voice", "reaction", "slash"] * 10
        )
    )
    assert await cog._get_xp(guild, member.id) == 100
    assert (await group.period_xp())["season"][str(member.id)] == 100
    replacement = LevelPlus(bot)
    await replacement._add_xp(guild, member, 50, source="voice")
    assert await replacement._get_xp(guild, member.id) == 100
    await replacement._add_xp(guild, member, 12)
    assert await replacement._get_xp(guild, member.id) == 112
    assert (await group.period_xp())["season"][str(member.id)] == 100


async def test_boost_scopes_expiry_and_calendar_days(bot, guild, monkeypatch):
    cog = LevelPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    ctx = make_context(guild, channel, member)
    clock = [datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr("levelplus.cog.time.time", lambda: clock[0])
    role = SimpleNamespace(id=5)
    member.roles = [role]
    await cog.boost.callback(cog, ctx, factor=2, minutes=1, role=role, channel=channel)
    await cog._add_xp(guild, member, 10, source="message", channel=channel)
    await cog._add_xp(guild, member, 10, source="voice", channel=None)
    clock[0] += 120
    await cog._add_xp(guild, member, 10, source="message", channel=channel)
    data = await cog.config.guild(guild).period_xp()
    assert data["days"]["2026-09-30"][str(member.id)] == 40
    assert await cog._get_xp(guild, member.id) == 40
    await cog.season_start.callback(cog, ctx, name="Autumn")
    assert not (await cog.config.guild(guild).period_xp())["season"]
    assert await cog._get_xp(guild, member.id) == 40
    export = json.load((await cog.red_get_data_for_user(user_id=member.id))["levelplus.json"])
    assert export[str(guild.id)]["periods"]["archives"][0]["xp"] == 40
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    assert not await cog.red_get_data_for_user(user_id=member.id)


def test_calendar_periods_respect_month_and_week_boundaries():
    timestamp = datetime(2026, 10, 1, 4, 30, tzinfo=timezone.utc).timestamp()
    day = day_at(timestamp, "America/Chicago")
    assert day.isoformat() == "2026-09-30"
    data = {
        "days": {"2026-09-01": {"1": 4}, "2026-09-28": {"1": 6}, "2026-10-01": {"1": 99}},
        "season": {"1": 200},
    }
    assert period_totals(data, "week", day) == {"1": 6}
    assert period_totals(data, "month", day) == {"1": 10}
    assert period_totals(data, "season", day) == {"1": 200}


async def test_repeated_messages_and_reaction_farming_can_be_disabled(bot, guild):
    cog = LevelPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    group = cog.config.guild(guild)
    await group.message.cooldown.set(0)
    await group.reaction.cooldown.set(0)
    await group.xp_features.repeat_seconds.set(120)
    await group.xp_features.reaction_once.set(True)
    for text in ("Hello world!", "hello WORLD", "another message"):
        await cog.on_message(make_message(member, channel, content=text))
    assert await cog._get_xp(guild, member.id) == 2
    payload = SimpleNamespace(
        guild_id=guild.id, channel_id=channel.id, user_id=member.id, member=member, message_id=444
    )
    channel.fetch_message.side_effect = discord.NotFound(
        SimpleNamespace(status=404, reason="Missing"), "Missing"
    )
    await cog.on_raw_reaction_add(payload)
    await cog.on_raw_reaction_add(payload)
    assert await cog._get_xp(guild, member.id) == 27


async def test_reward_roles_follow_current_level_even_when_announcements_are_off(bot, guild):
    cog = LevelPlus(bot)
    member = make_member(guild)
    state = Mock()

    def role(uid, position, permissions="0"):
        return discord.Role(
            guild=guild,
            state=state,
            data={
                "id": str(uid),
                "name": f"role{uid}",
                "position": position,
                "permissions": permissions,
                "color": 0,
                "managed": False,
            },
        )

    low, high, bot_role = role(1001, 1), role(1002, 2), role(1003, 10)
    guild.me.top_role = bot_role
    guild.roles = [low, high, bot_role]
    guild.get_role.side_effect = lambda uid: next(
        (role for role in guild.roles if role.id == uid), None
    )
    member.add_roles = AsyncMock(side_effect=lambda *roles, **kw: member.roles.extend(roles))
    member.remove_roles = AsyncMock(
        side_effect=lambda *roles, **kw: [member.roles.remove(role) for role in roles]
    )
    ctx = make_context(guild, author=member)
    await cog.rewards_add.callback(cog, ctx, role=low, threshold=1)
    await cog.rewards_add.callback(cog, ctx, role=high, threshold=2)
    await cog.config.guild(guild).levelup.enabled.set(False)
    await cog._set_xp(guild, member.id, 1000)
    assert set(member.roles) == {low, high}
    await cog.rewards_stack.callback(cog, ctx, enabled=False)
    await cog._sync_rewards(member)
    assert member.roles == [high]
    await cog._set_xp(guild, member.id, 0)
    assert member.roles == []
