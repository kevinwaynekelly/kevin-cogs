"""Search picks, three-requester fairness, and cancellation of late suggestions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_member
from test_audioplus import audio_runtime as native_fixture
from test_native_audio import FakeSource, FakeVoice, eventually, track

from audioplus.features import SearchView
from audioplus.player import GuildPlayer
from audioplus.resolver import MediaResolver

audio_runtime = native_fixture


async def test_search_limit_is_bounded_without_changing_default_queries():
    resolver = MediaResolver()
    resolver._extract = AsyncMock(return_value={"entries": []})
    for limit, expected in [
        (1, "ytsearch1:roar"),
        (10, "ytsearch10:roar"),
        (100, "ytsearch10:roar"),
    ]:
        await resolver.search("roar", limit=limit)
        assert resolver._extract.await_args.args[0] == expected
    await resolver.search("scsearch:roar", limit=5)
    assert resolver._extract.await_args.args[0] == "scsearch5:roar"


async def test_fair_queue_rotates_all_requesters_and_preserves_each_order(audio_runtime):
    cog, player, ctx = audio_runtime
    await cog.audioset_fairqueue.callback(cog, ctx, True)
    await player.enqueue([track("a1"), track("a2"), track("a3")], ctx)
    await eventually(lambda: player.playing)
    ctx.author = make_member(ctx.guild, 2)
    await player.enqueue([track("b1"), track("b2")], ctx)
    ctx.author = make_member(ctx.guild, 3)
    await player.enqueue([track("c1"), track("c2")], ctx)
    assert [song.title for song in player.queue] == ["b1", "c1", "a2", "b2", "c2", "a3"]
    expected = ["b1", "c1", "a2", "b2", "c2", "a3"]
    for index, title in enumerate(expected, 2):
        player.voice.finish()
        await eventually(lambda: len(player.voice.starts) == index)
        assert player.current.title == title
    snapshot = cog._snapshot(player)
    assert len(snapshot[-1][0]) <= 7


async def test_autoplay_excludes_recent_sources_and_stop_ignores_late_result(audio_runtime):
    cog, player, ctx = audio_runtime
    player.on_end = cog._autoplay_next
    player.voice.channel.members = [ctx.author]
    await cog.audioset_autoplay.callback(cog, ctx, True)
    cog._resolver.search = AsyncMock(return_value=[track("one"), track("two")])
    await cog._enqueue(player, [track("one")], ctx)
    await eventually(lambda: player.playing)
    player.voice.finish()
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.current.title == "two"
    started, release = asyncio.Event(), asyncio.Event()

    async def search(*args, **kwargs):
        started.set()
        await release.wait()
        return [track("late")]

    cog._resolver.search = search
    player.voice.finish()
    await started.wait()
    await player.stop()
    release.set()
    await eventually(lambda: player._runner.done())
    assert not player.queue and len(player.voice.starts) == 2


async def test_reconnect_keeps_requesters_and_fair_order(audio_runtime):
    cog, old, ctx = audio_runtime
    await cog.audioset_fairqueue.callback(cog, ctx, True)
    await old.enqueue([track("a1"), track("a2")], ctx)
    await eventually(lambda: old.playing)
    first_uid = ctx.author.id
    ctx.author = make_member(ctx.guild, 2)
    await old.enqueue([track("b1"), track("b2")], ctx)
    snapshot = cog._snapshot(old)
    await old.close()
    voice = FakeVoice(ctx.guild.id)
    voice.guild, voice.channel = ctx.guild, old.voice.channel
    ctx.guild.voice_client = voice
    player = GuildPlayer(
        voice, cog._resolver, cog._report_playback_failure, source_factory=FakeSource
    )
    cog._players[ctx.guild.id] = player
    await cog._restore(player, snapshot)
    await eventually(lambda: player.playing)
    assert player.current.title == "a1" and player._last_requester == first_uid
    assert [song.title for song in player.queue] == ["b1", "a2", "b2"]
    assert player._requesters[id(player.queue[0])] == 2


@pytest.mark.parametrize(
    "enabled,listeners,disabled", [(False, True, False), (True, False, False), (True, True, True)]
)
async def test_autoplay_requires_opt_in_and_a_listener(audio_runtime, enabled, listeners, disabled):
    cog, player, ctx = audio_runtime
    player.autoplay = enabled
    player.voice.channel.members = [ctx.author] if listeners else []
    cog._resolver.search = AsyncMock()
    cog.bot.cog_disabled_in_guild.return_value = disabled
    await cog._autoplay_next(player, track())
    cog._resolver.search.assert_not_awaited()


async def test_unload_cancels_autoplay_lookup(audio_runtime):
    cog, player, ctx = audio_runtime
    player.on_end = cog._autoplay_next
    player.voice.channel.members = [ctx.author]
    await cog.audioset_autoplay.callback(cog, ctx, True)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def search(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    cog._resolver.search = search
    await cog._enqueue(player, [track()], ctx)
    await eventually(lambda: player.playing)
    player.voice.finish()
    await started.wait()
    await cog.cog_unload()
    assert cancelled.is_set() and player.closed and player._runner.done()


async def test_search_waits_for_a_choice_and_cannot_queue_twice(audio_runtime, monkeypatch):
    cog, player, ctx = audio_runtime
    cog._resolver.search = AsyncMock(return_value=[track("one"), track("two")])
    ctx.send.return_value = SimpleNamespace(edit=AsyncMock())
    await cog.search.callback(cog, ctx, query="songs")
    assert not player.queue and not player.current
    view = next(view for view in cog._views if isinstance(view, SearchView))
    checked = AsyncMock(return_value=ctx)
    monkeypatch.setattr("audioplus.features.component_context", checked)
    cog._queue_saved = AsyncMock()
    interaction = SimpleNamespace(
        user=ctx.author,
        guild_id=ctx.guild.id,
        response=SimpleNamespace(is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    selector = view.children[0]
    selector._values = ["1"]
    await selector.callback(interaction)
    await selector.callback(interaction)
    assert cog._queue_saved.await_count == 1
    assert cog._queue_saved.await_args.args[1][0].title == "two"
    assert checked.await_args.kwargs["owner_id"] == ctx.author.id
    assert view.is_finished() and view not in cog._views


async def test_search_picker_rejects_another_guild(audio_runtime, monkeypatch):
    cog, _, ctx = audio_runtime
    view = SearchView(cog, ctx, [track()])
    checked = AsyncMock()
    monkeypatch.setattr("audioplus.features.component_context", checked)
    interaction = SimpleNamespace(
        user=ctx.author,
        guild_id=ctx.guild.id + 1,
        response=SimpleNamespace(is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await view.children[0].callback(interaction)
    checked.assert_not_awaited()
