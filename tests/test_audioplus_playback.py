"""Playback regressions using real Wavelink objects and a local Lavalink HTTP boundary."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
import wavelink
from aiohttp import web
from conftest import make_channel, make_context, make_member
from redbot.core import commands

from audioplus import AudioPlus


def track_payload(identifier="first", source="youtube"):
    return {
        "encoded": f"encoded-{identifier}",
        "info": {
            "identifier": identifier,
            "isSeekable": True,
            "author": "Artist",
            "length": 180000,
            "isStream": False,
            "position": 0,
            "title": f"Track {identifier}",
            "uri": f"https://example.invalid/{identifier}",
            "sourceName": source,
        },
        "pluginInfo": {},
    }


@pytest.fixture
async def audio_runtime(bot, guild):
    state = SimpleNamespace(requests=[], patch_status=200, load_error=None)
    app = web.Application()

    async def load(request):
        state.requests.append(("load", request.query["identifier"]))
        if state.load_error:
            return web.json_response({"loadType": "error", "data": state.load_error})
        return web.json_response({"loadType": "search", "data": [track_payload()]})

    async def update(request):
        state.requests.append(("play", await request.json()))
        if state.patch_status != 200:
            return web.json_response(
                {
                    "timestamp": 0,
                    "status": state.patch_status,
                    "error": "Playback request rejected",
                    "path": request.path,
                },
                status=state.patch_status,
            )
        return web.json_response({})

    app.router.add_get("/v4/loadtracks", load)
    app.router.add_patch("/v4/sessions/test/players/{guild_id}", update)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    cog = AudioPlus(bot)
    await cog.cog_load()
    bot.dispatch = Mock()
    node = wavelink.Node(
        identifier="playback-test",
        uri=f"http://127.0.0.1:{port}",
        password="secret",
        session=cog._http,
        client=bot,
        inactive_player_timeout=None,
    )
    node._status = wavelink.NodeStatus.CONNECTED
    node._session_id = "test"
    cog._node = node
    voice = make_channel(guild, kind=discord.VoiceChannel)
    player = wavelink.Player(client=bot, channel=voice, nodes=[node])
    # Discord's voice handshake is the mocked boundary; Wavelink playback is real.
    player._guild = guild
    player._connected = True
    guild.voice_client = player
    member = make_member(guild)
    member.voice = SimpleNamespace(channel=voice)
    ctx = make_context(guild, make_channel(guild), member)
    ctx.bot = bot
    try:
        yield cog, player, ctx, state
    finally:
        await cog.cog_unload()
        await runner.cleanup()


@pytest.mark.parametrize(
    "query,expected",
    [
        ("  Artist title  ", "ytsearch:Artist title"),
        ("scsearch:Artist title", "scsearch:Artist title"),
        ("ytsearch:Artist title", "ytsearch:Artist title"),
        ("ytmsearch:Artist title", "ytmsearch:Artist title"),
        ("spsearch:Artist title", "spsearch:Artist title"),
        ("https://example.invalid/track.mp3", "https://example.invalid/track.mp3"),
    ],
)
async def test_play_sends_source_query_and_unpaused_track_to_lavalink(
    audio_runtime, query, expected
):
    cog, player, ctx, state = audio_runtime
    await AudioPlus.audio_play.callback(cog, ctx, query=query)
    assert state.requests[0] == ("load", expected)
    request = state.requests[1][1]
    assert request["track"]["encoded"] == "encoded-first"
    assert request["paused"] is False
    assert player.current.title == "Track first"
    assert cog._playback_contexts[ctx.guild.id] == (player, ctx)


async def test_empty_search_does_not_connect_or_request_tracks(bot, guild):
    cog = AudioPlus(bot)
    cog._fetch_or_connect_player = AsyncMock()
    with pytest.raises(commands.BadArgument):
        await AudioPlus.audio_play.callback(cog, make_context(guild), query="  ")
    cog._fetch_or_connect_player.assert_not_awaited()


async def test_idle_player_with_old_pause_flag_starts_music(audio_runtime):
    cog, player, ctx, state = audio_runtime
    await player.pause(True)
    assert player.paused and not player.playing
    await AudioPlus.audio_play.callback(cog, ctx, query="Artist title")
    assert not player.paused
    assert player.playing
    assert state.requests[-1][1]["paused"] is False


async def test_adding_music_does_not_replace_or_resume_paused_track(audio_runtime):
    cog, player, ctx, state = audio_runtime
    await player.play(wavelink.Playable(track_payload("original")), paused=True)
    state.requests.clear()
    await AudioPlus.audio_play.callback(cog, ctx, query="Artist title")
    assert player.current.identifier == "original"
    assert player.paused
    assert [track.identifier for track in player.queue] == ["first"]
    assert all(kind == "load" for kind, _ in state.requests)


async def test_rejected_playback_keeps_queue_order_and_reports_failure(audio_runtime):
    cog, player, ctx, state = audio_runtime
    first, second = (wavelink.Playable(track_payload(name)) for name in ("first", "second"))
    player.queue.put([first, second])
    state.patch_status = 500
    with pytest.raises(commands.CommandError, match="track is still queued"):
        await cog._start_queued_tracks(player)
    assert list(player.queue) == [first, second]
    assert player.current is None
    state.patch_status = 200
    await cog._start_queued_tracks(player)
    assert player.current == first
    assert list(player.queue) == [second]


async def test_cancelled_start_keeps_track_queued(audio_runtime, monkeypatch):
    cog, player, _, _ = audio_runtime
    first = wavelink.Playable(track_payload())
    player.queue.put(first)
    monkeypatch.setattr(player, "play", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await cog._maybe_start_queue(player)
    assert list(player.queue) == [first]


async def test_concurrent_starts_make_one_playback_request(audio_runtime):
    cog, player, _, state = audio_runtime
    player.queue.put([wavelink.Playable(track_payload(name)) for name in ("first", "second")])
    await asyncio.gather(*(cog._maybe_start_queue(player) for _ in range(10)))
    assert len(state.requests) == 1
    assert player.current.identifier == "first"
    assert [track.identifier for track in player.queue] == ["second"]


async def test_source_load_error_is_not_announced_as_success(audio_runtime):
    cog, player, ctx, state = audio_runtime
    state.load_error = {
        "message": "Something broke when playing the track.",
        "severity": "fault",
        "cause": "java.lang.RuntimeException: No supported audio streams available, available types: ",
    }
    with pytest.raises(commands.CommandError, match="no playable audio stream"):
        await AudioPlus.audio_play.callback(cog, ctx, query="Artist title")
    assert not player.queue
    assert not player.playing
    ctx.send.assert_not_awaited()


@pytest.mark.parametrize("embeds", [True, False])
async def test_stream_failure_notifies_request_channel_without_double_advancement(
    audio_runtime, embeds
):
    cog, player, ctx, state = audio_runtime
    ctx.embed_requested = AsyncMock(return_value=embeds)
    await AudioPlus.audio_play.callback(cog, ctx, query="Artist title")
    ctx.send.reset_mock()
    state.requests.clear()
    payload = SimpleNamespace(
        player=player,
        track=player.current,
        exception={"cause": "java.lang.RuntimeException: No supported audio streams available"},
    )
    await cog.on_wavelink_track_exception(payload)
    if embeds:
        embed = ctx.send.await_args.kwargs["embed"]
        output = embed.description
        assert embed.title == "AudioPlus · Playback failed"
    else:
        output = ctx.send.await_args.args[0]
    assert "no playable audio stream" in output
    assert "!audio tone" in output
    assert "scsearch:" in output
    assert not state.requests
    assert ctx.guild.id in cog._last_playback_errors
    await cog.on_wavelink_track_start(SimpleNamespace(player=player, track=player.current))
    assert ctx.guild.id not in cog._last_playback_errors


async def test_foreign_node_failure_does_not_notify_or_change_state(audio_runtime):
    cog, player, ctx, _ = audio_runtime
    other = Mock(spec=wavelink.Player)
    other.node = object()
    other.guild = ctx.guild
    await cog.on_wavelink_track_exception(SimpleNamespace(player=other, track=player.current))
    ctx.send.assert_not_awaited()
    assert not cog._last_playback_errors


async def test_stale_player_failure_does_not_replace_current_error(audio_runtime):
    cog, player, ctx, _ = audio_runtime
    old = Mock(spec=wavelink.Player)
    old.node = player.node
    old.guild = ctx.guild
    cog._playback_contexts[ctx.guild.id] = (player, ctx)
    await cog.on_wavelink_track_exception(
        SimpleNamespace(player=old, track=wavelink.Playable(track_payload()), exception={})
    )
    ctx.send.assert_not_awaited()
    assert not cog._last_playback_errors


async def test_stuck_track_requests_one_skip_and_ignores_late_events(audio_runtime):
    cog, player, ctx, state = audio_runtime
    await AudioPlus.audio_play.callback(cog, ctx, query="Artist title")
    state.requests.clear()
    await cog.on_wavelink_track_stuck(SimpleNamespace(player=player, track=player.current))
    assert state.requests == [("play", {"track": {"encoded": None}})]
    state.requests.clear()
    await cog.on_wavelink_track_stuck(
        SimpleNamespace(player=player, track=wavelink.Playable(track_payload("old")))
    )
    assert not state.requests


async def test_ping_reports_source_plugins_and_latest_failure(audio_runtime):
    cog, _, ctx, _ = audio_runtime
    cog._fetch_lavalink_info = AsyncMock(
        return_value={
            "version": {"semver": "4.2.0"},
            "lavaplayer": "2.2.0",
            "sourceManagers": ["youtube", "soundcloud", "http"],
            "plugins": [{"name": "youtube-plugin", "version": "test-version"}],
        }
    )
    cog._last_playback_errors[ctx.guild.id] = "No playable audio stream"
    await AudioPlus.audio_pingnode.callback(cog, ctx)
    output = ctx.send.await_args.kwargs["embed"].description
    assert "youtube, soundcloud, http" in output
    assert "youtube-plugin test-version" in output
    assert "2.2.0" in output
    assert "Last playback failure" in output


@pytest.mark.parametrize(
    "connected,track,paused,playing",
    [
        (False, True, False, False),
        (True, False, False, False),
        (True, True, True, False),
        (True, True, False, True),
    ],
)
async def test_playerstate_uses_nested_voice_status_and_loaded_track(
    audio_runtime, connected, track, paused, playing
):
    cog, _, ctx, _ = audio_runtime
    cog._fetch_player_state = AsyncMock(
        return_value={
            "state": {"connected": connected},
            "track": track_payload() if track else None,
            "paused": paused,
        }
    )
    await AudioPlus.audio_playerstate.callback(cog, ctx)
    output = ctx.send.await_args.kwargs["embed"].description
    assert f"Connected: `{connected}`" in output
    assert f"Playing: `{playing}`" in output


async def test_leave_clears_transient_playback_information(audio_runtime, monkeypatch):
    cog, player, ctx, _ = audio_runtime
    cog._playback_contexts[ctx.guild.id] = (player, ctx)
    cog._last_playback_errors[ctx.guild.id] = "Old failure"
    monkeypatch.setattr(player, "disconnect", AsyncMock())
    await AudioPlus.audio_leave.callback(cog, ctx)
    assert not cog._playback_contexts
    assert not cog._last_playback_errors
