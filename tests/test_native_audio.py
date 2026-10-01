"""Exercise the native player, real subprocess cancellation, and local media decoding."""

import asyncio
import io
import sys
import threading
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from audioplus.player import GuildPlayer, NativeSource
from audioplus.resolver import MediaError, MediaResolver, Stream, Track, normalize_query


class FakeVoice:
    """Only the Discord voice/network boundary is simulated."""

    def __init__(self, guild_id=123):
        self.guild = SimpleNamespace(id=guild_id)
        self.channel = SimpleNamespace(id=456)
        self.connected = True
        self.source = None
        self.after = None
        self.paused = False
        self.starts = []
        self.disconnect = AsyncMock(side_effect=self._disconnect)

    async def _disconnect(self, **kwargs):
        self.connected = False
        self.stop()

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.source is not None and not self.paused

    def is_paused(self):
        return self.source is not None and self.paused

    def play(self, source, *, after):
        assert self.source is None
        self.source, self.after = source, after
        self.paused = False
        self.starts.append(source)

    def stop(self):
        if self.after:
            callback = self.after
            self.source = self.after = None
            self.paused = False
            callback(None)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def finish(self, error=None):
        callback = self.after
        self.source = self.after = None
        self.paused = False
        if callback:
            # discord.py invokes after from its audio thread, not the event loop.
            thread = threading.Thread(target=callback, args=(error,))
            thread.start()
            thread.join()


class FakeSource:
    def __init__(self, stream, *, volume, start):
        self.stream, self.volume, self.position = stream, volume, start
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


async def eventually(predicate):
    async def check():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(check(), 2)


@pytest.fixture
async def native_player():
    resolver = SimpleNamespace(resolve=AsyncMock(side_effect=lambda tr: Stream(tr.uri)))
    report = AsyncMock()
    player = GuildPlayer(FakeVoice(), resolver, report, source_factory=FakeSource)
    yield player
    await player.close()


def track(name="one"):
    return Track(f"https://example.invalid/{name}", name)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("  roar  ", "ytsearch1:roar"),
        ("ytsearch:roar", "ytsearch1:roar"),
        ("ytmsearch:roar", "ytsearch1:roar"),
        ("scsearch:roar", "scsearch1:roar"),
        ("https://example.invalid/song.mp3", "https://example.invalid/song.mp3"),
    ],
)
def test_native_search_mapping(query, expected):
    assert normalize_query(query) == expected


@pytest.mark.parametrize(
    "query", [" ", "scsearch:", "spsearch:roar", "ytsearch100000:roar", "file:///etc/passwd"]
)
def test_empty_unsupported_and_unbounded_searches_are_rejected(query):
    with pytest.raises(MediaError):
        normalize_query(query)


async def test_natural_completion_advances_once_from_voice_thread(native_player):
    player = native_player
    await player.enqueue([track("one"), track("two")])
    await eventually(lambda: player.playing)
    player.voice.finish()
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.current.title == "two"
    assert not player.queue
    assert player.voice.starts[0].cleaned
    player.voice.finish()
    await eventually(lambda: player._runner.done())
    assert player.current is None


async def test_enqueue_preserves_paused_current_track(native_player):
    player = native_player
    await player.enqueue([track()])
    await eventually(lambda: player.playing)
    player.voice.pause()
    await player.enqueue([track("two")])
    assert player.paused and len(player.voice.starts) == 1
    assert player.queue[0].title == "two"


async def test_skip_during_extraction_cancels_lookup_and_advances_once(native_player):
    player = native_player
    blocked = asyncio.Event()
    cancelled = asyncio.Event()

    async def resolve(tr):
        if tr.title == "one":
            blocked.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return Stream(tr.uri)

    player.resolver.resolve.side_effect = resolve
    await player.enqueue([track(), track("two")])
    await blocked.wait()
    assert (await player.skip()).title == "one"
    await eventually(lambda: player.playing)
    assert cancelled.is_set()
    assert player.current.title == "two" and len(player.voice.starts) == 1


async def test_stop_prevents_late_callback_from_restarting_queue(native_player):
    player = native_player
    await player.enqueue([track(), track("two")])
    await eventually(lambda: player.playing)
    late = player.voice.after
    await player.stop()
    late(MediaError("old failure"))
    await eventually(lambda: player._runner.done())
    assert not player.queue and player.current is None
    assert len(player.voice.starts) == 1
    player.report_error.assert_not_awaited()
    await player.enqueue([track("three")])
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.current.title == "three" and not player.paused


async def test_failed_track_is_reported_then_next_track_plays(native_player):
    player = native_player
    await player.enqueue([track(), track("two")])
    await eventually(lambda: player.playing)
    player.voice.finish(MediaError("Decoder failed"))
    await eventually(lambda: len(player.voice.starts) == 2)
    player.report_error.assert_awaited_once()
    assert player.report_error.await_args.args[2] == "Decoder failed"
    assert player.current.title == "two"


