"""Resource admission and deletion races at the actual AudioPlus command boundary."""

import asyncio
import time
from copy import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_context, make_member
from redbot.core import commands
from test_audioplus import audio_runtime as native_fixture
from test_native_audio import FakeVoice, eventually, track

from audioplus import AudioPlus
from audioplus.features import saved_track
from audioplus.player import GuildPlayer
from audioplus.requests import MAX_GUILD_REQUESTS, MAX_REQUESTS, PrivacyBarrier, PrivacyInterrupted

audio_runtime = native_fixture


async def test_play_and_search_share_bounded_admission_before_voice_and_lookup(audio_runtime):
    cog, player, ctx = audio_runtime
    release = asyncio.Event()

    async def search(query, **kwargs):
        await release.wait()
        return []

    cog._resolver.search = AsyncMock(side_effect=search)
    original = cog._fetch_or_connect_player
    cog._fetch_or_connect_player = AsyncMock(side_effect=original)
    pending = [
        asyncio.create_task(cog._queue_query(ctx, "song")) for _ in range(MAX_GUILD_REQUESTS)
    ]
    await eventually(lambda: len(cog._lookups) == MAX_GUILD_REQUESTS)
    assert player._queue_requests == MAX_GUILD_REQUESTS
    with pytest.raises(commands.CommandError, match="too many requests"):
        await cog._queue_query(ctx, "excess")
    with pytest.raises(commands.CommandError, match="too many requests"):
        await cog.search.callback(cog, ctx, query="excess")
    with pytest.raises(commands.CommandError, match="too many requests"):
        await cog.favorite_add.callback(cog, ctx, query="excess")
    assert cog._fetch_or_connect_player.await_count == MAX_GUILD_REQUESTS
    assert cog._resolver.search.await_count == MAX_GUILD_REQUESTS
    release.set()
    await asyncio.gather(*pending)
    assert not cog._lookups and not cog._requests.tasks and not cog._privacy.operations
    assert player._queue_requests == 0


async def test_global_budget_cannot_be_bypassed_with_different_guilds(bot, guild):
    cog = AudioPlus(bot)
    release = asyncio.Event()

    async def search(query, **kwargs):
        await release.wait()
        return []

    cog._resolver.search = AsyncMock(side_effect=search)
    contexts = []
    for index in range(MAX_REQUESTS):
        other = copy(guild)
        other.id = guild.id + index // MAX_GUILD_REQUESTS
        contexts.append(make_context(other, author=make_member(other, 1000 + index)))
    pending = [asyncio.create_task(cog.search.callback(cog, ctx, query="song")) for ctx in contexts]
    try:
        await eventually(lambda: len(cog._lookups) == MAX_REQUESTS)
        other = copy(guild)
        other.id = guild.id + 10
        with pytest.raises(commands.CommandError, match="too many requests"):
            await cog.search.callback(
                cog, make_context(other, author=make_member(other)), query="excess"
            )
        assert cog._resolver.search.await_count == MAX_REQUESTS
        release.set()
        await asyncio.gather(*pending)
        assert not cog._requests.tasks and not cog._requests.guilds
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await cog.cog_unload()


async def test_unload_cancels_admitted_requests_waiting_for_voice_lock(audio_runtime):
    cog, player, ctx = audio_runtime
    lock = cog._player_locks[ctx.guild.id]
    await lock.acquire()
    pending = [
        asyncio.create_task(cog._queue_query(ctx, "song")) for _ in range(MAX_GUILD_REQUESTS)
    ]
    await eventually(lambda: len(cog._requests.tasks) == MAX_GUILD_REQUESTS)
    with pytest.raises(commands.CommandError, match="too many requests"):
        await cog._queue_query(ctx, "excess")
    await cog.cog_unload()
    outcomes = await asyncio.gather(*pending, return_exceptions=True)
    lock.release()
    assert all(isinstance(result, asyncio.CancelledError) for result in outcomes)
    assert not cog._requests.tasks and not cog._privacy.users and not cog._lookups
    assert player._queue_requests == 0


async def test_delete_waits_for_cancel_resistant_favorite_and_rejects_old_overlap(bot, guild):
    cog = AudioPlus(bot)
    owner, other = make_member(guild, 123), make_member(guild, 456)
    ctx = make_context(guild, author=owner)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    group = cog.config.guild(guild)
    await group.favorites.set(
        {"123": [saved_track(track("old"))], "456": [saved_track(track("other"))]}
    )

    async def delayed(query):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return [track("late")]

    cog._load_tracks = delayed
    old = asyncio.create_task(cog.favorite_add.callback(cog, ctx, query="late"))
    await entered.wait()
    deletion = asyncio.create_task(cog.red_delete_data_for_user(requester="user", user_id=owner.id))
    await cancelled.wait()
    assert not deletion.done()
    with pytest.raises(commands.CommandError, match="being deleted"):
        await cog.favorite_add.callback(cog, ctx, query="new")
    release.set()
    await old
    await deletion
    assert await group.favorites() == {"456": [saved_track(track("other"))]}
    assert not cog._privacy.users and not cog._privacy.deleting
    assert not await cog.red_get_data_for_user(user_id=owner.id)
    # A deliberate new request after deletion can create fresh data normally.
    cog._load_tracks = AsyncMock(return_value=[track("new")])
    await cog.favorite_add.callback(cog, ctx, query="new")
    assert (await group.favorites())["123"][0]["title"] == "new"
    assert (await cog.red_get_data_for_user(user_id=other.id))["audioplus.json"]
    await cog.cog_unload()


