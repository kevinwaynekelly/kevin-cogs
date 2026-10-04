"""Resource budgets preserve messages and keep automatic work fair between servers."""

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import discord
import pytest
from conftest import make_channel, make_guild, make_member, make_message

from emojistealerplus import EmojiStealerPlus
from emojistealerplus.queue import MAX_GUILD_PENDING, MAX_PENDING, CaptureQueue
from owoplus import OwoPlus
from owoplus.repost import MAX_FILE_BYTES, MAX_MESSAGE_BYTES


def attachment(size=4, *, url="https://cdn.discordapp.com/attachments/123/456/file.txt"):
    return SimpleNamespace(
        size=size,
        url=url,
        filename="SPOILER_file.txt",
        description="Keep this description",
        is_spoiler=lambda: True,
        to_file=AsyncMock(side_effect=AssertionError("Unbounded attachment read")),
    )


def streamed_session(streams):
    managers, seen = [], []

    def get(url, **kwargs):
        stream = streams[len(managers)]

        async def chunks(size):
            async for data in stream():
                seen.append(len(data))
                yield data

        response = SimpleNamespace(
            status=200,
            content_length=None,
            content=SimpleNamespace(iter_chunked=chunks),
        )
        manager = Mock()
        manager.__aenter__ = AsyncMock(return_value=response)
        manager.__aexit__ = AsyncMock(return_value=False)
        managers.append(manager)
        return manager

    return SimpleNamespace(get=Mock(side_effect=get), close=AsyncMock()), managers, seen


def bytes_stream(data, chunk_size=65536):
    async def stream():
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    return stream


@pytest.mark.parametrize("sizes", [[MAX_FILE_BYTES + 1], [MAX_FILE_BYTES] * 3])
async def test_declared_attachment_budgets_reject_before_download_or_send(bot, guild, sizes):
    cog = OwoPlus(bot)
    message = make_message(
        make_member(guild), make_channel(guild), attachments=[attachment(size) for size in sizes]
    )
    cog._attachment_session = SimpleNamespace(get=Mock())
    cog._ensure_webhook = AsyncMock()
    assert not await cog._repost(message, "changed")
    cog._attachment_session.get.assert_not_called()
    cog._ensure_webhook.assert_not_awaited()
    message.delete.assert_not_awaited()
    assert not cog._repost_budget.tasks and not cog._repost_budget.guilds


async def test_streamed_file_limit_does_not_trust_metadata_and_closes_buffers(
    bot, guild, monkeypatch
):
    cog = OwoPlus(bot)
    buffers = []
    factory = io.BytesIO

    def buffer():
        result = factory()
        buffers.append(result)
        return result

    monkeypatch.setattr("owoplus.repost.io.BytesIO", buffer)
    cog._attachment_session, managers, seen = streamed_session(
        [bytes_stream(b"x" * (MAX_FILE_BYTES + 65536))]
    )
    item = attachment(1)
    message = make_message(make_member(guild), make_channel(guild), attachments=[item])
    cog._ensure_webhook = AsyncMock()
    assert not await cog._repost(message, "changed")
    assert sum(seen) == MAX_FILE_BYTES + 65536
    assert len(buffers) == 1 and buffers[0].closed
    managers[0].__aexit__.assert_awaited_once()
    item.to_file.assert_not_awaited()
    cog._ensure_webhook.assert_not_awaited()
    message.delete.assert_not_awaited()


async def test_streamed_total_budget_preserves_original_and_closes_prior_files(
    bot, guild, monkeypatch
):
    cog = OwoPlus(bot)
    buffers = []
    factory = io.BytesIO

    def buffer():
        result = factory()
        buffers.append(result)
        return result

    monkeypatch.setattr("owoplus.repost.io.BytesIO", buffer)
    body = b"x" * (6 * 1024 * 1024)
    cog._attachment_session, managers, seen = streamed_session([bytes_stream(body)] * 3)
    message = make_message(
        make_member(guild), make_channel(guild), attachments=[attachment(1) for _ in range(3)]
    )
    cog._ensure_webhook = AsyncMock()
    assert not await cog._repost(message, "changed")
    assert sum(seen) == MAX_MESSAGE_BYTES + 65536
    assert len(buffers) == 3 and all(value.closed for value in buffers)
    assert all(manager.__aexit__.await_count == 1 for manager in managers)
    cog._ensure_webhook.assert_not_awaited()
    message.delete.assert_not_awaited()


