"""Real cached audio, calendar retention, privacy, command checks and child ownership."""

import asyncio
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from redbot.core._cli import parse_cli_flags
from redbot.core._events import init_events
from test_audio_hybrid import red_command_runtime as runtime_fixture
from test_audioplus import audio_runtime as audio_fixture
from test_cog_hybrid import invoke_slash
from test_native_audio import FakeSource, FakeVoice, eventually
from test_native_audio import media_server as media_fixture

from audioplus.cache import RESERVE_BYTES, SongCache, expiry, song_key
from audioplus.player import GuildPlayer
from audioplus.resolver import MediaError, MediaResolver, Stream, Track
from audioplus.source import NativeSource

audio_runtime = audio_fixture
red_command_runtime = runtime_fixture
media_server = media_fixture
SONG = Track("https://www.youtube.com/watch?v=YE7VzlLtp-4", "Song", length=180000, source="youtube")
STREAM = Stream("https://cdn.invalid/stream?token=private", length=180000)


def make_cache(tmp_path, *, clock=None):
    cache = SongCache(**({"clock": clock} if clock else {}))
    cache.initialize(tmp_path / "songs")

    async def download(stream, path):
        path.write_bytes(b"OggS" + b"\0" * 20)

    cache._download = download
    cache._duration = AsyncMock(return_value=180000)
    return cache


async def prepare(cache, *, guild_id=123, user_id=456, track=SONG, stream=STREAM):
    task = cache.schedule(guild_id, track, stream, user_id, epoch=cache.epoch)
    assert task is not None
    await task
    await eventually(lambda: not cache.jobs)
    return cache.root / f"{song_key(guild_id, track)}.ogg"


async def test_real_audio_copy_replays_without_remote_resolution_and_survives_restart(
    tmp_path, media_server
):
    cache = SongCache()
    cache.initialize(tmp_path / "songs")
    stream = Stream(media_server, length=500)
    song = replace(SONG, length=500)
    try:
        path = await prepare(cache, track=song, stream=stream)
        assert path.read_bytes().startswith(b"OggS")
        assert path.stat().st_mode & 0o777 == 0o600
        assert cache.root.stat().st_mode & 0o777 == 0o700
        await cache.close()
        fresh = SongCache()
        fresh.initialize(cache.root)
        resolver = SimpleNamespace(
            resolve=AsyncMock(side_effect=AssertionError("Replay visited the provider"))
        )
        player = GuildPlayer(
            FakeVoice(), resolver, AsyncMock(), source_factory=FakeSource, cache=fresh
        )
        try:
            await player.enqueue([song])
            await eventually(lambda: player.playing)
            assert player.source.stream.local
            resolver.resolve.assert_not_awaited()
            # Decode, seek and normalize from the local copy with real FFmpeg.
            source = NativeSource(fresh.get(123, song), volume=50, start=100, normalize=True)
            try:
                chunks = []
                while frame := await asyncio.to_thread(source.read):
                    chunks.append(frame)
                assert chunks and all(len(frame) == 3840 for frame in chunks)
                assert 400 <= source.position <= 650
            finally:
                source.cleanup()
        finally:
            await player.close()
            await fresh.close()
    finally:
        await cache.close()


@pytest.mark.parametrize(
    "length,live,direct,eligible",
    [
        (299999, False, False, True),
        (300000, False, False, False),
        (300001, False, False, False),
        (0, False, False, False),
        (-1, False, False, False),
        (180000, True, False, False),
        (180000, False, True, False),
        (float("nan"), False, False, False),
    ],
)
async def test_exact_eligibility_uses_full_metadata_not_search_length(
    tmp_path, length, live, direct, eligible
):
    cache = make_cache(tmp_path)
    cache._duration.return_value = length
    task = cache.schedule(
        123,
        replace(SONG, length=1, direct=direct),
        replace(STREAM, length=length, live=live),
        456,
        epoch=cache.epoch,
    )
    assert (task is not None) == eligible
    if task:
        await task
        assert cache.entries[song_key(123, SONG)]["length"] == length
    await cache.close()


@pytest.mark.parametrize(
    "data,length,live",
    [
        ({"duration": 299.999}, 299999, False),
        ({"duration": 300}, 300000, False),
        ({"duration": 120, "is_live": True}, 120000, True),
        ({"duration": 120, "live_status": "post_live"}, 120000, True),
        ({"duration": float("inf")}, 0, False),
        ({"duration": True}, 0, False),
    ],
)
async def test_resolver_exposes_authoritative_length_and_live_status(data, length, live):
    resolver = MediaResolver()
    resolver._extract = AsyncMock(return_value={"url": STREAM.url, **data})
    resolved = await resolver.resolve(SONG)
    assert resolved.length == length and resolved.live is live and not resolved.local
    await resolver.close()


