"""Keep Discord packet timing steady while the decoder stalls, without losing PCM."""

import asyncio
import audioop
import struct
import threading
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from aiohttp import web
from conftest import make_channel
from test_audio_hybrid import red_command_runtime as red_runtime_fixture
from test_cog_hybrid import invoke_slash
from test_native_audio import FakeVoice, eventually

from audioplus.backend import require_voice
from audioplus.buffer import BUFFER_FRAMES, PREFILL_FRAMES, BufferedSource
from audioplus.player import GuildPlayer
from audioplus.resolver import MediaError, Stream, Track
from audioplus.source import DecoderError
from introplus.playback import OverlaySource

red_command_runtime = red_runtime_fixture


def pcm(sample=1000):
    return struct.pack("<h", sample) * 1920


class ControlledDecoder:
    """A child boundary with deterministic delivery, blocking and EOF/error behavior."""

    def __init__(self, frames, *, block_at=None, error=None):
        self.data = deque(frames)
        self.block_at = block_at
        self.error = error
        self.volume = 75
        self.start = 0
        self._local = False
        self.reads = 0
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.cleaned = threading.Event()
        self.finished = threading.Event()
        self._process = SimpleNamespace(wait=lambda timeout=None: 0)

    def read(self):
        if self.reads == self.block_at:
            self.blocked.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("The test did not release its decoder.")
        if self.cleaned.is_set():
            self.finished.set()
            return b""
        self.reads += 1
        if self.data:
            return self.data.popleft()
        self.finished.set()
        if self.error:
            raise self.error
        return b""

    def cleanup(self):
        self.cleaned.set()
        self.release.set()


def install_decoder(monkeypatch, decoder):
    calls = []

    def construct(*args, **kwargs):
        calls.append((args, kwargs))
        return decoder

    monkeypatch.setattr(discord, "FFmpegPCMAudio", construct)
    return calls


async def await_event(event, timeout=2):
    assert await asyncio.to_thread(event.wait, timeout), (
        "The controlled decoder never reached its gate."
    )


def voice_with_real_audio_thread(loop):
    require_voice()
    voice = object.__new__(discord.VoiceClient)
    voice.client = SimpleNamespace(loop=loop)
    voice.channel = SimpleNamespace(guild=SimpleNamespace(id=789))
    voice._connection = SimpleNamespace(
        is_connected=lambda: True, ws=SimpleNamespace(speak=AsyncMock())
    )
    voice._player = None
    voice.disconnect = AsyncMock()
    packets = []

    def send(data, encode=False):
        if encode:
            packets.append((time.monotonic(), voice.encoder.encode(data, 960)))

    voice.send_audio_packet = send
    return voice, packets


async def test_prefill_is_bounded_and_does_not_advance_music_progress():
    decoder = ControlledDecoder([pcm()] * (BUFFER_FRAMES * 3))
    source = BufferedSource(decoder)
    try:
        await source.prepare()
        assert BUFFER_FRAMES == 6000 and PREFILL_FRAMES == 150
        await eventually(lambda: decoder.reads >= BUFFER_FRAMES)
        assert decoder.reads <= BUFFER_FRAMES + 1
        assert source.frames == 0 and source.position == 0
        stats = source.diagnostics()
        assert stats["buffer_seconds"] == 120 and stats["target_seconds"] == 120
        for _ in range(10):
            assert source.read() == audioop.mul(pcm(), 2, 0.75)
        await eventually(lambda: decoder.reads >= BUFFER_FRAMES + 10)
        assert decoder.reads <= BUFFER_FRAMES + 11
        assert source.frames == 10 and source.position == 200
        assert source.diagnostics()["buffer_seconds"] <= 120
    finally:
        await asyncio.to_thread(source.cleanup)
    assert decoder.cleaned.is_set() and not source._thread.is_alive()


async def test_short_file_drains_in_order_and_ends_without_added_silence():
    frames = [pcm(i) for i in range(101, 107)]
    decoder = ControlledDecoder(frames)
    decoder.volume = 100
    source = BufferedSource(decoder)
    try:
        await source.prepare()
        assert source.frames == 0
        assert [source.read() for _ in frames] == frames
        assert source.read() == b""
        assert source.frames == 6 and source.position == 120
        assert source.diagnostics()["silence_ms"] == 0
    finally:
        source.cleanup()


