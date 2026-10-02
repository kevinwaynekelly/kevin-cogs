"""Native lifecycle accounting, temporary session records and playlist permissions."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from conftest import make_message
from redbot.core._cli import parse_cli_flags
from redbot.core._events import init_events
from test_audio_hybrid import red_command_runtime as red_fixture
from test_audioplus import audio_runtime as native_fixture
from test_cog_hybrid import invoke_slash
from test_native_audio import eventually

from audioplus.resolver import Track
from audioplus.sessions import SESSION_TTL, SessionView

audio_runtime = native_fixture
red_command_runtime = red_fixture


def record(guild, member):
    return {
        "id": "abcdef123456",
        "guild": guild.id,
        "at": int(time.time()) - 10,
        "ended": int(time.time()),
        "channel": 456,
        "count": 1,
        "played_ms": 1000,
        "tracks": [
            {
                "requester": member.id,
                "played_ms": 1000,
                "track": {
                    "uri": "https://example.invalid/song",
                    "title": "Song",
                    "author": "Artist",
                    "length": 180000,
                    "source": "http",
                    "direct": False,
                },
            }
        ],
    }


async def test_seek_segments_count_playback_once_and_idle_posts_one_summary(
    audio_runtime, monkeypatch
):
    cog, player, ctx = audio_runtime
    ctx.channel.id = 457
    monkeypatch.setattr("audioplus.player.IDLE_DISCONNECT_SECONDS", 0.02)
    await cog.config.guild(ctx.guild).music.session_summary.set(True)
    player.on_start, player.on_finish = cog._track_started, cog._session_segment
    await player.enqueue([Track("https://example.invalid/song", "Song", "Artist", 180000)], ctx)
    await eventually(lambda: ctx.guild.id in cog._sessions and not player.preparing)
    player.source.frames = 500
    await player.seek(1000)
    await eventually(lambda: len(player.voice.starts) == 2 and not player.preparing)
    player.source.frames = 250
    player.voice.finish()
    await eventually(lambda: ctx.guild.id in cog._last_sessions)
    saved = cog._last_sessions[ctx.guild.id]
    assert saved["count"] == 1 and saved["played_ms"] == 15000
    assert not player.voice.connected
    summaries = [
        call
        for call in ctx.channel.send.await_args_list
        if "Music session summary" in call.kwargs.get("embed", SimpleNamespace(title="")).title
    ]
    assert len(summaries) == 1 and isinstance(summaries[0].kwargs["view"], SessionView)
    await cog._finish_music_session(player)
    assert len(cog._last_sessions) == 1


async def test_disabled_default_and_privacy_cleanup(audio_runtime):
    cog, player, ctx = audio_runtime
    player.current = Track("https://example.invalid/song", "Song", "Artist", 1000)
    player.context = ctx
    player._requesters[id(player.current)] = ctx.author.id
    await cog._session_started(player)
    assert not cog._sessions
    await cog.config.guild(ctx.guild).music.session_summary.set(True)
    await cog._session_started(player)
    assert "music-sessions.json" in await cog.red_get_data_for_user(user_id=ctx.author.id)
    await cog.red_delete_data_for_user(requester="user", user_id=ctx.author.id)
    assert not cog._sessions[ctx.guild.id]["tracks"] and player._session_entry is None
    assert "music-sessions.json" not in await cog.red_get_data_for_user(user_id=ctx.author.id)


async def test_failed_summary_delivery_cannot_block_idle_disconnect_cleanup(
    audio_runtime, monkeypatch
):
    cog, player, ctx = audio_runtime
    ctx.channel.id = 457
    ctx.channel.send.side_effect = OSError("Discord transport unavailable")
    monkeypatch.setattr("audioplus.player.IDLE_DISCONNECT_SECONDS", 0.02)
    await cog.config.guild(ctx.guild).music.session_summary.set(True)
    player.on_start, player.on_finish = cog._track_started, cog._session_segment
    # Isolate summary delivery: normal now-playing/panel transport is covered elsewhere.
    cog._send_now_playing = AsyncMock()
    cog._update_panel = AsyncMock()
    await player.enqueue([Track("https://example.invalid/song", "Song", "Artist", 1000)], ctx)
    await eventually(lambda: player.playing and ctx.guild.id in cog._sessions)
    player.voice.finish()
    await eventually(lambda: ctx.guild.id not in cog._players)
    assert not player.voice.connected and not cog._views
    assert ctx.guild.id in cog._last_sessions


async def test_session_expiry_prunes_records_and_controls(red_command_runtime):
    bot, cog, member, invoke = red_command_runtime
    row = record(member.guild, member)
    row["ended"] -= SESSION_TTL + 1
    cog._last_sessions[member.guild.id] = row
    view = SessionView(cog, member.guild.id, row)
    cog._views.add(view)
    cog._prune_sessions()
    assert not cog._last_sessions and view.is_finished() and view not in cog._views


async def test_session_playlist_prefix_slash_no_overwrite_and_disabled_source(
    red_command_runtime, monkeypatch
):
    bot, cog, member, invoke = red_command_runtime
    init_events(bot, parse_cli_flags([]))
    cog._last_sessions[member.guild.id] = record(member.guild, member)
    ctx = await invoke("!playlist session tonight")
    assert not ctx.command_failed
    before = await cog.config.guild(member.guild).playlists()
    assert before[str(member.id)]["tonight"][0]["title"] == "Song"
    ctx = await invoke("!playlist session tonight")
    assert ctx.command_failed and await cog.config.guild(member.guild).playlists() == before
    cog.playlist_save.enabled = False
    ctx = await invoke_slash(bot, invoke, monkeypatch, "playlist session", name="blocked")
    assert ctx.command_failed and await cog.config.guild(member.guild).playlists() == before


async def test_button_opens_modal_without_deferring_and_uses_clicker(red_command_runtime):
    bot, cog, member, invoke = red_command_runtime
    ctx = await invoke("!queue")
    row = record(member.guild, member)
    view = SessionView(cog, member.guild.id, row)
    message = make_message(member, ctx.channel)
    message._state = bot._connection
    view.message = message
    interaction = SimpleNamespace(
        guild=member.guild,
        guild_id=member.guild.id,
        user=member,
        message=message,
        response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock(), send_modal=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await view.children[0].callback(interaction)
    interaction.response.defer.assert_not_awaited()
    modal = interaction.response.send_modal.await_args.args[0]
    modal.name._value = "session_saved"
    interaction.message = None
    await modal.on_submit(interaction)
    assert (await cog.config.guild(member.guild).playlists())[str(member.id)]["session_saved"]
    view.stop()
