"""Once-only custom goals, capped streaks and monthly/lifetime separation."""

import asyncio
from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace

from conftest import make_channel, make_context, make_member

from levelplus import LevelPlus
from levelplus.progression import PROGRESS_SETTINGS, advance_progress, new_progress


def test_streak_bonus_is_once_per_local_day_resets_and_is_bounded():
    conf = deepcopy(PROGRESS_SETTINGS)
    conf.update(streak=True, daily_bonus=100, max_bonus=150)
    record = new_progress()
    for day in (date(2026, 10, 1), date(2026, 10, 1), date(2026, 10, 2)):
        advance_progress(record, conf, source="message", amount=10, day=day, level=1, now=1)
    assert record["streak"] == 2 and record["pending"] == 250
    advance_progress(
        record, conf, source="message", amount=10, day=date(2026, 10, 4), level=1, now=1
    )
    assert record["streak"] == 1 and record["pending"] == 350
    record["pending"] = 250000
    advance_progress(
        record, conf, source="message", amount=10, day=date(2026, 10, 5), level=1, now=1
    )
    assert record["pending"] == 250000


async def test_concurrent_custom_goal_rewards_once_and_respects_daily_cap(bot, guild):
    cog = LevelPlus(bot)
    member = make_member(guild)
    ctx = make_context(guild, author=member)
    await cog.achievement_create.callback(cog, ctx, "talker", "message", 3, 100)
    await cog.config.guild(guild).xp_features.daily_cap.set(50)
    cog._settings_cache.clear()
    await asyncio.gather(*(cog._add_xp(guild, member, 10, source="message") for _ in range(3)))
    assert await cog._get_xp(guild, member.id) == 50
    record = await cog.config.guild(guild).progress.get_raw(str(member.id))
    assert len(record["earned"]) == 1 and record["pending"] == 80
    await cog._add_xp(guild, member, 10, source="message")
    assert await cog._get_xp(guild, member.id) == 50
    assert (await cog.config.guild(guild).progress.get_raw(str(member.id)))["pending"] == 80
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=member.id)
    assert not await cog.config.guild(guild).progress()


async def test_monthly_rollover_archives_winners_and_preserves_lifetime(bot, guild):
    cog = LevelPlus(bot)
    channel = make_channel(guild)
    group = cog.config.guild(guild)
    await group.progress_settings.monthly.set(True)
    await group.progress_settings.announce_channel.set(channel.id)
    await group.xp.set({"1": 5000})
    await group.season_calendar.month.set("2026-09")
    await group.period_xp.season_name.set("September 2026")
    await group.period_xp.season.set({"1": 123, "2": 50})
    await cog._season_tick(guild, now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp())
    data = await group.period_xp()
    assert data["season_name"] == "October 2026" and not data["season"]
    assert data["archives"][-1]["xp"] == {"1": 123, "2": 50}
    assert await group.xp() == {"1": 5000}
    channel.send.assert_awaited_once()
    await cog._season_tick(guild, now=datetime(2026, 10, 2, 12, tzinfo=timezone.utc).timestamp())
    assert channel.send.await_count == 1
    assert len((await group.period_xp())["archives"]) == 1


async def test_role_board_filters_current_role_members(bot, guild):
    cog = LevelPlus(bot)
    member = make_member(guild)
    role = SimpleNamespace(members=[member], mention="role")
    ctx = make_context(guild, author=member)
    await cog.config.guild(guild).xp.set({str(member.id): 10, "777": 9999})
    await cog.role_leaderboard.callback(cog, ctx, role)
    text = ctx.send.call_args.kwargs["embed"].description
    assert "10 XP" in text and "777" not in text