async def test_repeat_does_not_repeat_a_skipped_or_failed_track(native_player):
    player = native_player
    player.repeat = "track"
    await player.enqueue([track(), track("two")])
    await eventually(lambda: player.playing)
    player.voice.finish()
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.current.title == "one"
    await player.skip()
    await eventually(lambda: len(player.voice.starts) == 3)
    assert player.current.title == "two"
    player.voice.finish(MediaError("bad source"))
    await eventually(lambda: player._runner.done())
    assert not player.queue


async def test_volume_restart_and_pause_restore(native_player):
    player = native_player
    await player.enqueue([track(), track("two")])
    await eventually(lambda: player.playing)
    await player.set_volume(400)
    assert player.source.volume == 400
    await player.restart(player.current, start=45000, paused=True)
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.position == 45000 and player.paused
    assert player.source.volume == 400 and player.queue[0].title == "two"


async def test_queue_limit_is_atomic_under_concurrent_enqueues(native_player):
    player = native_player

    async def blocked(tr):
        await asyncio.Event().wait()

    player.resolver.resolve = AsyncMock(side_effect=blocked)
    results = await asyncio.gather(
        *(player.enqueue([track(str(i)) for i in range(60)]) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, MediaError) for result in results) == 1
    assert len(player.queue) <= 100


async def test_close_cancels_player_and_leaves_other_guild_running(native_player):
    one = native_player
    two = GuildPlayer(FakeVoice(456), one.resolver, AsyncMock(), source_factory=FakeSource)
    try:
        await one.enqueue([track()])
        await two.enqueue([track()])
        await eventually(lambda: one.playing and two.playing)
        source = one.source
        await one.close()
        assert source.cleaned and one._runner.done()
        assert two.playing and not two.closed
    finally:
        await two.close()


async def test_lookup_timeout_reaps_child_process(monkeypatch):
    resolver = MediaResolver(timeout=0.1)
    monkeypatch.setattr(
        resolver, "_command", lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(20)"]
    )
    with pytest.raises(MediaError, match="timed out"):
        await resolver._extract("query", flat=True)
    assert not resolver._processes


async def test_cancel_lookup_reaps_child_process(monkeypatch):
    resolver = MediaResolver()
    monkeypatch.setattr(
        resolver, "_command", lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(20)"]
    )
    task = asyncio.create_task(resolver._extract("query", flat=True))
    await eventually(lambda: bool(resolver._processes))
    process = next(iter(resolver._processes))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None and not resolver._processes


async def test_flat_playlist_metadata_and_stream_resolution(monkeypatch):
    resolver = MediaResolver()
    data = {
        "entries": [
            {"id": "abc", "url": "abc", "ie_key": "Youtube", "title": "A", "duration": 3},
            None,
            {"url": "https://soundcloud.com/artist/b", "ie_key": "Soundcloud", "title": "B"},
        ]
    }
    resolver._extract = AsyncMock(return_value=data)
    tracks = await resolver.search("https://youtube.com/playlist?list=abc")
    assert [tr.title for tr in tracks] == ["A", "B"]
    assert tracks[0].uri == "https://www.youtube.com/watch?v=abc" and tracks[0].length == 3000
    resolver._extract.return_value = {
        "url": "https://cdn.example.invalid/fresh",
        "http_headers": {"User-Agent": "test", "Cookie": "x\r\ny"},
    }
    stream = await resolver.resolve(tracks[0])
    assert stream.url.endswith("/fresh") and stream.headers == {"User-Agent": "test"}
    resolver._extract.assert_awaited_with(tracks[0].uri, flat=False)


async def test_process_errors_hide_signed_urls_and_credentials(monkeypatch):
    resolver = MediaResolver()
    error = "HTTP Error 403: https://cdn.invalid?token=secret"
    program = f"import sys; sys.stderr.write({error!r}); sys.exit(1)"
    monkeypatch.setattr(resolver, "_command", lambda *a, **k: [sys.executable, "-c", program])
    with pytest.raises(MediaError) as exc:
        await resolver._extract("query", flat=False)
    assert "denied" in str(exc.value) and "secret" not in str(exc.value)


@pytest.fixture
async def media_server():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x10\x00" * 8000)
    app = web.Application()

    async def audio(request):
        return web.Response(body=buffer.getvalue(), content_type="audio/wav")

    app.router.add_get("/media", audio)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/media"
    try:
        yield url
    finally:
        await runner.cleanup()


async def test_real_ytdlp_generic_extraction_and_ffmpeg_pcm(media_server):
    resolver = MediaResolver(timeout=20)
    tracks = await resolver.search(media_server)
    assert len(tracks) == 1
    stream = await resolver.resolve(tracks[0])
    source = NativeSource(stream, volume=100)
    process = source._audio._process
    try:
        chunks = []
        while chunk := await asyncio.to_thread(source.read):
            chunks.append(chunk)
        assert chunks and all(len(chunk) == 3840 for chunk in chunks)
        assert 400 <= source.position <= 600
    finally:
        source.cleanup()
        await resolver.close()
    assert process.poll() is not None


async def test_real_ffmpeg_failure_is_not_reported_as_natural_end(media_server):
    source = NativeSource(Stream(media_server + "-missing"), volume=100)
    try:
        with pytest.raises(MediaError, match="FFmpeg"):
            await asyncio.to_thread(source.read)
    finally:
        source.cleanup()
