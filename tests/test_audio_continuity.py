"""Queue persistence, empty-room policy and moderated collaborative playlists."""

import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import make_context, make_member
from redbot.core import commands
from test_audio_hybrid import red_command_runtime as runtime_fixture
from test_native_audio import FakeSource, FakeVoice, eventually, track

from audioplus import AudioPlus
from audioplus.player import GuildPlayer, NativeSource
from audioplus.resolver import Stream

red_command_runtime = runtime_fixture


async def test_checkpoint_never_persists_stream_and_deletion_forgets_requesters(bot, guild):
    cog = AudioPlus(bot)
    voice = FakeVoice(guild.id)
    voice.guild = guild
    player = GuildPlayer(voice, SimpleNamespace(), AsyncMock())
    cog._players[guild.id] = player
    player.current = track("current")
    player.queue.append(track("next"))
    player.source = SimpleNamespace(
        position=12345, stream=Stream("https://secret.invalid/signed?token=x", {})
    )
    player._requesters[id(player.current)] = 123
    await cog.config.guild(guild).continuity.recovery.set(True)
    await cog._save_recovery(player)
    record = await cog.config.guild(guild).recovery()
    assert record["position"] == 12345
    assert len(record["tracks"]) == 2
    assert "secret.invalid" not in str(record)
    await cog._continuity_delete_user(123)
    assert (await cog.config.guild(guild).recovery())["requesters"] == [0, 0]
    assert not await cog._continuity_user_data(123)


async def test_empty_room_auto_pause_resume_and_departure(bot, guild):
    cog = AudioPlus(bot)
    voice = FakeVoice(guild.id)
    voice.guild = guild
    voice.channel.members = []
    player = GuildPlayer(voice, SimpleNamespace(), AsyncMock())
    cog._players[guild.id] = player
    voice.source = object()
    player.current = track("a")
    await cog.config.guild(guild).continuity.empty_pause.set(True)
    await cog._empty_room(player, now=10)
    assert voice.is_paused()
    voice.channel.members = [make_member(guild)]
    await cog._empty_room(player, now=20)
    assert not voice.is_paused()
    # A manually paused track must stay paused after people return.
    voice.pause()
    voice.channel.members = []
    await cog._empty_room(player, now=30)
    voice.channel.members = [make_member(guild)]
    await cog._empty_room(player, now=35)
    assert voice.is_paused()
    voice.channel.members = []
    await cog._empty_room(player, now=40)
    await cog._empty_room(player, now=101)
    voice.disconnect.assert_awaited_once()
    assert guild.id not in cog._players


async def test_shared_playlist_requires_dj_approval_and_keeps_proposer_data(bot, guild):
    cog = AudioPlus(bot)
    ctx = make_context(guild, author=make_member(guild))
    ctx.author.guild_permissions.manage_guild = True
    await cog.server_playlist_create.callback(cog, ctx, "party")
    cog._load_tracks = AsyncMock(return_value=[track("song")])
    ctx.author.guild_permissions.manage_guild = False
    await cog.server_playlist_suggest.callback(cog, ctx, "party", query="song")
    with pytest.raises(commands.CheckFailure):
        await cog.server_playlist_approve.callback(cog, ctx, "party", 1)
    rows = await cog.config.guild(guild).server_playlists()
    assert not rows["party"]["tracks"]
    ctx.author.guild_permissions.manage_guild = True
    await cog.server_playlist_approve.callback(cog, ctx, "party", 1)
    rows = await cog.config.guild(guild).server_playlists()
    assert len(rows["party"]["tracks"]) == 1 and not rows["party"]["suggestions"]
    assert await cog._continuity_user_data(ctx.author.id)
    await cog._continuity_delete_user(ctx.author.id)
    assert (await cog.config.guild(guild).server_playlists())["party"]["tracks"][0]["user"] == 0


async def test_recover_restores_position_queue_and_pause(bot, guild):
    cog = AudioPlus(bot)
    ctx = make_context(guild, author=make_member(guild))
    ctx.author.guild_permissions.manage_guild = True
    voice = FakeVoice(guild.id)
    voice.guild = guild
    resolver = SimpleNamespace(
        resolve=AsyncMock(return_value=Stream("https://media.invalid/song", {}))
    )
    player = GuildPlayer(voice, resolver, AsyncMock(), source_factory=FakeSource)
    cog._fetch_or_connect_player = AsyncMock(return_value=(player, voice.channel))
    first, second = replace(track("a"), length=60000), track("b")
    from audioplus.features import saved_track

    await cog.config.guild(guild).continuity.recovery.set(True)
    await cog.config.guild(guild).recovery.set(
        {
            "at": int(time.time()),
            "tracks": [saved_track(first), saved_track(second)],
            "position": 4000,
            "paused": True,
            "volume": 75,
            "repeat": "off",
            "requesters": [123, 456],
        }
    )
    player.begin_queue_request()
    try:
        await cog.recover_queue.callback(cog, ctx)
        await eventually(lambda: player.source is not None)
        assert player.position == 4000 and player.paused and player.volume == 75
        assert list(player.queue) == [second]
    finally:
        await player.close()


def test_normalization_adds_filter_only_when_requested(monkeypatch):
    factory = Mock()
    monkeypatch.setattr("audioplus.player.discord.FFmpegPCMAudio", factory)
    source = NativeSource(Stream("https://media.invalid/song", {}), volume=100, normalize=True)
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in factory.call_args.kwargs["options"]
    source.cleanup()


async def test_concurrent_decoder_cleanup_waits_for_child_completion(monkeypatch):
    import asyncio
    import threading

    entered, release = threading.Event(), threading.Event()
    audio = Mock()

    def cleanup():
        entered.set()
        assert release.wait(2)

    audio.cleanup.side_effect = cleanup
    monkeypatch.setattr("audioplus.player.discord.FFmpegPCMAudio", Mock(return_value=audio))
    source = NativeSource(Stream("https://media.invalid/song", {}), volume=100)
    first = asyncio.create_task(asyncio.to_thread(source.cleanup))
    await asyncio.to_thread(entered.wait, 2)
    second = asyncio.create_task(asyncio.to_thread(source.cleanup))
    await asyncio.sleep(0)
    assert not source._cleaned
    release.set()
    await asyncio.gather(first, second)
    assert source._cleaned
    audio.cleanup.assert_called_once()
