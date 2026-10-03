"""Real Red commands, PCM decoding and owned lifecycle; Discord/YouTube transport mocked."""

import asyncio
import json
import struct
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import make_channel, make_member
from redbot.core._cli import parse_cli_flags
from redbot.core._events import init_events
from test_audio_hybrid import red_command_runtime as runtime_fixture
from test_cog_hybrid import invoke_slash
from test_native_audio import FakeVoice, eventually
from test_native_audio import media_server as media_fixture

from audioplus.backend import require_voice
from audioplus.player import GuildPlayer
from introplus import IntroPlus
from introplus.cog import IntroRequest
from introplus.constants import MAX_PENDING, QUEUE_TTL, __red_end_user_data_statement__
from introplus.playback import OverlaySource, play_overlay
from introplus.resolver import MediaError, Stream, Track
from introplus.source import NativeSource

red_command_runtime = runtime_fixture
media_server = media_fixture


class PCMSource(discord.AudioSource):
    def __init__(self, stream, *, volume, start=0, duration=0):
        self.volume, self.start, self.duration = volume, start, duration
        self.frames = 0
        self.cleaned = False

    @property
    def position(self):
        return self.start + self.frames * 20

    def read(self):
        if self.duration and self.frames * 20 >= self.duration:
            return b""
        self.frames += 1
        return struct.pack("<h", int(1000 * self.volume / 100)) * 1920

    def is_opus(self):
        return False

    def cleanup(self):
        self.cleaned = True


class Voice(FakeVoice):
    def stop(self):
        source = self.source
        super().stop()
        if source:
            source.cleanup()

    def finish(self, error=None):
        source = self.source
        super().finish(error)
        if source:
            source.cleanup()


@pytest.fixture
async def intro_runtime(red_command_runtime, monkeypatch):
    bot, audio, member, invoke = red_command_runtime
    init_events(bot, parse_cli_flags([]))
    monkeypatch.setattr(bot, "_delete_delay", AsyncMock())
    guild = member.guild
    bot._connection._guilds[guild.id] = guild
    channel = make_channel(guild, 987654321012345678, kind=discord.VoiceChannel)
    channel.members = [member]
    member.voice = SimpleNamespace(channel=channel)
    member.guild_permissions = discord.Permissions.none()
    for text in guild.channels:
        if isinstance(text, discord.TextChannel):
            text.permissions_for.side_effect = lambda target: target.guild_permissions
    cog = IntroPlus(bot)
    await bot.add_cog(cog)
    video = Track(
        "https://www.youtube.com/watch?v=YE7VzlLtp-4", "Entrance", "Artist", 120000, "youtube"
    )
    cog._resolver.search = AsyncMock(return_value=[video])
    cog._resolver.resolve = AsyncMock(
        return_value=Stream("https://cdn.invalid/audio?token=private")
    )
    cog._source_factory = PCMSource
    monkeypatch.setattr("introplus.cog.require_voice", lambda: None)
    monkeypatch.setattr(audio, "_require_voice", lambda: None)

    async def connect(*args, **kwargs):
        voice = Voice(guild.id)
        voice.guild, voice.channel = guild, channel
        guild.voice_client = voice
        return voice

    channel.connect = AsyncMock(side_effect=connect)
    audio._connect_voice = AsyncMock(side_effect=connect)
    try:
        yield bot, audio, cog, member, channel, invoke
    finally:
        await bot.remove_cog("IntroPlus")


async def saved_intro(cog, member, duration=0.5):
    clip = {
        "track": asdict(
            Track(
                "https://www.youtube.com/watch?v=YE7VzlLtp-4",
                "Entrance",
                "Artist",
                120000,
                "youtube",
            )
        ),
        "duration": duration,
        "start": 0.0,
    }
    await cog.config.member(member).clip.set(clip)
    return clip


async def join(cog, member, channel):
    member.voice = SimpleNamespace(channel=channel)
    await cog.on_voice_state_update(
        member, SimpleNamespace(channel=None), SimpleNamespace(channel=channel)
    )


