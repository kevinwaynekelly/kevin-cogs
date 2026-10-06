"""Audio failures reach the host outbox independently of Discord delivery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_context
from test_audio_watchdog import monitor_runtime as monitor_fixture

from audioplus import AudioPlus
from audioplus.resolver import MediaError

monitor_runtime = monitor_fixture


class NotificationPlus:
    def __init__(self):
        self.report = AsyncMock()


async def test_daily_failure_is_recorded_once_even_when_its_dm_retries(monitor_runtime):
    cog, monitor, now = monitor_runtime
    notifier = NotificationPlus()
    cog.bot.get_cog = lambda name: notifier if name == "NotificationPlus" else None
    monitor.report_failure = cog._report_check_failure
    monitor.probe.side_effect = MediaError("YouTube extraction failed.")
    monitor.notify.side_effect = RuntimeError("DM unavailable")
    await monitor.check()
    notifier.report.assert_awaited_once_with(
        "AudioPlus", "Daily playback check", guild_id=cog.bot.guilds[0].id, error=None, kind="daily"
    )
    state = await cog.config.watchdog()
    assert state["pending_alert"] and state["last_alert_error"]
    async with cog.config.all() as data:
        data["watchdog"]["alert_retry_at"] = 0
    await monitor.tick()
    assert monitor.notify.await_count == 2
    assert notifier.report.await_count == 1


async def test_playback_failure_without_context_still_reports(bot, guild):
    cog = AudioPlus(bot)
    notifier = NotificationPlus()
    bot.get_cog = lambda name: notifier if name == "NotificationPlus" else None
    player = SimpleNamespace(guild=guild, context=None)
    cog._players[guild.id] = player
    await cog._report_playback_failure(player, None, "Secret signed stream URL")
    notifier.report.assert_awaited_once_with(
        "AudioPlus", "Native playback", guild_id=guild.id, error=None, kind="playback"
    )


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("PyNaCl could not be imported by Red.", True),
        ("The media source could not be loaded.", True),
        ("Provide search terms or a media URL.", False),
        ("Too many media lookups are pending. Try again shortly.", False),
    ],
)
async def test_setup_errors_report_but_input_and_overload_do_not(bot, guild, reason, expected):
    cog = AudioPlus(bot)
    notifier = NotificationPlus()
    bot.get_cog = lambda name: notifier if name == "NotificationPlus" else None
    cog._presentation.command_error = AsyncMock()
    error = MediaError(reason)
    ctx = make_context(guild)
    await cog.cog_command_error(ctx, error)
    assert bool(notifier.report.await_count) is expected
    cog._presentation.command_error.assert_awaited_once_with(ctx, error)


async def test_alert_storage_failure_does_not_hide_playback_reply(bot, guild):
    cog = AudioPlus(bot)
    notifier = NotificationPlus()
    notifier.report.side_effect = OSError("private")
    bot.get_cog = lambda name: notifier if name == "NotificationPlus" else None
    player = SimpleNamespace(guild=guild, context=make_context(guild))
    cog._players[guild.id] = player
    cog._reply = AsyncMock()
    track = SimpleNamespace(title="Song")
    await cog._report_playback_failure(player, track, "Playback failed.")
    cog._reply.assert_awaited_once()
