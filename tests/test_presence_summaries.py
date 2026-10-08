"""Presence collection is hourly, bounded, ephemeral, and based on observations."""

import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import discord
import pytest

from logplus.presence import (
    MAX_ACTIVE_ACTIVITIES,
    MAX_ACTIVITY_LABELS,
    MAX_GUILD_MEMBERS,
    MAX_LABEL_LENGTH,
    MAX_MEMBERS,
    OTHER_ACTIVITIES,
    PresenceTracker,
)


def member(user_id=10, guild_id=123, *, status=discord.Status.online, activities=(), bot=False):
    return SimpleNamespace(
        id=user_id,
        guild=SimpleNamespace(id=guild_id),
        display_name="Kevin",
        status=status,
        activities=activities,
        bot=bot,
    )


def test_boundary_splits_status_and_activity_time_without_losing_change():
    tracker = PresenceTracker()
    user = member(activities=[discord.Game("First game")])
    tracker.observe(user, 3500)
    user.status = discord.Status.idle
    user.activities = [discord.Game("Second game")]
    tracker.observe(user, 3700)
    first = tracker.drain(3700)[0]
    assert (first.start, first.end, first.partial) == (3500, 3600, True)
    assert first.status_seconds["online"] == 100
    assert first.activity_seconds == {"Playing First game": 100}
    assert first.status_changes == 0
    second = tracker.drain(7200)[0]
    assert (second.start, second.end, second.partial) == (3600, 7200, False)
    assert second.status_seconds["online"] == 100
    assert second.status_seconds["idle"] == 3500
    assert second.activity_seconds == {"Playing First game": 100, "Playing Second game": 3500}
    assert second.status_changes == 1


def test_unchanged_presence_and_game_emit_every_hour_once():
    tracker = PresenceTracker()
    tracker.observe(member(activities=[discord.Game("Witcher")]), 0)
    assert not tracker.drain(3599)
    first = tracker.drain(3600)[0]
    assert first.status_seconds["online"] == 3600
    assert first.activity_seconds == {"Playing Witcher": 3600}
    assert not first.partial
    assert first.status_changes == 0
    assert not tracker.drain(3600)
    assert not tracker.drain(3601)
    second = tracker.drain(7200)[0]
    assert (second.start, second.end) == (3600, 7200)
    assert second.activity_seconds == {"Playing Witcher": 3600}
    assert not tracker.drain(7200)


def test_only_named_activity_types_are_collected_without_sensitive_details():
    activities = [
        discord.Game("Witcher"),
        discord.Streaming(name="Stream", url="https://private.invalid", details="Secret"),
        discord.Activity(type=discord.ActivityType.listening, name="Spotify", details="Song"),
        discord.Activity(type=discord.ActivityType.watching, name="Video", state="Private"),
        discord.Activity(type=discord.ActivityType.competing, name="Tournament"),
        discord.CustomActivity("Private custom status"),
    ]
    tracker = PresenceTracker()
    tracker.observe(member(activities=activities), 0)
    summary = tracker.drain(3600)[0]
    assert summary.activity_seconds == {
        "Playing Witcher": 3600,
        "Streaming Stream": 3600,
        "Listening Spotify": 3600,
        "Watching Video": 3600,
        "Competing Tournament": 3600,
    }
    exported = json.dumps(tracker.export_user(10))
    assert "private.invalid" not in exported and "Secret" not in exported
    assert "Private custom status" not in exported and "Song" not in exported


def test_rapid_status_changes_count_even_at_identical_timestamps():
    tracker = PresenceTracker()
    user = member()
    tracker.observe(user, 0)
    for status, now in (
        (discord.Status.idle, 100),
        (discord.Status.dnd, 100),
        (discord.Status.offline, 200),
        (discord.Status.online, 200),
        (discord.Status.online, 250),
    ):
        user.status = status
        tracker.observe(user, now)
    summary = tracker.drain(3600)[0]
    assert summary.status_changes == 4
    assert summary.status_seconds == {"online": 3500, "idle": 0, "dnd": 100, "offline": 0}
    assert sum(summary.status_seconds.values()) == 3600


def test_first_observation_is_partial_and_never_credits_earlier_time():
    tracker = PresenceTracker()
    tracker.observe(member(status=discord.Status.dnd), 1800.5)
    summary = tracker.drain(3600)[0]
    assert summary.partial and summary.start == 1800.5
    assert summary.status_seconds["dnd"] == 1799.5
    assert summary.status_changes == 0
    assert tracker.export_user(10)["123"]["observed_from"] == 1800.5