async def test_prefix_and_slash_clip_controls_and_permissions(intro_runtime, monkeypatch):
    bot, audio, cog, member, channel, invoke = intro_runtime
    ctx = await invoke("!intro set 8 entrance music")
    assert not ctx.command_failed
    clip = await cog.config.member(member).clip()
    assert clip["duration"] == 8 and clip["start"] == 0
    ctx = await invoke_slash(bot, invoke, monkeypatch, "intro start", value=25.5)
    assert not ctx.command_failed and ctx.defer.await_args.kwargs["ephemeral"]
    ctx = await invoke("!intro duration 4")
    assert not ctx.command_failed
    clip = await cog.config.member(member).clip()
    assert clip["start"] == 25.5 and clip["duration"] == 4
    ctx = await invoke("!intro status")
    assert not ctx.command_failed and ctx.send.await_args.kwargs["embed"].title.startswith(
        "IntroPlus"
    )
    ctx = await invoke("!intro enable false")
    assert ctx.command_failed and await cog.config.guild(member.guild).enabled()
    member.guild_permissions = discord.Permissions(manage_guild=True)
    ctx = await invoke_slash(bot, invoke, monkeypatch, "intro enable", enabled=False)
    assert not ctx.command_failed and not await cog.config.guild(member.guild).enabled()
    ctx = await invoke("!intro clear")
    assert not ctx.command_failed and not await cog.config.member(member).clip()


@pytest.mark.parametrize("duration", ["nan", "inf", "-1", "0.1", "31"])
async def test_invalid_clip_length_is_rejected_before_lookup(intro_runtime, duration):
    _, _, cog, member, _, invoke = intro_runtime
    ctx = await invoke(f"!intro set {duration} entrance")
    assert ctx.command_failed and not await cog.config.member(member).clip()
    cog._resolver.search.assert_not_awaited()


@pytest.mark.parametrize(
    "query",
    ["https://localhost/private", "https://user:password@youtube.com/watch?v=x", "scsearch:roar"],
)
async def test_non_youtube_and_credential_urls_are_rejected(intro_runtime, query):
    _, _, cog, _, _, invoke = intro_runtime
    ctx = await invoke(f"!intro set 5 {query}")
    assert ctx.command_failed
    cog._resolver.search.assert_not_awaited()


async def test_source_failures_and_playlists_leave_existing_clip_intact(intro_runtime):
    _, _, cog, member, _, invoke = intro_runtime
    clip = await saved_intro(cog, member)
    cog._resolver.search = AsyncMock(
        side_effect=MediaError("The provider requires authentication.")
    )
    ctx = await invoke("!intro set 8 new video")
    assert (
        ctx.command_failed and "authentication" in ctx.send.await_args.kwargs["embed"].description
    )
    assert await cog.config.member(member).clip() == clip
    cog._resolver.search = AsyncMock(return_value=[Track(clip["track"]["uri"], "A")] * 2)
    ctx = await invoke("!intro set 8 playlist")
    assert ctx.command_failed and await cog.config.member(member).clip() == clip
    ctx = await invoke("!intro start 121")
    assert ctx.command_failed and await cog.config.member(member).clip() == clip


async def test_join_plays_finite_clip_and_disconnects_owned_voice(intro_runtime):
    _, _, cog, member, channel, _ = intro_runtime
    await saved_intro(cog, member)
    await join(cog, member, channel)
    await eventually(lambda: member.guild.voice_client and member.guild.voice_client.is_playing())
    voice = member.guild.voice_client
    source = voice.source
    while source.read():
        pass
    voice.finish()
    await eventually(lambda: not cog._workers)
    assert source.frames == 25 and source.cleaned and voice.disconnect.await_count == 1
    assert member.guild.voice_client is None and not cog._voices and not cog._pending
    assert cog._last[member.guild.id] == "Played a 0.5-second intro."


