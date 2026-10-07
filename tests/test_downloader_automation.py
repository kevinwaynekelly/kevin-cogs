"""Private HTTP trigger, durable daily scheduling and checked slash synchronization."""

import asyncio
import hashlib
import json
from base64 import b64encode
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redbot.core import commands
from test_cog_hybrid import invoke_slash
from test_downloader_webhook import SECRET, hook
from test_management_plus import core_runtime, download_runtime, red_command_runtime
from test_native_audio import eventually

from downloaderplus import DownloaderPlus
from downloaderplus.constants import DAILY_DEFAULTS
from downloaderplus.daily import DailyUpdates, next_daily
from downloaderplus.slash_sync import sync_enabled
from downloaderplus.trigger_page import HEADERS, PAGE, SCRIPT, trigger_token, trigger_url

__all__ = ["core_runtime", "download_runtime", "red_command_runtime", "hook"]


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize(
    "now,clock,expected",
    [
        ("2026-10-07T08:00:00", "04:00", "2026-10-07T09:00:00"),
        ("2026-10-07T09:00:00", "04:00", "2026-10-08T09:00:00"),
        ("2026-03-08T07:00:00", "02:30", "2026-03-08T08:30:00"),
        ("2026-03-08T08:45:00", "02:30", "2026-03-09T07:30:00"),
        ("2026-11-01T05:30:00", "01:30", "2026-11-01T06:30:00"),
        ("2026-11-01T07:15:00", "01:30", "2026-11-02T07:30:00"),
    ],
)
def test_daily_calendar_and_dst(now, clock, expected):
    assert next_daily(stamp(now), clock, "America/Chicago") == stamp(expected)


@pytest.mark.parametrize("clock,zone", [("4:00", "UTC"), ("24:00", "UTC"), ("04:00", "bad/zone")])
def test_daily_rejects_invalid_schedules(clock, zone):
    with pytest.raises(ValueError):
        next_daily(0, clock, zone)


@pytest.fixture
async def daily_service():
    cog = DownloaderPlus(SimpleNamespace(get_cog=lambda name: None))
    now = stamp("2026-10-07T09:00:00")
    policy = {**DAILY_DEFAULTS, "enabled": True, "generation": "one", "next_run": now - 86400 * 3}
    await cog.config.daily.set(policy)
    update = AsyncMock(return_value=("complete", "Updated and synced.", None))
    service = DailyUpdates(cog.config, update, now=lambda: now, poll=0.01)
    try:
        yield service, cog.config, update, now
    finally:
        await service.close()


async def test_daily_claim_survives_restart_and_coalesces_missed_days(daily_service):
    service, config, update, now = daily_service
    entered = asyncio.Event()

    async def blocked(policy):
        assert (await config.daily())["next_run"] == now + 86400
        entered.set()
        await asyncio.Event().wait()

    update.side_effect = blocked
    await service.start()
    await asyncio.wait_for(entered.wait(), 2)
    await service.close()
    await service.start()
    await asyncio.sleep(0.04)
    update.assert_awaited_once()


async def test_daily_failure_is_recorded_without_hot_retry(daily_service, caplog):
    service, config, update, now = daily_service
    update.side_effect = RuntimeError("private provider URL")
    await service.start()
    await eventually(lambda: update.await_count == 1)
    await asyncio.sleep(0.03)
    policy = await config.daily()
    assert policy["last_result"]["status"] == "failed"
    assert policy["next_run"] > now
    assert "private provider URL" not in caplog.text
    update.assert_awaited_once()


async def test_daily_self_reload_completes_without_second_update(daily_service):
    service, config, update, now = daily_service
    replacement = DailyUpdates(config, update, now=lambda: now, poll=0.01)

    async def reload():
        assert (await config.daily())["last_result"]["status"] == "reloading"
        await service.close()
        await replacement.start()

    update.return_value = ("complete", "Updated and synced.", reload)
    try:
        await service.start()
        await eventually(lambda: replacement.worker is not None)
        await asyncio.sleep(0.03)
        assert (await config.daily())["last_result"]["status"] == "complete"
        update.assert_awaited_once()
    finally:
        await replacement.close()