def test_quiet_offline_and_bots_do_not_consume_capacity_or_emit():
    tracker = PresenceTracker()
    tracker.observe(member(status=discord.Status.offline), 0)
    tracker.observe(member(user_id=11, bot=True), 0)
    tracker.observe(
        member(
            user_id=12, status=discord.Status.offline, activities=[discord.CustomActivity("Hi")]
        ),
        0,
    )
    assert len(tracker) == 0
    assert not tracker.drain(3600)
    user = member()
    tracker.observe(user, 3600)
    user.status = discord.Status.offline
    tracker.observe(user, 4000)
    summary = tracker.drain(7200)[0]
    assert summary.status_changes == 1
    assert summary.status_seconds == {"online": 400, "idle": 0, "dnd": 0, "offline": 3200}
    assert not tracker.drain(10800)


def test_status_change_at_boundary_is_part_of_new_hour():
    tracker = PresenceTracker()
    user = member()
    tracker.observe(user, 0)
    user.status = discord.Status.offline
    tracker.observe(user, 3600)
    first = tracker.drain(3600)[0]
    assert first.status_changes == 0 and first.status_seconds["online"] == 3600
    second = tracker.drain(7200)[0]
    assert second.status_changes == 1 and second.status_seconds["offline"] == 3600
    assert not tracker.drain(10800)


def test_activity_limit_preserves_time_in_bounded_overflow_bucket():
    tracker = PresenceTracker()
    user = member(activities=[discord.Game("Game 0")])
    tracker.observe(user, 0)
    for index in range(1, 25):
        user.activities = [discord.Game(f"Game {index}")]
        tracker.observe(user, index * 10)
    summary = tracker.drain(3600)[0]
    assert len(summary.activity_seconds) == MAX_ACTIVITY_LABELS
    assert OTHER_ACTIVITIES in summary.activity_seconds
    assert summary.activity_seconds["Playing Game 0"] == 10
    assert sum(summary.activity_seconds.values()) == 3600
    next_hour = tracker.drain(7200)[0]
    assert next_hour.activity_seconds == {"Playing Game 24": 3600}


def test_activity_labels_and_simultaneous_activity_count_are_bounded():
    tracker = PresenceTracker()
    games = [discord.Game("x" * 1000)] + [discord.Game(f"Game {i}") for i in range(10)]
    tracker.observe(member(activities=[games[0], games[0], *games[1:]]), 0)
    summary = tracker.drain(3600)[0]
    assert len(summary.activity_seconds) == MAX_ACTIVE_ACTIVITIES
    assert max(map(len, summary.activity_seconds)) == MAX_LABEL_LENGTH
    assert sum(summary.activity_seconds.values()) == MAX_ACTIVE_ACTIVITIES * 3600


def test_member_and_guild_limits_release_capacity_when_removed():
    tracker = PresenceTracker(max_members=3, max_guild_members=2)
    for user_id in (1, 2, 3):
        tracker.observe(member(user_id), 0)
    tracker.observe(member(4, guild_id=456), 0)
    tracker.observe(member(5, guild_id=456), 0)
    assert len(tracker) == 3
    assert not tracker.export_user(3) and not tracker.export_user(5)
    tracker.drop_member(123, 1)
    tracker.observe(member(3), 0)
    assert len(tracker) == 3 and tracker.export_user(3)
    tracker.drop_guild(123)
    tracker.observe(member(5, guild_id=456), 0)
    assert len(tracker) == 2 and tracker.export_user(5)
    assert PresenceTracker(max_members=100000).max_members == MAX_MEMBERS
    assert PresenceTracker(max_guild_members=100000).max_guild_members == MAX_GUILD_MEMBERS


def test_rotating_online_cohorts_reuse_quiet_offline_capacity():
    tracker = PresenceTracker(max_members=2, max_guild_members=2)
    first_cohort = [member(1), member(2)]
    for user in first_cohort:
        tracker.observe(user, 0)
        user.status = discord.Status.offline
        tracker.observe(user, 100)
    # Undrained online time and transitions cannot be evicted for a newcomer.
    tracker.observe(member(3), 200)
    assert not tracker.export_user(3)
    summaries = tracker.drain(3600)
    assert len(summaries) == 2 and all(tracker.is_current(row) for row in summaries)
    for user_id in (3, 4):
        tracker.observe(member(user_id), 3601)
    assert len(tracker) == 2
    assert not tracker.export_user(1) and not tracker.export_user(2)
    assert tracker.export_user(3) and tracker.export_user(4)
    assert not any(tracker.is_current(row) for row in summaries)
    assert {row.user_id for row in tracker.drain(7200)} == {3, 4}