async def test_flag_updates_disabled_cog_scope_bots_and_cooldowns_do_not_replay(intro_runtime):
    bot, _, cog, member, channel, _ = intro_runtime
    await saved_intro(cog, member)
    await cog.on_voice_state_update(
        member, SimpleNamespace(channel=channel), SimpleNamespace(channel=channel)
    )
    assert not cog._workers
    await cog.config.guild(member.guild).enabled.set(False)
    await join(cog, member, channel)
    assert not cog._workers
    await cog.config.guild(member.guild).enabled.set(True)
    await cog.config.guild(member.guild).channel.set(9999)
    await join(cog, member, channel)
    assert not cog._workers
    await cog.config.guild(member.guild).channel.set(0)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(bot, "cog_disabled_in_guild", AsyncMock(return_value=True))
    try:
        await join(cog, member, channel)
        assert not cog._workers
    finally:
        monkeypatch.undo()
    member.bot = True
    await join(cog, member, channel)
    assert not cog._workers
    member.bot = False
    cog._cooldowns[(member.guild.id, member.id)] = time.monotonic()
    await join(cog, member, channel)
    assert not cog._workers


async def test_leaving_during_resolution_cancels_it_without_connecting(intro_runtime):
    _, _, cog, member, channel, _ = intro_runtime
    await saved_intro(cog, member)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(track):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    cog._resolver.resolve = blocked
    await join(cog, member, channel)
    await started.wait()
    member.voice = None
    await cog.on_voice_state_update(
        member, SimpleNamespace(channel=channel), SimpleNamespace(channel=None)
    )
    await eventually(lambda: not cog._workers)
    assert cancelled.is_set() and channel.connect.await_count == 0


async def test_bounded_queue_serializes_members_and_expires_stale_requests(intro_runtime):
    _, _, cog, member, channel, _ = intro_runtime
    clip = await saved_intro(cog, member)
    blocked = asyncio.Event()

    async def wait_for_stream(track):
        await blocked.wait()
        return Stream("https://cdn.invalid/audio")

    cog._resolver.resolve = wait_for_stream
    await join(cog, member, channel)
    await eventually(lambda: bool(cog._active))
    requests = []
    for number in range(MAX_PENDING + 1):
        other = make_member(member.guild, 6000 + number)
        other.voice = SimpleNamespace(channel=channel)
        await cog.config.member(other).clip.set(clip)
        request = IntroRequest(
            member.guild.id, other.id, other.id, channel.id, clip, time.monotonic()
        )
        requests.append(request)
        assert cog._enqueue(request, 60) is (number < MAX_PENDING)
    assert len(cog._queues[member.guild.id]) == MAX_PENDING
    await cog._stop_guild(member.guild.id)
    assert not cog._pending and not cog._workers
    stale = replace(requests[0], created=time.monotonic() - QUEUE_TTL - 1, manual=True)
    assert cog._enqueue(stale, 60)
    await eventually(lambda: not cog._workers)
    assert "expired" in cog._last[member.guild.id]


async def test_overlay_ducks_music_without_changing_queue_or_position(intro_runtime):
    _, audio, cog, member, channel, _ = intro_runtime
    await saved_intro(cog, member)
    voice = Voice(member.guild.id)
    voice.guild, voice.channel = member.guild, channel
    member.guild.voice_client = voice
    player = GuildPlayer(voice, audio._resolver, AsyncMock(), source_factory=PCMSource)
    audio._resolver.resolve = AsyncMock(return_value=Stream("https://cdn.invalid/music"))
    audio._players[member.guild.id] = player
    music = Track("https://www.youtube.com/watch?v=music", "Music", length=120000)
    await player.enqueue([music, music])
    await eventually(lambda: player.playing)
    base = player.source
    await join(cog, member, channel)
    await eventually(lambda: isinstance(voice.source, OverlaySource))
    overlay = voice.source
    frame = overlay.read()
    assert struct.unpack("<h", frame[:2])[0] == 1000  # 30% music + 70% intro
    for _ in range(25):
        overlay.read()
    await eventually(lambda: not cog._workers)
    assert voice.source is base and not base.cleaned and base.frames == 26
    assert player.current is music and list(player.queue) == [music] and player.position == 520
    assert voice.disconnect.await_count == 0 and overlay.clip.cleaned