async def test_startup_uses_three_seconds_without_waiting_for_two_minute_capacity():
    decoder = ControlledDecoder([pcm()] * BUFFER_FRAMES, block_at=PREFILL_FRAMES)
    source = BufferedSource(decoder)
    try:
        await asyncio.wait_for(source.prepare(), 1)
        await await_event(decoder.blocked)
        assert source.frames == 0 and source.position == 0
        assert not decoder.release.is_set()
        stats = source.diagnostics()
        assert stats["buffer_seconds"] == stats["prefill_seconds"] == 3
        assert stats["target_seconds"] == 120
    finally:
        source.cleanup()


async def test_decoder_failure_is_delivered_after_already_decoded_music():
    failure = DecoderError("FFmpeg: the audio connection failed or timed out.", retryable=True)
    frames = [pcm(400), pcm(500), pcm(600)]
    decoder = ControlledDecoder(frames, error=failure)
    decoder.volume = 100
    source = BufferedSource(decoder)
    try:
        await source.prepare()
        assert [source.read() for _ in frames] == frames
        with pytest.raises(DecoderError) as caught:
            source.read()
        assert caught.value is failure
        assert source.frames == 3 and source.position == 60
    finally:
        source.cleanup()


async def test_underflow_counts_one_incident_without_skipping_song_position():
    decoder = ControlledDecoder([pcm(800)] * BUFFER_FRAMES + [pcm(900)] * 8, block_at=BUFFER_FRAMES)
    decoder.volume = 100
    source = BufferedSource(decoder)
    try:
        await source.prepare()
        await eventually(lambda: source.diagnostics()["buffer_seconds"] == BUFFER_FRAMES * 0.02)
        for _ in range(BUFFER_FRAMES):
            assert source.read() == pcm(800)
        await await_event(decoder.blocked)
        assert source.position == BUFFER_FRAMES * 20
        assert source.read() == bytes(3840)
        assert source.read() == bytes(3840)
        assert source.position == BUFFER_FRAMES * 20 and source.frames == BUFFER_FRAMES
        stats = source.diagnostics()
        assert stats["underruns"] == 1 and stats["silence_ms"] == 40 and stats["refilling"]
        decoder.release.set()
        await await_event(decoder.finished)
        assert [source.read() for _ in range(8)] == [pcm(900)] * 8
        assert source.read() == b"" and source.position == (BUFFER_FRAMES + 8) * 20
        assert source.diagnostics()["underruns"] == 1
    finally:
        source.cleanup()


async def test_volume_and_seek_offset_apply_when_consumed_not_when_prefilled():
    decoder = ControlledDecoder([pcm(1000)] * 4)
    decoder.volume = 100
    decoder.start = 42000
    source = BufferedSource(decoder)
    try:
        await source.prepare()
        assert source.position == 42000 and decoder.volume == 100
        source.volume = 25
        assert source.read() == pcm(250)
        source.volume = 150
        assert source.read() == pcm(1500)
        source.volume = 0
        assert source.read() == bytes(3840)
        assert source.position == 42060 and source.frames == 3
    finally:
        source.cleanup()


async def test_underflow_refills_to_target_before_consuming_partial_new_audio():
    class TwoPhaseDecoder(ControlledDecoder):
        def __init__(self):
            super().__init__([pcm(800)] * BUFFER_FRAMES + [pcm(900)] * 200, block_at=BUFFER_FRAMES)
            self.second_gate = threading.Event()
            self.second_release = threading.Event()

        def read(self):
            if self.reads == BUFFER_FRAMES + 20:
                self.second_gate.set()
                assert self.second_release.wait(timeout=5)
            return super().read()

        def cleanup(self):
            self.second_release.set()
            super().cleanup()

    decoder = TwoPhaseDecoder()
    decoder.volume = 100
    source = BufferedSource(decoder)
    try:
        await source.prepare()
        await eventually(lambda: source.diagnostics()["buffer_seconds"] == BUFFER_FRAMES * 0.02)
        for _ in range(BUFFER_FRAMES):
            source.read()
        await await_event(decoder.blocked)
        assert source.read() == bytes(3840)
        decoder.release.set()
        await await_event(decoder.second_gate)
        stats = source.diagnostics()
        assert stats["buffer_seconds"] == 0.4 and stats["refilling"]
        assert source.read() == bytes(3840)
        assert source.position == BUFFER_FRAMES * 20 and source.frames == BUFFER_FRAMES
        decoder.second_release.set()
        await eventually(lambda: source.diagnostics()["buffer_seconds"] >= PREFILL_FRAMES * 0.02)
        assert source.read() == pcm(900)
        assert source.position == (BUFFER_FRAMES + 1) * 20
        assert source.diagnostics()["underruns"] == 1
    finally:
        source.cleanup()


