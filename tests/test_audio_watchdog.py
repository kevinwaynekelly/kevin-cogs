"""Check daily scheduling, private alerts, and real silent native audio probes."""

import asyncio
import io
import json
import wave
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from aiohttp import web
from conftest import forbidden, make_channel
from redbot.core import commands
from redbot.core._cli import parse_cli_flags
from redbot.core._events import init_events
from test_audio_hybrid import red_command_runtime as command_fixture
from test_native_audio import FakeVoice, eventually

import audioplus.watchdog as watchdog_module
from audioplus import AudioPlus
from audioplus.backend import require_voice
from audioplus.resolver import MediaError, Stream, Track
from audioplus.watchdog import (
    DEFAULT_WATCHDOG,
    CheckResult,
    PlaybackWatchdog,
    check_due,
    schedule_time,
    youtube_video,
)

red_command_runtime = command_fixture


@pytest.fixture
async def watchdog_commands(red_command_runtime, monkeypatch):
    bot, cog, member, invoke = red_command_runtime
    init_events(bot, parse_cli_flags([]))
    monkeypatch.setattr(bot, "_delete_delay", AsyncMock())
    yield bot, cog, member, invoke


@pytest.fixture
async def monitor_runtime(bot, guild):
    cog = AudioPlus(bot)
    monitor = cog._watchdog
    now = [datetime(2026, 10, 1, 14, tzinfo=timezone.utc)]
    monitor.clock = lambda: now[0]
    monitor.probe = AsyncMock(return_value=CheckResult("ok", "Playback works."))
    monitor.notify = AsyncMock()
    await monitor.configure(enabled=True, recipient_id=888, guild_id=guild.id)
    try:
        yield cog, monitor, now
    finally:
        await cog.cog_unload()


@pytest.mark.parametrize(
    "instant,due",
    [
        ("2026-10-01T13:59:00+00:00", False),
        ("2026-10-01T14:00:00+00:00", True),
        ("2026-12-01T14:59:00+00:00", False),
        ("2026-12-01T15:00:00+00:00", True),
        ("2026-03-08T13:59:00+00:00", False),
        ("2026-03-08T14:00:00+00:00", True),
        ("2026-11-01T14:59:00+00:00", False),
        ("2026-11-01T15:00:00+00:00", True),
    ],
)
def test_local_daily_time_tracks_daylight_saving_changes(instant, due):
    state = {**DEFAULT_WATCHDOG, "enabled": True}
    assert check_due(state, datetime.fromisoformat(instant)) is due


@pytest.mark.parametrize(
    "value,zone", [("25:00", "UTC"), ("09:60", "UTC"), ("9:00", "UTC"), ("09:00", "Bad/Zone")]
)
def test_schedule_rejects_invalid_time_or_timezone(value, zone):
    with pytest.raises(commands.BadArgument, match="Use"):
        schedule_time(value, zone)


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/playlist?list=x",
        "https://youtube.com/channel/x",
        "https://evil.invalid/watch?v=YE7VzlLtp-4",
        "https://user:secret@youtube.com/watch?v=YE7VzlLtp-4",
    ],
)
def test_test_video_rejects_nonvideo_sources_and_credentials(url):
    with pytest.raises((commands.BadArgument, MediaError), match="Use"):
        youtube_video(url)


def test_video_links_are_canonicalized_and_drop_playlist_or_tracking_arguments():
    assert (
        youtube_video("https://youtu.be/YE7VzlLtp-4?list=private") == DEFAULT_WATCHDOG["video_url"]
    )
    assert (
        youtube_video("https://www.youtube.com/watch?v=YE7VzlLtp-4&list=private")
        == DEFAULT_WATCHDOG["video_url"]
    )


async def test_checks_once_per_local_day_and_keep_cursor_after_reload(monitor_runtime, bot):
    cog, monitor, now = monitor_runtime
    await monitor.tick()
    await monitor.tick()
    monitor.probe.assert_awaited_once()
    monitor.notify.assert_not_awaited()
    assert (await cog.config.watchdog())["last_check_day"] == "2026-10-01"
    await monitor.close()
    replacement = AudioPlus(bot)
    replacement._watchdog.clock = monitor.clock
    replacement._watchdog.probe = monitor.probe
    replacement._watchdog.notify = monitor.notify
    try:
        await replacement._watchdog.tick()
        monitor.probe.assert_awaited_once()
        now[0] += timedelta(days=1)
        await replacement._watchdog.tick()
        assert monitor.probe.await_count == 2
    finally:
        await replacement.cog_unload()