@pytest.mark.parametrize("change", ["disable", "replace"])
async def test_daily_old_completion_cannot_overwrite_reconfiguration(daily_service, change):
    service, config, update, now = daily_service

    async def finish():
        async with config.daily() as policy:
            if change == "disable":
                policy["enabled"] = False
            else:
                policy["generation"] = "replacement"
            policy["last_result"] = {"status": "replacement"}

    update.return_value = ("complete", "Old result", finish)
    await service.start()
    await eventually(lambda: update.await_count == 1)
    await asyncio.sleep(0.03)
    assert (await config.daily())["last_result"] == {"status": "replacement"}


@pytest.mark.parametrize("slash", [False, True])
async def test_daily_controls_owner_checks_defaults_and_privacy(
    download_runtime, monkeypatch, slash
):
    bot, cog, source, repo, installed, member, invoke = download_runtime

    async def enable():
        return (
            await invoke_slash(
                bot, invoke, monkeypatch, "download daily enable", clock="", timezone=""
            )
            if slash
            else await invoke("!download daily enable")
        )

    assert (await enable()).command_failed
    bot.owner_ids.add(member.id)
    ctx = await enable()
    assert not ctx.command_failed, bot.on_command_error.call_args
    policy = await cog.config.daily()
    assert (
        policy["enabled"] and policy["time"] == "04:00" and policy["timezone"] == "America/Chicago"
    )
    assert policy["channel_id"] == ctx.channel.id and policy["owner_id"] == member.id
    assert cog._daily.worker is not None
    exported = await cog.red_get_data_for_user(user_id=member.id)
    data = json.loads(exported["downloaderplus.json"].getvalue())
    assert data["daily"]["owner_id"] == member.id
    assert "generation" not in data["daily"]
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=member.id)
    assert (await cog.config.daily()) == DAILY_DEFAULTS
    assert cog._daily.worker is None


async def test_daily_native_checks_and_owner_revocation(download_runtime, monkeypatch):
    bot, cog, source, repo, installed, member, invoke = download_runtime
    bot.owner_ids.add(member.id)
    ctx = await invoke("!download daily enable")
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: ctx.channel)
    logic = AsyncMock()
    monkeypatch.setattr(source, "_cog_update_logic", logic)
    policy = await cog.config.daily()
    cog.daily.disable_in(member.guild)
    with pytest.raises(commands.DisabledCommand):
        await cog._daily_update(policy)
    cog.daily.enable_in(member.guild)
    bot.owner_ids.remove(member.id)
    with pytest.raises(commands.CheckFailure):
        await cog._daily_update(policy)
    logic.assert_not_awaited()


async def test_private_page_get_head_and_auth_failures_never_update(hook):
    service, config, update, client, url = hook
    endpoint = url.replace("/github", "/update")
    for method in (client.get, client.head):
        async with method(endpoint) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
    for headers in ({}, {"Authorization": "Bearer " + SECRET}, {"Authorization": "Bearer invalid"}):
        async with client.post(endpoint, headers=headers) as response:
            assert response.status == 403
        async with client.get(endpoint + "/status", headers=headers) as response:
            assert response.status == 403
    async with client.post(endpoint + "?token=" + trigger_token(SECRET)) as response:
        assert response.status == 403
    assert not (await config.webhook())["pending"]
    update.assert_not_awaited()
    assert SECRET not in PAGE and trigger_token(SECRET) not in PAGE
    assert (
        b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
        in HEADERS["Content-Security-Policy"]
    )


async def test_private_trigger_and_status_coalesce_and_hide_secrets(hook):
    service, config, update, client, url = hook
    endpoint = url.replace("/github", "/update")
    auth = {"Authorization": "Bearer " + trigger_token(SECRET)}
    service.coalesce = 0.1
    for _ in range(5):
        async with client.post(endpoint, headers=auth) as response:
            assert response.status == 202
    await eventually(lambda: update.await_count == 1)
    async with client.get(endpoint + "/status", headers=auth) as response:
        data = await response.json()
        assert data["result"]["status"] == "complete"
        assert set(data["result"]) == {"status", "detail", "at"}
    assert (await config.webhook())["deliveries"] == []
    async with config.webhook() as policy:
        policy["secret"] = "rotated"
    async with client.post(endpoint, headers=auth) as response:
        assert response.status == 503
    update.assert_awaited_once()


