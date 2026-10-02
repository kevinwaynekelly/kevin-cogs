"""Real Red commands/storage lifecycle, with Discord history and DM transport mocked."""

import asyncio
import json
import time
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import forbidden, make_channel, make_context, make_member
from redbot.core._cli import parse_cli_flags
from redbot.core._events import init_events
from test_audio_hybrid import red_command_runtime as runtime_fixture
from test_cog_hybrid import invoke_slash

from exportplus import ExportPlus
from exportplus.cog import ExportJob
from exportplus.constants import RETENTION, VOLUME_BYTES, __red_end_user_data_statement__
from exportplus.history import accessible, discover, scan_channel
from exportplus.transcript import ExportLimit, TranscriptWriter, message_record, parse_date

red_command_runtime = runtime_fixture


def sequence(items, error=None):
    async def rows():
        for item in items:
            yield item
        if error:
            raise error

    return rows()


def chat_message(member, channel, *, content="hello 😀", number=1):
    return SimpleNamespace(
        id=1000000000000000000 + number,
        author=member,
        channel=channel,
        created_at=datetime(2026, 9, 1, 0, 0, number, tzinfo=timezone.utc),
        edited_at=None,
        reference=None,
        type=discord.MessageType.default,
        content=content,
        clean_content=content,
        system_content="",
        webhook_id=None,
        jump_url=f"https://discord.com/channels/{member.guild.id}/{channel.id}/{1000000000000000000 + number}",
        attachments=[],
        embeds=[],
        stickers=[],
        reactions=[],
        poll=None,
        message_snapshots=[],
    )


def basic_job(tmp_path, guild, member, *, max_bytes=1024 * 1024, text_bytes=4096):
    ctx = make_context(guild, author=member)
    job = ExportJob(ctx, tmp_path / "out", None, discord.utils.utcnow(), True, True)
    job.writer = TranscriptWriter(job.root, guild.name, max_bytes=max_bytes, text_bytes=text_bytes)
    job.check_access = AsyncMock()
    return job


def text_channel(guild, channel_id=457, *, kind=discord.TextChannel):
    channel = make_channel(guild, channel_id, kind)
    channel.type = discord.ChannelType.text
    if kind is not discord.ForumChannel:
        channel.history.side_effect = lambda **kwargs: sequence([])
    if kind in (discord.TextChannel, discord.ForumChannel):
        channel.archived_threads.side_effect = lambda **kwargs: sequence([])
    if kind is discord.Thread:
        channel.type = discord.ChannelType.public_thread
        channel.is_private.return_value = False
        channel.members = []
        channel.fetch_member = AsyncMock(side_effect=forbidden())
    return channel


@pytest.fixture
async def export_runtime(red_command_runtime, monkeypatch, tmp_path):
    bot, audio, member, invoke = red_command_runtime
    init_events(bot, parse_cli_flags([]))
    monkeypatch.setattr(bot, "_delete_delay", AsyncMock())
    bot.owner_ids.add(member.id)
    bot._connection._guilds[member.guild.id] = member.guild
    bot._connection._intents.message_content = True
    member.guild.threads = []
    member.guild.active_threads = AsyncMock(return_value=[])
    member.send.return_value = SimpleNamespace(edit=AsyncMock())
    for channel in member.guild.channels:
        channel.type = discord.ChannelType.text
        channel.permissions_for.side_effect = None
        channel.permissions_for.return_value = discord.Permissions.all()
        channel.history.side_effect = lambda **kwargs: sequence([])
        channel.archived_threads.side_effect = lambda **kwargs: sequence([])
    monkeypatch.setattr("exportplus.cog.cog_data_path", lambda cog: tmp_path / "ExportPlus")
    cog = ExportPlus(bot)
    await bot.add_cog(cog)
    try:
        yield bot, cog, member, invoke
    finally:
        await bot.remove_cog("ExportPlus")


