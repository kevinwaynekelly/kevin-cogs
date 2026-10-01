"""Advance the idle deadline without sleeping; keep player and command lifetimes real."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redbot.core import commands
from test_audioplus import audio_runtime as native_runtime_fixture
from test_native_audio import FakeSource, FakeVoice, eventually, track
from test_native_audio import native_player as native_player_fixture

import audioplus.cog as audio_module
import audioplus.player as player_module
from audioplus import AudioPlus
from audioplus.player import GuildPlayer
from audioplus.resolver import MediaError, Stream

audio_runtime = native_runtime_fixture
native_player = native_player_fixture


@pytest.fixture
def idle_clock(monkeypatch):
    deadlines = []

    async def delay(seconds):
        deadline = asyncio.get_running_loop().create_future()
        deadlines.append((seconds, deadline))
        await deadline

    # Replace only the player module's clock, not asyncio used by Discord or pytest.
    clock = SimpleNamespace(**vars(asyncio))
    clock.sleep = delay
    monkeypatch.setattr(player_module, "asyncio", clock)
    return deadlines


async def drain(player, deadlines, count=1):
    player.voice.finish()
    await eventually(lambda: len(deadlines) == count)
    assert deadlines[-1][0] == 10
    assert player.voice.is_connected() and not player.closed
    return player._idle_task


async def test_disconnects_ten_seconds_after_the_last_song(audio_runtime, idle_clock):
    cog, player, ctx = audio_runtime
    await player.enqueue([track("one"), track("two")])
    await eventually(lambda: player.playing)
    player.voice.finish()
    await eventually(lambda: len(player.voice.starts) == 2)
    assert not idle_clock and player.current.title == "two"
    timer = await drain(player, idle_clock)
    idle_clock[0][1].set_result(None)
    await asyncio.wait_for(timer, 2)
    player.voice.disconnect.assert_awaited_once_with(force=True)
    assert player.closed and not player.voice.is_connected()
    assert not cog._players and ctx.guild.voice_client is None


async def test_idle_disconnect_voice_event_does_not_close_the_player_twice(
    audio_runtime, idle_clock
):
    cog, player, ctx = audio_runtime
    disconnect = player.voice._disconnect

    async def notify_disconnect(**kwargs):
        await disconnect(**kwargs)
        await cog.on_voice_state_update(
            SimpleNamespace(id=cog.bot.user.id, guild=ctx.guild),
            SimpleNamespace(channel=player.voice.channel),
            SimpleNamespace(channel=None),
        )

    player.voice.disconnect.side_effect = notify_disconnect
    await player.enqueue([track()])
    await eventually(lambda: player.playing)
    timer = await drain(player, idle_clock)
    idle_clock[0][1].set_result(None)
    await asyncio.wait_for(timer, 2)
    player.voice.disconnect.assert_awaited_once_with(force=True)
    assert not cog._players


async def test_new_song_cancels_the_countdown_and_gets_a_fresh_deadline(native_player, idle_clock):
    player = native_player
    await player.enqueue([track()])
    await eventually(lambda: player.playing)
    previous_timer = await drain(player, idle_clock)
    await player.enqueue([track("two")])
    await eventually(lambda: player.playing)
    assert previous_timer.done() and idle_clock[0][1].cancelled()
    player.voice.disconnect.assert_not_awaited()
    timer = await drain(player, idle_clock, 2)
    idle_clock[1][1].set_result(None)
    await asyncio.wait_for(timer, 2)
    player.voice.disconnect.assert_awaited_once_with(force=True)


async def test_stream_preparation_and_paused_playback_do_not_start_idle_timer(
    native_player, idle_clock
):
    player = native_player
    started, release = asyncio.Event(), asyncio.Event()

    async def resolve(selected):
        started.set()
        await release.wait()
        return Stream(selected.uri)

    player.resolver.resolve.side_effect = resolve
    await player.enqueue([track()])
    await started.wait()
    assert player.preparing and not idle_clock
    release.set()
    await eventually(lambda: player.playing)
    player.voice.pause()
    await player.enqueue([track("two")])
    assert player.paused and not idle_clock
    player.voice.resume()
    player.voice.finish()
    await eventually(lambda: len(player.voice.starts) == 2)
    assert not idle_clock
    await drain(player, idle_clock)


@pytest.mark.parametrize("mode", ["track", "queue"])
async def test_repeat_keeps_playing_until_stopped(native_player, idle_clock, mode):
    player = native_player
    player.repeat = mode
    await player.enqueue([track()])
    await eventually(lambda: player.playing)
    player.voice.finish()
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.current.title == "one" and not idle_clock
    await player.stop()
    await eventually(lambda: len(idle_clock) == 1)
    timer = player._idle_task
    idle_clock[0][1].set_result(None)
    await asyncio.wait_for(timer, 2)
    assert player.closed and not player.voice.is_connected()


@pytest.mark.parametrize("outcome", ["tracks", "empty", "error", "cancelled"])
async def test_pending_song_search_cancels_idle_until_it_finishes(
    audio_runtime, idle_clock, outcome
):
    cog, player, ctx = audio_runtime
    await player.enqueue([track()])
    await eventually(lambda: player.playing)
    previous_timer = await drain(player, idle_clock)
    started, release = asyncio.Event(), asyncio.Event()

    async def search(query):
        started.set()
        await release.wait()
        if outcome == "error":
            raise MediaError("Search failed")
        return [track("two")] if outcome == "tracks" else []

    cog._resolver.search = search
    task = asyncio.create_task(AudioPlus.audio_play.callback(cog, ctx, query="two"))
    await started.wait()
    assert previous_timer.done() and idle_clock[0][1].cancelled()
    assert player._queue_requests == 1 and player._idle_task is None
    assert len(idle_clock) == 1
    if outcome == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        if outcome == "error":
            with pytest.raises(commands.CommandError, match="Search failed"):
                await task
        else:
            await task
    assert player._queue_requests == 0
    player.voice.disconnect.assert_not_awaited()
    if outcome == "tracks":
        await eventually(lambda: player.playing)
        await drain(player, idle_clock, 2)
    else:
        await eventually(lambda: len(idle_clock) == 2)
    timer = player._idle_task
    idle_clock[1][1].set_result(None)
    await asyncio.wait_for(timer, 2)
    assert player.closed and not cog._players


async def test_concurrent_searches_keep_connection_until_both_are_finished(
    audio_runtime, idle_clock
):
    cog, player, ctx = audio_runtime
    started = {name: asyncio.Event() for name in ("one", "two")}
    release = {name: asyncio.Event() for name in started}

    async def search(query):
        name = query.rsplit(":", 1)[-1]
        started[name].set()
        await release[name].wait()
        return []

    cog._resolver.search = search
    tasks = {
        name: asyncio.create_task(AudioPlus.audio_play.callback(cog, ctx, query=name))
        for name in started
    }
    try:
        await asyncio.gather(*(event.wait() for event in started.values()))
        assert player._queue_requests == 2
        release["one"].set()
        await tasks["one"]
        assert player._queue_requests == 1 and not idle_clock
        release["two"].set()
        await tasks["two"]
        await eventually(lambda: len(idle_clock) == 1)
        assert idle_clock[0][0] == 10 and player.voice.is_connected()
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


async def test_failed_voice_setup_releases_the_pending_queue_request(audio_runtime, idle_clock):
    cog, player, ctx = audio_runtime
    await player.enqueue([track()])
    await eventually(lambda: player.playing)
    previous = await drain(player, idle_clock)
    cog._stage_unsuppress_if_needed = AsyncMock(side_effect=RuntimeError("Voice setup failed"))
    with pytest.raises(commands.CommandError, match="Discord voice could not connect"):
        await AudioPlus.audio_play.callback(cog, ctx, query="two")
    assert player._queue_requests == 0
    await eventually(lambda: len(idle_clock) == 2)
    assert previous.done() and idle_clock[0][1].cancelled()
    timer = player._idle_task
    idle_clock[1][1].set_result(None)
    await asyncio.wait_for(timer, 2)
    assert player.closed and not cog._players


@pytest.mark.parametrize("action", ["disconnect", "unload"])
async def test_manual_disconnect_and_unload_cancel_idle_timer(audio_runtime, idle_clock, action):
    cog, player, ctx = audio_runtime
    await player.enqueue([track()])
    await eventually(lambda: player.playing)
    timer = await drain(player, idle_clock)
    if action == "disconnect":
        await AudioPlus.audio_leave.callback(cog, ctx)
    else:
        await cog.cog_unload()
    assert timer.done() and idle_clock[0][1].cancelled()
    assert not cog._players and player.closed
    player.voice.disconnect.assert_awaited_once_with(force=True)


async def test_expired_timer_rechecks_new_queue_before_disconnecting(audio_runtime, idle_clock):
    cog, player, ctx = audio_runtime
    await player.enqueue([track()])
    await eventually(lambda: player.playing)
    timer = await drain(player, idle_clock)
    async with cog._player_locks[ctx.guild.id]:
        idle_clock[0][1].set_result(None)
        # Let the timeout expire while its disconnect waits for the guild lock.
        await asyncio.sleep(0)
        await player.enqueue([track("two")])
    await eventually(lambda: player.playing)
    assert timer.done() and player.current.title == "two"
    player.voice.disconnect.assert_not_awaited()


async def test_idle_cleanup_leaves_another_cogs_voice_connection_alone(audio_runtime, idle_clock):
    cog, player, ctx = audio_runtime
    await player.enqueue([track()])
    await eventually(lambda: player.playing)
    timer = await drain(player, idle_clock)
    foreign = FakeVoice()
    ctx.guild.voice_client = foreign
    idle_clock[0][1].set_result(None)
    await asyncio.wait_for(timer, 2)
    assert player.closed and not cog._players
    player.voice.disconnect.assert_not_awaited()
    foreign.disconnect.assert_not_awaited()
    assert foreign.is_connected()


async def test_each_guild_has_its_own_idle_timer(native_player, idle_clock):
    one = native_player
    two = GuildPlayer(FakeVoice(456), one.resolver, AsyncMock(), source_factory=FakeSource)
    try:
        await one.enqueue([track()])
        await two.enqueue([track()])
        await eventually(lambda: one.playing and two.playing)
        await drain(one, idle_clock)
        first = one._idle_task
        await drain(two, idle_clock, 2)
        second = two._idle_task
        idle_clock[0][1].set_result(None)
        await asyncio.wait_for(first, 2)
        assert one.closed and not two.closed and two.voice.is_connected()
        two.voice.disconnect.assert_not_awaited()
        await two.close()
        assert second.done() and idle_clock[1][1].cancelled()
    finally:
        await two.close()


async def test_play_reconnects_after_automatic_disconnect(audio_runtime, idle_clock, monkeypatch):
    cog, old, ctx = audio_runtime
    await old.enqueue([track()])
    await eventually(lambda: old.playing)
    timer = await drain(old, idle_clock)
    idle_clock[0][1].set_result(None)
    await asyncio.wait_for(timer, 2)
    new_voice = FakeVoice()
    new_voice.guild, new_voice.channel = ctx.guild, old.voice.channel

    async def connect(**kwargs):
        ctx.guild.voice_client = new_voice
        return new_voice

    old.voice.channel.connect = AsyncMock(side_effect=connect)
    monkeypatch.setattr(
        audio_module,
        "GuildPlayer",
        lambda voice, resolver, report, **kwargs: GuildPlayer(
            voice, resolver, report, source_factory=FakeSource, **kwargs
        ),
    )
    cog._resolver.search = AsyncMock(return_value=[track("two")])
    await AudioPlus.audio_play.callback(cog, ctx, query="two")
    new = cog._get_player(ctx.guild)
    await eventually(lambda: new.playing)
    assert new is not old and new.current.title == "two"
    assert new.voice is new_voice and not new.closed