async def test_deferred_check_retries_without_failure_dm(monitor_runtime):
    cog, monitor, now = monitor_runtime
    monitor.probe.side_effect = [
        CheckResult("deferred", "Voice is busy."),
        CheckResult("ok", "Ready."),
    ]
    await monitor.tick()
    assert (await cog.config.watchdog())["last_check_day"] is None
    now[0] += timedelta(seconds=899)
    await monitor.tick()
    monitor.probe.assert_awaited_once()
    now[0] += timedelta(seconds=1)
    await monitor.tick()
    assert monitor.probe.await_count == 2
    monitor.notify.assert_not_awaited()
    assert (await cog.config.watchdog())["last_check_day"] == "2026-10-01"


@pytest.mark.parametrize(
    "error",
    [
        MediaError("YouTube authentication required."),
        RuntimeError("secret signed URL"),
        asyncio.TimeoutError(),
    ],
)
async def test_failed_check_sends_one_private_alert_and_stores_safe_result(monitor_runtime, error):
    cog, monitor, now = monitor_runtime
    monitor.probe.side_effect = error
    await monitor.tick()
    await monitor.tick()
    monitor.probe.assert_awaited_once()
    monitor.notify.assert_awaited_once()
    settings, result = monitor.notify.await_args.args
    assert settings["recipient_id"] == 888
    assert result["status"] == "failed"
    assert "secret" not in result["detail"]
    state = await cog.config.watchdog()
    assert not state["pending_alert"] and state["last_check_day"] == "2026-10-01"


async def test_undelivered_alert_survives_reload_and_retries_without_replaying(monitor_runtime):
    cog, monitor, now = monitor_runtime
    monitor.probe.side_effect = MediaError("YouTube denied access.")
    monitor.notify.side_effect = forbidden()
    await monitor.tick()
    state = await cog.config.watchdog()
    assert state["pending_alert"] and state["last_alert_error"]
    await monitor.close()
    retry = PlaybackWatchdog(
        cog.config, AsyncMock(), monitor.probe, monitor.notify, clock=monitor.clock
    )
    try:
        now[0] += timedelta(seconds=899)
        await retry.tick()
        monitor.notify.assert_awaited_once()
        now[0] += timedelta(seconds=1)
        monitor.notify.side_effect = None
        await retry.tick()
        assert monitor.notify.await_count == 2
        monitor.probe.assert_awaited_once()
        assert not (await cog.config.watchdog())["pending_alert"]
        await retry.tick()
        assert monitor.notify.await_count == 2
    finally:
        await retry.close()


async def test_manual_check_and_due_scheduler_cannot_duplicate_daily_probe(monitor_runtime):
    cog, monitor, now = monitor_runtime
    started, release = asyncio.Event(), asyncio.Event()

    async def probe(settings):
        started.set()
        await release.wait()
        return CheckResult("ok", "Ready.")

    monitor.probe.side_effect = probe
    manual = asyncio.create_task(monitor.check())
    await started.wait()
    scheduled = asyncio.create_task(monitor.tick())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(manual, scheduled)
    monitor.probe.assert_awaited_once()


async def test_disable_cancels_probe_and_pending_alerts(monitor_runtime):
    cog, monitor, now = monitor_runtime
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def probe(settings):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monitor.probe.side_effect = probe
    pending = asyncio.create_task(monitor.check())
    await started.wait()
    await monitor.configure(enabled=False)
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert cleaned.is_set()
    await monitor.tick()
    monitor.notify.assert_not_awaited()
    assert not (await cog.config.watchdog())["last_result"]


async def test_timeout_cleans_probe_and_scheduler_recovers_from_an_error(
    monitor_runtime, monkeypatch
):
    cog, monitor, now = monitor_runtime
    cleaned = asyncio.Event()

    async def never_finishes(settings):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monitor.probe.side_effect = never_finishes
    monkeypatch.setattr(watchdog_module, "PROBE_TIMEOUT", 0.01)
    result = await monitor.check()
    assert result.status == "failed" and cleaned.is_set()
    await monitor.configure(last_check_day=None)
    monitor.probe.side_effect = None
    monitor.ready = AsyncMock(side_effect=[RuntimeError("not ready"), None, None])
    monkeypatch.setattr(watchdog_module, "POLL_SECONDS", 0.01)
    monitor.start()
    await eventually(lambda: monitor.probe.await_count == 2)
    assert not monitor._task.done()