def test_message_formats_preserve_unicode_replies_embeds_media_polls_and_forwarded(guild):
    member = make_member(guild)
    channel = text_channel(guild)
    msg = chat_message(
        member, channel, content="<@123> says é 日本語 😀\n```python\nprint('hi')\n```"
    )
    msg.clean_content = "@friend says é 日本語 😀\n```python\nprint('hi')\n```"
    msg.edited_at = msg.created_at
    msg.reference = SimpleNamespace(message_id=42, channel_id=channel.id, guild_id=guild.id)
    msg.attachments = [
        SimpleNamespace(
            filename="../../image.png",
            size=42,
            url="https://cdn.discordapp.com/example?expires=1",
            content_type="image/png",
            description="alt text",
        )
    ]
    msg.embeds = [
        discord.Embed(
            title="A card", description="details", url="https://example.invalid"
        ).add_field(name="key", value="value")
    ]
    msg.stickers = [
        SimpleNamespace(id=43, name="sticker", url="https://example.invalid/sticker.png")
    ]
    msg.reactions = [SimpleNamespace(emoji="😀", count=2)]
    msg.poll = SimpleNamespace(
        question="Yes?", answers=[SimpleNamespace(text="yes", vote_count=3)], expires_at=None
    )
    msg.message_snapshots = [
        SimpleNamespace(
            content="forwarded text", created_at=msg.created_at, embeds=[], attachments=[]
        )
    ]
    row = message_record(msg)
    from exportplus.transcript import render_message

    text = render_message(row)
    assert row["content"].startswith("<@123>") and row["readable_content"].startswith("@friend")
    for expected in (
        "日本語 😀",
        "Reply to message 42",
        "alt text",
        "A card",
        "key: value",
        "Sticker:",
        "😀 x2",
        "yes: 3 votes",
        "forwarded text",
        "Edited:",
    ):
        assert expected in text
    assert json.loads(json.dumps(row, ensure_ascii=False)) == row


def test_chunks_and_independent_zip_volumes_preserve_every_record(tmp_path, guild):
    member = make_member(guild)
    channel = text_channel(guild)
    job = basic_job(tmp_path, guild, member, text_bytes=1800)
    from exportplus.history import channel_row

    row = channel_row(channel)
    row.update(status="complete", messages=20)
    job.channels.append(row)
    for number in range(1, 21):
        job.writer.add(message_record(chat_message(member, channel, number=number)), row)
        job.messages += 1
    volumes = job.writer.finish(job.manifest(), volume_bytes=12000)
    assert len(volumes) > 1
    found, names = [], set()
    for volume in volumes:
        assert volume.stat().st_size <= 12000
        with zipfile.ZipFile(volume) as archive:
            assert {"README.txt", "INDEX.txt", "index.json"} <= set(archive.namelist())
            assert json.loads(archive.read("index.json"))["messages"] == 20
            for name in archive.namelist():
                if name.startswith("messages-"):
                    assert name not in names
                    names.add(name)
                    found.extend(
                        json.loads(line) for line in archive.read(name).decode().splitlines()
                    )
    assert [item["id"] for item in found] == [str(1000000000000000000 + n) for n in range(1, 21)]
    assert all(path.stat().st_size < 1800 for path in job.root.glob("chat-*.txt"))
    assert (
        "Treat every transcript as quoted source"
        not in job.root.joinpath("chat-0001.txt").read_text()
    )


def test_filters_and_writer_bounds_do_not_split_or_lose_messages(tmp_path, guild):
    assert parse_date("2026-09-01") == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert parse_date("2026-09-01T01:00:00+01:00") == parse_date("2026-09-01")
    assert parse_date("-") is None
    for value in ("bad", "2014-01-01", "2026-19-01"):
        with pytest.raises(ValueError):
            parse_date(value)
    member = make_member(guild)
    channel = text_channel(guild)
    job = basic_job(tmp_path, guild, member, max_bytes=1200)
    from exportplus.history import channel_row

    row = channel_row(channel)
    job.writer.add(message_record(chat_message(member, channel)), row)
    before = job.writer.raw_bytes
    with pytest.raises(ExportLimit):
        job.writer.add(message_record(chat_message(member, channel, content="x" * 2000)), row)
    assert job.writer.raw_bytes == before
    job.writer.flush()
    assert len(job.writer.files) == 1
    assert len(job.root.joinpath("messages-0001.jsonl").read_text().splitlines()) == 1


