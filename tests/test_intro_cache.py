"""Local intro reuse, real FFmpeg segment downloads and owned cache cleanup."""

import asyncio
import audioop
import json
import struct
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from test_introplus import intro_runtime as intro_fixture
from test_introplus import join, saved_intro
from test_introplus import red_command_runtime as runtime_fixture
from test_native_audio import eventually
from test_native_audio import media_server as media_fixture

from introplus.cache import FRAME_BYTES, CachedSource, ClipCache, clip_key
from introplus.resolver import MediaError, Stream, Track

intro_runtime = intro_fixture
media_server = media_fixture
red_command_runtime = runtime_fixture


def chosen(start=0.0, duration=0.2, video="YE7VzlLtp-4"):
    track = Track(f"https://www.youtube.com/watch?v={video}", "Intro", length=120000)
    return {"track": asdict(track), "start": start, "duration": duration}, track


def make_cache(tmp_path, uri="https://cdn.invalid/stream?secret=private"):
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=Stream(uri)))
    cache = ClipCache(tmp_path / "clips", resolver)
    cache.initialize({})
    return cache


async def frames_download(stream, clip, path):
    path.write_bytes(struct.pack("<h", 1000) * 1920 * int(clip["duration"] * 1000 / 20))


async def test_real_ffmpeg_prepares_only_segment_and_replays_without_lookup(tmp_path, media_server):
    cache = make_cache(tmp_path, media_server)
    clip, track = chosen(start=0.1)
    try:
        path = await cache.get(123, 456, clip, track)
        assert path.stat().st_size == 10 * FRAME_BYTES
        assert path.stat().st_mode & 0o777 == 0o600
        cache.resolver.resolve.side_effect = AssertionError("Cached replay visited YouTube")
        assert await cache.get(123, 456, clip, track) == path
        assert cache.resolver.resolve.await_count == 1
        source, normal = CachedSource(path, volume=50, start=100), CachedSource(path, volume=100)
        try:
            assert source.read() == audioop.mul(normal.read(), 2, 0.5)
            while source.read():
                pass
            assert source.frames == 10 and source.position == 300
        finally:
            source.cleanup()
            normal.cleanup()
        assert source.cleaned and source.read() == b""
        await cache.close()
        fresh = make_cache(tmp_path / "other")
        fresh.root = cache.root
        fresh.initialize({clip_key(123, 456, clip): 10 * FRAME_BYTES})
        try:
            assert await fresh.get(123, 456, clip, track) == path
            fresh.resolver.resolve.assert_not_awaited()
        finally:
            await fresh.close()
    finally:
        await cache.close()


async def test_lru_eviction_bounds_audio_and_preserves_open_source(tmp_path, monkeypatch):
    monkeypatch.setattr("introplus.cache.MAX_CACHE_BYTES", FRAME_BYTES * 2)
    monkeypatch.setattr("introplus.cache.MAX_CACHE_ITEMS", 2)
    cache = make_cache(tmp_path)
    cache._download = frames_download
    clips = [chosen(duration=0.02, video=str(number)) for number in range(3)]
    try:
        first = await cache.get(123, 1, *clips[0])
        second = await cache.get(123, 2, *clips[1])
        playing = CachedSource(second, volume=100)
        assert cache.path(123, 1, clips[0][0]) == first
        third = await cache.get(123, 3, *clips[2])
        assert first.exists() and third.exists() and not second.exists()
        assert len(cache.entries) == 2 and sum(cache.entries.values()) == FRAME_BYTES * 2
        assert playing.read() and not playing.read()
        playing.cleanup()
        await cache.remove(member_id=1)
        assert not first.exists() and third.exists()
        await cache.remove(guild_id=123)
        assert not list(cache.root.iterdir())
    finally:
        await cache.close()


async def test_reload_prunes_orphans_partial_files_and_invalid_audio(tmp_path):
    cache = make_cache(tmp_path)
    cache._download = frames_download
    clip, track = chosen(duration=0.04)
    first = await cache.get(123, 456, clip, track)
    second = await cache.get(124, 457, clip, track)
    broken = cache.root / f"125_458_{'a' * 64}.pcm"
    partial = cache.root / "leftover.part"
    broken.write_bytes(b"invalid")
    partial.write_bytes(b"partial")
    await cache.close()
    fresh = ClipCache(cache.root, cache.resolver)
    fresh.initialize({first.stem: 2 * FRAME_BYTES, broken.stem: 2 * FRAME_BYTES})
    assert first.exists() and not second.exists() and not broken.exists() and not partial.exists()
    first.write_bytes(b"truncated")
    assert fresh.path(123, 456, clip) is None and not first.exists()
    await fresh.close()


