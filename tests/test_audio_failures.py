"""Playback-stage diagnostics preserve actionable causes and exclude arbitrary error text."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import make_context
from test_audio_watchdog import monitor_runtime as monitor_fixture
from test_audio_watchdog import probe_runtime as probe_fixture
from test_native_audio import native_player as player_fixture
from test_native_audio import track

from audioplus import AudioPlus
from audioplus.failures import PlaybackFailure, playback_stage, safe_exception
from audioplus.resolver import MediaError

monitor_runtime = monitor_fixture
probe_runtime = probe_fixture
native_player = player_fixture


@pytest.mark.parametrize(
    "error,expected",
    [
        (RuntimeError("https://signed.invalid/?token=private Cookie: private"), "RuntimeError"),
        (TypeError("Bearer private"), "TypeError"),
        (OSError(13, "private", "/private/file"), "errno 13"),
        (ImportError("private", name="nacl._sodium"), "nacl._sodium"),
        (AttributeError("private", name="frames", obj=SimpleNamespace()), "frames"),
        (RuntimeError("PyNaCl library needed in order to use voice"), "audiorepair"),
        (asyncio.TimeoutError(), "timed out"),
        (
            discord.HTTPException(
                SimpleNamespace(status=403, reason="private"), {"code": 50013, "message": "private"}
            ),
            "Discord error 50013",
        ),
    ],
)
def test_exception_summary_uses_structured_fields_not_private_messages(error, expected):
    result = safe_exception(error)
    assert expected in result
    assert "private" not in result and "signed.invalid" not in result


async def test_generic_monitor_exception_identifies_type_and_location_without_secrets(
    monitor_runtime, caplog
):
    cog, monitor, now = monitor_runtime

    async def failure(settings):
        raise TypeError("https://signed.invalid/?token=private Cookie: private")

    monitor.probe.side_effect = failure
    result = await monitor.check()
    assert result.status == "failed"
    assert "Playback probe" in result.detail and "TypeError" in result.detail
    assert "test_audio_failures.py" in result.detail and "failure" in result.detail
    assert "private" not in result.detail and "private" not in caplog.text
    assert "traceback:" in caplog.text and "test_audio_failures.py" in caplog.text
    assert (await cog.config.watchdog())["last_result"]["detail"] == result.detail
    monitor.notify.assert_awaited_once()


@pytest.mark.parametrize("stage", ["lookup", "connection", "playback"])
async def test_real_monitor_records_the_stage_of_unexpected_failures(
    probe_runtime, monkeypatch, stage
):
    cog, guild, channel, voice, packets, settings, processes = probe_runtime
    if stage == "lookup":
        cog._resolver.search.side_effect = AttributeError(
            "private", name="metadata", obj=SimpleNamespace()
        )
        expected = "YouTube lookup"
    elif stage == "connection":
        channel.connect.side_effect = OSError(13, "private", "/private/file")
        expected = "Discord voice connection"
    else:
        monkeypatch.setattr(
            "audioplus.cog.GuildPlayer",
            lambda *args, **kwargs: (_ for _ in ()).throw(TypeError("private")),
        )
        expected = "Native playback"
    await cog._watchdog.configure(**settings)
    cog._watchdog.notify = AsyncMock()
    result = await cog._watchdog.check()
    assert result.status == "failed" and expected in result.detail
    assert "private" not in result.detail
    assert "local player failed" not in result.detail
    assert guild.voice_client is None and all(process.poll() is not None for process in processes)
    cog._watchdog.notify.assert_awaited_once()


async def test_cleanup_failure_does_not_replace_original_probe_error(
    probe_runtime, monkeypatch, caplog
):
    cog, guild, channel, voice, packets, settings, processes = probe_runtime
    monkeypatch.setattr(
        "audioplus.cog.GuildPlayer",
        lambda *args, **kwargs: (_ for _ in ()).throw(TypeError("primary private")),
    )
    disconnect = voice.disconnect.side_effect

    async def fail_cleanup(**kwargs):
        await disconnect(**kwargs)
        raise OSError(13, "cleanup private")

    voice.disconnect.side_effect = fail_cleanup
    with pytest.raises(PlaybackFailure) as error:
        await cog._probe_playback(settings)
    assert "Native playback" in str(error.value) and "TypeError" in str(error.value)
    assert "errno 13" not in str(error.value)
    assert "Voice cleanup" in caplog.text and "errno 13" in caplog.text
    assert "private" not in caplog.text and guild.voice_client is None


async def test_connection_cleanup_keeps_original_handshake_failure(
    probe_runtime, monkeypatch, caplog
):
    cog, guild, channel, voice, packets, settings, processes = probe_runtime
    monkeypatch.setattr(discord, "VoiceClient", lambda *args: voice)
    disconnect = voice.disconnect.side_effect

    async def connect(*, cls, **kwargs):
        cls(cog.bot, channel)
        raise TypeError("primary private")

    async def cleanup(**kwargs):
        await disconnect(**kwargs)
        raise OSError(13, "cleanup private")

    channel.connect.side_effect = connect
    voice.disconnect.side_effect = cleanup
    with pytest.raises(PlaybackFailure) as error:
        await cog._probe_playback(settings)
    assert "Discord voice connection" in str(error.value) and "TypeError" in str(error.value)
    assert "errno 13" not in str(error.value) and "private" not in str(error.value)
    assert "Voice connection cleanup" in caplog.text and "private" not in caplog.text
    assert guild.voice_client is None


async def test_cleanup_failure_prevents_false_success(probe_runtime, caplog):
    cog, guild, channel, voice, packets, settings, processes = probe_runtime
    disconnect = voice.disconnect.side_effect

    async def fail_cleanup(**kwargs):
        await disconnect(**kwargs)
        raise OSError(13, "private")

    voice.disconnect.side_effect = fail_cleanup
    with pytest.raises(PlaybackFailure, match="Voice cleanup"):
        await cog._probe_playback(settings)
    assert guild.voice_client is None and all(process.poll() is not None for process in processes)


async def test_dependency_repairs_postpone_probe_without_alert(probe_runtime):
    cog, guild, channel, voice, packets, settings, processes = probe_runtime
    cog._voice_repair._task = asyncio.current_task()
    try:
        await cog._watchdog.configure(**settings)
        cog._watchdog.notify = AsyncMock()
        result = await cog._watchdog.check()
        assert result.status == "deferred" and "repair is running" in result.detail
        assert (await cog.config.watchdog())["retry_at"] > 0
        cog._watchdog.notify.assert_not_awaited()
        cog._resolver.search.assert_not_awaited()
        channel.connect.assert_not_awaited()
    finally:
        cog._voice_repair._task = None


async def test_native_player_keeps_unexpected_error_type_without_private_message(
    native_player, caplog
):
    native_player.resolver.resolve.side_effect = TypeError("private signed URL")
    await native_player.enqueue([track()])
    await native_player._runner
    assert "TypeError" in native_player.last_error
    assert "private" not in native_player.last_error and "private" not in caplog.text
    assert "Native playback" in caplog.text and "traceback:" in caplog.text
    native_player.report_error.assert_awaited_once()


async def test_audiostatus_retains_monitor_result_after_probe_player_is_gone(
    bot, guild, monkeypatch
):
    cog = AudioPlus(bot)
    await cog._watchdog.configure(
        guild_id=guild.id,
        recipient_id=888,
        last_result={"status": "failed", "at": 123, "detail": "YouTube lookup: TypeError"},
    )
    monkeypatch.setattr(
        "audioplus.cog.diagnostics",
        AsyncMock(
            return_value={
                "ready": True,
                "voice_error": None,
                "packages": {},
                "ffmpeg": "test",
                "deno": "missing",
                "node": "v22.0.0",
                "quickjs": "missing",
                "runtimes": ["Node"],
            }
        ),
    )
    ctx = make_context(guild)
    await cog._diagnostic_reply(ctx)
    output = ctx.send.await_args.kwargs["embed"].description
    assert "Last daily playback check" in output and "YouTube lookup: TypeError" in output
    assert "888" not in output
    await cog.config.watchdog.guild_id.set(guild.id + 1)
    await cog._diagnostic_reply(ctx)
    assert "YouTube lookup: TypeError" not in ctx.send.await_args.kwargs["embed"].description
    # Switching the monitor must not display the previous server's result in the new one.
    await cog._watchdog.configure(guild_id=guild.id)
    await cog._diagnostic_reply(ctx)
    assert "YouTube lookup: TypeError" not in ctx.send.await_args.kwargs["embed"].description


def test_nested_stages_keep_original_stage_and_cancellation_stays_cancelled():
    with pytest.raises(PlaybackFailure) as error:
        with playback_stage("Outer"):
            with playback_stage("Inner"):
                raise MediaError("FFmpeg cannot read this stream.")
    assert "Inner" in str(error.value) and "Outer" not in str(error.value)
    with pytest.raises(asyncio.CancelledError):
        with playback_stage("Cancelled"):
            raise asyncio.CancelledError