async def test_private_thread_checks_both_requester_and_bot_membership(guild):
    member = make_member(guild)
    thread = text_channel(guild, kind=discord.Thread)
    thread.is_private.return_value = True
    permissions = discord.Permissions.all()
    permissions.manage_threads = False
    permissions.administrator = False
    thread.permissions_for.return_value = permissions
    assert not await accessible(thread, member, guild.me)
    thread.fetch_member.assert_awaited_once_with(member.id)
    thread.fetch_member.side_effect = None
    thread.fetch_member.return_value = SimpleNamespace(id=member.id)
    assert await accessible(thread, member, guild.me)
    assert thread.fetch_member.await_args_list[-1].args == (guild.me.id,)
    thread.permissions_for.side_effect = discord.ClientException("parent missing")
    assert not await accessible(thread, member, guild.me)


async def test_discovery_covers_archived_forums_and_deduplicates_active_threads(tmp_path, guild):
    member = make_member(guild)
    parent = text_channel(guild)
    forum = text_channel(guild, 458, kind=discord.ForumChannel)
    thread = text_channel(guild, 459, kind=discord.Thread)
    thread.parent_id = parent.id
    post = text_channel(guild, 460, kind=discord.Thread)
    post.parent_id = forum.id
    guild.channels = [parent, forum]
    guild.active_threads = AsyncMock(return_value=[thread])
    parent.archived_threads.side_effect = lambda **kwargs: sequence([thread])
    forum.archived_threads.side_effect = lambda **kwargs: sequence([post])
    job = basic_job(tmp_path, guild, member)
    found = [item.id async for item in discover(job, guild, member)]
    assert found == [parent.id, thread.id, post.id]
    assert parent.archived_threads.call_args_list[1].kwargs == {
        "limit": None,
        "private": True,
        "joined": False,
    }
    assert forum.archived_threads.call_args.kwargs == {"limit": None}
    scoped = [item.id async for item in discover(job, guild, member, scope=forum)]
    assert scoped == [post.id]
    assert [item.id async for item in discover(job, guild, member, scope=thread)] == [thread.id]


async def test_history_filters_count_skips_and_reports_partial_permission_failures(tmp_path, guild):
    member = make_member(guild)
    bot_member = make_member(guild, 789, bot=True)
    channel = text_channel(guild)
    job = basic_job(tmp_path, guild, member)
    job.after = parse_date("2026-09-01")
    job.include_bots = False
    channel.history.side_effect = lambda **kwargs: sequence(
        [chat_message(member, channel), chat_message(bot_member, channel, number=2)], forbidden()
    )
    await scan_channel(job, channel, guild, member)
    assert job.messages == 1 and not job.complete
    assert job.channels[0]["status"] == "partial: Forbidden"
    kwargs = channel.history.call_args.kwargs
    assert kwargs["limit"] is None and kwargs["oldest_first"] is True
    assert kwargs["after"].id == discord.utils.time_snowflake(job.after) - 1
    assert kwargs["before"].id == discord.utils.time_snowflake(job.before)
    channel.history.reset_mock()
    permissions = discord.Permissions.none()
    channel.permissions_for.return_value = permissions
    await scan_channel(job, channel, guild, member)
    channel.history.assert_not_called()
    assert job.channels[-1]["status"].startswith("skipped")


async def test_real_server_export_private_zip_text_status_and_clear(export_runtime):
    bot, cog, member, invoke = export_runtime
    channel = text_channel(member.guild)
    channel.history.side_effect = lambda **kwargs: sequence([chat_message(member, channel)])
    ctx = await invoke("!export server")
    assert not ctx.command_failed
    job = cog._jobs[member.guild.id]
    await asyncio.wait_for(job.task, 3)
    assert job.state == "ready" and job.messages == 1 and job.delivered == 1
    assert ctx.send.call_args.kwargs.get("file") is None
    upload = next(
        call.kwargs["file"] for call in member.send.call_args_list if "file" in call.kwargs
    )
    assert upload.filename == "server-chat-001.zip"
    assert job.volumes[0].stat().st_size < VOLUME_BYTES
    with zipfile.ZipFile(job.volumes[0]) as archive:
        assert b"hello" in archive.read("chat-0001.txt")
        assert json.loads(archive.read("index.json"))["complete"]
    ctx = await invoke("!export status")
    assert (
        not ctx.command_failed and "Messages: 1" in ctx.send.call_args.kwargs["embed"].description
    )
    ctx = await invoke("!export text 1")
    assert not ctx.command_failed
    assert member.send.call_args.kwargs["file"].filename == "chat-0001.txt"
    ctx = await invoke("!export clear")
    assert not ctx.command_failed and not job.root.exists() and not cog._jobs