async def test_duplicate_preparations_coalesce_and_jobs_are_bounded(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    clip, track = chosen()
    started, ready = asyncio.Event(), asyncio.Event()

    async def blocked(track):
        started.set()
        await ready.wait()
        return Stream("https://cdn.invalid/stream")

    cache.resolver.resolve = AsyncMock(side_effect=blocked)
    cache._download = frames_download
    monkeypatch.setattr("introplus.cache.MAX_PREPARING", 1)
    first = cache.schedule(123, 456, clip, track)
    assert cache.schedule(123, 456, clip, track) is first
    await started.wait()
    with pytest.raises(MediaError, match="Too many"):
        cache.schedule(123, 457, clip, track)
    assert "downloading" in cache.status(123, 456, clip)
    ready.set()
    await first
    await eventually(lambda: not cache.jobs)
    assert "ready" in cache.status(123, 456, clip)
    await cache.close()
    with pytest.raises(MediaError, match="unloading"):
        cache.schedule(123, 456, clip, track)


class Process:
    def __init__(self):
        self.returncode = None
        self.killed = False
        self.done = asyncio.Event()

    async def wait(self):
        await self.done.wait()
        return self.returncode

    def kill(self):
        self.killed, self.returncode = True, -9
        self.done.set()


@pytest.mark.parametrize("during_spawn", [False, True])
async def test_download_cancellation_kills_child_and_removes_partial(
    tmp_path, monkeypatch, during_spawn
):
    cache = make_cache(tmp_path)
    clip, track = chosen()
    child, started, ready = Process(), asyncio.Event(), asyncio.Event()

    async def spawn(*args, **kwargs):
        kwargs["stdout"].write(b"partial")
        started.set()
        if during_spawn:
            await ready.wait()
        return child

    monkeypatch.setattr("introplus.cache.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("introplus.cache.shutil.which", lambda name: "/usr/bin/ffmpeg")
    task = cache.schedule(123, 456, clip, track)
    await started.wait()
    removal = asyncio.create_task(cache.remove(member_id=456))
    await asyncio.sleep(0)
    ready.set()
    await removal
    assert task.cancelled() and child.killed and not cache.jobs
    assert not list(cache.root.iterdir())
    await cache.close()


async def test_failed_or_timed_out_download_leaves_no_partial_or_private_urls(
    tmp_path, monkeypatch
):
    cache = make_cache(tmp_path)
    clip, track = chosen()
    child = Process()
    monkeypatch.setattr(
        "introplus.cache.asyncio.create_subprocess_exec", AsyncMock(return_value=child)
    )
    monkeypatch.setattr("introplus.cache.shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr("introplus.cache.DOWNLOAD_TIMEOUT", 0.01)
    with pytest.raises(MediaError, match="timed out") as caught:
        await cache.get(123, 456, clip, track)
    assert "private" not in str(caught.value) and child.killed
    await eventually(lambda: not cache.jobs)
    assert "failed" in cache.status(123, 456, clip) and not list(cache.root.iterdir())
    cache._download = frames_download
    assert (await cache.get(123, 456, clip, track)).exists()
    assert "ready" in cache.status(123, 456, clip)
    await cache.close()


async def test_prepared_intro_joins_without_youtube_or_decoder(intro_runtime):
    _, _, cog, member, channel, invoke = intro_runtime
    await invoke("!intro set 0.5 entrance")
    await eventually(lambda: not cog._cache.jobs)
    cog._resolver.resolve.side_effect = AssertionError("YouTube lookup on a cached join")
    cog._cache._download = AsyncMock(side_effect=AssertionError("FFmpeg on a cached join"))
    for _ in range(2):
        await invoke("!intro test")
        await eventually(
            lambda: member.guild.voice_client and member.guild.voice_client.is_playing()
        )
        source = member.guild.voice_client.source
        assert isinstance(source, CachedSource)
        assert source.read()
        await cog._stop_guild(member.guild.id)
    cog._resolver.resolve.assert_awaited_once()
    cog._cache._download.assert_not_awaited()
    ctx = await invoke("!intro show")
    assert "ready, plays from a local copy" in ctx.send.await_args.kwargs["embed"].description


async def test_timing_change_replaces_local_segment_and_clear_deletes_it(intro_runtime):
    _, _, cog, member, _, invoke = intro_runtime
    await invoke("!intro set 0.5 entrance")
    await eventually(lambda: not cog._cache.jobs)
    first = list(cog._cache.root.iterdir())
    await invoke("!intro start 30")
    await eventually(lambda: not cog._cache.jobs)
    second = list(cog._cache.root.iterdir())
    assert len(second) == 1 and not first[0].exists() and second[0] != first[0]
    await invoke("!intro duration 1")
    await eventually(lambda: not cog._cache.jobs)
    third = list(cog._cache.root.iterdir())
    assert len(third) == 1 and not second[0].exists()
    assert third[0].stat().st_size == 50 * FRAME_BYTES
    member.guild_permissions = discord.Permissions(manage_guild=True)
    lookups = cog._resolver.resolve.await_count
    ctx = await invoke("!intro volume 40")
    assert not ctx.command_failed and third[0].exists()
    assert cog._resolver.resolve.await_count == lookups
    await invoke("!intro test")
    await eventually(lambda: member.guild.voice_client and member.guild.voice_client.is_playing())
    assert member.guild.voice_client.source.volume == 40
    await cog._stop_guild(member.guild.id)
    ctx = await invoke("!intro show")
    assert "ready" in ctx.send.await_args.kwargs["embed"].description
    await invoke("!intro clear")
    assert not cog._cache.entries and not list(cog._cache.root.iterdir())


@pytest.mark.parametrize("action", ["delete", "member_remove", "guild_remove", "unload"])
async def test_privacy_deletion_removes_audio_and_unload_preserves_ready_copies(
    intro_runtime, action
):
    bot, _, cog, member, _, invoke = intro_runtime
    await invoke("!intro set 0.5 entrance")
    await eventually(lambda: not cog._cache.jobs)
    path = next(cog._cache.root.iterdir())
    if action == "delete":
        await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=member.id)
    elif action == "member_remove":
        await cog.on_member_remove(member)
    elif action == "guild_remove":
        await cog.on_guild_remove(member.guild)
    else:
        await bot.remove_cog("IntroPlus")
    assert path.exists() is (action == "unload")
    assert not cog._cache.jobs
    if action != "unload":
        assert not await cog.config.member(member).clip()
        assert not list(cog._cache.root.iterdir())


async def test_clearing_during_lookup_cancels_background_download(intro_runtime):
    _, _, cog, member, _, invoke = intro_runtime
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(track):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    cog._resolver.resolve = blocked
    ctx = await invoke("!intro set 0.5 entrance")
    assert not ctx.command_failed
    await started.wait()
    ctx = await invoke("!intro show")
    assert "downloading" in ctx.send.await_args.kwargs["embed"].description
    await invoke("!intro clear")
    assert cancelled.is_set() and not cog._cache.jobs and not list(cog._cache.root.iterdir())
    assert not await cog.config.member(member).clip()


async def test_existing_choices_are_prepared_on_load_and_reused_after_reload(
    intro_runtime, monkeypatch
):
    from introplus import IntroPlus

    bot, _, cog, member, channel, _ = intro_runtime
    clip = await saved_intro(cog, member)
    await bot.remove_cog("IntroPlus")

    async def download(self, stream, clip, path):
        await frames_download(stream, clip, path)

    monkeypatch.setattr("introplus.cache.ClipCache._download", download)
    fresh = IntroPlus(bot)
    fresh._resolver.resolve = AsyncMock(return_value=Stream("https://cdn.invalid/stream"))
    await bot.add_cog(fresh)
    await eventually(lambda: bool(fresh._cache.entries) and not fresh._cache.jobs)
    assert fresh._cache.path(member.guild.id, member.id, clip)
    fresh._resolver.resolve.assert_awaited_once()
    await bot.remove_cog("IntroPlus")
    reloaded = IntroPlus(bot)
    reloaded._resolver.resolve = AsyncMock(side_effect=AssertionError("Reload redownloaded"))
    await bot.add_cog(reloaded)
    try:
        await reloaded._warm_task
        assert reloaded._cache.path(member.guild.id, member.id, clip)
        await join(reloaded, member, channel)
        await eventually(
            lambda: member.guild.voice_client and member.guild.voice_client.is_playing()
        )
        reloaded._resolver.resolve.assert_not_awaited()
        exports = await reloaded.red_get_data_for_user(user_id=member.id)
        assert json.loads(exports["intros.json"].getvalue())[str(member.guild.id)]["clip"] == clip
    finally:
        await bot.remove_cog("IntroPlus")