@pytest.mark.parametrize("local", [False, True])
async def test_persistent_stall_has_a_finite_deadline_and_safe_error(monkeypatch, local):
    decoder = ControlledDecoder([pcm()] * BUFFER_FRAMES, block_at=BUFFER_FRAMES)
    decoder._local = local
    source = BufferedSource(decoder)
    monkeypatch.setattr("audioplus.buffer.STALL_TIMEOUT", 0.02)
    try:
        await source.prepare()
        await eventually(lambda: source.diagnostics()["buffer_seconds"] == BUFFER_FRAMES * 0.02)
        for _ in range(BUFFER_FRAMES):
            source.read()
        assert source.read() == bytes(3840)
        await asyncio.sleep(0.03)
        with pytest.raises(DecoderError) as caught:
            source.read()
        assert caught.value.retryable is not local
        assert "https" not in str(caught.value) and "stalled" in str(caught.value)
        assert source.position == BUFFER_FRAMES * 20
    finally:
        source.cleanup()
    assert not source._thread.is_alive()


async def test_prepare_timeout_keeps_child_cleanup_owned(monkeypatch):
    decoder = ControlledDecoder([pcm()], block_at=0)
    source = BufferedSource(decoder)
    monkeypatch.setattr("audioplus.buffer.STARTUP_TIMEOUT", 0.02)
    try:
        with pytest.raises(MediaError):
            await source.prepare()
        assert source.frames == 0
    finally:
        await asyncio.to_thread(source.cleanup)
    assert decoder.cleaned.is_set() and not source._thread.is_alive()


async def test_real_ffmpeg_blocked_network_read_is_reaped_on_cancelled_prefill(monkeypatch):
    from test_security_media import allow_test_loopback

    allow_test_loopback(monkeypatch)
    connected = asyncio.Event()
    release = asyncio.Event()
    app = web.Application()

    async def stalled(request):
        response = web.StreamResponse(headers={"Content-Type": "audio/wav"})
        await response.prepare(request)
        connected.set()
        await release.wait()
        return response

    app.router.add_get("/stalled", stalled)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/stalled"
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=Stream(url)))
    player = GuildPlayer(FakeVoice(), resolver, AsyncMock())
    try:
        await player.enqueue([Track(url, "blocked local HTTP audio", direct=True)])
        await asyncio.wait_for(connected.wait(), 2)
        source = player.source
        process = source.decoder._audio._process
        assert source.frames == 0 and process.poll() is None and player.preparing
        await asyncio.wait_for(player.close(), 2)
        assert process.poll() is not None and not source._thread.is_alive()
        assert not source.decoder._stderr._thread.is_alive()
        assert not player.voice.starts
        player.report_error.assert_not_awaited()
    finally:
        release.set()
        await player.close()
        await runner.cleanup()


async def test_skip_while_prefilling_reaps_reader_and_does_not_start_voice(monkeypatch):
    decoder = ControlledDecoder([pcm()] * BUFFER_FRAMES, block_at=0)
    install_decoder(monkeypatch, decoder)
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=Stream("https://cdn.invalid/audio")))
    report = AsyncMock()
    player = GuildPlayer(FakeVoice(), resolver, report)
    try:
        await player.enqueue([Track("https://www.youtube.com/watch?v=abc", "blocked")])
        await await_event(decoder.blocked)
        source = player.source
        assert source is not None and player.preparing and not player.voice.starts
        assert (await player.skip()).title == "blocked"
        await eventually(lambda: player._runner.done())
        assert decoder.cleaned.is_set() and not source._thread.is_alive()
        assert not player.voice.starts
        report.assert_not_awaited()
    finally:
        await player.close()