async def test_legacy_settings_upgrade_and_recipient_data_hooks(bot, guild):
    cog = AudioPlus(bot)
    await cog.config.host.set("legacy-host")
    await cog.config.password.set("legacy-secret")
    await cog.config.watchdog.clear()
    replacement = AudioPlus(bot)
    assert await replacement.config.watchdog() == DEFAULT_WATCHDOG
    assert await replacement.config.password() == "legacy-secret"
    await cog._watchdog.configure(enabled=True, recipient_id=888, guild_id=guild.id)
    assert await cog.red_get_data_for_user(user_id=999) == {}
    exported = await cog.red_get_data_for_user(user_id=888)
    raw = exported["audioplus.json"].getvalue()
    assert b"legacy-secret" not in raw
    assert json.loads(raw)["watchdog"]["recipient_id"] == 888
    await cog.red_delete_data_for_user(requester="user", user_id=888)
    assert await cog.config.watchdog() == DEFAULT_WATCHDOG
    assert (
        await cog.config.host() == "legacy-host" and await cog.config.password() == "legacy-secret"
    )
    assert await cog.red_get_data_for_user(user_id=888) == {}


@pytest.mark.parametrize("owner", [False, True])
async def test_real_prefix_setup_targets_requesting_owner_and_keeps_owner_checks(
    watchdog_commands, owner
):
    bot, cog, member, invoke = watchdog_commands
    if owner:
        bot.owner_ids.add(member.id)
    ctx = await invoke("!audiocheck enable")
    assert ctx.command_failed is (not owner)
    state = await cog.config.watchdog()
    assert state["enabled"] is owner
    assert state["recipient_id"] == (member.id if owner else None)
    assert member.send.await_count == int(owner)
    if owner:
        assert state["guild_id"] == member.guild.id
        await invoke("!audiocheck time 21:30 UTC")
        await invoke("!audiocheck video https://youtu.be/YE7VzlLtp-4?list=x")
        state = await cog.config.watchdog()
        assert (state["hour"], state["minute"], state["timezone"]) == (21, 30, "UTC")
        assert state["video_url"] == DEFAULT_WATCHDOG["video_url"]
        await invoke("!audiocheck disable")
        assert (await cog.config.watchdog())["enabled"] is False


async def test_enable_does_not_claim_success_if_setup_dm_is_blocked(watchdog_commands):
    bot, cog, member, invoke = watchdog_commands
    bot.owner_ids.add(member.id)
    member.send.side_effect = forbidden()
    ctx = await invoke("!audiocheck enable")
    assert ctx.command_failed
    assert (await cog.config.watchdog())["enabled"] is False
    assert "could not DM" in ctx.send.await_args.kwargs["embed"].description


@pytest.fixture
async def probe_runtime(bot, guild, monkeypatch):
    from test_security_media import allow_test_loopback

    allow_test_loopback(monkeypatch)
    require_voice()
    cog = AudioPlus(bot)
    channel = make_channel(guild, kind=discord.VoiceChannel)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x10\x00" * 64000)
    app = web.Application()

    async def media(request):
        return web.Response(body=buffer.getvalue(), content_type="audio/wav")

    app.router.add_get("/media", media)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/media"
    voice = object.__new__(discord.VoiceClient)
    voice.client = SimpleNamespace(loop=asyncio.get_running_loop())
    voice.channel = channel
    connected = [True]
    voice._connection = SimpleNamespace(
        is_connected=lambda: connected[0], ws=SimpleNamespace(speak=AsyncMock())
    )
    voice._player = None
    packets = []
    voice.send_audio_packet = lambda data, encode=False: packets.append(
        (data, voice.encoder.encode(data, 960) if encode else data)
    )

    async def disconnect(**kwargs):
        connected[0] = False
        voice.stop()
        if guild.voice_client is voice:
            guild.voice_client = None

    voice.disconnect = AsyncMock(side_effect=disconnect)

    async def connect(**kwargs):
        guild.voice_client = voice
        return voice

    channel.connect = AsyncMock(side_effect=connect)
    track = Track(DEFAULT_WATCHDOG["video_url"], "Test video", length=4000, source="youtube")
    cog._resolver.search = AsyncMock(return_value=[track])
    cog._resolver.resolve = AsyncMock(return_value=Stream(url))
    processes = []
    ffmpeg_audio = discord.FFmpegPCMAudio

    def decoder(*args, **kwargs):
        source = ffmpeg_audio(*args, **kwargs)
        processes.append(source._process)
        return source

    monkeypatch.setattr(discord, "FFmpegPCMAudio", decoder)
    settings = {
        **deepcopy(DEFAULT_WATCHDOG),
        "enabled": True,
        "guild_id": guild.id,
        "recipient_id": 888,
    }
    try:
        yield cog, guild, channel, voice, packets, settings, processes
    finally:
        await cog.cog_unload()
        await runner.cleanup()


async def test_probe_uses_real_ffmpeg_discord_audio_thread_and_opus_then_disconnects(probe_runtime):
    cog, guild, channel, voice, packets, settings, processes = probe_runtime
    result = await cog._probe_playback(settings)
    assert result.status == "ok"
    cog._resolver.search.assert_awaited_once_with(settings["video_url"])
    cog._resolver.resolve.assert_awaited_once()
    # Discord also sends encoded Opus silence packets at the end of its audio thread.
    pcm = [(data, opus) for data, opus in packets if len(data) == 3840]
    assert len(pcm) >= 150 and all(data == bytes(3840) and opus for data, opus in pcm)
    assert voice._player is None
    assert processes[-1].poll() is not None
    voice.disconnect.assert_awaited_once_with(force=True)
    assert guild.voice_client is None and not cog._players


