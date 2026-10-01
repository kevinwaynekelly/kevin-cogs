"""Native AudioPlus command registration, ownership, dependencies, and lifecycle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import make_channel, make_context, make_member
from redbot.core import commands
from test_native_audio import FakeSource, FakeVoice, eventually, track

import audioplus.cog as audio_module
from audioplus import AudioPlus
from audioplus.player import GuildPlayer
from audioplus.resolver import MediaError


@pytest.fixture
async def audio_runtime(bot, guild):
    cog = AudioPlus(bot)
    await cog.cog_load()
    channel = make_channel(guild, kind=discord.VoiceChannel)
    voice = FakeVoice(guild.id)
    voice.guild, voice.channel = guild, channel
    guild.voice_client = voice
    player = GuildPlayer(
        voice, cog._resolver, cog._report_playback_failure, source_factory=FakeSource
    )
    cog._players[guild.id] = player
    cog._resolver.resolve = AsyncMock(side_effect=lambda tr: SimpleNamespace(url=tr.uri))
    member = make_member(guild)
    member.voice = SimpleNamespace(channel=channel)
    ctx = make_context(guild, make_channel(guild), member)
    ctx.bot = bot
    yield cog, player, ctx
    await cog.cog_unload()


async def test_load_keeps_setup_available_with_missing_system_dependencies(bot, monkeypatch):
    cog = AudioPlus(bot)
    monkeypatch.setattr(
        audio_module, "require_voice", lambda: (_ for _ in ()).throw(MediaError("missing"))
    )
    await cog.cog_load()
    assert not cog._players and not cog._resolver._processes
    await cog.cog_unload()
    assert cog._resolver._closed


async def test_voice_connection_is_serialized_and_owned(bot, guild, monkeypatch):
    cog = AudioPlus(bot)
    monkeypatch.setattr(audio_module, "require_voice", lambda: None)
    channel = make_channel(guild, kind=discord.VoiceChannel)
    voice = FakeVoice()
    voice.guild, voice.channel = guild, channel

    async def connect(**kwargs):
        await asyncio.sleep(0)
        guild.voice_client = voice
        return voice

    channel.connect = AsyncMock(side_effect=connect)
    member = make_member(guild)
    member.voice = SimpleNamespace(channel=channel)
    ctx = make_context(guild, author=member)
    try:
        results = await asyncio.gather(*(cog._fetch_or_connect_player(ctx) for _ in range(10)))
        assert all(result[0] is results[0][0] for result in results)
        channel.connect.assert_awaited_once_with(
            cls=channel.connect.await_args.kwargs["cls"],
            timeout=30,
            reconnect=True,
            self_deaf=False,
            self_mute=False,
        )
    finally:
        await cog.cog_unload()
    voice.disconnect.assert_awaited_once_with(force=True)


async def test_foreign_voice_connection_and_controls_are_left_alone(bot, guild, monkeypatch):
    cog = AudioPlus(bot)
    monkeypatch.setattr(audio_module, "require_voice", lambda: None)
    foreign = FakeVoice()
    guild.voice_client = foreign
    channel = make_channel(guild, kind=discord.VoiceChannel)
    member = make_member(guild)
    member.voice = SimpleNamespace(channel=channel)
    ctx = make_context(guild, author=member)
    with pytest.raises(commands.CommandError, match="Another cog"):
        await cog._fetch_or_connect_player(ctx)
    for callback in (
        AudioPlus.audio_skip,
        AudioPlus.audio_stop,
        AudioPlus.audio_leave,
        AudioPlus.audio_pause,
        AudioPlus.audio_resume,
    ):
        await callback.callback(cog, ctx)
    await cog.cog_unload()
    foreign.disconnect.assert_not_awaited()


async def test_unload_cancels_pending_command_lookup_and_player(audio_runtime):
    cog, player, ctx = audio_runtime
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def lookup(query):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    cog._resolver.search = lookup
    task = asyncio.create_task(AudioPlus.audio_play.callback(cog, ctx, query="roar"))
    await started.wait()
    await cog.cog_unload()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and player.closed and not cog._players


async def test_missing_bot_voice_permission_is_reported_as_bot_failure(bot, guild, monkeypatch):
    cog = AudioPlus(bot)
    monkeypatch.setattr(audio_module, "require_voice", lambda: None)
    channel = make_channel(guild, kind=discord.VoiceChannel)
    permissions = discord.Permissions.all()
    permissions.speak = False
    channel.permissions_for.return_value = permissions
    member = make_member(guild)
    member.voice = SimpleNamespace(channel=channel)
    with pytest.raises(commands.BotMissingPermissions) as exc:
        await cog._fetch_or_connect_player(make_context(guild, author=member))
    assert exc.value.missing.speak
    assert not exc.value.missing.connect
    channel.connect.assert_not_called()


async def test_legacy_settings_preserve_defaults_and_hide_password(bot, guild):
    cog = AudioPlus(bot)
    ctx = make_context(guild)
    await AudioPlus.audio_setnode.callback(cog, ctx, "10.10.1.200", 2333, "private-secret", True)
    await cog.config.resume_timeout.set(125)
    data = await cog.config.all()
    assert data["host"] == "10.10.1.200" and data["resume_timeout"] == 125
    assert data["password"] == "private-secret" and data["secure"] is True
    await AudioPlus.audio_shownode.callback(cog, ctx)
    output = "\n".join(call.kwargs["embed"].description for call in ctx.send.await_args_list)
    assert "private-secret" not in output and "Native Discord" in output
    with pytest.raises(commands.BadArgument):
        await AudioPlus.audio_setnode.callback(cog, ctx, "http://bad/path", 2333, "secret")


async def test_pingnode_reports_local_packages_and_latest_failure(audio_runtime, monkeypatch):
    cog, player, ctx = audio_runtime
    player.last_error = "Decoder failed"
    diagnostic = {
        "ready": True,
        "voice_error": None,
        "packages": {"yt-dlp": "2026.8.19", "davey": "0.1.6"},
        "ffmpeg": "FFmpeg test",
        "deno": "missing",
        "node": "v22.1.0",
        "quickjs": "missing",
        "runtimes": ["Node"],
    }
    monkeypatch.setattr(audio_module, "diagnostics", AsyncMock(return_value=diagnostic))
    await AudioPlus.audio_pingnode.callback(cog, ctx)
    output = ctx.send.await_args.kwargs["embed"].description
    assert "Native Discord voice" in output and "2026.8.19" in output and "Decoder failed" in output
    assert "**Discord voice** · Ready" in output
    assert "Not used" in output and "Dependency checks do not test" in output


async def test_backend_checks_detect_missing_runtime_and_native_voice(monkeypatch):
    import audioplus.backend as backend

    monkeypatch.setattr(
        backend, "require_voice", lambda: (_ for _ in ()).throw(MediaError("Missing Opus"))
    )
    monkeypatch.setattr(
        backend,
        "executable_version",
        AsyncMock(side_effect=["FFmpeg test", "missing", "v18.0.0", "missing"]),
    )
    result = await backend.diagnostics()
    assert not result["ready"] and result["voice_error"] == "Missing Opus"
    assert not result["runtimes"]


async def test_empty_search_rejected_before_connecting(bot, guild):
    cog = AudioPlus(bot)
    cog._fetch_or_connect_player = AsyncMock()
    with pytest.raises(commands.BadArgument):
        await AudioPlus.audio_play.callback(cog, make_context(guild), query="  ")
    cog._fetch_or_connect_player.assert_not_awaited()


async def test_leave_and_guild_removal_clean_owned_players(audio_runtime):
    cog, player, ctx = audio_runtime
    await player.enqueue([track()])
    await eventually(lambda: player.playing)
    await cog.on_guild_remove(ctx.guild)
    assert player.closed and player._runner.done() and not cog._players
    player.voice.disconnect.assert_awaited_once_with(force=True)


async def test_unload_cancels_partial_voice_handshake_and_disconnects_owned_client(
    bot, guild, monkeypatch
):
    cog = AudioPlus(bot)
    channel = make_channel(guild, kind=discord.VoiceChannel)
    voice = FakeVoice()
    started = asyncio.Event()
    monkeypatch.setattr(audio_module.discord, "VoiceClient", lambda client, ch: voice)

    async def connect(**kwargs):
        kwargs["cls"](bot, channel)
        started.set()
        await asyncio.Event().wait()

    channel.connect = AsyncMock(side_effect=connect)
    task = asyncio.create_task(cog._connect_voice(channel))
    await started.wait()
    await cog.cog_unload()
    with pytest.raises(asyncio.CancelledError):
        await task
    voice.disconnect.assert_awaited_once_with(force=True)
    assert not cog._connections