async def test_close_while_prefilling_reaps_reader_despite_repeated_cleanup(monkeypatch):
    decoder = ControlledDecoder([pcm()] * BUFFER_FRAMES, block_at=0)
    install_decoder(monkeypatch, decoder)
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=Stream("https://cdn.invalid/audio")))
    player = GuildPlayer(FakeVoice(), resolver, AsyncMock())
    await player.enqueue([Track("https://www.youtube.com/watch?v=abc", "blocked")])
    await await_event(decoder.blocked)
    source = player.source
    try:
        await player.close()
        await asyncio.gather(*(asyncio.to_thread(source.cleanup) for _ in range(3)))
        assert decoder.cleaned.is_set() and not source._thread.is_alive()
        assert player._runner.done() and not player.voice.starts
    finally:
        await player.close()


async def test_pause_and_seek_restore_without_consuming_prefetched_music(monkeypatch):
    decoders = []

    def construct(*args, **kwargs):
        decoder = ControlledDecoder([pcm()] * BUFFER_FRAMES * 2)
        decoders.append(decoder)
        return decoder

    monkeypatch.setattr(discord, "FFmpegPCMAudio", construct)
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=Stream("https://cdn.invalid/audio")))
    player = GuildPlayer(FakeVoice(), resolver, AsyncMock())
    current = Track("https://www.youtube.com/watch?v=abc", "current", length=180000)
    queued = Track("https://www.youtube.com/watch?v=def", "queued", length=180000)
    try:
        await player.enqueue([current, queued])
        await eventually(lambda: player.playing)
        player.pause()
        source = player.source
        assert player.paused and source.frames == 0
        assert source.read() and source.frames == 1
        await player.restart(current, start=65000, paused=True)
        await eventually(lambda: len(player.voice.starts) == 2)
        assert source._thread.is_alive() is False and decoders[0].cleaned.is_set()
        assert player.paused and player.position == 65000 and player.source.frames == 0
        assert list(player.queue) == [queued]
        await player.set_volume(25)
        assert player.source.read() == pcm(250)
        assert player.position == 65020
    finally:
        await player.close()


async def test_buffer_shortage_totals_survive_seek_and_retry_but_reset_with_voice(monkeypatch):
    def construct(*args, **kwargs):
        return ControlledDecoder([pcm()] * BUFFER_FRAMES * 2, block_at=BUFFER_FRAMES)

    monkeypatch.setattr(discord, "FFmpegPCMAudio", construct)
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=Stream("https://cdn.invalid/audio")))
    player = GuildPlayer(FakeVoice(), resolver, AsyncMock())
    current = Track("https://www.youtube.com/watch?v=abc", "current", length=180000)
    try:
        await player.enqueue([current])
        await eventually(lambda: player.playing)
        first = player.source
        await eventually(lambda: first.diagnostics()["buffer_seconds"] == 120)
        for _ in range(BUFFER_FRAMES):
            first.read()
        assert first.read() == first.read() == bytes(3840)
        await player.restart(current, start=10000)
        await eventually(lambda: len(player.voice.starts) == 2)
        assert player.buffer_status["underruns"] == 1
        assert player.buffer_status["silence_ms"] == 40
        second = player.source
        await eventually(lambda: second.diagnostics()["buffer_seconds"] == 120)
        for _ in range(BUFFER_FRAMES):
            second.read()
        assert second.read() == bytes(3840)
        player.voice.finish(
            DecoderError("The audio supply stalled while refilling.", retryable=True)
        )
        await eventually(lambda: len(player.voice.starts) == 3)
        assert player.position == 130000
        assert player.buffer_status["underruns"] == 2
        assert player.buffer_status["silence_ms"] == 60
        assert resolver.resolve.await_count == 3
        await player.stop()
        await eventually(lambda: player._runner.done())
        assert player.buffer_status["buffer_seconds"] == 0
        assert player.buffer_status["underruns"] == 2
        assert player.buffer_status["silence_ms"] == 60
        replacement = GuildPlayer(FakeVoice(), resolver, AsyncMock())
        assert replacement.buffer_status is None
        await replacement.close()
    finally:
        await player.close()