async def test_probe_never_disturbs_an_existing_voice_connection(probe_runtime):
    cog, guild, channel, voice, packets, settings, processes = probe_runtime
    foreign = FakeVoice()
    guild.voice_client = foreign
    result = await cog._probe_playback(settings)
    assert result.status == "deferred"
    cog._resolver.search.assert_not_awaited()
    channel.connect.assert_not_awaited()
    foreign.disconnect.assert_not_awaited()


async def test_voice_becoming_busy_during_lookup_is_left_alone(probe_runtime):
    cog, guild, channel, voice, packets, settings, processes = probe_runtime
    tracks = cog._resolver.search.return_value
    foreign = FakeVoice()

    async def search(query):
        guild.voice_client = foreign
        return tracks

    cog._resolver.search.side_effect = search
    assert (await cog._probe_playback(settings)).status == "deferred"
    channel.connect.assert_not_awaited()
    foreign.disconnect.assert_not_awaited()


async def test_probe_decoder_failure_and_unload_reap_owned_resources(probe_runtime):
    cog, guild, channel, voice, packets, settings, processes = probe_runtime
    cog._resolver.resolve.return_value = Stream("http://127.0.0.1:1/unavailable")
    with pytest.raises(MediaError, match="FFmpeg"):
        await cog._probe_playback(settings)
    assert processes[-1].poll() is not None
    assert guild.voice_client is None


@pytest.mark.parametrize(
    "failure",
    ["unavailable_video", "missing_channel", "permissions", "connect", "resolution", "muted"],
)
async def test_actual_probe_failure_paths_alert_privately_and_release_voice(probe_runtime, failure):
    cog, guild, channel, voice, packets, settings, processes = probe_runtime
    settings["channel_id"] = channel.id
    if failure == "unavailable_video":
        cog._resolver.search.return_value = []
    elif failure == "missing_channel":
        settings["channel_id"] = 0xBAD
    elif failure == "permissions":
        channel.permissions_for.return_value = discord.Permissions.none()
    elif failure == "connect":
        channel.connect.side_effect = discord.ClientException("failed handshake")
    elif failure == "resolution":
        cog._resolver.resolve.side_effect = MediaError("YouTube requires authentication.")
    elif failure == "muted":
        guild.me.voice = SimpleNamespace(mute=True, self_mute=False)
    await cog._watchdog.configure(**settings)
    cog._watchdog.notify = AsyncMock()
    result = await cog._watchdog.check()
    assert result.status == "failed"
    cog._watchdog.notify.assert_awaited_once()
    assert guild.voice_client is None
    assert all(process.poll() is not None for process in processes)


async def test_disabled_cog_does_not_connect_or_report_failure(probe_runtime):
    cog, guild, channel, voice, packets, settings, processes = probe_runtime
    cog.bot.cog_disabled_in_guild.return_value = True
    assert (await cog._probe_playback(settings)).status == "deferred"
    channel.connect.assert_not_awaited()
    cog._resolver.search.assert_not_awaited()


async def test_unload_during_live_probe_cancels_decoder_voice_and_pending_notification(
    probe_runtime,
):
    cog, guild, channel, voice, packets, settings, processes = probe_runtime
    await cog._watchdog.configure(**settings)
    cog._watchdog.notify = AsyncMock()
    pending = asyncio.create_task(cog._watchdog.check())
    await eventually(lambda: bool(packets))
    source = voice._player.source
    await cog.cog_unload()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert source._cleaned and processes[-1].poll() is not None
    assert guild.voice_client is None and not cog._watchdog._checks
    cog._watchdog.notify.assert_not_awaited()


async def test_failure_dm_contains_public_source_and_uses_only_configured_recipient(bot, guild):
    cog = AudioPlus(bot)
    user = SimpleNamespace(send=AsyncMock())
    bot.get_user = lambda user_id: None
    bot.fetch_user = AsyncMock(return_value=user)
    settings = {**DEFAULT_WATCHDOG, "recipient_id": 888, "guild_id": guild.id}
    await cog._notify_check_failure(settings, {"at": 1, "detail": "YouTube denied access."})
    bot.fetch_user.assert_awaited_once_with(888)
    embed = user.send.await_args.kwargs["embed"]
    assert (
        "YouTube denied access" in embed.description and settings["video_url"] in embed.description
    )
    assert user.send.await_args.kwargs["allowed_mentions"].everyone is False