@pytest.mark.parametrize(
    "date,expected",
    [
        ("2026-01-31T15:00:00", "2026-04-30T15:00:00"),
        ("2026-11-30T15:00:00", "2027-02-28T15:00:00"),
        ("2027-11-30T15:00:00", "2028-02-29T15:00:00"),
        ("2026-10-03T15:00:00", "2027-01-03T15:00:00"),
    ],
)
def test_expiry_is_three_calendar_months(date, expected):
    timestamp = datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()
    assert (
        expiry(timestamp)
        == datetime.fromisoformat(expected).replace(tzinfo=timezone.utc).timestamp()
    )


async def test_replay_does_not_extend_retention_and_expired_audio_is_removed(tmp_path):
    now = [datetime(2026, 10, 3, tzinfo=timezone.utc).timestamp()]
    cache = make_cache(tmp_path, clock=lambda: now[0])
    path = await prepare(cache)
    key = song_key(123, SONG)
    created = cache.entries[key]["created"]
    now[0] = expiry(created) - 1
    assert cache.get(123, SONG, 789).local
    assert cache.entries[key]["created"] == created
    now[0] += 1
    assert cache.get(123, SONG) is None
    assert not path.exists() and not path.with_suffix(".json").exists()
    await cache.close()


async def test_load_removes_expired_orphans_partial_files_and_symlinks_without_touching_target(
    tmp_path,
):
    cache = make_cache(tmp_path)
    path = await prepare(cache)
    key = song_key(123, SONG)
    entry = cache.entries[key]
    entry["created"] = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    cache._save(key, entry)
    outside = tmp_path / "private"
    outside.write_bytes(b"OggSoutside")
    linked_key = song_key(123, replace(SONG, uri=SONG.uri + "&other"))
    (cache.root / f"{linked_key}.ogg").symlink_to(outside)
    (cache.root / f"{linked_key}.json").write_text(json.dumps(entry))
    (cache.root / "interrupted.ogg.part").write_bytes(b"partial")
    orphan = cache.root / f"{song_key(789, SONG)}.ogg"
    orphan.write_bytes(b"OggSorphan")
    await cache.close()
    fresh = SongCache()
    fresh.initialize(cache.root)
    assert not fresh.entries and not list(cache.root.iterdir())
    assert outside.read_bytes() == b"OggSoutside" and not path.exists()
    await fresh.close()


async def test_first_play_is_not_delayed_and_duplicate_downloads_are_coalesced(tmp_path):
    cache = make_cache(tmp_path)
    began, release = asyncio.Event(), asyncio.Event()
    download = cache._download

    async def blocked(stream, path):
        began.set()
        await release.wait()
        await download(stream, path)

    cache._download = blocked
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=STREAM))
    player = GuildPlayer(FakeVoice(), resolver, AsyncMock(), source_factory=FakeSource, cache=cache)
    try:
        await player.enqueue([SONG])
        await began.wait()
        assert player.playing and not player.source.stream.local
        first = next(iter(cache.jobs.values()))
        second = cache.schedule(123, SONG, STREAM, 789, epoch=cache.epoch)
        assert first is second and len(cache.jobs) == 1
        release.set()
        await first
        assert sorted(cache.entries[song_key(123, SONG)]["requesters"]) == [789]
        # The first automatic playback was anonymous; the joined warmer has attribution.
    finally:
        await player.close()
        await cache.close()


async def test_cache_download_failures_leave_music_and_queue_running(tmp_path):
    cache = make_cache(tmp_path)
    cache._download = AsyncMock(side_effect=OSError("secret stream credentials"))
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=STREAM))
    player = GuildPlayer(FakeVoice(), resolver, AsyncMock(), source_factory=FakeSource, cache=cache)
    try:
        await player.enqueue([SONG])
        await eventually(lambda: player.playing and cache.last_error)
        assert cache.last_error == "OSError" and not cache.entries
        player.report_error.assert_not_awaited()
        assert player.current is SONG
    finally:
        await player.close()
        await cache.close()