async def test_red_prefix_slash_and_support_report_show_buffer_diagnostics(
    red_command_runtime, monkeypatch
):
    bot, cog, member, invoke = red_command_runtime
    decoder = ControlledDecoder([pcm()] * BUFFER_FRAMES, block_at=PREFILL_FRAMES)
    install_decoder(monkeypatch, decoder)
    guild = member.guild
    channel = make_channel(guild, kind=discord.VoiceChannel)
    voice = FakeVoice(guild.id)
    voice.guild, voice.channel = guild, channel
    guild.voice_client = voice
    guild.me.voice = SimpleNamespace(
        channel=channel, mute=False, deaf=False, self_mute=False, self_deaf=False, suppress=False
    )
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=Stream("https://cdn.invalid/audio")))
    player = GuildPlayer(voice, resolver, AsyncMock())
    cog._players[guild.id] = player
    monkeypatch.setattr(
        "audioplus.cog.diagnostics",
        AsyncMock(
            return_value={
                "voice_error": None,
                "ready": True,
                "ffmpeg": "test ffmpeg",
                "packages": {},
                "voice_packages": {},
                "runtimes": ["node"],
                "deno": "missing",
                "node": "test node",
                "quickjs": "missing",
            }
        ),
    )
    try:
        await player.enqueue([Track("https://www.youtube.com/watch?v=abc", "music")])
        await eventually(lambda: player.playing)
        for _ in range(PREFILL_FRAMES):
            player.source.read()
        assert player.source.read() == player.source.read() == bytes(3840)
        for name in ("playerstate", "debugvc", "audiostatus"):
            prefix = await invoke("!" + name)
            slash = await invoke_slash(bot, invoke, monkeypatch, name)
            for ctx in (prefix, slash):
                assert not ctx.command_failed
                text = ctx.send.await_args.kwargs["embed"].description
                assert "Playback buffer" in text and "/ 120 seconds" in text
                assert "start/refill at 3 seconds" in text
                assert "Buffer shortages** · 1 · inserted silence 0.04 seconds" in text
        report = await cog.diagnostic_report(guild.id)
        assert report["playback_buffer"]["underruns"] == 1
        assert report["playback_buffer"]["silence_ms"] == 40
        assert report["playback_buffer"]["target_seconds"] == 120
    finally:
        await player.close()


async def test_intro_overlay_preserves_buffered_music_progress_and_lifetime():
    decoder = ControlledDecoder([pcm(1000)] * 10)
    decoder.volume = 100
    music = BufferedSource(decoder)
    intro = ControlledDecoder([pcm(100)] * 2)
    complete = []
    overlay = OverlaySource(music, intro, complete.append)
    try:
        await music.prepare()
        assert overlay.read() == pcm(400)
        assert overlay.read() == pcm(400)
        assert music.position == 40 and music.frames == 2
        assert overlay.read() == pcm(1000)
        assert complete == [None] and intro.cleaned.is_set()
        overlay.detach()
        assert not decoder.cleaned.is_set()
        assert music.read() == pcm(1000) and music.position == 80
    finally:
        overlay.detach()
        music.cleanup()
    assert not music._thread.is_alive()


async def test_real_discord_thread_sends_steady_packets_during_short_decoder_stall(monkeypatch):
    decoder = ControlledDecoder([pcm()] * 400, block_at=PREFILL_FRAMES)
    install_decoder(monkeypatch, decoder)
    voice, packets = voice_with_real_audio_thread(asyncio.get_running_loop())
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=Stream("https://cdn.invalid/audio")))
    report = AsyncMock()
    player = GuildPlayer(voice, resolver, report)
    try:
        await player.enqueue([Track("https://www.youtube.com/watch?v=abc", "music")])
        await await_event(decoder.blocked)
        await eventually(lambda: len(packets) >= 20)
        assert not decoder.release.is_set()
        timestamps = [stamp for stamp, _ in packets[:20]]
        assert max(b - a for a, b in zip(timestamps, timestamps[1:])) < 0.12
        assert player.source.diagnostics()["underruns"] == 0
        assert player.position >= 400 and player.current.title == "music"
        assert all(packet for _, packet in packets[:20])
        decoder_opus = discord.opus.Decoder()
        assert len(decoder_opus.decode(packets[0][1], fec=False)) == 3840
        decoder.release.set()
        await eventually(lambda: len(packets) >= 25)
        report.assert_not_awaited()
    finally:
        await player.close()
    assert decoder.cleaned.is_set()
