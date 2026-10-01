"""Actual Red storage and commands with mocked Discord/media transport."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import make_member
from redbot.core import commands
from redbot.core._cli import parse_cli_flags
from redbot.core._events import init_events
from test_audio_hybrid import red_command_runtime as red_fixture
from test_audioplus import audio_runtime as native_fixture
from test_cog_hybrid import invoke_slash
from test_native_audio import eventually

from audioplus.listening import HISTORY_BYTES, recent_records
from audioplus.resolver import Track

audio_runtime = native_fixture
red_command_runtime = red_fixture


def song(name="Song", seconds=180):
    return Track(f"https://example.invalid/{name}", name, "Artist", seconds * 1000)


async def test_history_records_starts_preserves_ids_and_does_not_duplicate_seek(audio_runtime):
    cog, player, ctx = audio_runtime
    player.on_start = cog._track_started
    await player.enqueue([song()], ctx)
    await eventually(lambda: ctx.channel.send.await_count > 0)
    initial = await cog.config.guild(ctx.guild).listening_history()
    assert len(initial) == 1 and initial[0]["requester"] == ctx.author.id
    assert initial[0]["track"]["uri"] == "https://example.invalid/Song"
    await player.seek(1000)
    await eventually(lambda: len(player.voice.starts) == 2)
    assert await cog.config.guild(ctx.guild).listening_history() == initial
    player.repeat = "track"
    player.voice.finish()
    await eventually(lambda: len(player.voice.starts) == 3)
    await eventually(lambda: ctx.channel.send.await_count >= 1 and not player.preparing)
    # The audio start callback writes before the panel update.
    for _ in range(20):
        records = await cog.config.guild(ctx.guild).listening_history()
        if len(records) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(records) == 2 and records[0]["id"] != records[1]["id"]


async def test_history_switch_erases_retained_data_and_prevents_collection(audio_runtime):
    cog, player, ctx = audio_runtime
    player.current = song()
    player._requesters[id(player.current)] = ctx.author.id
    await cog._record_listening_history(player)
    await cog.audioset_history.callback(cog, ctx, enabled=False)
    await cog._record_listening_history(player)
    assert not await cog.config.guild(ctx.guild).listening_history()
    await cog.audioset_history.callback(cog, ctx, enabled=True)
    await cog._record_listening_history(player)
    assert len(await cog.config.guild(ctx.guild).listening_history()) == 1


def test_history_age_count_and_byte_budgets():
    now = time.time()
    records = [{"id": str(i), "at": now, "track": {"title": "a" * 9000}} for i in range(120)]
    result = recent_records([{"at": now - 31 * 86400}, *records], now)
    assert len(result) < 100 and result[-1]["id"] == "119"
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= HISTORY_BYTES


async def test_history_data_hooks_remove_only_the_identified_requests(audio_runtime):
    cog, player, ctx = audio_runtime
    group = cog.config.guild(ctx.guild)
    await group.listening_history.set(
        [
            {"id": "mine", "at": int(time.time()), "requester": ctx.author.id, "track": {}},
            {"id": "other", "at": int(time.time()), "requester": 42, "track": {}},
        ]
    )
    exported = json.load((await cog.red_get_data_for_user(user_id=ctx.author.id))["audioplus.json"])
    assert exported["listening_history"][str(ctx.guild.id)][0]["id"] == "mine"
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=ctx.author.id)
    assert [r["id"] for r in await group.listening_history()] == ["other"]


async def test_limits_are_atomic_for_concurrent_requests_and_include_current(audio_runtime):
    cog, player, ctx = audio_runtime
    ctx.author.guild_permissions = discord.Permissions.none()
    await cog.audioset_limits.callback(cog, ctx, max_seconds=600, per_member=2)
    results = await asyncio.gather(
        *(cog._enqueue(player, [song(str(i))], ctx) for i in range(5)), return_exceptions=True
    )
    assert sum(isinstance(r, commands.CommandError) for r in results) == 3
    assert len(player.queue) + bool(player.current) == 2
    with pytest.raises(commands.CommandError, match="already have 2"):
        await cog._enqueue(player, [song("extra")], ctx)


@pytest.mark.parametrize("seconds", [0, 601])
async def test_overlong_unknown_and_playlist_requests_are_rejected_entirely(audio_runtime, seconds):
    cog, player, ctx = audio_runtime
    ctx.author.guild_permissions = discord.Permissions.none()
    await cog.audioset_limits.callback(cog, ctx, max_seconds=600)
    with pytest.raises(commands.CommandError, match="known duration"):
        await cog._enqueue(player, [song("valid"), song("blocked", seconds)], ctx)
    assert not player.current and not player.queue and not player._requesters


@pytest.mark.parametrize("exemption", ["dj", "manager", "owner"])
async def test_configured_djs_managers_and_owner_are_exempt(audio_runtime, exemption):
    cog, player, ctx = audio_runtime
    ctx.author.guild_permissions = discord.Permissions.none()
    await cog.audioset_limits.callback(cog, ctx, max_seconds=1, per_member=1)
    if exemption == "dj":
        ctx.author.roles = [SimpleNamespace(id=77)]
        await cog.config.guild(ctx.guild).music.dj_role.set(77)
    elif exemption == "manager":
        ctx.author.guild_permissions = discord.Permissions(manage_guild=True)
    else:
        cog.bot.is_owner = AsyncMock(return_value=True)
    await cog._enqueue(player, [song("one"), song("two", 0)], ctx)
    assert len(player.queue) + bool(player.current) == 2


async def test_limits_apply_separately_to_each_requester(audio_runtime):
    cog, player, ctx = audio_runtime
    ctx.author.guild_permissions = discord.Permissions.none()
    await cog.audioset_limits.callback(cog, ctx, per_member=1)
    await cog._enqueue(player, [song("one")], ctx)
    ctx.author = make_member(ctx.guild, 42)
    ctx.author.guild_permissions = discord.Permissions.none()
    await cog._enqueue(player, [song("two")], ctx)
    assert len(player.queue) + bool(player.current) == 2


@pytest.mark.parametrize("seconds, count", [(-1, 0), (86401, 0), (0, -1), (0, 101)])
async def test_invalid_limits_do_not_write(audio_runtime, seconds, count):
    cog, player, ctx = audio_runtime
    before = await cog.config.guild(ctx.guild).music()
    with pytest.raises(commands.BadArgument):
        await cog.audioset_limits.callback(cog, ctx, max_seconds=seconds, per_member=count)
    assert await cog.config.guild(ctx.guild).music() == before


async def test_replay_checks_original_play_permissions_and_uses_normal_queue(
    red_command_runtime, monkeypatch
):
    bot, cog, member, invoke = red_command_runtime
    init_events(bot, parse_cli_flags([]))
    monkeypatch.setattr(bot, "_delete_delay", AsyncMock())
    record = {"id": "abcdef", "at": int(time.time()), "requester": member.id, "track": {}}
    from audioplus.features import saved_track

    record["track"] = saved_track(song())
    await cog.config.guild(member.guild).listening_history.set([record])
    cog._queue_saved = AsyncMock()
    ctx = await invoke("!replay abcdef")
    assert not ctx.command_failed
    assert cog._queue_saved.await_args.args[1][0].title == "Song"
    cog._queue_saved.reset_mock()
    cog.replay.reset_cooldown(ctx)
    bot.get_command("audio play").disable_in(member.guild)
    ctx = await invoke("!replay abcdef")
    assert ctx.command_failed
    cog._queue_saved.assert_not_awaited()


async def test_history_and_limits_slash_use_actual_red_conversion(red_command_runtime, monkeypatch):
    bot, cog, member, invoke = red_command_runtime
    bot.owner_ids.add(member.id)
    ctx = await invoke_slash(
        bot, invoke, monkeypatch, "audioset limits", max_seconds=600, per_member=3
    )
    assert not ctx.command_failed
    assert (await cog.config.guild(member.guild).music())["per_member"] == 3
    ctx = await invoke_slash(bot, invoke, monkeypatch, "history list", page=1)
    assert (
        not ctx.command_failed
        and "No recent songs" in ctx.send.call_args.kwargs["embed"].description
    )