async def test_overlay_cancel_preserves_user_pause_and_replacement_source():
    voice = Voice()
    base = PCMSource(None, volume=100)
    voice.play(base, after=lambda error: None)
    clip = PCMSource(None, volume=70, duration=500)
    task = asyncio.create_task(play_overlay(voice, clip, 0.5))
    await eventually(lambda: isinstance(voice.source, OverlaySource))
    voice.pause()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert voice.source is base and voice.is_paused() and not base.cleaned and clip.cleaned
    voice.resume()
    clip = PCMSource(None, volume=70, duration=500)
    task = asyncio.create_task(play_overlay(voice, clip, 0.5))
    await eventually(lambda: isinstance(voice.source, OverlaySource))
    overlay = voice.source
    replacement = PCMSource(None, volume=100)
    voice.source = replacement
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert voice.source is replacement and not replacement.cleaned
    overlay.cleanup()


async def test_music_play_takes_over_intro_voice_without_deadlock(intro_runtime):
    _, audio, cog, member, channel, invoke = intro_runtime
    await saved_intro(cog, member)
    await join(cog, member, channel)
    await eventually(lambda: member.guild.voice_client and member.guild.voice_client.is_playing())
    intro_voice = member.guild.voice_client
    audio._resolver.search = AsyncMock(
        return_value=[Track("https://www.youtube.com/watch?v=music", "Music", length=120000)]
    )
    audio._resolver.resolve = AsyncMock(return_value=Stream("https://cdn.invalid/music"))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "audioplus.cog.GuildPlayer",
        lambda *args, **kwargs: GuildPlayer(*args, **kwargs, source_factory=PCMSource),
    )
    try:
        ctx = await invoke("!play music")
        assert not ctx.command_failed
        await eventually(lambda: audio._get_player(member.guild).playing)
        assert intro_voice.disconnect.await_count == 1 and not cog._voices
        assert member.guild.voice_client is not intro_voice
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize("action", ["unload", "delete", "member_remove", "guild_remove"])
async def test_cleanup_cancels_owned_playback_and_saved_user_data(intro_runtime, action):
    bot, _, cog, member, channel, _ = intro_runtime
    await saved_intro(cog, member)
    exported = await cog.red_get_data_for_user(user_id=member.id)
    assert (
        json.loads(exported["intros.json"].getvalue())[str(member.guild.id)]["clip"]["duration"]
        == 0.5
    )
    await join(cog, member, channel)
    await eventually(lambda: member.guild.voice_client and member.guild.voice_client.is_playing())
    voice, source = member.guild.voice_client, member.guild.voice_client.source
    if action == "unload":
        await bot.remove_cog("IntroPlus")
        assert await cog.config.member(member).clip()
    elif action == "delete":
        await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=member.id)
        assert not await cog.red_get_data_for_user(user_id=member.id)
    elif action == "member_remove":
        await cog.on_member_remove(member)
        assert not await cog.config.member(member).clip()
    else:
        await cog.on_guild_remove(member.guild)
        assert not await cog.config.all_members(member.guild)
    await eventually(lambda: not cog._workers)
    assert source.cleaned and voice.disconnect.await_count == 1 and not cog._voices


async def test_real_ffmpeg_clip_seek_limit_and_cleanup(media_server):
    source = NativeSource(Stream(media_server), volume=100, start=100, duration=200)
    process = source._audio._process
    try:
        frames = []
        while frame := await asyncio.to_thread(source.read):
            frames.append(frame)
        assert len(frames) == 10 and source.position == 300
        assert all(len(frame) == 3840 for frame in frames)
    finally:
        await asyncio.to_thread(source.cleanup)
    assert process.poll() is not None