async def test_unreadable_cached_audio_retries_stream_once_without_duplicate_start(tmp_path):
    cache = make_cache(tmp_path)
    path = await prepare(cache)
    resolver = SimpleNamespace(resolve=AsyncMock(return_value=replace(STREAM, length=0)))
    on_start = AsyncMock()
    player = GuildPlayer(
        FakeVoice(),
        resolver,
        AsyncMock(),
        source_factory=FakeSource,
        cache=cache,
        on_start=on_start,
    )
    try:
        await player.enqueue([SONG])
        await eventually(lambda: player.playing)
        assert player.source.stream.local
        player.source.position = 40
        player.voice.finish(MediaError("Broken local copy"))
        await eventually(lambda: len(player.voice.starts) == 2)
        assert not player.source.stream.local and player.position == 40
        assert not path.exists()
        on_start.assert_awaited_once()
        resolver.resolve.assert_awaited_once_with(SONG)
        player.report_error.assert_not_awaited()
    finally:
        await player.close()
        await cache.close()


async def test_storage_limits_reserve_pending_work_and_keep_unexpired_copies(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    path = await prepare(cache)
    existing_size = path.stat().st_size
    monkeypatch.setattr("audioplus.cache.MAX_CACHE_BYTES", existing_size + RESERVE_BYTES)
    blocked = asyncio.Event()

    async def download(*args):
        await blocked.wait()

    cache._download = download
    other = replace(SONG, uri=SONG.uri + "&other")
    task = cache.schedule(123, other, STREAM, epoch=cache.epoch)
    assert task is not None
    assert (
        cache.schedule(123, replace(SONG, uri=SONG.uri + "&third"), STREAM, epoch=cache.epoch)
        is None
    )
    assert path.exists() and cache.get(123, SONG)
    await cache.close()
    assert task.cancelled() and path.exists()


async def test_low_space_skips_download_without_erasing_ready_copy(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    path = await prepare(cache)
    monkeypatch.setattr("audioplus.cache.shutil.disk_usage", lambda path: SimpleNamespace(free=0))
    assert (
        cache.schedule(123, replace(SONG, uri=SONG.uri + "&other"), STREAM, epoch=cache.epoch)
        is None
    )
    assert cache.last_error == "Low disk space" and path.exists()
    await cache.close()


async def test_clear_blocks_late_lookup_from_repopulating_cache(tmp_path):
    cache = make_cache(tmp_path)
    began, release = asyncio.Event(), asyncio.Event()

    async def lookup(track):
        began.set()
        await release.wait()
        return STREAM

    resolver = SimpleNamespace(resolve=AsyncMock(side_effect=lookup))
    player = GuildPlayer(FakeVoice(), resolver, AsyncMock(), source_factory=FakeSource, cache=cache)
    try:
        await player.enqueue([SONG])
        await began.wait()
        await cache.remove(guild_id=123)
        release.set()
        await eventually(lambda: player.playing)
        assert not cache.jobs and not cache.entries
    finally:
        await player.close()
        await cache.close()


async def test_user_and_server_deletion_are_scoped_and_cancel_owned_preparation(tmp_path):
    cache = make_cache(tmp_path)
    one = await prepare(cache, guild_id=123, user_id=456)
    two = await prepare(cache, guild_id=789, user_id=999)
    cache.get(123, SONG, 888)
    assert not cache.get(555, SONG)
    exported = cache.user_data(456)
    assert len(exported) == 1 and exported[0]["server_id"] == 123
    assert "private" not in json.dumps(exported) and "888" not in json.dumps(exported)
    began = asyncio.Event()

    async def blocked(*args):
        began.set()
        await asyncio.Event().wait()

    cache._download = blocked
    pending = cache.schedule(
        123, replace(SONG, uri=SONG.uri + "&pending"), STREAM, 456, epoch=cache.epoch
    )
    await began.wait()
    await cache.remove(user_id=456)
    assert not one.exists() and two.exists() and pending.cancelled()
    assert not cache.user_data(456) and not cache.user_data(888)
    await cache.remove(guild_id=789)
    assert not two.exists() and not cache.entries
    await cache.close()


@pytest.mark.parametrize("repeated", [False, True])
async def test_cancellation_during_child_spawn_reaps_child_and_partial_file(
    tmp_path, monkeypatch, repeated
):
    cache = make_cache(tmp_path)
    original = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed(*args, **kwargs):
        process = await original(sys.executable, "-c", "import time; time.sleep(20)", **kwargs)
        processes.append(process)
        spawned.set()
        await release.wait()
        return process

    async def download(stream, path):
        path.write_bytes(b"OggSpartial")
        await cache._process(["unused"], stdout=asyncio.subprocess.DEVNULL, timeout=90)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    cache._download = download
    task = cache.schedule(123, SONG, STREAM, 456, epoch=cache.epoch)
    await spawned.wait()
    closing = asyncio.create_task(cache.close())
    await asyncio.sleep(0)
    if repeated:
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    release.set()
    await closing
    assert task.cancelled() and processes[0].returncode is not None
    assert not cache.jobs and not cache.entries and not list(cache.root.glob("*.part"))


async def test_child_timeout_is_reaped(monkeypatch):
    original = asyncio.create_subprocess_exec
    processes = []

    async def observed(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observed)
    with pytest.raises(MediaError, match="timed out"):
        await SongCache._process(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            stdout=asyncio.subprocess.DEVNULL,
            timeout=0.05,
        )
    assert processes[0].returncode is not None


async def test_owned_hourly_expiry_task_stops_on_unload(tmp_path, monkeypatch):
    now = [datetime(2026, 10, 3, tzinfo=timezone.utc).timestamp()]
    cache = make_cache(tmp_path, clock=lambda: now[0])
    path = await prepare(cache)
    now[0] = expiry(now[0])
    monkeypatch.setattr("audioplus.cache.PRUNE_INTERVAL", 0.001)
    cache.start()
    await eventually(lambda: not path.exists())
    await cache.close()
    assert cache._maintenance.done()


async def test_commands_show_scoped_status_and_check_admin_in_prefix_and_slash(
    red_command_runtime, monkeypatch
):
    bot, cog, member, invoke = red_command_runtime
    init_events(bot, parse_cli_flags([]))
    monkeypatch.setattr(bot, "_delete_delay", AsyncMock())
    member.guild_permissions = discord.Permissions.none()
    for channel in member.guild.channels:
        channel.permissions_for.side_effect = lambda target: target.guild_permissions
    cache = cog._cache
    cache._download = make_cache(cache.root.parent / "stub")._download
    cache._duration = AsyncMock(return_value=180000)
    path = await prepare(cache, guild_id=member.guild.id, user_id=member.id)
    status = await invoke("!audiocache")
    assert not status.command_failed
    assert "3 calendar months" in status.send.await_args.kwargs["embed"].description
    denied = await invoke("!audiocache clear")
    assert denied.command_failed and path.exists()
    denied = await invoke_slash(bot, invoke, monkeypatch, "audiocache clear")
    assert denied.command_failed and path.exists()
    member.guild_permissions = discord.Permissions(manage_guild=True)
    assert not (await invoke_slash(bot, invoke, monkeypatch, "audiocache clear")).command_failed
    assert not path.exists()
    await prepare(cache, guild_id=member.guild.id, user_id=member.id)
    assert not (await invoke("!audiocache clear")).command_failed
    assert not cache.entries


async def test_red_user_data_hooks_export_cache_metadata_and_erase_audio(audio_runtime):
    cog, player, ctx = audio_runtime
    cache = cog._cache
    cache._download = make_cache(cache.root.parent / "stub")._download
    cache._duration = AsyncMock(return_value=180000)
    path = await prepare(cache, guild_id=ctx.guild.id, user_id=ctx.author.id)
    data = json.load((await cog.red_get_data_for_user(user_id=ctx.author.id))["audioplus.json"])
    assert data["song_cache"][0]["server_id"] == ctx.guild.id
    assert "token=private" not in json.dumps(data)
    await cog.red_delete_data_for_user(requester="user", user_id=ctx.author.id)
    assert not path.exists() and not cache.entries


def test_intro_decoder_and_resolver_remain_independently_vendored():
    for name in ("source.py", "resolver.py"):
        intro = Path("introplus", name).read_text()
        # Only each independent cog's diagnostic/error hints differ.
        assert (
            intro.replace("IntroPlus", "AudioPlus").replace("intro diagnostics", "audio pingnode")
            == Path("audioplus", name).read_text()
        )


@pytest.mark.parametrize("size,duration", [(24, 1000), (24, 300000), (40, 180000)])
async def test_incomplete_long_or_oversized_download_is_never_admitted(
    tmp_path, monkeypatch, size, duration
):
    cache = make_cache(tmp_path)
    monkeypatch.setattr("audioplus.cache.MAX_FILE_BYTES", 32)

    async def download(stream, path):
        path.write_bytes(b"OggS" + b"\0" * (size - 4))

    cache._download = download
    cache._duration.return_value = duration
    task = cache.schedule(123, SONG, STREAM, 456, epoch=cache.epoch)
    with pytest.raises(MediaError):
        await task
    assert not cache.entries and not list(cache.root.iterdir())
    await cache.close()
