"""Exercise safe decoder diagnostics against real local HTTP and FFmpeg processes."""

import asyncio
import io
import subprocess
import sys
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from aiohttp import web
from test_security_media import allow_test_loopback

from audioplus.backend import require_voice
from audioplus.player import GuildPlayer
from audioplus.resolver import Stream, Track
from audioplus.source import DecoderError, NativeSource


@pytest.fixture
async def decoder_server(monkeypatch):
    allow_test_loopback(monkeypatch)
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x10\x00" * 4000)
    requests = []
    app = web.Application()

    async def denied(request):
        requests.append(request)
        return web.Response(status=int(request.match_info["status"]))

    async def valid(request):
        requests.append(request)
        return web.Response(body=audio.getvalue(), content_type="audio/wav")

    async def corrupt(request):
        return web.Response(body=b"invalid-media-secret", content_type="audio/wav")

    app.router.add_get("/status/{status}", denied)
    app.router.add_get("/valid", valid)
    app.router.add_get("/corrupt", corrupt)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    root = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        yield root, requests
    finally:
        await runner.cleanup()


def cleanup_decoder(source, process, stderr):
    source.cleanup()
    source.cleanup()
    assert process.poll() is not None
    assert not stderr._thread.is_alive()


@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 500, 503])
async def test_http_failures_identify_safe_status_and_allow_fresh_stream_retry(
    decoder_server, status
):
    root, requests = decoder_server
    source = NativeSource(
        Stream(
            f"{root}/status/{status}?token=signed-url-secret",
            {"Cookie": "session=header-secret"},
        ),
        volume=100,
    )
    process, stderr = source._audio._process, source._stderr
    try:
        with pytest.raises(DecoderError) as caught:
            await asyncio.to_thread(source.read)
        failure = caught.value
        assert failure.http_status == status
        assert failure.retryable
        assert str(status) in str(failure)
        assert root not in str(failure)
        assert "signed-url-secret" not in str(failure)
        assert "header-secret" not in str(failure)
        assert len(str(failure)) < 1000
        assert requests and requests[0].headers["Cookie"] == "session=header-secret"
    finally:
        cleanup_decoder(source, process, stderr)


async def test_corrupt_http_media_is_not_retried_as_a_transient_failure(decoder_server):
    root, _ = decoder_server
    source = NativeSource(Stream(f"{root}/corrupt?token=private-url"), volume=100)
    process, stderr = source._audio._process, source._stderr
    try:
        with pytest.raises(DecoderError) as caught:
            await asyncio.to_thread(source.read)
        assert caught.value.http_status is None
        assert not caught.value.retryable
        assert "private-url" not in str(caught.value)
        assert "invalid-media-secret" not in str(caught.value)
        assert root not in str(caught.value)
    finally:
        cleanup_decoder(source, process, stderr)


async def test_local_corrupt_cache_is_a_safe_nonremote_error(tmp_path):
    path = tmp_path / "private-cache-file.ogg"
    path.write_bytes(b"OggS-invalid-media-private-content")
    source = NativeSource(Stream(str(path), local=True), volume=100)
    process, stderr = source._audio._process, source._stderr
    try:
        with pytest.raises(DecoderError) as caught:
            await asyncio.to_thread(source.read)
        assert caught.value.http_status is None
        assert not caught.value.retryable
        assert str(path) not in str(caught.value)
        assert "private-content" not in str(caught.value)
    finally:
        cleanup_decoder(source, process, stderr)


async def test_successful_decode_preserves_headers_and_reaps_diagnostic_reader(decoder_server):
    root, requests = decoder_server
    source = NativeSource(
        Stream(
            f"{root}/valid?signature=private-signature",
            {
                "User-Agent": "AudioPlus regression test",
                "Referer": "https://example.invalid/watch?v=private-source",
                "Cookie": "session=private-cookie",
            },
        ),
        volume=100,
    )
    process, stderr = source._audio._process, source._stderr
    try:
        frames = []
        while frame := await asyncio.to_thread(source.read):
            frames.append(frame)
        assert frames and all(len(frame) == 3840 for frame in frames)
        assert requests
        received = requests[0].headers
        assert received["User-Agent"] == "AudioPlus regression test"
        assert received["Referer"] == "https://example.invalid/watch?v=private-source"
        assert received["Cookie"] == "session=private-cookie"
    finally:
        cleanup_decoder(source, process, stderr)


async def test_configured_proxy_transport_is_explicitly_refused(monkeypatch):
    from audioplus.resolver import MediaError

    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9999")
    with pytest.raises(MediaError, match="proxy environment"):
        NativeSource(Stream("https://example.invalid/private?token=proxy-test-secret"), volume=100)


