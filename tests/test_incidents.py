"""Retry-independent moderation counts, burst limits, private errors and cases."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_channel, make_context, make_member

from logplus import LogPlus
from logplus.delivery import PendingLog


async def test_burst_threshold_and_cooldown_are_bounded(bot):
    cog = LogPlus(bot)
    key = (1, "joins")
    assert not cog._window_hit(key, now=0, window=60, threshold=3)
    assert not cog._window_hit(key, now=1, window=60, threshold=3)
    assert cog._window_hit(key, now=2, window=60, threshold=3)
    assert not cog._window_hit(key, now=3, window=60, threshold=3)
    assert not cog._window_hit(key, now=100, window=60, threshold=3)
    for index in range(300):
        cog._window_hit((index, "joins"), now=200, window=60, threshold=3)
    assert len(cog._alert_windows) <= 200


async def test_permissions_only_alerts_for_permission_changes(bot, guild):
    cog = LogPlus(bot)
    await cog.config.guild(guild).alert_settings.bursts.permissions.set(
        {"enabled": True, "threshold": 2, "window": 60}
    )
    cog._alert_channel = AsyncMock()
    record = PendingLog([], None, "role_updated", "server")
    await cog._observe_log(guild, record)
    await cog._observe_log(guild, record)
    assert not cog._alert_tasks
    record.permission_change = True
    await cog._observe_log(guild, record)
    await cog._observe_log(guild, record)
    await asyncio.gather(*cog._alert_tasks)
    cog._alert_channel.assert_awaited_once()


async def test_digest_is_opt_in_and_sends_only_once_per_day(bot, guild):
    cog = LogPlus(bot)
    channel = make_channel(guild)
    now = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    group = cog.config.guild(guild)
    await group.alert_settings.channel.set(channel.id)
    await group.moderation_summary.days.set({"2026-09-30": {"member": 5}})
    await cog._moderation_tick(guild, now=now)
    channel.send.assert_not_awaited()
    await group.alert_settings.digest.set({"enabled": True, "timezone": "UTC", "hour": 9})
    await cog._moderation_tick(guild, now=now)
    await cog._moderation_tick(guild, now=now)
    channel.send.assert_awaited_once()
    assert "Member: 5" in channel.send.call_args.kwargs["embed"].description


async def test_error_notices_are_aggregated_and_owner_checked(bot, guild):
    cog = LogPlus(bot)
    owner = SimpleNamespace(id=888, send=AsyncMock())
    bot.get_user = lambda uid: owner
    bot.is_owner = AsyncMock(return_value=True)
    await cog.config.guild(guild).alert_settings.errors.set(
        {"enabled": True, "recipient": owner.id, "threshold": 2, "window": 300}
    )
    ctx = make_context(guild, author=make_member(guild))
    ctx.command = SimpleNamespace(qualified_name="play")
    error = RuntimeError("secret-token-that-must-not-be-sent")
    await cog.on_command_error(ctx, error)
    await cog.on_command_error(ctx, error)
    await asyncio.gather(*cog._alert_tasks)
    owner.send.assert_awaited_once()
    text = owner.send.call_args.kwargs["embed"].description
    assert "RuntimeError" in text and "secret-token" not in text
    bot.is_owner.return_value = False
    await cog._owner_error(guild, ("play", "RuntimeError"), 2, owner.id)
    assert owner.send.await_count == 1
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=owner.id)
    assert not (await cog.config.guild(guild).alert_settings())["errors"]["enabled"]


async def test_incident_resolution_and_user_deletion_remove_identified_cases(bot, guild):
    cog = LogPlus(bot)
    member = make_member(guild)
    ctx = make_context(guild, author=member)
    subject = make_member(guild, 777)
    await cog.incident_create.callback(cog, ctx, "Investigation", subject)
    key = next(iter(await cog.config.guild(guild).incident_cases()))
    await cog.incident_note.callback(cog, ctx, key, text="Reviewed with staff.")
    await cog.incident_resolve.callback(cog, ctx, key, resolution="Closed after review.")
    assert await cog._incident_user_data(subject.id)
    with pytest.raises(Exception, match="open case"):
        await cog.incident_note.callback(cog, ctx, key, text="Too late.")
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=subject.id)
    assert not await cog.config.guild(guild).incident_cases()