async def test_delete_cancels_playlist_save_waiting_for_player_lock(audio_runtime):
    cog, player, ctx = audio_runtime
    player.current = track("one")
    await player.lock.acquire()
    task = asyncio.create_task(cog.playlist_save.callback(cog, ctx, "late"))
    await eventually(lambda: ctx.author.id in cog._privacy.users)
    await cog.red_delete_data_for_user(requester="user", user_id=ctx.author.id)
    player.lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await cog.config.guild(ctx.guild).playlists()


async def test_delete_cancels_cross_guild_personal_work_but_keeps_other_requester(bot, guild):
    cog = AudioPlus(bot)
    release = asyncio.Event()

    async def search(query):
        await release.wait()
        return []

    cog._resolver.search = AsyncMock(side_effect=search)
    first = make_context(guild, author=make_member(guild, 123))
    second_guild = copy(guild)
    second_guild.id = guild.id + 1
    second = make_context(second_guild, author=make_member(second_guild, 123))
    other = make_context(guild, author=make_member(guild, 456))
    # Suggestions invoke the real _load_tracks; no voice connection is needed.
    for server in (guild, second_guild):
        await cog.config.guild(server).server_playlists.set(
            {"party": {"tracks": [], "suggestions": []}}
        )
    tasks = [
        asyncio.create_task(cog.server_playlist_suggest.callback(cog, ctx, "party", query="song"))
        for ctx in (first, second, other)
    ]
    await eventually(lambda: len(cog._lookups) == 3)
    await cog.red_delete_data_for_user(requester="user", user_id=123)
    assert tasks[0].cancelled() and tasks[1].cancelled() and not tasks[2].done()
    assert len(cog._lookups) == 1
    tasks[2].cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert not cog._privacy.users and not cog._requests.tasks
    await cog.cog_unload()


async def test_delete_invalidates_recovery_started_by_another_dj(bot, guild):
    cog = AudioPlus(bot)
    dj = make_member(guild, 999)
    dj.guild_permissions.manage_guild = True
    ctx = make_context(guild, author=dj)
    group = cog.config.guild(guild)
    await group.continuity.recovery.set(True)
    await group.recovery.set(
        {
            "at": int(time.time()),
            "tracks": [saved_track(track("old")), saved_track(track("other"))],
            "position": 0,
            "paused": False,
            "volume": 100,
            "repeat": "off",
            "requesters": [123, 456],
        }
    )
    entered = asyncio.Event()

    async def connect(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    cog._fetch_or_connect_player = connect
    task = asyncio.create_task(cog.recover_queue.callback(cog, ctx))
    await entered.wait()
    await cog.red_delete_data_for_user(requester="user", user_id=123)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await group.recovery())["requesters"] == [0, 456]
    assert not await cog._continuity_user_data(123)
    assert await cog._continuity_user_data(456)
    await cog.cog_unload()


async def test_history_writer_is_serialized_with_deletion_and_repeat_stays_anonymous(bot, guild):
    cog = AudioPlus(bot)
    voice = FakeVoice(guild.id)
    voice.guild = guild
    player = GuildPlayer(voice, SimpleNamespace(), AsyncMock())
    cog._players[guild.id] = player
    player.current = track("song")
    player._requesters[id(player.current)] = 123
    entered, release = asyncio.Event(), asyncio.Event()

    async def disabled(*args):
        entered.set()
        await release.wait()
        return False

    cog.bot.cog_disabled_in_guild = AsyncMock(side_effect=disabled)
    writer = asyncio.create_task(cog._record_listening_history(player))
    await entered.wait()
    deletion = asyncio.create_task(cog.red_delete_data_for_user(requester="user", user_id=123))
    await eventually(lambda: 123 in cog._privacy.deleting)
    assert not deletion.done()
    release.set()
    await writer
    await deletion
    assert not await cog.config.guild(guild).listening_history()
    assert player._requesters[id(player.current)] == 0
    await cog._record_listening_history(player)
    history = await cog.config.guild(guild).listening_history()
    assert history[0]["requester"] == 0
    assert not await cog.red_get_data_for_user(user_id=123)
    await cog.cog_unload()


async def test_detached_session_summary_cannot_restore_deleted_requesters(audio_runtime):
    cog, player, ctx = audio_runtime
    await cog.config.guild(ctx.guild).music.session_summary.set(True)
    player.context, player.current = ctx, track("one")
    player._requesters[id(player.current)] = ctx.author.id
    await cog._session_started(player)
    entered, release = asyncio.Event(), asyncio.Event()

    async def disabled(*args):
        entered.set()
        await release.wait()
        return False

    cog.bot.cog_disabled_in_guild = AsyncMock(side_effect=disabled)
    summary = asyncio.create_task(cog._post_music_session(player))
    await entered.wait()
    await cog.red_delete_data_for_user(requester="user", user_id=ctx.author.id)
    release.set()
    with pytest.raises(PrivacyInterrupted):
        await summary
    assert ctx.guild.id not in cog._last_sessions
    assert not cog._session_user_data(ctx.author.id)
    assert not await cog.red_get_data_for_user(user_id=ctx.author.id)