async def test_flooded_decoder_stderr_is_bounded_drained_and_never_exposed(monkeypatch):
    # Exercise a real child's OS pipe and exit, without depending on malformed
    # media producing any particular amount of FFmpeg diagnostic output.
    program = (
        "import sys; "
        "sys.stderr.buffer.write(b'private-stderr-secret' * 200000); "
        "sys.stderr.buffer.write(b'\\nHTTP error 403 Forbidden\\n'); "
        "sys.stderr.flush(); sys.exit(1)"
    )

    def spawn(self, args, **kwargs):
        return subprocess.Popen([sys.executable, "-c", program], **kwargs)

    monkeypatch.setattr(discord.FFmpegAudio, "_spawn_process", spawn)
    source = NativeSource(Stream("https://example.invalid/?token=private-url"), volume=100)
    process, stderr = source._audio._process, source._stderr
    try:
        with pytest.raises(DecoderError) as caught:
            await asyncio.wait_for(asyncio.to_thread(source.read), 5)
        assert caught.value.http_status == 403
        assert caught.value.retryable
        assert len(stderr._tail) <= stderr.LIMIT
        assert "private-stderr-secret" not in str(caught.value)
        assert "private-url" not in str(caught.value)
    finally:
        cleanup_decoder(source, process, stderr)
    assert not stderr._tail


def test_failed_decoder_spawn_closes_owned_diagnostic_pipe(monkeypatch):
    import audioplus.source as decoder

    captures = []
    original = decoder._DecoderStderr

    def capture():
        created = original()
        captures.append(created)
        return created

    def failed_spawn(self, args, **kwargs):
        raise discord.ClientException("FFmpeg unavailable")

    monkeypatch.setattr(decoder, "_DecoderStderr", capture)
    monkeypatch.setattr(discord.FFmpegAudio, "_spawn_process", failed_spawn)
    with pytest.raises(discord.ClientException, match="FFmpeg unavailable"):
        NativeSource(Stream("https://example.invalid/audio"), volume=100)
    assert len(captures) == 1
    assert captures[0].writer.closed
    assert captures[0].reader.closed
    assert not captures[0]._thread.is_alive()


async def test_real_decoder_failure_refreshes_and_plays_through_discord_audio_thread(
    decoder_server,
):
    root, requests = decoder_server
    require_voice()
    loop = asyncio.get_running_loop()
    # Discord's handshake and UDP boundary are replaced; its player thread,
    # error callback, Opus encoder, and both FFmpeg subprocesses remain real.
    voice = object.__new__(discord.VoiceClient)
    voice.client = SimpleNamespace(loop=loop)
    voice.channel = SimpleNamespace(guild=SimpleNamespace(id=789))
    voice._connection = SimpleNamespace(
        is_connected=lambda: True, ws=SimpleNamespace(speak=AsyncMock())
    )
    voice._player = None
    voice.disconnect = AsyncMock()
    packets = []
    voice.send_audio_packet = lambda data, encode=False: packets.append(
        voice.encoder.encode(data, 960) if encode else data
    )
    track = Track("https://example.invalid/watch?v=public-track", "Retry regression")
    resolver = SimpleNamespace(
        resolve=AsyncMock(
            side_effect=[
                Stream(f"{root}/status/403?token=expired-private-url"),
                Stream(f"{root}/valid?token=fresh-private-url"),
            ]
        )
    )
    report = AsyncMock()
    on_start = AsyncMock()
    sources = []

    def capture_source(stream, **kwargs):
        source = NativeSource(stream, **kwargs)
        sources.append((source, source._audio._process, source._stderr))
        return source

    player = GuildPlayer(voice, resolver, report, source_factory=capture_source, on_start=on_start)
    try:
        await player.enqueue([track])
        await asyncio.wait_for(asyncio.shield(player._runner), 5)
        assert player._runner.done()
        assert resolver.resolve.await_count == 2
        assert all(call.args == (track,) for call in resolver.resolve.await_args_list)
        assert [request.path for request in requests] == ["/status/403", "/valid"]
        assert len(sources) == 2
        assert sources[0][0].frames == 0
        assert sources[1][0].frames >= 10
        assert packets and all(packets)
        assert len(discord.opus.Decoder().decode(packets[0], fec=False)) == 3840
        on_start.assert_awaited_once_with(player)
        report.assert_not_awaited()
        assert player.current is None and not player.queue
        assert player.last_error is None
        for source, process, stderr in sources:
            assert source._cleaned
            assert process.poll() is not None
            assert not stderr._thread.is_alive()
            assert not stderr._tail
    finally:
        await player.close()
