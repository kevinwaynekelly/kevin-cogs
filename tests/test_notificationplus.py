"""Real persistent alerts and Red commands; Unraid transport stays separate."""

import asyncio
import json
import logging
import os
import sys
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redbot.core import commands
from redbot.core._cli import parse_cli_flags
from redbot.core._events import init_events
from test_audio_hybrid import red_command_runtime as runtime_fixture
from test_cog_hybrid import invoke_slash

from notificationplus import NotificationPlus
from notificationplus.constants import MAX_EVENTS, MAX_PENDING, MAX_SEQUENCE
from notificationplus.detection import namespace, sanitized_error, source
from notificationplus.outbox import Outbox, fresh_state, safe_exception, validate

red_command_runtime = runtime_fixture


@pytest.fixture
async def notification_runtime(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "notificationplus.cog.cog_data_path", lambda cog: tmp_path / "NotificationPlus"
    )
    bot.get_cog = lambda name: None
    cog = NotificationPlus(bot)
    await cog.cog_load()
    try:
        yield cog
    finally:
        await cog.cog_unload()


@pytest.fixture
async def owner_runtime(red_command_runtime, tmp_path, monkeypatch):
    bot, audio, member, invoke = red_command_runtime
    init_events(bot, parse_cli_flags([]))
    monkeypatch.setattr(bot, "_delete_delay", AsyncMock())
    monkeypatch.setattr(
        "notificationplus.cog.cog_data_path", lambda cog: tmp_path / "NotificationPlus"
    )
    cog = NotificationPlus(bot)
    await bot.add_cog(cog)
    try:
        yield bot, cog, member, invoke
    finally:
        await bot.remove_cog("NotificationPlus")


async def enable(cog):
    await cog.config.enabled.set(True)
    assert await cog._outbox.configure(True)


async def settle(cog):
    # Allow already admitted thread-safe callbacks to reach the queue.
    await asyncio.sleep(0)
    await asyncio.wait_for(cog._pending.join(), 3)


def record(name="communityplus.cog", *, level=logging.ERROR, exc_info=None, **extra):
    item = logging.LogRecord(
        name, level, "secret/path", 13, "secret %s", ("token",), exc_info, "listener"
    )
    item.__dict__.update(extra)
    return item


async def test_disabled_default_requires_owner_enable_and_test_is_explicit(notification_runtime):
    cog = notification_runtime
    state = json.loads(cog._outbox.path.read_text())
    assert state["enabled"] is False and state["events"] == []
    assert not await cog.report("AudioPlus", "playback", error=RuntimeError("private"))
    with pytest.raises(commands.BadArgument, match="Enable notifications"):
        await cog.notifications_test.callback(cog, SimpleNamespace())
    await enable(cog)
    assert await cog.report("NotificationPlus", "owner test", kind="test")
    assert await cog.report("NotificationPlus", "owner test", kind="test")
    assert [item["seq"] for item in cog._outbox.state["events"]] == [1, 2]
    assert os.stat(cog._outbox.path).st_mode & 0o777 == 0o600


async def test_persistent_dedupe_reload_disable_clear_and_monotonic_sequences(notification_runtime):
    cog = notification_runtime
    await enable(cog)
    clock = [datetime(2026, 10, 6, 14, tzinfo=timezone.utc)]
    cog._outbox.clock = lambda: clock[0]
    assert await cog.report(
        "AudioPlus",
        "playback",
        guild_id=286656490947739651,
        error=RuntimeError("https://signed.invalid?token=private"),
        kind="playback",
    )
    producer = cog._outbox.state["producer"]
    restored = Outbox(cog._outbox.path, clock=cog._outbox.clock)
    await restored.load(True)
    assert not await restored.report(
        "AudioPlus",
        "playback",
        286656490947739651,
        error=RuntimeError("another secret"),
        kind="playback",
    )
    assert restored.state["producer"] == producer
    assert restored.state["events"][0]["guild_id"] == "286656490947739651"
    clock[0] += timedelta(minutes=10)
    assert await restored.report(
        "AudioPlus",
        "playback",
        286656490947739651,
        error=RuntimeError("another secret"),
        kind="playback",
    )
    assert await restored.configure(False)
    assert restored.state["events"] == [] and restored.state["sequence"] == 2
    assert not await restored.report("AudioPlus", "playback")
    await restored.load(False)
    assert await restored.configure(True)
    assert await restored.report("AudioPlus", "playback")
    assert restored.state["events"][0]["seq"] == 3
    assert restored.state["producer"] == producer