def test_quiet_completed_hour_releases_offline_slots_without_losing_transition():
    tracker = PresenceTracker(max_members=1)
    user = member()
    tracker.observe(user, 0)
    user.status = discord.Status.offline
    tracker.observe(user, 100)
    transition = tracker.drain(3600)[0]
    assert tracker.is_current(transition) and len(tracker) == 1
    assert not tracker.drain(3601)
    assert tracker.is_current(transition)
    assert not tracker.drain(7200)
    assert len(tracker) == 0 and not tracker.is_current(transition)
    tracker.observe(member(2), 7201)
    assert len(tracker) == 1 and tracker.export_user(2)


def test_global_admission_evicts_quiet_offline_member_from_another_guild():
    tracker = PresenceTracker(max_members=1)
    user = member(guild_id=123)
    tracker.observe(user, 0)
    user.status = discord.Status.offline
    tracker.observe(user, 100)
    tracker.drain(3600)
    tracker.observe(member(2, guild_id=456), 3601)
    assert len(tracker) == 1
    assert not tracker.export_user(user.id)
    assert tracker.export_user(2)


def test_full_active_guild_does_not_evict_unrelated_offline_guild_for_rejected_user():
    tracker = PresenceTracker(max_members=3, max_guild_members=2)
    for user_id in (1, 2):
        tracker.observe(member(user_id), 0)
    offline = member(3, guild_id=456)
    tracker.observe(offline, 0)
    offline.status = discord.Status.offline
    tracker.observe(offline, 100)
    tracker.drain(3600)
    tracker.observe(member(4), 3601)
    assert len(tracker) == 3
    assert tracker.export_user(3) and not tracker.export_user(4)


def test_delayed_drain_returns_only_latest_hour_without_historical_backfill():
    tracker = PresenceTracker()
    tracker.observe(member(activities=[discord.Game("Witcher")]), 1800)
    summaries = tracker.drain(18000 + 100)
    assert len(summaries) == 1
    summary = summaries[0]
    assert (summary.start, summary.end, summary.partial) == (14400, 18000, False)
    assert summary.status_seconds["online"] == 3600
    assert summary.activity_seconds["Playing Witcher"] == 3600
    assert not tracker.drain(18000 + 101)


def test_summaries_are_immutable_and_privacy_deletion_invalidates_drained_data():
    tracker = PresenceTracker()
    tracker.observe(member(), 0)
    tracker.observe(member(guild_id=456), 0)
    summary = tracker.drain(3600)[0]
    assert tracker.is_current(summary)
    with pytest.raises(FrozenInstanceError):
        summary.name = "Changed"
    with pytest.raises(TypeError):
        summary.status_seconds["online"] = 0
    with pytest.raises(TypeError):
        summary.activity_seconds["Playing Private"] = 50
    exported = tracker.export_user(10)
    json.dumps(exported)
    exported["123"]["current_hour"]["status_seconds"]["online"] = -1
    assert tracker.export_user(10)["123"]["current_hour"]["status_seconds"]["online"] == 0
    tracker.drop_user(10)
    assert not tracker.export_user(10) and not tracker.is_current(summary)
    tracker.observe(member(), 3600)
    assert not tracker.is_current(summary)
    newer = tracker.drain(7200)[0]
    tracker.clear()
    tracker.observe(member(), 7200)
    assert not tracker.is_current(newer)
    assert len(tracker) == 1


def test_backward_timestamps_do_not_change_observed_state_or_send_future_window():
    tracker = PresenceTracker()
    user = member()
    tracker.observe(user, 100)
    user.status = discord.Status.offline
    tracker.observe(user, 50)
    assert tracker.export_user(10)["123"]["status"] == "online"
    tracker.observe(member(), 3700)
    assert not tracker.drain(3500)
    assert tracker.drain(3700)[0].status_seconds["online"] == 3500
    for invalid in (float("inf"), float("nan")):
        with pytest.raises(ValueError):
            tracker.drain(invalid)