async def test_streamed_download_keeps_spoiler_description_and_disables_redirects(bot, guild):
    cog = OwoPlus(bot)
    cog._attachment_session, managers, _ = streamed_session([bytes_stream(b"test")])
    message = make_message(make_member(guild), make_channel(guild), attachments=[attachment()])
    posted = SimpleNamespace(delete=AsyncMock())
    hook = SimpleNamespace(send=AsyncMock(return_value=posted))
    cog._ensure_webhook = AsyncMock(return_value=hook)
    assert await cog._repost(message, "changed")
    file = hook.send.call_args.kwargs["files"][0]
    assert file.filename == "SPOILER_file.txt" and file.spoiler
    assert file.description == "Keep this description" and file.fp.closed
    assert cog._attachment_session.get.call_args.kwargs["allow_redirects"] is False
    assert cog._attachment_session.get.call_args.kwargs["timeout"].total == 15
    managers[0].__aexit__.assert_awaited_once()
    message.delete.assert_awaited_once()
    await cog.cog_unload()


@pytest.mark.parametrize("failure", ["http", "timeout", "network"])
async def test_attachment_response_failures_preserve_original_and_release_budget(
    bot, guild, monkeypatch, failure
):
    cog = OwoPlus(bot)
    buffers = []
    factory = io.BytesIO

    def buffer():
        result = factory()
        buffers.append(result)
        return result

    monkeypatch.setattr("owoplus.repost.io.BytesIO", buffer)
    manager = Mock()
    manager.__aenter__ = AsyncMock(return_value=SimpleNamespace(status=503))
    manager.__aexit__ = AsyncMock(return_value=False)
    get = Mock(return_value=manager)
    if failure == "timeout":
        manager.__aenter__.side_effect = asyncio.TimeoutError()
    elif failure == "network":
        get.side_effect = aiohttp.ClientConnectionError("Connection unavailable")
    cog._attachment_session = SimpleNamespace(get=get)
    message = make_message(make_member(guild), make_channel(guild), attachments=[attachment()])
    cog._ensure_webhook = AsyncMock()
    assert not await cog._repost(message, "changed")
    assert len(buffers) == 1 and buffers[0].closed
    assert not cog._repost_budget.tasks and not cog._repost_budget.guilds
    cog._ensure_webhook.assert_not_awaited()
    message.delete.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "http://10.10.1.200/file.txt",
        "https://cdn.discordapp.com.evil.invalid/attachments/1/2/file.txt",
        "https://media.discordapp.net:123/attachments/1/2/file.txt",
        "https://cdn.discordapp.com/not-attachments/file.txt",
        "https://secret@cdn.discordapp.com/attachments/1/2/file.txt",
    ],
)
async def test_reposts_only_download_discord_attachment_urls(bot, guild, url):
    cog = OwoPlus(bot)
    cog._attachment_session = SimpleNamespace(get=Mock())
    message = make_message(
        make_member(guild), make_channel(guild), attachments=[attachment(url=url)]
    )
    assert not await cog._repost(message, "changed")
    cog._attachment_session.get.assert_not_called()
    message.delete.assert_not_awaited()


async def test_unload_cancels_streams_closes_buffers_and_retains_original(bot, guild, monkeypatch):
    cog = OwoPlus(bot)
    buffers, started = [], asyncio.Event()
    factory = io.BytesIO

    def buffer():
        result = factory()
        buffers.append(result)
        return result

    async def blocked():
        yield b"first"
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("owoplus.repost.io.BytesIO", buffer)
    session, managers, _ = streamed_session([blocked])
    cog._attachment_session = session
    message = make_message(make_member(guild), make_channel(guild), attachments=[attachment(5)])
    task = asyncio.create_task(cog._repost(message, "changed"))
    await asyncio.wait_for(started.wait(), 1)
    await cog.cog_unload()
    assert task.cancelled() and buffers[0].closed
    managers[0].__aexit__.assert_awaited_once()
    session.close.assert_awaited_once()
    message.delete.assert_not_awaited()
    assert not cog._repost_budget.tasks and not cog._repost_budget.guilds


