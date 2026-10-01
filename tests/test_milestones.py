"""Challenge rewards honor source policies, weekly boundaries, caps, and erasure."""

import asyncio
import io
import json
from datetime import datetime, timezone

import pytest
from conftest import make_channel, make_context, make_member
from PIL import Image
from redbot.core import commands

from levelplus import LevelPlus
from levelplus.rankcards import render_card


async def setup_goals(cog, guild, *, target=2, reward=30):
    async with cog.config.guild(guild).milestone_settings() as data:
        data["challenges"] = True
        data["goals"]["message"] = {"target": target, "reward": reward}


async def test_concurrent_earned_rewards_are_paid_once_without_progress_recursion(bot, guild):
    cog, member = LevelPlus(bot), make_member(guild)
    await setup_goals(cog, guild)
    await asyncio.gather(*(cog._add_xp(guild, member, 10, source="message") for _ in range(10)))
    row = await cog.config.guild(guild).milestones.get_raw(str(member.id))
    assert await cog._get_xp(guild, member.id) == 130
    assert row["counts"] == {"message": 10, "xp": 100}
    assert row["completed"] == ["message"] and row["pending"] == 0
    assert row["earned_xp"] == 130 and "first" in row["badges"]
    await cog._add_xp(guild, member, 999)
    assert (await cog.config.guild(guild).milestones.get_raw(str(member.id))) == row
    export = json.load((await cog.red_get_data_for_user(user_id=member.id))["levelplus.json"])
    assert export[str(guild.id)]["milestones"] == row
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=member.id)
    assert not await cog.red_get_data_for_user(user_id=member.id)


async def test_caps_defer_unpaid_rewards_across_reload_and_days(bot, guild, monkeypatch):
    cog, member = LevelPlus(bot), make_member(guild)
    now = [datetime(2026, 10, 1, 17, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr("levelplus.cog.time.time", lambda: now[0])
    await setup_goals(cog, guild, target=1, reward=5)
    await cog.config.guild(guild).xp_features.daily_cap.set(8)
    await cog._add_xp(guild, member, 6, source="message")
    assert await cog._get_xp(guild, member.id) == 8
    assert (await cog.config.guild(guild).milestones.get_raw(str(member.id)))["pending"] == 3
    replacement = LevelPlus(bot)
    await replacement._add_xp(guild, member, 10, source="voice")
    assert await replacement._get_xp(guild, member.id) == 8
    now[0] += 86400
    await replacement._add_xp(guild, member, 1, source="message")
    assert await replacement._get_xp(guild, member.id) == 12
    assert (await replacement.config.guild(guild).milestones.get_raw(str(member.id)))[
        "pending"
    ] == 0
    assert (await replacement.config.guild(guild).earned_today())["xp"][str(member.id)] == 4


async def test_new_week_resets_counts_and_reward_is_not_boosted(bot, guild, monkeypatch):
    cog, member = LevelPlus(bot), make_member(guild)
    now = [datetime(2026, 10, 5, 4, 59, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr("levelplus.cog.time.time", lambda: now[0])
    await setup_goals(cog, guild, target=1, reward=10)
    await cog.config.guild(guild).xp_features.boosts.set(
        [{"factor": 2, "role": None, "channel": None, "expires": now[0] + 1000}]
    )
    await cog._add_xp(guild, member, 5, source="message")
    first = await cog.config.guild(guild).milestones.get_raw(str(member.id))
    assert first["week"] == "2026-09-28" and first["earned_xp"] == 20
    now[0] += 120
    await cog._add_xp(guild, member, 5, source="message")
    row = await cog.config.guild(guild).milestones.get_raw(str(member.id))
    assert row["week"] == "2026-10-05" and row["counts"]["message"] == 1
    assert await cog._get_xp(guild, member.id) == 40


async def test_badges_retain_original_dates_and_disable_freezes_progress(bot, guild):
    cog, member = LevelPlus(bot), make_member(guild)
    ctx = make_context(guild, make_channel(guild), member)
    await cog._add_xp(guild, member, 10000, source="voice")
    row = await cog.config.guild(guild).milestones.get_raw(str(member.id))
    assert {"first", "xp1000", "xp10000", "level5"} <= row["badges"].keys()
    await cog._add_xp(guild, member, 1, source="voice")
    assert (await cog.config.guild(guild).milestones.get_raw(str(member.id)))["badges"] == row[
        "badges"
    ]
    await cog.badge_setting.callback(cog, ctx, False)
    saved = await cog.config.guild(guild).milestones.get_raw(str(member.id))
    await cog._add_xp(guild, member, 999, source="message")
    assert await cog.config.guild(guild).milestones.get_raw(str(member.id)) == saved
    for args in [("unknown", 2, 10), ("message", 0, 10), ("xp", 10, -1)]:
        with pytest.raises(commands.BadArgument):
            await cog.challenge_goal.callback(cog, ctx, *args)


@pytest.mark.parametrize(
    "xp,lower,upper", [(0, 0, 100), (50, 0, 100), (100, 100, None), (10**100, 0, 10**101)]
)
def test_rank_png_size_palette_and_progress_pixels(xp, lower, upper):
    png = render_card("Kevin Kelly", 12, xp, lower, upper, 2, 4)
    image = Image.open(io.BytesIO(png))
    assert image.size == (900, 340) and image.format == "PNG"
    assert image.getpixel((20, 80)) == (129, 140, 248)
    fraction = 1 if upper is None else max(0, xp - lower) / (upper - lower)
    assert image.getpixel((490, 169)) == ((129, 140, 248) if fraction >= 0.5 else (52, 56, 78))
    assert len(png) < 150000
