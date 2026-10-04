"""Retained history checks and narrowly scoped privacy erasure at mocked Discord boundaries."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import forbidden, make_channel, make_context, make_guild, make_member
from test_audio_hybrid import red_command_runtime as audio_runtime_fixture
from test_cog_hybrid import command_runtime as commands_runtime_fixture
from test_cog_hybrid import invoke_slash
from test_exportplus import basic_job, chat_message, text_channel

from exportplus import ExportPlus
from exportplus.history import scan_channel
from exportplus.transcript import message_record
from logplus import LogPlus
from logplus.access import visible_records

red_command_runtime = audio_runtime_fixture
command_runtime = commands_runtime_fixture


def record(source, *, number=0, text="visible", member=123456789012345678):
    return {
        "time": time.time() - number,
        "event": "message_deleted",
        "category": "message",
        "source": source,
        "users": [member],
        "title": text,
        "description": "details",
        "fields": [],
    }


async def test_retained_queries_filter_before_limit_and_recheck_both_people(bot, guild):
    cog = LogPlus(bot)
    member = make_member(guild)
    ctx = make_context(guild, author=member)
    hidden, visible, bot_hidden = [make_channel(guild, i) for i in (111, 222, 333)]
    hidden.permissions_for.return_value = discord.Permissions.none()
    bot_hidden.permissions_for.side_effect = lambda person: (
        discord.Permissions.none() if person.id == guild.me.id else discord.Permissions.all()
    )
    guild.fetch_channel = AsyncMock(side_effect=forbidden())
    rows = [record(visible.id, number=40), record(None, number=39)] + [
        record(hidden.id, number=i, text="private") for i in range(30)
    ]
    rows.extend([record(bot_hidden.id), record(444, text="deleted")])
    rows.sort(key=lambda row: row["time"])
    await cog.config.guild(guild).history_records.set(rows)
    found = await cog._history_query(guild, ctx=ctx, limit=2)
    assert len(found) == 2 and {row["source"] for row in found} == {None, visible.id}
    assert not await cog._history_query(guild, ctx=ctx, query="private")
    visible.permissions_for.return_value = discord.Permissions(view_channel=True)
    assert [row["source"] for row in await cog._history_query(guild, ctx=ctx)] == [None]
    guild.members.remove(member)
    assert not await cog._history_query(guild, ctx=ctx)


@pytest.mark.parametrize("slash", [False, True])
async def test_native_history_commands_never_return_hidden_source_records(
    command_runtime, monkeypatch, slash
):
    bot, loaded, member, invoke = command_runtime
    bot.owner_ids.add(member.id)  # Red owner checks still cannot grant source visibility
    cog = bot.get_cog("LogPlus")
    hidden, visible = make_channel(member.guild, 111), make_channel(member.guild, 222)
    hidden.permissions_for.return_value = discord.Permissions.none()
    await cog.config.guild(member.guild).history_records.set(
        [record(hidden.id, text="secret-marker"), record(visible.id, text="public-marker")]
    )
    for path, command, options in (
        ("timeline", f"!timeline <@{member.id}>", {"member": member, "days": 7}),
        ("logsearch", "!logsearch marker", {"query": "marker", "category": "all", "days": 7}),
        (
            "logexport",
            "!logexport json",
            {"format": "json", "category": "all", "days": 7, "query": ""},
        ),
    ):
        ctx = (
            await invoke_slash(bot, invoke, monkeypatch, path, **options)
            if slash
            else await invoke(command)
        )
        assert not ctx.command_failed
        descriptions = " ".join(
            (call.kwargs.get("embed").description or "")
            for call in ctx.send.await_args_list
            if call.kwargs.get("embed")
        )
        assert "secret-marker" not in descriptions
        if path == "logexport":
            upload = ctx.send.await_args.kwargs["file"]
            assert [row["title"] for row in json.load(upload.fp)] == ["public-marker"]
            upload.close()
        else:
            assert "public-marker" in descriptions


async def test_archived_private_threads_require_current_membership_and_visibility(guild):
    member = make_member(guild)
    ctx = make_context(guild, author=member)
    thread = make_channel(guild, 456, discord.Thread)
    thread.is_private.return_value = True
    guild.channels.remove(thread)  # archived threads need to be resolved from Discord
    guild.fetch_channel = AsyncMock(return_value=thread)
    thread.permissions_for.return_value = discord.Permissions(
        view_channel=True, read_message_history=True
    )
    thread.fetch_member = AsyncMock(return_value=SimpleNamespace(id=member.id))
    rows = [record(thread.id)]
    assert await visible_records(ctx, rows) == rows
    assert {call.args[0] for call in thread.fetch_member.await_args_list} == {
        member.id,
        guild.me.id,
    }
    thread.members = [SimpleNamespace(id=member.id), SimpleNamespace(id=guild.me.id)]
    thread.fetch_member.side_effect = forbidden()
    assert not await visible_records(ctx, rows)  # stale cache cannot grant membership
    thread.permissions_for.return_value = discord.Permissions.all()
    assert await visible_records(ctx, rows) == rows  # Manage Threads can read private threads
    thread.permissions_for.return_value = discord.Permissions.none()
    assert not await visible_records(ctx, rows)
    guild.fetch_channel.side_effect = forbidden()
    assert not await visible_records(ctx, rows)


async def test_incident_provenance_migrates_and_hidden_events_stay_hidden(bot, guild):
    cog = LogPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    ctx = make_context(guild, author=member)
    source = record(channel.id)
    linked = {
        "time": source["time"],
        "users": source["users"],
        "title": source["title"],
        "description": "visible\ndetails",
    }
    case = {
        "title": "Staff case",
        "creator": member.id,
        "subject": member.id,
        "created": int(time.time()),
        "notes": [],
        "events": [linked, {**linked, "time": 1}],
        "resolution": {},
    }
    await cog.config.guild(guild).history_records.set([source])
    await cog.config.guild(guild).incident_cases.set({"case": case})
    await cog._migrate_case_sources()
    saved = (await cog.config.guild(guild).incident_cases())["case"]["events"]
    assert [row["source"] for row in saved] == [channel.id, -1]
    assert await visible_records(ctx, saved, missing_source=True) == [saved[0]]
    channel.permissions_for.return_value = discord.Permissions.none()
    assert not await visible_records(ctx, saved, missing_source=True)
    cog._reply = AsyncMock()
    await cog.incident.callback(cog, ctx, "case")
    assert "Log ·" not in cog._reply.call_args.args[1]


def saved_job(tmp_path, guild, owner, author, *, flushed):
    job = basic_job(tmp_path / str(guild.id), guild, owner)
    channel = text_channel(guild)
    job.writer.add(
        message_record(chat_message(author, channel)), {"id": str(channel.id), "name": channel.name}
    )
    if flushed:
        job.volumes = job.writer.finish(job.manifest())
    job.state = "ready"
    return job


@pytest.mark.parametrize("flushed", [True, False])
async def test_deletion_removes_only_author_or_requester_jobs(bot, tmp_path, monkeypatch, flushed):
    monkeypatch.setattr("exportplus.cog.cog_data_path", lambda cog: tmp_path / "ExportPlus")
    cog = ExportPlus(bot)
    guild_a, guild_b, guild_c = [make_guild(i) for i in (1, 2, 3)]
    owner_a, owner_b = make_member(guild_a, 11), make_member(guild_b, 22)
    deleted = make_member(guild_a, 33)
    author_b = make_member(guild_b, 44)
    affected = saved_job(tmp_path, guild_a, owner_a, deleted, flushed=flushed)
    unaffected = saved_job(tmp_path, guild_b, owner_b, author_b, flushed=flushed)
    requester = make_member(guild_c, deleted.id)
    requested = saved_job(tmp_path, guild_c, requester, make_member(guild_c, 55), flushed=flushed)
    cog._jobs = {job.guild_id: job for job in (affected, unaffected, requested)}
    before = {path.name: path.read_bytes() for path in unaffected.root.iterdir()}
    await cog.red_delete_data_for_user(requester="user", user_id=deleted.id)
    assert cog._jobs == {guild_b.id: unaffected}
    assert not affected.root.exists() and not requested.root.exists()
    assert {path.name: path.read_bytes() for path in unaffected.root.iterdir()} == before
    assert unaffected.root.exists()


async def test_deletion_barrier_skips_later_author_rows_without_stopping_unrelated_job(
    bot, guild, tmp_path, monkeypatch
):
    monkeypatch.setattr("exportplus.cog.cog_data_path", lambda cog: tmp_path / "ExportPlus")
    cog = ExportPlus(bot)
    owner, deleted, other = [make_member(guild, user_id=i) for i in (11, 22, 33)]
    channel = text_channel(guild)
    job = basic_job(tmp_path, guild, owner)
    paused, released = asyncio.Event(), asyncio.Event()

    async def history(**kwargs):
        paused.set()
        await released.wait()
        yield chat_message(deleted, channel)
        yield chat_message(other, channel, number=2)

    channel.history.side_effect = history
    cog._jobs[guild.id] = job
    job.task = asyncio.create_task(scan_channel(job, channel, guild, owner))
    await paused.wait()
    await cog.red_delete_data_for_user(requester="user", user_id=deleted.id)
    assert cog._jobs[guild.id] is job and not job.task.done()
    released.set()
    await job.task
    assert job.messages == 1 and not job.complete
    assert not job.writer.contains_author(deleted.id)
    assert job.writer.contains_author(other.id)
    assert job.channels[0]["status"] == "partial: author data erased during scan"


async def test_deletion_cancels_affected_inflight_job_after_buffered_rows(
    bot, guild, tmp_path, monkeypatch
):
    monkeypatch.setattr("exportplus.cog.cog_data_path", lambda cog: tmp_path / "ExportPlus")
    cog = ExportPlus(bot)
    owner, deleted = make_member(guild, 11), make_member(guild, 22)
    channel = text_channel(guild)
    job = basic_job(tmp_path, guild, owner)
    buffered = asyncio.Event()

    async def history(**kwargs):
        yield chat_message(deleted, channel)
        buffered.set()
        await asyncio.Event().wait()

    channel.history.side_effect = history
    cog._jobs[guild.id] = job
    job.task = asyncio.create_task(scan_channel(job, channel, guild, owner))
    await buffered.wait()
    assert job.writer.contains_author(deleted.id)
    await cog.red_delete_data_for_user(requester="user", user_id=deleted.id)
    assert job.task.cancelled() and not job.root.exists() and not cog._jobs


async def test_affected_privacy_erasure_cancels_owned_download_without_waiting_for_dm(
    bot, guild, tmp_path, monkeypatch
):
    monkeypatch.setattr("exportplus.cog.cog_data_path", lambda cog: tmp_path / "ExportPlus")
    cog = ExportPlus(bot)
    owner, deleted = make_member(guild, 11), make_member(guild, 22)
    job = saved_job(tmp_path, guild, owner, deleted, flushed=True)
    cog._jobs[guild.id] = job
    cog._authorize = AsyncMock()
    sending = asyncio.Event()

    async def blocked_dm(*args, **kwargs):
        sending.set()
        await asyncio.Event().wait()

    cog._private = blocked_dm
    download = asyncio.create_task(cog._deliver(job))
    await sending.wait()
    await asyncio.wait_for(cog.red_delete_data_for_user(requester="user", user_id=deleted.id), 1)
    assert download.cancelled() and not job.transfers and not job.root.exists()