async def test_retention_maximum_and_sequence_overflow_produce_valid_schema(tmp_path):
    clock = [datetime(2026, 10, 6, 14, tzinfo=timezone.utc)]
    outbox = Outbox(tmp_path / "alerts" / "unraid.json", clock=lambda: clock[0])
    await outbox.load(True)
    for number in range(MAX_EVENTS + 5):
        assert await outbox.report("CommunityPlus", f"event {number}")
    assert len(outbox.state["events"]) == MAX_EVENTS
    assert outbox.state["events"][0]["seq"] == 6
    clock[0] += timedelta(days=7)
    assert (await outbox.snapshot())["events"] == []
    assert json.loads(outbox.path.read_text())["events"] == []
    old_producer = outbox.state["producer"]
    outbox.state["sequence"] = MAX_SEQUENCE
    assert await outbox.report("AudioPlus", "playback")
    assert outbox.state["sequence"] == 1 and outbox.state["producer"] != old_producer
    assert validate(outbox.state) == outbox.state


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("producer", "https://private"),
        ("sequence", 2**53),
        ("enabled", 1),
        ("events", {}),
        ("private", "token"),
    ],
)
async def test_corrupt_schema_replaced_without_forwarding_arbitrary_content(tmp_path, field, value):
    path = tmp_path / "unraid.json"
    state = fresh_state(True)
    previous = state["producer"]
    state[field] = value
    path.write_text(json.dumps(state))
    outbox = Outbox(path)
    await outbox.load(True)
    assert outbox.state["producer"] != previous
    assert outbox.state["events"] == []
    validate(json.loads(path.read_text()))


@pytest.mark.parametrize(
    "field,value",
    [
        ("cog", []),
        ("kind", {}),
        ("guild_id", 123),
        ("guild_id", "0"),
        ("guild_id", str(2**64)),
        ("cause", "https://private?token=secret"),
        ("stage", "line\nsecret"),
        ("at", "not a timestamp"),
        ("seq", True),
    ],
)
async def test_invalid_event_types_cannot_break_cog_loading(tmp_path, field, value):
    outbox = Outbox(tmp_path / "unraid.json")
    await outbox.load(True)
    await outbox.report("LogPlus", "delivery")
    state = deepcopy(outbox.state)
    state["events"][0][field] = value
    outbox.path.write_text(json.dumps(state))
    restored = Outbox(outbox.path)
    await restored.load(True)
    assert restored.state["events"] == []


async def test_alert_privacy_never_formats_exception_or_logs_and_only_accepts_fixed_causes(
    notification_runtime,
):
    cog = notification_runtime
    await enable(cog)

    class SecretError(Exception):
        status = 403
        errno = 13

        def __str__(self):
            raise AssertionError("Exception formatting is forbidden")

    error = SecretError("private member song credential token")
    assert safe_exception(error) == "SecretError (HTTP 403) (errno 13)"
    assert await cog.report(
        "ExportPlus", "delivery", guild_id=123, error=error, cause="private raw query or signed URL"
    )
    assert not await cog.report("UnknownCog", "failure", error=error)
    cog._handler.emit(record(notification_error="private\nURL", notification_stage="\nsecret"))
    cog._handler.emit(record(exc_info=(SecretError, error, None)))
    await settle(cog)
    payload = cog._outbox.path.read_text()
    assert all(secret not in payload for secret in ("member", "song", "credential", "token", "URL"))
    assert "SecretError (HTTP 403) (errno 13)" in payload
    assert "unspecified" in payload
    assert await cog.red_get_data_for_user(user_id=123) == {}
    before = payload
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=123)
    assert cog._outbox.path.read_text() == before


