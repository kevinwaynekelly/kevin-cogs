import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import wavelink
from aiohttp import web
from conftest import make_context
from redbot.core import commands

from audioplus import AudioPlus
from audioplus.constants import NodeConfig


async def test_load_succeeds_without_lavalink_and_closes_http(bot, monkeypatch):
    cog = AudioPlus(bot)
    connect = AsyncMock()
    monkeypatch.setattr(wavelink.Pool, "connect", connect)
    await cog.cog_load()
    session = cog._http
    connect.assert_not_awaited()
    assert not session.closed
    await cog.cog_unload()
    assert session.closed


async def test_connect_is_serialized_and_session_owned_by_cog(bot, monkeypatch):
    cog = AudioPlus(bot)
    await cog.cog_load()
    created = []

    def new_node(**kwargs):
        assert kwargs["session"] is cog._http
        node = SimpleNamespace(status=wavelink.NodeStatus.DISCONNECTED, close=AsyncMock())
        created.append(node)
        return node

    async def connect(*, nodes, client):
        await asyncio.sleep(0)
        nodes[0].status = wavelink.NodeStatus.CONNECTED
        return {}

    monkeypatch.setattr(wavelink, "Node", new_node)
    monkeypatch.setattr(wavelink.Pool, "connect", connect)
    cfg = NodeConfig.from_parts("localhost", 2333, "secret", False)
    await asyncio.gather(*(cog._ensure_nodes(cfg) for _ in range(10)))
    assert len(created) == 1
    await cog.cog_unload()
    created[0].close.assert_awaited_once_with(eject=True)


async def test_failed_pool_connection_is_detected_and_cleaned_up(bot, monkeypatch):
    cog = AudioPlus(bot)
    node = SimpleNamespace(status=wavelink.NodeStatus.DISCONNECTED, close=AsyncMock())
    monkeypatch.setattr(wavelink, "Node", Mock(return_value=node))
    monkeypatch.setattr(wavelink.Pool, "connect", AsyncMock(return_value={}))
    with pytest.raises(commands.CommandError):
        await cog._ensure_nodes(NodeConfig.from_parts("localhost", 2333, "secret", False))
    assert cog._node is None
    node.close.assert_awaited_once_with(eject=True)


async def test_unload_does_not_close_other_cogs_nodes(bot, monkeypatch):
    cog = AudioPlus(bot)
    cog._node = SimpleNamespace(close=AsyncMock())
    own_node = cog._node
    close_pool = AsyncMock()
    monkeypatch.setattr(wavelink.Pool, "close", close_pool)
    await cog.cog_unload()
    own_node.close.assert_awaited_once_with(eject=True)
    close_pool.assert_not_awaited()


async def test_real_http_status_and_response_validation(bot):
    cog = AudioPlus(bot)
    app = web.Application()

    async def valid(request):
        return web.json_response({"version": "test"})

    async def array(request):
        return web.json_response(["bad shape"])

    async def error(request):
        return web.json_response({"error": "denied"}, status=401)

    app.router.add_get("/valid", valid)
    app.router.add_get("/array", array)
    app.router.add_get("/error", error)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    node = SimpleNamespace(uri=f"http://127.0.0.1:{port}", password="secret")
    await cog.cog_load()
    session = cog._http
    try:
        assert await cog._request_json(node, "/valid") == {"version": "test"}
        assert await cog._request_json(node, "/array") is None
        assert await cog._request_json(node, "/error") is None
        assert cog._http is session
    finally:
        await cog.cog_unload()
        await runner.cleanup()


async def test_playlist_queues_all_tracks_and_starts_once(bot, guild, monkeypatch):
    cog = AudioPlus(bot)
    tracks = [Mock(spec=wavelink.Playable) for _ in range(3)]
    player = Mock(spec=wavelink.Player)
    player.guild = guild
    player.queue = wavelink.Queue()
    player.playing = player.paused = False
    player.play = AsyncMock()
    playlist = Mock(spec=wavelink.Playlist)
    playlist.tracks = tracks
    cog._fetch_or_connect_player = AsyncMock(return_value=(player, None))
    fetch = AsyncMock(return_value=playlist)
    monkeypatch.setattr(wavelink.Pool, "fetch_tracks", fetch)
    await AudioPlus.audio_play.callback(
        cog, make_context(guild), query="https://example.invalid/list"
    )
    assert list(player.queue) == tracks[1:]
    player.play.assert_awaited_once_with(tracks[0], paused=False)
    fetch.assert_awaited_once_with("https://example.invalid/list", node=cog._node)


async def test_skip_lets_wavelink_advance_queue_once(bot, guild):
    cog = AudioPlus(bot)
    player = Mock(spec=wavelink.Player)
    player.skip = AsyncMock()
    player.current = SimpleNamespace(title="Track")
    cog._node = player.node = object()
    guild.voice_client = player
    ctx = make_context(guild)
    ctx.voice_client = player
    cog._maybe_start_queue = AsyncMock()
    await AudioPlus.audio_skip.callback(cog, ctx)
    player.skip.assert_awaited_once_with(force=True)
    cog._maybe_start_queue.assert_not_awaited()


async def test_rest_diagnostics_use_own_node_session(bot, guild):
    cog = AudioPlus(bot)
    node = SimpleNamespace(status=wavelink.NodeStatus.CONNECTED, session_id="owned-session")
    cog._node = node
    cog._request_json = AsyncMock(return_value={"state": {}})
    await cog._fetch_player_state(guild.id)
    cog._request_json.assert_awaited_once_with(
        node, f"/v4/sessions/owned-session/players/{guild.id}"
    )


async def test_resume_timeout_is_loaded_and_host_is_validated(bot, guild):
    cog = AudioPlus(bot)
    await cog.config.resume_timeout.set(125)
    assert (await cog._get_node_config()).resume_timeout == 125
    with pytest.raises(commands.BadArgument):
        await AudioPlus.audio_setnode.callback(
            cog, make_context(guild), "http://bad/path", 2333, "secret"
        )


async def test_playback_controls_do_not_touch_another_cogs_player(bot, guild):
    cog = AudioPlus(bot)
    cog._node = object()
    other = Mock(spec=wavelink.Player)
    other.node = object()
    other.skip = AsyncMock()
    guild.voice_client = other
    await AudioPlus.audio_skip.callback(cog, make_context(guild))
    other.skip.assert_not_awaited()
