"""Actual persistent Red Config with mocked Discord delivery and update boundaries."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redbot.core import Config

from downloaderplus.constants import DISCORD_DEFAULTS
from downloaderplus.discord_trigger import MAX_REQUESTS, DiscordTrigger


def message(message_id=100, **values):
    return SimpleNamespace(
        **{
            "id": message_id,
            "webhook_id": 10,
            "guild": SimpleNamespace(id=20),
            "channel": SimpleNamespace(id=30),
            "author": SimpleNamespace(bot=True),
            "edited_at": None,
            "content": "updateall",
            **values,
        }
    )


@pytest.fixture
async def runtime():
    config = Config.get_conf(SimpleNamespace(), identifier=927304, force_registration=True)
    config.register_global(discord_trigger=deepcopy(DISCORD_DEFAULTS))
    policy = deepcopy(DISCORD_DEFAULTS)
    policy.update(
        enabled=True,
        webhook_id=10,
        guild_id=20,
        channel_id=30,
        owner_id=40,
        generation="first-generation",
    )
    await config.discord_trigger.set(policy)
    services = []

    async def create(*, update=None, **kwargs):
        update = update or AsyncMock(return_value=("complete", "Updated.", None))
        service = DiscordTrigger(config, update, coalesce=0.005, interval=0.02, **kwargs)
        services.append(service)
        await service.start()
        return service, update

    yield config, create
    for service in services:
        await service.close()


async def settled(config, status="complete"):
    for _ in range(500):
        policy = await config.discord_trigger()
        if policy["last_result"].get("status") == status:
            return policy
        await asyncio.sleep(0.002)
    raise AssertionError(f"Update never reached {status!r}.")


async def test_identity_content_and_new_message_only(runtime):
    config, create = runtime
    service, update = await create()
    for invalid in (
        message(webhook_id=None),
        message(webhook_id=11),
        message(guild=None),
        message(guild=SimpleNamespace(id=21)),
        message(channel=SimpleNamespace(id=31)),
        message(edited_at=object()),
        message(content="!updateall extra"),
        message(content="hello updateall"),
        message(content="/updateall"),
        message(content="!updateall\n!shutdown"),
        message(content=None),
        message(message_id=0),
    ):
        assert not await service.accept(invalid)
    update.assert_not_awaited()
    assert await service.accept(message(content="  !UpDaTeAlL\n"))
    await settled(config)
    update.assert_awaited_once()
    stored = await config.discord_trigger()
    assert stored["last_message_id"] == 100
    assert not stored["pending"]
    assert "content" not in stored


async def test_unrelated_messages_do_not_wait_for_config(runtime):
    config, create = runtime
    service, _ = await create()
    async with config.discord_trigger():
        assert not await asyncio.wait_for(service.accept(message(webhook_id=999)), 0.1)
        assert not await asyncio.wait_for(service.accept(message(content="hello")), 0.1)


async def test_deduplicates_out_of_order_messages_across_restart(runtime):
    config, create = runtime
    service, update = await create()
    assert await service.accept(message(200))
    await settled(config)
    assert not await service.accept(message(200))
    assert not await service.accept(message(199))
    await service.close()
    replacement, replacement_update = await create()
    assert not await replacement.accept(message(199))
    assert not await replacement.accept(message(200))
    assert await replacement.accept(message(201))
    await asyncio.wait_for(replacement._event.wait(), 0.2)
    await asyncio.sleep(0.06)
    update.assert_awaited_once()
    replacement_update.assert_awaited_once()


async def test_admission_is_bounded_while_storage_is_blocked(runtime):
    config, create = runtime
    ready = asyncio.Event()
    service, update = await create(ready=ready.wait)
    async with config.discord_trigger():
        tasks = [asyncio.create_task(service.accept(message(index))) for index in range(1, 301)]
        await asyncio.sleep(0.02)
        assert len(service._requests) == MAX_REQUESTS
        assert sum(not task.done() for task in tasks) == MAX_REQUESTS
    accepted = await asyncio.gather(*tasks)
    assert sum(accepted) == MAX_REQUESTS
    assert not service._requests
    assert (await config.discord_trigger())["pending"]
    update.assert_not_awaited()
    ready.set()
    await settled(config)
    update.assert_awaited_once()


async def test_bursts_produce_one_active_and_one_followup(runtime):
    config, create = runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def run(policy):
        entered.set()
        await release.wait()
        return "complete", "Updated.", None

    service, update = await create(update=AsyncMock(side_effect=run))
    assert await service.accept(message())
    await asyncio.wait_for(entered.wait(), 0.5)
    for message_id in range(101, 201):
        assert await service.accept(message(message_id))
    update.assert_awaited_once()
    assert (await config.discord_trigger())["pending"]
    release.set()
    await asyncio.sleep(0.1)
    assert update.await_count == 2
    assert not (await config.discord_trigger())["pending"]


@pytest.mark.parametrize(
    "changes", [{"enabled": False}, {"generation": "replacement"}, {"channel_id": 31}]
)
async def test_changed_policy_rejects_cached_authorization(runtime, changes):
    config, create = runtime
    service, update = await create()
    async with config.discord_trigger() as policy:
        policy.update(changes)
    assert not await service.accept(message())
    update.assert_not_awaited()


async def test_disabled_start_and_idempotent_start(runtime):
    config, create = runtime
    async with config.discord_trigger() as policy:
        policy["enabled"] = False
    service, _ = await create()
    assert service.closed and service.worker is None
    assert not await service.accept(message())
    async with config.discord_trigger() as policy:
        policy["enabled"] = True
    await service.start()
    worker = service.worker
    await service.start()
    assert service.worker is worker


async def test_pending_survives_unload_and_waits_for_readiness(runtime):
    config, create = runtime
    ready = asyncio.Event()
    service, update = await create(ready=ready.wait)
    assert await service.accept(message())
    await service.close()
    assert (await config.discord_trigger())["pending"]
    replacement, replacement_update = await create(ready=ready.wait)
    await asyncio.sleep(0.03)
    update.assert_not_awaited()
    replacement_update.assert_not_awaited()
    ready.set()
    await settled(config)
    replacement_update.assert_awaited_once()
    assert not replacement.closed


async def test_interrupted_running_update_recovers_with_persisted_cooldown(runtime):
    config, create = runtime
    entered = asyncio.Event()

    async def run(policy):
        entered.set()
        await asyncio.Event().wait()

    service, update = await create(update=AsyncMock(side_effect=run))
    service.interval = 0.15
    assert await service.accept(message())
    await asyncio.wait_for(entered.wait(), 0.5)
    started = (await config.discord_trigger())["last_result"]["started_at"]
    await service.close()
    replacement, replacement_update = await create(ready=AsyncMock())
    replacement.interval = 0.15
    await settled(config)
    replacement_update.assert_awaited_once()
    new_started = (await config.discord_trigger())["last_result"]["started_at"]
    assert new_started - started >= 0.14
    update.assert_awaited_once()


async def test_self_reload_finishes_without_repeating_the_running_update(runtime):
    config, create = runtime
    replacements = []

    async def finish():
        assert (await config.discord_trigger())["last_result"]["status"] == "reloading"
        await service.close()
        replacements.append(await create())

    service, _ = await create(update=AsyncMock(return_value=("complete", "Synced.", finish)))
    old_worker = service.worker
    assert await service.accept(message())
    result = await settled(config)
    await asyncio.wait_for(old_worker, 0.5)
    assert result["last_result"]["detail"] == "Synced."
    assert service.closed
    assert len(replacements) == 1
    replacements[0][1].assert_not_awaited()


async def test_self_reload_cannot_overwrite_a_newer_run(runtime):
    config, create = runtime

    async def finish():
        assert await service.accept(message(101))
        await service.close()
        await create(update=AsyncMock(return_value=("complete", "Newer run.", None)))
        await settled(config)

    service, _ = await create(update=AsyncMock(return_value=("complete", "Old run.", finish)))
    old_worker = service.worker
    assert await service.accept(message())
    await asyncio.wait_for(old_worker, 0.5)
    result = await config.discord_trigger()
    assert result["last_result"]["detail"] == "Newer run."
    assert result["last_message_id"] == 101


async def test_self_reload_cannot_overwrite_reconfigured_policy(runtime):
    config, create = runtime

    async def finish():
        await service.close()
        async with config.discord_trigger() as policy:
            policy.update(generation="replacement", last_result={})

    service, _ = await create(update=AsyncMock(return_value=("complete", "Old run.", finish)))
    old_worker = service.worker
    assert await service.accept(message())
    await asyncio.wait_for(old_worker, 0.5)
    assert (await config.discord_trigger())["last_result"] == {}


async def test_close_cancels_waiting_receipts_and_worker(runtime):
    config, create = runtime
    service, _ = await create()
    worker = service.worker
    async with config.discord_trigger():
        receipt = asyncio.create_task(service.accept(message()))
        await asyncio.sleep(0.01)
        await service.close()
    assert receipt.cancelled() and worker.cancelled()
    assert not service._requests
    assert not await service.accept(message())


@pytest.mark.parametrize("during_finish", [False, True])
async def test_failures_report_safe_types_without_exception_secrets(runtime, caplog, during_finish):
    config, create = runtime
    failure = RuntimeError("private URL or request content must not be logged")
    update = (
        AsyncMock(return_value=("complete", "Updated.", AsyncMock(side_effect=failure)))
        if during_finish
        else AsyncMock(side_effect=failure)
    )
    service, _ = await create(update=update)
    assert await service.accept(message())
    result = await settled(config, "failed")
    records = [
        record for record in caplog.records if record.name == "downloaderplus.discord_trigger"
    ]
    assert len(records) == 1
    assert records[0].notification_error == "RuntimeError"
    assert records[0].notification_stage == (
        "Discord automatic reload" if during_finish else "Discord update"
    )
    assert str(failure) not in caplog.text
    assert str(failure) not in str(result)


async def test_ready_failure_is_typed_and_keeps_pending_for_restart(runtime, caplog):
    config, create = runtime
    async with config.discord_trigger() as policy:
        policy["pending"] = True
    service, update = await create(ready=AsyncMock(side_effect=RuntimeError("secret")))
    await asyncio.wait_for(service.worker, 0.5)
    update.assert_not_awaited()
    assert (await config.discord_trigger())["pending"]
    record = caplog.records[-1]
    assert record.notification_error == "RuntimeError"
    assert record.notification_stage == "Discord trigger readiness"
    assert "secret" not in caplog.text


async def test_receipt_storage_failure_does_not_advance_replay_state(runtime, monkeypatch, caplog):
    config, create = runtime
    service, update = await create()
    original_get = config._driver.get
    monkeypatch.setattr(config._driver, "get", AsyncMock(side_effect=OSError("private path")))
    assert not await service.accept(message())
    assert not service._requests
    update.assert_not_awaited()
    record = caplog.records[-1]
    assert record.notification_error == "OSError"
    assert record.notification_stage == "Discord trigger receipt"
    assert "private path" not in caplog.text
    monkeypatch.setattr(config._driver, "get", original_get)
    assert (await config.discord_trigger())["last_message_id"] == 0
    assert await service.accept(message())
    await settled(config)
    update.assert_awaited_once()