async def test_failed_enable_disable_roll_back_config_and_in_memory_state(
    notification_runtime, monkeypatch
):
    cog = notification_runtime
    cog.bot.is_owner.return_value = True
    ctx = SimpleNamespace(author=SimpleNamespace(id=888))

    def denied(state):
        raise PermissionError(13, "private filesystem path")

    original = cog._outbox._write
    monkeypatch.setattr(cog._outbox, "_write", denied)
    with pytest.raises(commands.CommandError, match="persistent notification"):
        await cog._configure(ctx, True)
    assert not await cog.config.enabled() and not cog._outbox.state["enabled"]
    monkeypatch.setattr(cog._outbox, "_write", original)
    await enable(cog)
    await cog.report("IntroPlus", "playback")
    previous = deepcopy(cog._outbox.state)
    monkeypatch.setattr(cog._outbox, "_write", denied)
    with pytest.raises(commands.CommandError):
        await cog._configure(ctx, False)
    assert await cog.config.enabled() and cog._outbox.state == previous
    assert cog._outbox.last_error == "PermissionError (errno 13)"
    assert json.loads(cog._outbox.path.read_text()) == previous


async def test_repeated_cancellation_keeps_lock_until_write_thread_finishes(tmp_path, monkeypatch):
    outbox = Outbox(tmp_path / "unraid.json")
    await outbox.load(True)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = outbox._write

    def blocked(state):
        started.set()
        assert release.wait(3)
        original(state)
        finished.set()

    monkeypatch.setattr(outbox, "_write", blocked)
    operation = asyncio.create_task(outbox.report("AudioPlus", "playback"))
    assert await asyncio.to_thread(started.wait, 2)
    operation.cancel()
    await asyncio.sleep(0)
    operation.cancel()
    await asyncio.sleep(0)
    assert not operation.done() and outbox.lock.locked() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert finished.is_set() and not outbox.lock.locked()
    assert json.loads(outbox.path.read_text())["events"][0]["seq"] == 1


async def test_logging_admission_bounded_before_thread_callbacks_and_unload_detaches(
    notification_runtime,
):
    cog = notification_runtime
    await enable(cog)
    handler = cog._handler

    def flood():
        for number in range(MAX_PENDING + 25):
            handler.emit(record(notification_stage=f"event {number}"))

    thread = threading.Thread(target=flood)
    thread.start()
    thread.join()
    assert handler._pending == MAX_PENDING and handler.dropped == 25
    await settle(cog)
    assert handler._pending == 0 and len(cog._outbox.state["events"]) == MAX_EVENTS
    await cog.cog_unload()
    assert handler not in logging.getLogger().handlers
    assert cog._worker.done()
    before = deepcopy(cog._outbox.state)
    handler.emit(record())
    assert not await cog.report("AudioPlus", "later")
    await asyncio.sleep(0)
    assert cog._outbox.state == before


async def test_direct_reports_have_nonwaiting_admission_limit(notification_runtime):
    cog = notification_runtime
    await enable(cog)
    await cog._outbox.lock.acquire()
    tasks = [
        asyncio.create_task(cog.report("IntroPlus", f"event {index}"))
        for index in range(MAX_PENDING + 9)
    ]
    await asyncio.sleep(0)
    assert cog._reporting == MAX_PENDING and cog._direct_dropped == 9
    assert sum(task.done() for task in tasks) == 9
    cog._outbox.lock.release()
    await asyncio.gather(*tasks)
    assert cog._reporting == 0


