"""Exercise bounded provider refreshes through Discord's asynchronous callbacks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_native_audio import FakeSource, FakeVoice, eventually, track

from audioplus.cache import song_key
from audioplus.player import GuildPlayer
from audioplus.resolver import MediaError, Stream
from audioplus.source import DecoderError


class CountingSource(FakeSource):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.frames = 0


@pytest.fixture
async def retry_player():
    resolver = SimpleNamespace(resolve=AsyncMock(side_effect=lambda tr: Stream(tr.uri)))
    player = GuildPlayer(
        FakeVoice(),
        resolver,
        AsyncMock(),
        source_factory=CountingSource,
        on_start=AsyncMock(),
        on_finish=AsyncMock(),
    )
    yield player
    await player.close()


def transient():
    return DecoderError("The media server rejected the expired stream (HTTP 403).", retryable=True)


async def test_refresh_preserves_progress_pause_and_single_start(retry_player):
    player = retry_player
    selected = track()
    player.resolver.resolve.side_effect = [
        Stream("https://example.invalid/expired"),
        Stream("https://example.invalid/fresh"),
    ]
    await player.restart(selected, start=40_000, paused=True)
    await eventually(lambda: len(player.voice.starts) == 1)
    first = player.source
    first.frames = 25
    first.position = 40_500
    player.voice.finish(transient())
    await eventually(lambda: len(player.voice.starts) == 2)
    assert first.cleaned
    assert player.current is selected and player.position == 40_500 and player.paused
    assert player.source.stream.url.endswith("/fresh")
    assert player.resolver.resolve.await_args_list[0].args == (selected,)
    assert player.resolver.resolve.await_args_list[1].args == (selected,)
    player.on_start.assert_awaited_once()
    player.on_finish.assert_awaited_once_with(player, selected, 500)
    player.report_error.assert_not_awaited()


@pytest.mark.parametrize("resume", [False, True])
async def test_refresh_restores_current_pause_intent(retry_player, resume):
    player = retry_player
    await player.enqueue([track()])
    await eventually(lambda: len(player.voice.starts) == 1)
    player.pause()
    if resume:
        player.resume()
    player.source.frames, player.source.position = 10, 200
    player.voice.finish(transient())
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.paused is not resume and player.position == 200
    player.on_start.assert_awaited_once()
    player.report_error.assert_not_awaited()


async def test_failed_refresh_reports_once_and_keeps_next_track(retry_player):
    player = retry_player
    first, second = track(), track("two")
    await player.enqueue([first, second])
    await eventually(lambda: len(player.voice.starts) == 1)
    player.voice.finish(transient())
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.current is first and list(player.queue) == [second]
    player.report_error.assert_not_awaited()
    player.voice.finish(transient())
    await eventually(lambda: len(player.voice.starts) == 3)
    assert player.current is second and not player.queue
    assert [call.args[0] for call in player.resolver.resolve.await_args_list] == [
        first,
        first,
        second,
    ]
    player.report_error.assert_awaited_once_with(player, first, str(transient()))
    assert player.voice.starts[0].cleaned and player.voice.starts[1].cleaned
    assert player.on_start.await_count == 2


@pytest.mark.parametrize(
    "failure",
    [
        DecoderError("The file contains no supported audio.", retryable=False),
        MediaError("Discord voice was lost."),
    ],
)
async def test_permanent_or_unrelated_failure_is_not_refreshed(retry_player, failure):
    player = retry_player
    first, second = track(), track("two")
    await player.enqueue([first, second])
    await eventually(lambda: len(player.voice.starts) == 1)
    player.voice.finish(failure)
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.current is second
    assert [call.args[0] for call in player.resolver.resolve.await_args_list] == [first, second]
    player.report_error.assert_awaited_once_with(player, first, str(failure))


async def test_unexpected_failure_is_not_retried_or_exposed(retry_player):
    player = retry_player
    first, second = track(), track("two")
    await player.enqueue([first, second])
    await eventually(lambda: len(player.voice.starts) == 1)
    player.voice.finish(ValueError("secret signed stream token"))
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.current is second
    assert [call.args[0] for call in player.resolver.resolve.await_args_list] == [first, second]
    player.report_error.assert_awaited_once()
    assert "secret" not in player.report_error.await_args.args[2]


async def test_direct_url_failure_does_not_retry_the_same_url(retry_player):
    player = retry_player
    direct = track()
    direct = type(direct)(direct.uri, direct.title, direct=True)
    await player.enqueue([direct])
    await eventually(lambda: len(player.voice.starts) == 1)
    player.voice.finish(transient())
    await eventually(lambda: player._runner.done())
    assert len(player.voice.starts) == 1
    player.resolver.resolve.assert_awaited_once_with(direct)
    player.report_error.assert_awaited_once()


async def test_live_stream_refresh_does_not_seek_into_previous_connection(retry_player):
    player = retry_player
    player.resolver.resolve.return_value = Stream("https://example.invalid/live", live=True)
    player.resolver.resolve.side_effect = None
    await player.enqueue([track()])
    await eventually(lambda: len(player.voice.starts) == 1)
    player.source.frames, player.source.position = 100, 2000
    player.voice.finish(transient())
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.position == 0
    assert player.resolver.resolve.await_count == 2
    player.on_start.assert_awaited_once()


async def test_broken_cache_then_remote_refresh_is_bounded_and_preserves_progress(retry_player):
    player = retry_player
    selected = track()
    player.cache = SimpleNamespace(
        epoch=0,
        get=Mock(return_value=Stream("/cache/broken.ogg", local=True)),
        schedule=Mock(),
        invalidate=Mock(),
    )
    await player.enqueue([selected])
    await eventually(lambda: len(player.voice.starts) == 1)
    player.source.frames, player.source.position = 2, 40
    player.voice.finish(MediaError("Corrupt local copy"))
    await eventually(lambda: len(player.voice.starts) == 2)
    assert not player.source.stream.local and player.position == 40
    player.source.frames, player.source.position = 3, 100
    player.voice.finish(transient())
    await eventually(lambda: len(player.voice.starts) == 3)
    assert player.position == 100
    player.cache.invalidate.assert_called_once_with(song_key(player.guild.id, selected))
    assert player.resolver.resolve.await_count == 2
    player.on_start.assert_awaited_once()
    assert [call.args[2] for call in player.on_finish.await_args_list] == [40, 60]
    player.report_error.assert_not_awaited()
    player.voice.finish(transient())
    await eventually(lambda: player._runner.done())
    assert len(player.voice.starts) == 3 and player.resolver.resolve.await_count == 2
    player.report_error.assert_awaited_once()


@pytest.mark.parametrize("action", ["skip", "stop"])
async def test_skip_or_stop_cancels_refresh_without_reporting_old_failure(retry_player, action):
    player = retry_player
    first, second = track(), track("two")
    refresh_started, refresh_cancelled = asyncio.Event(), asyncio.Event()
    resolves = 0

    async def resolve(selected):
        nonlocal resolves
        if selected is first:
            resolves += 1
            if resolves == 2:
                refresh_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    refresh_cancelled.set()
        return Stream(selected.uri)

    player.resolver.resolve.side_effect = resolve
    await player.enqueue([first, second])
    await eventually(lambda: len(player.voice.starts) == 1)
    late = player.voice.after
    player.voice.finish(transient())
    await asyncio.wait_for(refresh_started.wait(), 2)
    assert player.preparing and player.voice.starts[0].cleaned
    await getattr(player, action)()
    late(transient())
    await asyncio.wait_for(refresh_cancelled.wait(), 2)
    if action == "skip":
        await eventually(lambda: len(player.voice.starts) == 2)
        assert player.current is second and not player.queue
    else:
        await eventually(lambda: player._runner.done())
        assert player.current is None and not player.queue
        assert len(player.voice.starts) == 1
    player.report_error.assert_not_awaited()


async def test_refresh_lookup_failure_is_reported_once_and_advances(retry_player):
    player = retry_player
    first, second = track(), track("two")
    player.resolver.resolve.side_effect = [
        Stream(first.uri),
        MediaError("The provider denied this server."),
        Stream(second.uri),
    ]
    await player.enqueue([first, second])
    await eventually(lambda: len(player.voice.starts) == 1)
    player.voice.finish(transient())
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.current is second
    player.report_error.assert_awaited_once_with(player, first, "The provider denied this server.")
    assert player.resolver.resolve.await_count == 3
