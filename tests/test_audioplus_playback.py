"""Exercise commands against the native engine; Discord and providers are mocked."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redbot.core import commands
from test_audioplus import audio_runtime as native_runtime_fixture
from test_native_audio import FakeSource, FakeVoice, eventually, track

import audioplus.cog as audio_module
from audioplus import AudioPlus
from audioplus.player import GuildPlayer
from audioplus.resolver import MediaError, Track

audio_runtime = native_runtime_fixture


@pytest.mark.parametrize(
    "query,expected",
    [
        ("roar", "ytsearch1:roar"),
        ("ytsearch:roar", "ytsearch1:roar"),
        ("ytmsearch:roar", "ytsearch1:roar"),
        ("scsearch:roar", "scsearch1:roar"),
        ("https://youtube.com/watch?v=abc", "https://youtube.com/watch?v=abc"),
    ],
)
async def test_play_routes_native_search_and_starts_once(audio_runtime, query, expected):
    cog, player, ctx = audio_runtime
    cog._resolver.search = AsyncMock(return_value=[track()])
    await AudioPlus.audio_play.callback(cog, ctx, query=query)
    await eventually(lambda: player.playing)
    cog._resolver.search.assert_awaited_once_with(expected)
    assert player.current.title == "one" and player.context is ctx and len(player.voice.starts) == 1


@pytest.mark.parametrize("embeds", [True, False])
async def test_play_identifies_the_selected_track_without_exposing_stream_url(
    audio_runtime, embeds
):
    cog, player, ctx = audio_runtime
    selected = Track(
        "https://www.youtube.com/watch?v=public-video",
        "Song [live]",
        "Artist *name*",
        268000,
        "youtube",
    )
    ctx.embed_requested = AsyncMock(return_value=embeds)
    cog._resolver.search = AsyncMock(return_value=[selected])
    cog._resolver.resolve = AsyncMock(
        return_value=SimpleNamespace(url="https://media.invalid/audio?token=private-secret")
    )
    await AudioPlus.audio_play.callback(cog, ctx, query="song")
    await eventually(lambda: player.playing)
    reply = ctx.send.await_args
    output = reply.kwargs["embed"].description if embeds else reply.args[0]
    assert "Song \\[live\\]" in output and "Artist \\*name\\*" in output
    assert selected.uri in output and "4:28" in output
    assert "private-secret" not in output and "media.invalid" not in output


async def test_playlist_queues_every_result_in_order(audio_runtime):
    cog, player, ctx = audio_runtime
    cog._resolver.search = AsyncMock(return_value=[track(str(i)) for i in range(3)])
    await AudioPlus.audio_play.callback(cog, ctx, query="https://youtube.com/playlist?list=abc")
    await eventually(lambda: player.playing)
    assert player.current.title == "0" and [tr.title for tr in player.queue] == ["1", "2"]
    assert "Queued 3" in ctx.send.await_args.kwargs["embed"].description
    assert all(f"**[{i}]" in ctx.send.await_args.kwargs["embed"].description for i in range(3))


async def test_command_skip_advances_once_despite_stale_callback(audio_runtime):
    cog, player, ctx = audio_runtime
    await player.enqueue([track(), track("two")], ctx)
    await eventually(lambda: player.playing)
    late = player.voice.after
    await AudioPlus.audio_skip.callback(cog, ctx)
    await eventually(lambda: len(player.voice.starts) == 2)
    late(None)
    await asyncio.sleep(0)
    assert player.current.title == "two" and len(player.voice.starts) == 2


async def test_command_pause_enqueue_resume_and_volume(audio_runtime):
    cog, player, ctx = audio_runtime
    await player.enqueue([track()], ctx)
    await eventually(lambda: player.playing)
    await AudioPlus.audio_pause.callback(cog, ctx)
    cog._resolver.search = AsyncMock(return_value=[track("two")])
    await AudioPlus.audio_play.callback(cog, ctx, query="two")
    assert player.paused and len(player.voice.starts) == 1
    await AudioPlus.audio_volume.callback(cog, ctx, value=400)
    assert player.volume == player.source.volume == 400
    await AudioPlus.audio_volume.callback(cog, ctx, value=5000)
    assert player.volume == 1000
    await AudioPlus.audio_resume.callback(cog, ctx)
    assert player.playing and not player.paused


async def test_provider_failure_reports_in_request_channel_and_continues(audio_runtime):
    cog, player, ctx = audio_runtime
    await player.enqueue([track(), track("two")], ctx)
    await eventually(lambda: player.playing)
    player.voice.finish(MediaError("Provider rejected the stream"))
    await eventually(lambda: len(player.voice.starts) == 2)
    message = ctx.send.await_args.kwargs["embed"]
    assert message.title == "AudioPlus · Playback failed"
    assert "Provider rejected" in message.description and "!audiostatus" in message.description


async def test_source_lookup_error_is_an_actionable_command_error(audio_runtime):
    cog, player, ctx = audio_runtime
    cog._resolver.search = AsyncMock(side_effect=MediaError("YouTube extraction failed"))
    with pytest.raises(commands.CommandError, match="YouTube extraction failed"):
        await AudioPlus.audio_play.callback(cog, ctx, query="roar")
    assert not player.queue and not player.voice.starts


async def test_playerstate_and_nowplaying_report_local_state(audio_runtime):
    cog, player, ctx = audio_runtime
    await player.enqueue([track()], ctx)
    await eventually(lambda: player.playing)
    player.voice.pause()
    await AudioPlus.audio_playerstate.callback(cog, ctx)
    output = ctx.send.await_args.kwargs["embed"].description
    assert "Native player" in output and "Playing: `False`" in output and "Paused: `True`" in output
    await AudioPlus.audio_nowplaying.callback(cog, ctx)
    fields = ctx.send.await_args.kwargs["embed"].fields
    assert any(field.name == "Playback" and field.value == "Paused" for field in fields)


async def test_rejoin_restores_track_position_volume_pause_and_queue(audio_runtime, monkeypatch):
    cog, old, ctx = audio_runtime
    await old.enqueue([track(), track("two")], ctx)
    await eventually(lambda: old.playing)
    old.source.position = 42000
    await old.set_volume(175)
    old.voice.pause()
    guild, channel = ctx.guild, old.voice.channel
    new_voice = FakeVoice()
    new_voice.guild, new_voice.channel = guild, channel

    async def disconnect(**kwargs):
        old.voice.connected = False
        old.voice.stop()
        guild.voice_client = None

    old.voice.disconnect.side_effect = disconnect

    async def connect(**kwargs):
        guild.voice_client = new_voice
        return new_voice

    channel.connect = AsyncMock(side_effect=connect)
    monkeypatch.setattr(
        audio_module,
        "GuildPlayer",
        lambda voice, resolver, report: GuildPlayer(
            voice, resolver, report, source_factory=FakeSource
        ),
    )
    assert await cog._rebind_voice(guild)
    new = cog._players[guild.id]
    await eventually(lambda: new.paused)
    assert old.closed and new.current.title == "one" and new.position == 42000
    assert new.volume == 175 and new.queue[0].title == "two" and new.context is ctx


async def test_failed_rejoin_preserves_tracks_for_later_join(audio_runtime, monkeypatch):
    cog, old, ctx = audio_runtime
    await old.enqueue([track(), track("two")], ctx)
    await eventually(lambda: old.playing)
    guild, channel = ctx.guild, old.voice.channel

    async def disconnect(**kwargs):
        old.voice.connected = False
        old.voice.stop()
        guild.voice_client = None

    old.voice.disconnect.side_effect = disconnect
    channel.connect = AsyncMock(side_effect=asyncio.TimeoutError)
    assert not await cog._rebind_voice(guild)
    assert old.closed and old._restart[0].title == "one" and old.queue[0].title == "two"
    new_voice = FakeVoice()
    new_voice.guild, new_voice.channel = guild, channel

    async def connect(**kwargs):
        guild.voice_client = new_voice
        return new_voice

    channel.connect.side_effect = connect
    monkeypatch.setattr(
        audio_module,
        "GuildPlayer",
        lambda voice, resolver, report: GuildPlayer(
            voice, resolver, report, source_factory=FakeSource
        ),
    )
    new, _ = await cog._fetch_or_connect_player(ctx)
    await eventually(lambda: new.playing)
    assert new.current.title == "one" and new.queue[0].title == "two"


async def test_old_failure_cannot_post_after_new_player_takes_over(audio_runtime):
    cog, old, ctx = audio_runtime
    old.context = ctx
    cog._players[ctx.guild.id] = object()
    try:
        await cog._report_playback_failure(old, track(), "old failure")
        ctx.send.assert_not_awaited()
    finally:
        cog._players[ctx.guild.id] = old


async def test_repeat_validation_and_queue_listing(audio_runtime):
    cog, player, ctx = audio_runtime
    await AudioPlus.audio_repeat.callback(cog, ctx, mode="queue")
    assert player.repeat == "queue"
    with pytest.raises(commands.BadArgument):
        await AudioPlus.audio_repeat.callback(cog, ctx, mode="invalid")
    player.queue.extend(track(str(i)) for i in range(15))
    await AudioPlus.audio_queue.callback(cog, ctx)
    output = ctx.send.await_args.kwargs["embed"].description
    assert "1. 0" in output and "10. 9" in output and "5 more" in output
