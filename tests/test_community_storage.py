"""Bound game catalogs without erasing useful totals or racing member writers."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from conftest import make_channel, make_member, make_message

from communityplus import CommunityPlus
from communityplus.activity import MAX_GAME_NAME_LENGTH, MAX_GAME_NAMES, bounded_games, record_game


def test_catalog_keeps_recent_games_and_merges_truncated_names():
    names = {f"Game {i}": i + 1 for i in range(MAX_GAME_NAMES)}
    names = record_game(names, "Game 0")
    names = record_game(names, "New game")
    assert len(names) == MAX_GAME_NAMES
    assert names["Game 0"] == 2 and "Game 1" not in names
    long = "A" * MAX_GAME_NAME_LENGTH
    names = bounded_games({long + "1": 4, long + "2": 5, "": 10})
    assert names == {long: 9}
    names = record_game(names, long + "3")
    assert names == {long: 10}


async def test_concurrent_launches_are_bounded_but_lifetime_totals_stay_exact(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    before = SimpleNamespace(activities=[])
    afters = [make_member(guild, member.id) for _ in range(MAX_GAME_NAMES + 25)]
    for index, after in enumerate(afters):
        after.activities = [
            SimpleNamespace(type=discord.ActivityType.playing, name=f"Game {index}")
        ]
    await asyncio.gather(*(cog._handle_presence_update_logic(before, after) for after in afters))
    record = await cog.config.member(member).all()
    assert len(record["activity_names"]) == MAX_GAME_NAMES
    assert record["stats"]["game_launches"] == MAX_GAME_NAMES + 25
    assert record["stats"]["activity_starts"]["playing"] == MAX_GAME_NAMES + 25
    assert sum(record["activity_names"].values()) == MAX_GAME_NAMES


async def test_reload_migration_preserves_other_member_records_and_is_idempotent(bot, guild):
    cog = CommunityPlus(bot)
    group = cog.config.member_from_ids(guild.id + 1, 456)  # Departed/uncached member.
    await group.activity_names.set({f"Game {i}": i for i in range(MAX_GAME_NAMES + 5)})
    await group.sticky_roles.set([789])
    await group.stats.messages.set(123)
    await group.stats.game_launches.set(12345)
    await group.celebrations.birthday.set([10, 4])
    before = await group.all()
    await cog._prune_game_catalogs()
    after = await group.all()
    assert len(after["activity_names"]) == MAX_GAME_NAMES
    assert "Game 0" not in after["activity_names"]
    before.pop("activity_names")
    after.pop("activity_names")
    assert before == after
    saved = deepcopy(await group.all())
    driver = cog.config._driver
    original = driver.set
    driver.set = AsyncMock(wraps=original)
    try:
        await cog._prune_game_catalogs()
        driver.set.assert_not_awaited()
        assert await group.all() == saved
    finally:
        driver.set = original


async def test_migration_rechecks_under_member_lock_after_privacy_clear(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    group = cog.config.member(member)
    await group.activity_names.set({f"Game {i}": 1 for i in range(MAX_GAME_NAMES + 1)})
    async with group.get_lock():
        task = asyncio.create_task(cog._prune_game_catalogs())
        await asyncio.sleep(0)
        assert not task.done()
        await group.clear()
    await task
    assert await cog.config.all_members(guild) == {}


async def test_disabled_tracking_collects_no_message_or_game_data(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    await cog.config.guild(guild).seen.enabled.set(False)
    before = SimpleNamespace(activities=[])
    member.activities = [SimpleNamespace(type=discord.ActivityType.playing, name="Private game")]
    await cog.on_presence_update(before, member)
    await cog.on_message(make_message(member, channel))
    assert await cog.config.all_members() == {}


async def test_startup_applies_limit_even_when_cog_tracking_is_disabled(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    await cog.config.guild(guild).seen.enabled.set(False)
    await cog.config.member(member).activity_names.set(
        {f"Game {i}": 1 for i in range(MAX_GAME_NAMES + 1)}
    )
    await cog._restore_solo_timers()
    assert len(await cog.config.member(member).activity_names()) == MAX_GAME_NAMES