async def test_real_discord_audio_thread_overlays_and_preserves_pause():
    require_voice()
    voice = object.__new__(discord.VoiceClient)
    voice.client = SimpleNamespace(loop=asyncio.get_running_loop())
    voice.channel = SimpleNamespace(guild=SimpleNamespace(id=789))
    voice._connection = SimpleNamespace(
        is_connected=lambda: True, ws=SimpleNamespace(speak=AsyncMock())
    )
    voice._player = None
    packets = []
    voice.send_audio_packet = lambda data, encode=False: packets.append(
        voice.encoder.encode(data, 960) if encode else data
    )
    base = PCMSource(None, volume=100)
    voice.play(base)
    clip = PCMSource(None, volume=70, duration=100)
    try:
        await play_overlay(voice, clip, 0.1)
        assert voice.source is base and voice.is_playing() and not base.cleaned
        assert len(packets) >= 5 and all(packets)
        clip = PCMSource(None, volume=70, duration=500)
        task = asyncio.create_task(play_overlay(voice, clip, 0.5))
        await eventually(lambda: isinstance(voice.source, OverlaySource))
        voice.pause()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert voice.source is base and voice.is_paused() and not base.cleaned and clip.cleaned
    finally:
        worker = voice._player
        voice.stop()
        if worker:
            await asyncio.to_thread(worker.join, 2)
    assert base.cleaned


async def test_intro_works_without_audio_cog_loaded(intro_runtime):
    bot, _, cog, member, channel, _ = intro_runtime
    await bot.remove_cog("AudioPlus")
    await saved_intro(cog, member)
    await join(cog, member, channel)
    await eventually(lambda: member.guild.voice_client and member.guild.voice_client.is_playing())
    await cog._stop_guild(member.guild.id)
    assert not cog._voices and member.guild.voice_client is None


@pytest.mark.parametrize("change", ["disable", "permissions", "clip", "move"])
async def test_lookup_rechecks_current_policy_clip_and_permissions(intro_runtime, change):
    _, _, cog, member, channel, _ = intro_runtime
    await saved_intro(cog, member)
    started, ready = asyncio.Event(), asyncio.Event()

    async def blocked(track):
        started.set()
        await ready.wait()
        return Stream("https://cdn.invalid/audio")

    cog._resolver.resolve = blocked
    await join(cog, member, channel)
    await started.wait()
    if change == "disable":
        await cog.config.guild(member.guild).enabled.set(False)
    elif change == "permissions":
        channel.permissions_for.return_value = discord.Permissions.none()
    elif change == "clip":
        await cog.config.member(member).clip.clear()
    else:
        member.voice = None
    ready.set()
    await eventually(lambda: not cog._workers)
    channel.connect.assert_not_awaited()


async def test_pending_set_is_cancelled_by_privacy_deletion(intro_runtime):
    _, _, cog, member, _, invoke = intro_runtime
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    cog._resolver.search = blocked
    task = asyncio.create_task(invoke("!intro set 5 video"))
    await started.wait()
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=member.id)
    await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set() and not await cog.config.member(member).clip()
    assert not cog._commands


async def test_assignment_rechecks_manager_permissions_after_lookup(intro_runtime):
    _, _, cog, member, _, invoke = intro_runtime
    other = make_member(member.guild, 777777777777777777)
    member.guild_permissions = discord.Permissions(manage_guild=True)
    started, ready = asyncio.Event(), asyncio.Event()
    tracks = await cog._resolver.search("video")

    async def blocked(*args, **kwargs):
        started.set()
        await ready.wait()
        return tracks

    cog._resolver.search = blocked
    task = asyncio.create_task(invoke(f"!intro assign {other.mention} 5 video"))
    await started.wait()
    member.guild_permissions = discord.Permissions.none()
    ready.set()
    ctx = await task
    assert ctx.command_failed and not await cog.config.member(other).clip()


def test_borrowed_decoder_native_checks_theme_and_data_statement_match():
    for name in (
        "source.py",
        "voice_libraries.py",
        "presentation.py",
        "command_support.py",
        "interactive.py",
    ):
        assert Path("introplus", name).read_bytes() == Path("audioplus", name).read_bytes()
    assert (
        json.loads(Path("introplus/info.json").read_text())["end_user_data_statement"]
        == __red_end_user_data_statement__
    )