async def test_repost_budgets_drop_excess_without_waiters_and_allow_other_guild(bot, guild):
    cog = OwoPlus(bot)
    second = make_guild(789)
    bot.guilds.append(second)
    all_started, release = asyncio.Event(), asyncio.Event()
    started = []

    async def webhook(channel):
        started.append(channel.guild.id)
        if len(started) == 4:
            all_started.set()
        await release.wait()
        return None

    cog._ensure_webhook = webhook
    messages = [make_message(make_member(guild, 1000 + i), make_channel(guild)) for i in range(50)]
    messages += [
        make_message(make_member(second, 2000 + i), make_channel(second)) for i in range(50)
    ]
    tasks = [asyncio.create_task(cog._repost(message, "changed")) for message in messages]
    await asyncio.wait_for(all_started.wait(), 1)
    assert started.count(guild.id) == started.count(second.id) == 2
    assert len(cog._repost_budget.tasks) == 4 and sum(task.done() for task in tasks) == 96
    release.set()
    assert not any(await asyncio.gather(*tasks))
    assert not cog._repost_budget.tasks and not cog._repost_budget.guilds
    assert all(message.delete.await_count == 0 for message in messages)


async def test_automatic_reposts_reserve_before_slow_configuration_or_rendering(bot, guild):
    cog = OwoPlus(bot)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def settings(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
        await release.wait()
        return {"enabled": False}

    cog._settings = settings
    messages = [make_message(make_member(guild, 1000 + i), make_channel(guild)) for i in range(100)]
    tasks = [asyncio.create_task(cog.on_message(message)) for message in messages]
    await asyncio.wait_for(entered.wait(), 1)
    assert calls == 2 and sum(task.done() for task in tasks) == 98
    release.set()
    await asyncio.gather(*tasks)
    assert not cog._repost_budget.tasks


async def test_emoji_flood_is_capped_per_guild_and_other_server_gets_next_turn(bot, guild):
    cog = EmojiStealerPlus(bot)
    second = make_guild(789)
    bot.guilds.append(second)
    first_channel, second_channel = make_channel(guild), make_channel(second)
    # Park the worker so all arrivals are inspected before any upload completes.
    parked = asyncio.Event()
    cog._run = AsyncMock(side_effect=parked.wait)
    try:
        for index in range(200):
            emoji = discord.PartialEmoji(name="flood", id=111111111111111111 + index)
            await cog._queue_emoji(guild, first_channel, emoji)
        assert len(cog._pending) == cog._queue.qsize() == MAX_GUILD_PENDING
        other = discord.PartialEmoji(name="other", id=222222222222222222)
        await cog._queue_emoji(second, second_channel, other)
        assert cog._queue.qsize() == MAX_GUILD_PENDING + 1
        assert cog._queue.get_nowait()[0] == guild.id
        cog._queue.task_done()
        assert cog._queue.get_nowait()[0] == second.id
        cog._queue.task_done()
        assert cog._guild_pending[guild.id] == MAX_GUILD_PENDING
        assert cog._guild_pending[second.id] == 1
    finally:
        await cog.cog_unload()
    assert not cog._pending and not cog._guild_pending and cog._queue.empty()


async def test_inflight_emoji_admission_cannot_recreate_queue_after_unload(bot, guild):
    cog = EmojiStealerPlus(bot)
    started, release = asyncio.Event(), asyncio.Event()

    async def copied():
        started.set()
        await release.wait()
        return {}

    group = SimpleNamespace(
        capture=AsyncMock(return_value={"enabled": True, "channel": None}), copied=copied
    )
    cog.config = SimpleNamespace(guild=lambda value: group)
    cog._run = AsyncMock()
    task = asyncio.create_task(
        cog._queue_emoji(
            guild, make_channel(guild), discord.PartialEmoji(name="emoji", id=111111111111111111)
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    await cog.cog_unload()
    release.set()
    await task
    assert cog._queue.empty() and not cog._pending and not cog._guild_pending
    cog._run.assert_not_awaited()


async def test_fair_queue_bounds_global_work_and_retains_asyncio_task_accounting():
    queue = CaptureQueue(maxsize=MAX_PENDING)
    for guild_id in range(10):
        for item in range(10):
            queue.put_nowait((guild_id, item))
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait((999, 0))
    assert not queue._putters
    assert [queue.get_nowait()[0] for _ in range(20)] == list(range(10)) * 2
    for _ in range(20):
        queue.task_done()
    while not queue.empty():
        await queue.get()
        queue.task_done()
    await asyncio.wait_for(queue.join(), 1)
    assert queue.qsize() == 0
