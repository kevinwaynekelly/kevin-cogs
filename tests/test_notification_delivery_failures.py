"""Failed background deliveries stay retryable and publish sanitized alert metadata."""

import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import make_channel, make_context, make_member

from communityplus import CommunityPlus
from exportplus import ExportPlus
from exportplus.cog import ExportJob
from introplus import IntroPlus
from introplus.cog import IntroRequest
from introplus.resolver import MediaError
from levelplus import LevelPlus
from logplus import LogPlus
from owoplus import OwoPlus

PRIVATE_ERROR = "private-channel-name signed-url?token=secret member-display-name"


def delivery_error(error_type=discord.Forbidden):
    status = 404 if error_type is discord.NotFound else 403
    return error_type(SimpleNamespace(status=status, reason="Delivery failed"), PRIVATE_ERROR)


def failure_records(caplog, stage):
    return [
        record for record in caplog.records if getattr(record, "notification_stage", None) == stage
    ]


def assert_safe_failure(caplog, stage, guild_id, error_type="Forbidden"):
    records = failure_records(caplog, stage)
    assert records
    for record in records:
        assert record.levelno == logging.WARNING
        assert record.notification_error == error_type
        assert record.notification_guild_id == guild_id
        assert record.exc_info is None
        assert PRIVATE_ERROR not in record.getMessage()
    assert PRIVATE_ERROR not in caplog.text


async def test_failed_event_notifications_remain_pending_and_retry_without_private_logs(
    bot, guild, caplog
):
    cog = CommunityPlus(bot)
    cog._now_ts = lambda: 1000
    channel, member = make_channel(guild), make_member(guild)
    ctx = make_context(guild, channel, member)
    ctx.send.return_value = SimpleNamespace(id=555, edit=AsyncMock())
    try:
        await cog.event_create.callback(cog, ctx, "Game night", "1600", 15)
        key = next(iter(await cog.config.guild(guild).social.events()))
        await cog.event_remind.callback(cog, ctx, key, True)
        channel.send.side_effect = delivery_error()
        member.send.side_effect = delivery_error()
        await cog._event_reminders(guild, key)
        row = await cog._social_record(guild, "events", key)
        assert not row["announced"] and not row["notified"]
        assert_safe_failure(caplog, "event_announcement", guild.id)
        assert_safe_failure(caplog, "event_reminder", guild.id)

        channel.send.side_effect = member.send.side_effect = None
        await cog._event_reminders(guild, key)
        await cog._event_reminders(guild, key)
        row = await cog._social_record(guild, "events", key)
        assert row["announced"] and row["notified"] == [str(member.id)]
        assert channel.send.await_count == member.send.await_count == 2
    finally:
        await cog.cog_unload()


async def test_failed_level_notice_preserves_earned_xp(bot, guild, caplog):
    cog = LevelPlus(bot)
    channel, member = make_channel(guild), make_member(guild)
    await cog.config.guild(guild).levelup.enabled.set(True)
    await cog.config.guild(guild).levelup.channel_id.set(channel.id)
    await cog.config.guild(guild).xp.set({str(member.id): 321})
    channel.send.side_effect = delivery_error()
    try:
        await cog.maybe_announce_levelup(guild, member, 0, 1)
        assert await cog._get_xp(guild, member.id) == 321
        assert_safe_failure(caplog, "level_up_announcement", guild.id)
    finally:
        await cog.cog_unload()


async def test_failed_log_notification_keeps_existing_delivery_accounting(bot, guild, caplog):
    cog = LogPlus(bot)
    channel = make_channel(guild)
    await cog.config.guild(guild).log_channel.set(channel.id)
    await cog.config.guild(guild).features.retry.set(False)
    channel.send.side_effect = delivery_error()
    try:
        await cog._send(guild, await cog._E(guild, "Joined", etype="member_joined"))
        assert cog._delivery_status[guild.id]["failures"] == 1
        assert cog._delivery_status[guild.id]["dropped"] == 1
        assert not cog._retry_tasks
        assert_safe_failure(caplog, "log_delivery", guild.id)
    finally:
        await cog.cog_unload()


async def test_failed_intro_reports_and_advances_the_queue(
    bot, guild, monkeypatch, tmp_path, caplog
):
    monkeypatch.setattr("introplus.cog.cog_data_path", lambda cog: tmp_path / "IntroPlus")
    cog = IntroPlus(bot)
    first = IntroRequest(guild.id, 111, 111, 456, {}, time.monotonic())
    second = IntroRequest(guild.id, 222, 222, 456, {}, time.monotonic())
    cog._queues[guild.id].extend([first, second])
    cog._pending.update({(guild.id, 111), (guild.id, 222)})
    cog._play = AsyncMock(side_effect=[MediaError(PRIVATE_ERROR), None])
    try:
        await cog._drain(guild.id)
        assert cog._play.await_count == 2
        assert not cog._pending and not cog._queues and not cog._active
        assert_safe_failure(caplog, "intro_playback", guild.id, "MediaError")
    finally:
        await cog.cog_unload()


@pytest.mark.parametrize("deleted", [False, True])
async def test_export_progress_failure_reports_except_deleted_control(
    bot, guild, monkeypatch, tmp_path, caplog, deleted
):
    monkeypatch.setattr("exportplus.cog.cog_data_path", lambda cog: tmp_path / "ExportPlus")
    cog = ExportPlus(bot)
    ctx = make_context(guild, make_channel(guild), make_member(guild))
    job = ExportJob(ctx, tmp_path / "job", None, discord.utils.utcnow(), True, True)
    job.progress = SimpleNamespace(
        edit=AsyncMock(
            side_effect=delivery_error(discord.NotFound if deleted else discord.Forbidden)
        )
    )
    try:
        await cog._progress(job, force=True)
        assert job.state == "running"
        if deleted:
            assert not failure_records(caplog, "export_progress")
        else:
            assert_safe_failure(caplog, "export_progress", guild.id)
    finally:
        await cog.cog_unload()


async def test_poetry_winner_failure_stays_unannounced_until_delivery_succeeds(bot, guild, caplog):
    cog = OwoPlus(bot)
    channel = make_channel(guild)
    ctx = make_context(guild, channel, make_member(guild))
    try:
        await cog.haikucontest_create.callback(cog, ctx, 1, title="Autumn")
        group = cog.config.guild(guild).poetry
        key = next(iter((await group())["contests"]))
        async with group() as data:
            data["contests"][key]["ends"] = time.time() - 1
        channel.send.side_effect = delivery_error()
        await cog._poetry_tick(guild)
        assert not (await group())["contests"][key]["announced"]
        assert_safe_failure(caplog, "poetry_winner", guild.id)

        channel.send.side_effect = None
        await cog._poetry_tick(guild)
        await cog._poetry_tick(guild)
        assert (await group())["contests"][key]["announced"]
        assert channel.send.await_count == 2
    finally:
        await cog.cog_unload()