async def test_watchdog_setup_waiting_for_dm_cannot_recreate_deleted_owner(bot, guild):
    cog = AudioPlus(bot)
    ctx = make_context(guild, author=make_member(guild, 123))
    entered = asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    ctx.author.send = AsyncMock(side_effect=send)
    task = asyncio.create_task(cog.audiocheck_enable.callback(cog, ctx))
    await entered.wait()
    await cog.red_delete_data_for_user(requester="user", user_id=ctx.author.id)
    with pytest.raises(asyncio.CancelledError):
        await task
    state = await cog.config.watchdog()
    assert state["recipient_id"] is None and not state["enabled"]
    assert not await cog.red_get_data_for_user(user_id=ctx.author.id)
    await cog.cog_unload()


async def test_delete_anonymizes_rejoin_snapshot_and_keeps_other_queue_tracks(audio_runtime):
    cog, player, ctx = audio_runtime
    current, next_song = track("deleted"), track("other")
    player.current, player.context = current, ctx
    player.queue.append(next_song)
    player._requesters = {id(current): 123, id(next_song): 456}
    ctx.author.id = 999
    ctx.author.guild_permissions.manage_guild = True
    entered = asyncio.Event()

    async def connect(channel):
        entered.set()
        await asyncio.Event().wait()

    cog._connect_voice = connect
    task = asyncio.create_task(cog.audio_rejoin.callback(cog, ctx))
    await entered.wait()
    await cog.red_delete_data_for_user(requester="user", user_id=123)
    with pytest.raises(asyncio.CancelledError):
        await task
    kept = cog._players[ctx.guild.id]
    assert kept._restart[0] is current and list(kept.queue) == [next_song]
    assert kept._requesters == {id(current): 0, id(next_song): 456}
    assert not cog._privacy.operations


async def test_delete_cancels_command_during_hybrid_preparation(audio_runtime, monkeypatch):
    cog, _, ctx = audio_runtime
    entered = asyncio.Event()

    async def preparing(ctx):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("audioplus.cog.prepare_hybrid", preparing)
    task = asyncio.create_task(cog.cog_before_invoke(ctx))
    await entered.wait()
    await cog.red_delete_data_for_user(requester="user", user_id=ctx.author.id)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not cog._privacy.operations and not cog._requests.tasks
    assert not getattr(ctx, "_audioplus_request_scopes", ())


async def test_delete_cancels_shortcut_waiting_to_defer_without_invoking_source(audio_runtime):
    cog, _, ctx = audio_runtime
    ctx.interaction = SimpleNamespace(response=SimpleNamespace(is_done=lambda: False))
    entered = asyncio.Event()

    async def defer():
        entered.set()
        await asyncio.Event().wait()

    ctx.defer = defer
    command = SimpleNamespace(callback=AsyncMock())
    task = asyncio.create_task(cog._invoke_control(ctx, command, query="song"))
    await entered.wait()
    await cog.red_delete_data_for_user(requester="user", user_id=ctx.author.id)
    with pytest.raises(asyncio.CancelledError):
        await task
    command.callback.assert_not_awaited()
    assert not cog._privacy.operations and not cog._requests.tasks


async def test_overlapping_deletion_retries_when_predecessor_is_cancelled():
    barrier = PrivacyBarrier()
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stored = [123]
    erasures = []

    async def old_work():
        async with barrier.operation(123):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                # Simulate a transport settling after more than one cancellation.
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                stored.append(123)

    async def erase():
        async with barrier.deletion(123) as perform:
            if perform:
                stored.clear()
                erasures.append(True)

    old = asyncio.create_task(old_work())
    await entered.wait()
    first = asyncio.create_task(erase())
    await cancelled.wait()
    second = asyncio.create_task(erase())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.sleep(0)
    assert not second.done() and not old.done()
    release.set()
    await old
    await second
    assert erasures == [True] and not stored
    assert not barrier.deleting and not barrier.users


async def test_pending_session_send_is_cancelled_before_delete_returns(audio_runtime):
    cog, player, ctx = audio_runtime
    await cog.config.guild(ctx.guild).music.session_summary.set(True)
    player.context, player.current = ctx, track("one")
    player._requesters[id(player.current)] = ctx.author.id
    await cog._session_started(player)
    entered, delivered = asyncio.Event(), []

    async def send(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        delivered.append(kwargs)

    cog._reply = send
    summary = asyncio.create_task(cog._finish_music_session(player))
    await entered.wait()
    await cog.red_delete_data_for_user(requester="user", user_id=ctx.author.id)
    await summary
    assert not delivered and not cog._session_user_data(ctx.author.id)
    assert not cog._privacy.operations