async def test_disabled_generation_never_replays_old_log_events_after_enable(notification_runtime):
    cog = notification_runtime
    await enable(cog)
    cog._handler.emit(record(notification_stage="old event"))
    # Equivalent to a completed owner disable/enable sequence before a thread
    # callback gets its first turn on the loop.
    await cog._outbox.configure(False)
    cog._generation += 1
    await cog._outbox.configure(True)
    cog._generation += 1
    await settle(cog)
    assert cog._outbox.state["events"] == []


def test_log_sources_whitelist_modules_tracebacks_and_exclude_self():
    assert namespace("red.kevin_cogs.coreplus.help") == "CorePlus"
    assert namespace("red.kevin.dashboardplus") == "DashboardPlus"
    assert namespace("notificationplus.outbox") is None
    assert source(record("discord.gateway")) is None
    context = {"__name__": "introplus.cog"}
    exec("def fail():\n    raise RuntimeError('secret URL')", context)
    try:
        context["fail"]()
    except RuntimeError:
        item = record("red", exc_info=sys.exc_info())
    assert source(item) == "IntroPlus"
    detached = sanitized_error(item)
    assert type(detached).__name__ == "RuntimeError" and detached.__traceback__ is None
    assert source(record("notificationplus.cog", exc_info=item.exc_info)) is None


async def test_command_errors_ignore_expected_errors_and_preserve_only_safe_failure_metadata(
    notification_runtime,
):
    cog = notification_runtime
    await enable(cog)
    ctx = SimpleNamespace(
        command=SimpleNamespace(
            cog=SimpleNamespace(qualified_name="CommunityPlus"),
            qualified_name="community event create",
        ),
        guild=SimpleNamespace(id=123),
    )
    for error in (
        commands.BadArgument("private"),
        commands.CheckFailure("private"),
        commands.CommandError("ordinary handled error"),
        commands.CommandNotFound(),
    ):
        await cog.on_command_error(ctx, error)
    assert cog._outbox.state["events"] == []
    await cog.on_command_error(ctx, commands.CommandInvokeError(RuntimeError("signed URL secret")))
    assert cog._outbox.state["events"][0]["cause"] == "RuntimeError"
    assert cog._outbox.state["events"][0]["kind"] == "command"
    ctx.command.cog.qualified_name = "Downloader"
    await cog.on_command_error(ctx, commands.CommandInvokeError(OSError("private")))
    assert len(cog._outbox.state["events"]) == 1
    cog.bot.get_cog = lambda name: object() if name == "DownloaderPlus" else None
    await cog.on_command_error(ctx, commands.CommandInvokeError(OSError("private")))
    assert cog._outbox.state["events"][-1]["cog"] == "DownloaderPlus"


async def test_owner_prefix_and_slash_controls_use_real_red_checks(owner_runtime, monkeypatch):
    bot, cog, member, invoke = owner_runtime
    ctx = await invoke("!notifications enable")
    assert ctx.command_failed and not await cog.config.enabled()
    ctx = await invoke_slash(bot, invoke, monkeypatch, "notifications enable")
    assert ctx.command_failed and not await cog.config.enabled()
    bot.owner_ids.add(member.id)
    ctx = await invoke("!notifications enable")
    assert not ctx.command_failed and await cog.config.enabled()
    ctx = await invoke_slash(bot, invoke, monkeypatch, "notifications status")
    assert not ctx.command_failed
    ctx.defer.assert_awaited_once_with(ephemeral=True)
    assert "does not confirm" in ctx.send.await_args.kwargs["embed"].description
    for path in ("!notifications test", "!notifications test"):
        ctx = await invoke(path)
        assert not ctx.command_failed
    assert [event["seq"] for event in cog._outbox.state["events"]] == [1, 2]
    ctx = await invoke_slash(bot, invoke, monkeypatch, "notifications disable")
    assert not ctx.command_failed and not await cog.config.enabled()
    assert cog._outbox.state["events"] == []
    payload = cog.notifications.app_command.to_dict(bot.tree)
    assert {item["name"] for item in payload["options"]} == {"status", "enable", "disable", "test"}