@pytest.mark.parametrize("suffix,data", [("?anything=yes", None), ("", b"body")])
async def test_private_trigger_rejects_query_or_body(hook, suffix, data):
    _, _, update, client, url = hook
    async with client.post(
        url.replace("/github", "/update") + suffix,
        data=data,
        headers={"Authorization": "Bearer " + trigger_token(SECRET)},
    ) as response:
        assert response.status == 400
    update.assert_not_awaited()


@pytest.mark.parametrize(
    "base",
    [
        "ftp://host",
        "https://user:pass@host",
        "https://host/path",
        "https://host?x=y",
        "https://host/#token=x",
        "https://host:0",
        "https://host bad",
    ],
)
def test_private_link_rejects_unsafe_base(base):
    with pytest.raises(ValueError):
        trigger_url(base, 8766, SECRET)


async def test_private_link_is_dm_only_and_does_not_rotate(download_runtime):
    bot, cog, source, repo, installed, member, invoke = download_runtime
    bot.owner_ids.add(member.id)
    await invoke("!download webhook setup")
    before = await cog.config.webhook()
    ctx = await invoke("!download webhook link https://updates.example.com")
    assert not ctx.command_failed
    token = trigger_token(before["secret"])
    assert f"https://updates.example.com/update#token={token}" in member.send.call_args.args[0]
    assert token not in str(ctx.send.call_args)
    assert await cog.config.webhook() == before


async def test_updateall_sync_is_after_update_and_deduplicated(download_runtime, monkeypatch):
    bot, cog, source, repo, installed, member, invoke = download_runtime
    bot.owner_ids.add(member.id)
    calls = []

    async def update(*args, **kwargs):
        calls.append("updated_and_reloaded")

    async def sync():
        calls.append("synced")
        return []

    monkeypatch.setattr(source, "_cog_update_logic", update)
    bot.tree.sync.side_effect = sync
    assert not (await invoke("!updateall")).command_failed
    assert calls == ["updated_and_reloaded", "synced"]
    assert not (await invoke("!updateall")).command_failed
    assert calls == ["updated_and_reloaded", "synced", "updated_and_reloaded"]


async def test_sync_preserves_enabled_choices_and_checks_native_disable(download_runtime):
    bot, cog, source, repo, installed, member, invoke = download_runtime
    bot.owner_ids.add(member.id)
    ctx = await invoke("!download")
    await bot._config.enabled_slash_commands.set({"download": cog.qualified_name})
    before = await bot._config.enabled_slash_commands()
    await sync_enabled(bot, cog.config, ctx)
    assert await bot._config.enabled_slash_commands() == before
    assert {item.name for item in bot.tree.get_commands()} == {"download"}
    bot.get_command("slash sync").disable_in(member.guild)
    with pytest.raises(commands.DisabledCommand):
        await sync_enabled(bot, cog.config, ctx)
    bot.tree.sync.assert_awaited_once()


async def test_failed_sync_is_not_cached_and_respects_retry_budget(download_runtime, monkeypatch):
    bot, cog, source, repo, installed, member, invoke = download_runtime
    bot.owner_ids.add(member.id)
    ctx = await invoke("!download")
    bot.tree.sync.side_effect = RuntimeError("Discord unavailable")
    with pytest.raises(RuntimeError):
        await sync_enabled(bot, cog.config, ctx)
    assert not (await cog.config.slash_sync())["fingerprint"]
    sleep = AsyncMock()
    monkeypatch.setattr("downloaderplus.slash_sync.asyncio.sleep", sleep)
    bot.tree.sync.side_effect = None
    await sync_enabled(bot, cog.config, ctx)
    sleep.assert_awaited_once()
    assert 50 < sleep.call_args.args[0] <= 60
    assert (await cog.config.slash_sync())["fingerprint"]