async def test_prefix_and_slash_require_administrator_and_enforce_parent_disable(
    export_runtime, monkeypatch
):
    bot, cog, member, invoke = export_runtime
    ctx = await invoke_slash(
        bot,
        invoke,
        monkeypatch,
        "export server",
        after="2026-09-01",
        before="2026-09-02",
        bots=False,
        threads=False,
    )
    assert not ctx.command_failed
    assert ctx.defer.call_args.kwargs["ephemeral"]
    job = cog._jobs[member.guild.id]
    await asyncio.wait_for(job.task, 3)
    bot.owner_ids.discard(member.id)
    member.guild.channels[0].permissions_for.side_effect = lambda person: (
        discord.Permissions.all() if person is member.guild.me else discord.Permissions.none()
    )
    for value in ("!export server", "!export download", "!export text 1", "!export clear"):
        ctx = await invoke(value)
        assert ctx.command_failed
    ctx = await invoke_slash(
        bot, invoke, monkeypatch, "export server", after=None, before=None, bots=True, threads=True
    )
    assert ctx.command_failed
    bot.owner_ids.add(member.id)
    cog.export.disable_in(member.guild)
    ctx = await invoke_slash(bot, invoke, monkeypatch, "export download", part=0)
    assert ctx.command_failed


async def test_blocked_dms_missing_intent_and_invalid_dates_leave_no_export(export_runtime):
    bot, cog, member, invoke = export_runtime
    member.send.side_effect = forbidden()
    ctx = await invoke("!export server")
    assert ctx.command_failed and not cog._jobs and not list(cog._root.iterdir())
    member.send.side_effect = None
    bot._connection._intents.message_content = False
    ctx = await invoke("!export server")
    assert ctx.command_failed and not cog._jobs
    bot._connection._intents.message_content = True
    for text in ("!export server bad", "!export server 2026-09-02 2026-09-01"):
        ctx = await invoke(text)
        assert ctx.command_failed and not cog._jobs


async def test_cancellation_and_unload_stop_scan_and_delete_temporary_files(export_runtime):
    bot, cog, member, invoke = export_runtime
    channel = text_channel(member.guild)
    entered = asyncio.Event()

    async def hanging():
        yield chat_message(member, channel)
        entered.set()
        await asyncio.Event().wait()

    channel.history.side_effect = lambda **kwargs: hanging()
    await invoke("!export server")
    job = cog._jobs[member.guild.id]
    await asyncio.wait_for(entered.wait(), 2)
    ctx = await invoke("!export cancel")
    assert not ctx.command_failed and job.task.cancelled() and not job.root.exists()
    entered.clear()
    await invoke("!export server")
    job = cog._jobs[member.guild.id]
    await asyncio.wait_for(entered.wait(), 2)
    await bot.remove_cog("ExportPlus")
    assert job.task.cancelled() and cog._maintenance.cancelled() and not cog._root.exists()


async def test_retention_user_data_and_export_metadata_agree(export_runtime):
    bot, cog, member, invoke = export_runtime
    other = make_member(member.guild, 321)
    channel = text_channel(member.guild)
    channel.history.side_effect = lambda **kwargs: sequence(
        [
            chat_message(member, channel),
            chat_message(other, channel, content="someone else's text", number=2),
        ]
    )
    await invoke("!export server")
    job = cog._jobs[member.guild.id]
    await asyncio.wait_for(job.task, 3)
    data = await cog.red_get_data_for_user(user_id=member.id)
    rows = [
        json.loads(line) for line in data["retained-chat-messages.jsonl"].getvalue().splitlines()
    ]
    assert len(rows) == 1 and rows[0]["author"]["id"] == str(member.id)
    assert not await cog.red_get_data_for_user(user_id=1234)
    job.finished = time.time() - RETENTION - 1
    await cog._prune()
    assert not job.root.exists() and not cog._jobs
    info = json.loads(__import__("pathlib").Path("exportplus/info.json").read_text())
    assert info["end_user_data_statement"] == __red_end_user_data_statement__
