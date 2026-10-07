"""Real HTTP signatures/lifecycle and native Red update parsing; no live git installs."""

import asyncio
import hashlib
import hmac
import json
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest
from conftest import make_member, make_message
from redbot.core import commands
from test_management_plus import core_runtime, download_runtime, red_command_runtime
from test_native_audio import eventually

from downloaderplus import DownloaderPlus
from downloaderplus.cog import WebhookContext
from downloaderplus.constants import WEBHOOK_DEFAULTS
from downloaderplus.webhook import MAX_BODY, GitHubWebhook, matches_push, repository_name

__all__ = ["core_runtime", "download_runtime", "red_command_runtime"]
SECRET = "a" * 64


def push(*, name="kevinwaynekelly/kevin-cogs", branch="main", after="abc"):
    return {"repository": {"full_name": name}, "ref": "refs/heads/" + branch, "after": after}


def headers(body, delivery="delivery-1", event="push", secret=SECRET):
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": "sha256="
        + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
    }


@pytest.fixture
async def hook(unused_tcp_port):
    repos = [
        SimpleNamespace(url="https://github.com/kevinwaynekelly/kevin-cogs.git", branch="main")
    ]
    cog = DownloaderPlus(SimpleNamespace(get_cog=lambda name: None))
    policy = deepcopy(WEBHOOK_DEFAULTS)
    policy.update(enabled=True, secret=SECRET, port=unused_tcp_port)
    await cog.config.webhook.set(policy)
    update = AsyncMock(return_value=("complete", "Updated unpinned packages.", None))
    service = GitHubWebhook(cog.config, lambda: repos, update, coalesce=0.01, interval=0.02)
    await service.start()
    async with aiohttp.ClientSession() as client:
        yield service, cog.config, update, client, f"http://127.0.0.1:{unused_tcp_port}/github"
    await service.close()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/Owner/Repo.git", "owner/repo"),
        ("git@github.com:Owner/Repo.git", "owner/repo"),
        ("ssh://git@github.com/Owner/Repo.git", "owner/repo"),
        ("https://github.com.evil.invalid/owner/repo", None),
        ("https://github.com/owner/repo/extra", None),
        ("https://github.com/owner/repo?token=private", None),
        ("https://owner:private@github.com/owner/repo", None),
        ("file:///owner/repo", None),
    ],
)
def test_installed_github_remote_allowlist(url, expected):
    assert repository_name(url) == expected


def test_push_only_matches_installed_tracked_branch():
    repos = [SimpleNamespace(url="git@github.com:KevinWayneKelly/kevin-cogs.git", branch="main")]
    assert matches_push(push(), repos)
    assert not matches_push(push(branch="unreviewed"), repos)
    assert not matches_push(push(name="elsewhere/kevin-cogs"), repos)
    assert not matches_push({**push(), "deleted": True}, repos)
    assert not matches_push({**push(), "ref": "refs/tags/main"}, repos)
    assert not matches_push({"repository": None}, repos)


async def test_real_signed_push_and_ping(hook):
    service, config, update, client, url = hook
    body = json.dumps({"zen": "ping"}).encode()
    async with client.post(url, data=body, headers=headers(body, event="ping")) as response:
        assert response.status == 200
        assert (await response.json())["status"] == "ready"
    update.assert_not_awaited()
    body = json.dumps(push()).encode()
    async with client.post(url, data=body, headers=headers(body)) as response:
        assert response.status == 202
        assert (await response.json())["status"] == "queued"
    await eventually(lambda: update.await_count == 1)
    await eventually(lambda: not service._event.is_set())
    assert (await config.webhook())["last_result"]["status"] == "complete"
    assert not (await config.webhook())["pending"]


async def test_tampered_unsigned_noninstalled_and_bad_json_are_not_updates(hook):
    _, _, update, client, url = hook
    body = json.dumps(push()).encode()
    for data, request_headers, expected in (
        (body, {}, 403),
        (body + b" ", headers(body), 403),
        (b"{", headers(b"{"), 400),
        (body, {**headers(body), "X-GitHub-Delivery": ""}, 400),
        (json.dumps(push(branch="dev")).encode(), None, 202),
        (json.dumps(push(name="outside/uninstalled")).encode(), None, 202),
        (body, headers(body, event="issues"), 202),
    ):
        async with client.post(
            url, data=data, headers=headers(data) if request_headers is None else request_headers
        ) as response:
            assert response.status == expected
    update.assert_not_awaited()


async def test_oversized_body_refused_without_update(hook):
    _, _, update, client, url = hook
    body = b"x" * (MAX_BODY + 1)
    async with client.post(url, data=body, headers=headers(body)) as response:
        assert response.status == 413
    update.assert_not_awaited()


async def test_replay_blocks_changed_unsigned_delivery_header_and_survives_restart(hook):
    service, config, update, client, url = hook
    body = json.dumps(push()).encode()
    async with client.post(url, data=body, headers=headers(body)) as response:
        assert response.status == 202
    await eventually(lambda: update.await_count == 1)
    await service.close()
    await service.start()
    for delivery in ("delivery-1", "replacement-id"):
        async with client.post(url, data=body, headers=headers(body, delivery)) as response:
            assert (await response.json())["status"] == "duplicate"
    assert update.await_count == 1
    assert len((await config.webhook())["deliveries"]) == 1


async def test_push_burst_queues_only_one_followup_during_active_update(hook):
    service, config, update, client, url = hook
    entered, release = asyncio.Event(), asyncio.Event()

    async def run(policy):
        entered.set()
        await release.wait()
        return "complete", "Updated.", None

    update.side_effect = run
    body = json.dumps(push()).encode()
    async with client.post(url, data=body, headers=headers(body)) as response:
        assert response.status == 202
    await entered.wait()
    for index in range(20):
        body = json.dumps(push(after=f"commit-{index}")).encode()
        async with client.post(
            url, data=body, headers=headers(body, f"delivery-{index + 2}")
        ) as response:
            assert response.status == 202
    assert update.await_count == 1
    assert (await config.webhook())["pending"]
    release.set()
    await eventually(lambda: update.await_count == 2)
    await asyncio.sleep(0.05)
    assert update.await_count == 2
    assert (await config.webhook())["last_result"]["status"] == "complete"


async def test_disable_cancels_work_without_writing_completion(hook):
    service, config, update, client, url = hook
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def run(policy):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    update.side_effect = run
    body = json.dumps(push()).encode()
    async with client.post(url, data=body, headers=headers(body)) as response:
        assert response.status == 202
    await entered.wait()
    async with config.webhook() as policy:
        policy.update(enabled=False, pending=False)
    await service.close()
    assert cancelled.is_set()
    assert service.runner is None
    assert (await config.webhook())["last_result"]["status"] == "running"


async def test_self_reload_closes_listener_and_current_worker_finishes(hook):
    service, config, update, client, url = hook
    completed = asyncio.Event()

    async def reload():
        assert (await config.webhook())["last_result"]["status"] == "reloading"
        await service.close()
        completed.set()

    update.return_value = "complete", "Updated.", reload
    body = json.dumps(push()).encode()
    async with client.post(url, data=body, headers=headers(body)) as response:
        assert response.status == 202
    await completed.wait()
    assert service.closed and service.runner is None
    await eventually(lambda: service.worker is None)
    assert (await config.webhook())["last_result"]["status"] == "complete"


async def test_previous_interrupted_update_retries_after_load(hook):
    service, config, update, _, _ = hook
    await service.close()
    async with config.webhook() as policy:
        policy["last_result"] = {"status": "running", "detail": "Interrupted.", "at": 1}
    await service.start()
    await eventually(lambda: update.await_count == 1)
    assert (await config.webhook())["last_result"]["status"] == "complete"


async def test_failed_update_safe_status_log_and_later_events_continue(hook, caplog):
    service, config, update, client, url = hook
    update.side_effect = RuntimeError("mock native failure")
    body = json.dumps(push()).encode()
    async with client.post(url, data=body, headers=headers(body)) as response:
        assert response.status == 202
    await eventually(lambda: update.await_count == 1)
    await asyncio.sleep(0.02)
    assert (await config.webhook())["last_result"]["status"] == "failed"
    assert "Automatic repository/cog update failed" in caplog.text
    update.side_effect = None
    body = json.dumps(push(after="next")).encode()
    async with client.post(url, data=body, headers=headers(body, "next-delivery")) as response:
        assert response.status == 202
    await eventually(lambda: update.await_count == 2)
    assert (await config.webhook())["last_result"]["status"] == "complete"


async def test_claim_storage_failure_retries_accepted_push(hook, monkeypatch, caplog):
    service, config, update, client, url = hook
    original_set = config._driver.set
    failed = asyncio.Event()

    async def save(identifier, value=None):
        if isinstance(value, dict) and value.get("last_result", {}).get("status") == "running":
            if not failed.is_set():
                failed.set()
                raise OSError("private storage path")
        await original_set(identifier, value)

    monkeypatch.setattr(config._driver, "set", save)
    body = json.dumps(push()).encode()
    async with client.post(url, data=body, headers=headers(body)) as response:
        assert response.status == 202
    await asyncio.wait_for(failed.wait(), 1)
    await eventually(lambda: update.await_count == 1)
    assert (await config.webhook())["last_result"]["status"] == "complete"
    assert not service.worker.done()
    assert "private storage path" not in caplog.text


async def test_failed_result_storage_does_not_kill_worker(hook, monkeypatch, caplog):
    service, config, update, client, url = hook
    original_set = config._driver.set
    failed = asyncio.Event()

    async def save(identifier, value=None):
        if isinstance(value, dict) and value.get("last_result", {}).get("status") == "failed":
            failed.set()
            raise OSError("private storage path")
        await original_set(identifier, value)

    monkeypatch.setattr(config._driver, "set", save)
    update.side_effect = RuntimeError("private update error")
    body = json.dumps(push()).encode()
    async with client.post(url, data=body, headers=headers(body)) as response:
        assert response.status == 202
    await asyncio.wait_for(failed.wait(), 1)
    update.side_effect = None
    body = json.dumps(push(after="next")).encode()
    async with client.post(url, data=body, headers=headers(body, "next-delivery")) as response:
        assert response.status == 202
    await eventually(lambda: update.await_count == 2)
    assert (await config.webhook())["last_result"]["status"] == "complete"
    assert not service.worker.done()
    assert "private storage path" not in caplog.text
    assert "private update error" not in caplog.text


async def test_webhook_native_all_update_preserves_pins_dependencies_and_deferred_reload(
    download_runtime, monkeypatch
):
    bot, cog, source, repo, installed, member, invoke = download_runtime
    bot.owner_ids.add(member.id)
    context = await invoke("!download")
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: context.channel)
    monkeypatch.setattr(commands.Context, "typing", lambda self: nullcontext())
    source._repo_manager.update_repos = AsyncMock(return_value=((repo,), []))
    installed[1].pinned = True
    available = AsyncMock(return_value=((installed[0],), ()))
    monkeypatch.setattr(source, "_available_updates", available)
    install = AsyncMock(return_value=({"audioplus", "downloaderplus"}, "Cogs updated."))
    monkeypatch.setattr(source, "_update_cogs_and_libs", install)
    monkeypatch.setitem(bot._BotBase__extensions, "audioplus", SimpleNamespace())
    monkeypatch.setitem(bot._BotBase__extensions, "downloaderplus", SimpleNamespace())
    core_reload = AsyncMock(
        return_value={
            "loaded_packages": ["audioplus", "downloaderplus"],
            "failed_packages": [],
            "invalid_pkg_names": [],
            "notfound_packages": [],
            "failed_with_reason_packages": {},
            "repos_with_shared_libs": [],
        }
    )
    monkeypatch.setattr(bot.get_cog("Core"), "_reload", core_reload)
    policy = {**WEBHOOK_DEFAULTS, "owner_id": member.id, "channel_id": context.channel.id}
    status, detail, reload = await cog._webhook_update(policy)
    assert status == "complete"
    source._repo_manager.update_repos.assert_awaited_once_with()
    assert available.call_args.args[0] == {installed[0]}
    assert install.call_args.args[1:3] == ((installed[0],), ())
    assert isinstance(install.call_args.args[0], WebhookContext)
    message = install.call_args.args[0].message
    assert isinstance(message, discord.Message)
    assert await bot.ignored_channel_or_guild(message)
    assert await bot.message_eligible_as_command(message)
    await bot._config.guild(member.guild).delete_delay.set(0)
    await bot._delete_delay(install.call_args.args[0])
    core_reload.assert_not_awaited()
    await reload()
    core_reload.assert_awaited_once_with(("audioplus", "downloaderplus"))


async def test_webhook_obeys_native_disabling_and_owner_revocation(download_runtime, monkeypatch):
    bot, cog, source, repo, installed, member, invoke = download_runtime
    bot.owner_ids.add(member.id)
    context = await invoke("!download")
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: context.channel)
    policy = {**WEBHOOK_DEFAULTS, "owner_id": member.id, "channel_id": context.channel.id}
    bot.get_command("cog update").disable_in(member.guild)
    with pytest.raises(commands.DisabledCommand):
        await cog._webhook_update(policy)
    bot.get_command("cog update").enable_in(member.guild)
    bot.owner_ids.remove(member.id)
    with pytest.raises(commands.CheckFailure):
        await cog._webhook_update(policy)


async def test_webhook_setup_owner_private_secret_and_deletion(download_runtime):
    bot, cog, source, repo, installed, member, invoke = download_runtime
    denied = await invoke("!download webhook setup")
    assert denied.command_failed
    assert not (await cog.config.webhook())["secret"]
    bot.owner_ids.add(member.id)
    ctx = await invoke("!download webhook setup")
    assert not ctx.command_failed, bot.on_command_error.call_args
    policy = await cog.config.webhook()
    assert len(policy["secret"]) == 64 and not policy["enabled"]
    assert policy["secret"] in member.send.call_args.args[0]
    result = getattr(ctx, "_kevin_management_sender", ctx.send).call_args.kwargs["embed"]
    assert policy["secret"] not in str(result.to_dict())
    data = await cog.red_get_data_for_user(user_id=member.id)
    assert policy["secret"].encode() not in data["downloaderplus.json"].getvalue()
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=member.id + 1)
    assert (await cog.config.webhook())["secret"] == policy["secret"]
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=member.id)
    assert (await cog.config.webhook()) == WEBHOOK_DEFAULTS


async def test_real_native_failed_dependency_report_is_not_success(
    download_runtime, monkeypatch, caplog
):
    from redbot.core._global_checks import init_global_checks

    bot, cog, source, repo, installed, member, invoke = download_runtime
    bot.owner_ids.add(member.id)
    context = await invoke("!download")
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: context.channel)
    monkeypatch.setattr(commands.Context, "typing", lambda self: nullcontext())
    init_global_checks(bot)
    source._repo_manager.update_repos = AsyncMock(return_value=((repo,), []))
    monkeypatch.setattr(source, "_available_updates", AsyncMock(return_value=((installed[0],), ())))
    monkeypatch.setattr(source, "_install_requirements", AsyncMock(return_value={"PyNaCl"}))
    policy = {**WEBHOOK_DEFAULTS, "owner_id": member.id, "channel_id": context.channel.id}
    status, detail, reload = await cog._webhook_update(policy)
    assert status == "failed" and reload is not None
    assert "requirement" in context.channel.send.call_args.kwargs["embed"].description.lower()
    assert "repository or dependency failure" in caplog.text
    await reload()
    bot.tree.sync.assert_awaited_once()


@pytest.mark.parametrize("action", ["setup", "enable"])
async def test_privacy_erasure_waits_for_configuring_owner_and_blocks_old_writes(
    download_runtime, monkeypatch, action
):
    bot, cog, source, repo, installed, member, invoke = download_runtime
    bot.owner_ids.add(member.id)
    context = await invoke("!download")
    entered = asyncio.Event()

    async def pause(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    if action == "setup":
        monkeypatch.setattr(member, "send", pause)
        callback = cog.webhook_setup.callback
    else:
        policy = {
            **WEBHOOK_DEFAULTS,
            "secret": SECRET,
            "owner_id": member.id,
            "channel_id": context.channel.id,
        }
        await cog.config.webhook.set(policy)
        monkeypatch.setattr(cog, "_source", pause)
        callback = cog.webhook_enable.callback
    task = asyncio.create_task(callback(cog, context))
    await entered.wait()
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=member.id)
    assert task.cancelled()
    assert (await cog.config.webhook()) == WEBHOOK_DEFAULTS
    assert not cog._privacy.operations and not cog._privacy.deleting
    if action == "setup":
        monkeypatch.setattr(member, "send", AsyncMock())
        await callback(cog, context)
        assert (await cog.config.webhook())["owner_id"] == member.id


async def test_failed_reload_after_self_close_updates_durable_state(hook):
    service, config, update, client, url = hook
    failed = asyncio.Event()

    async def reload():
        await service.close()
        failed.set()
        raise RuntimeError("mock native reload failure")

    update.return_value = "complete", "Updated.", reload
    body = json.dumps(push()).encode()
    async with client.post(url, data=body, headers=headers(body)) as response:
        assert response.status == 202
    await failed.wait()
    await asyncio.sleep(0.02)
    assert (await config.webhook())["last_result"]["status"] == "failed"


async def test_real_core_self_reload_preserves_listener_and_shared_update_lock(
    download_runtime, monkeypatch, unused_tcp_port
):
    from redbot.core._global_checks import init_global_checks

    bot, cog, source, repo, installed, member, invoke = download_runtime
    bot.owner_ids.add(member.id)
    context = await invoke("!download")
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: context.channel)
    monkeypatch.setattr(commands.Context, "typing", lambda self: nullcontext())
    init_global_checks(bot)
    source._repo_manager.update_repos = AsyncMock(return_value=((repo,), []))
    available = AsyncMock(side_effect=[((installed[0],), ()), ((), ())])
    monkeypatch.setattr(source, "_available_updates", available)
    monkeypatch.setattr(
        source, "_update_cogs_and_libs", AsyncMock(return_value=({"downloaderplus"}, "Updated."))
    )
    monkeypatch.setitem(bot._BotBase__extensions, "downloaderplus", SimpleNamespace())
    policy = {
        **WEBHOOK_DEFAULTS,
        "enabled": True,
        "secret": SECRET,
        "port": unused_tcp_port,
        "owner_id": member.id,
        "channel_id": context.channel.id,
    }
    await cog.config.webhook.set(policy)
    cog._webhook.coalesce = 0.01
    cog._webhook.interval = 0.01
    await cog._webhook.start()
    replacement = None
    queued = asyncio.Event()
    url = f"http://127.0.0.1:{unused_tcp_port}/github"

    async def unload(packages):
        assert tuple(packages) == ("downloaderplus",)
        await bot.remove_cog("DownloaderPlus")

    async def load(packages):
        nonlocal replacement
        replacement = DownloaderPlus(bot)
        replacement._webhook.coalesce = 0.01
        replacement._webhook.interval = 0.01
        assert replacement._operation_lock is cog._operation_lock
        await bot.add_cog(replacement)
        body = json.dumps(push(after="next-commit")).encode()
        async with aiohttp.ClientSession() as client:
            async with client.post(url, data=body, headers=headers(body, "followup")) as response:
                assert response.status == 202
        await asyncio.sleep(0.04)
        # The replacement can queue its new push, but the old Core reload
        # still owns the shared lock until all package loading finishes.
        assert source._repo_manager.update_repos.await_count == 1
        queued.set()
        return {
            "loaded_packages": ["downloaderplus"],
            "failed_packages": [],
            "invalid_pkg_names": [],
            "notfound_packages": [],
            "failed_with_reason_packages": {},
            "repos_with_shared_libs": [],
        }

    monkeypatch.setattr(bot.get_cog("Core"), "_unload", unload)
    monkeypatch.setattr(bot.get_cog("Core"), "_load", load)
    body = json.dumps(push()).encode()
    async with aiohttp.ClientSession() as client:
        async with client.post(url, data=body, headers=headers(body)) as response:
            assert response.status == 202
    await asyncio.wait_for(queued.wait(), timeout=2)
    await eventually(lambda: source._repo_manager.update_repos.await_count == 2)
    assert cog._webhook.closed
    assert replacement._webhook.runner is not None
    await asyncio.sleep(0.02)
    assert (await replacement.config.webhook())["last_result"]["status"] == "complete"


async def test_other_owner_can_configure_only_after_previous_owner_erasure_finishes(
    download_runtime, monkeypatch, unused_tcp_port
):
    bot, cog, source, repo, installed, owner_a, invoke = download_runtime
    bot.owner_ids.add(owner_a.id)
    context_a = await invoke("!download")
    await cog.webhook_setup.callback(cog, context_a, port=unused_tcp_port, bind="127.0.0.1")
    owner_b = make_member(owner_a.guild, user_id=owner_a.id + 1, name="Other owner")
    bot.owner_ids.add(owner_b.id)
    message = make_message(owner_b, context_a.channel, content="!download")
    message._state = bot._connection
    context_b = await bot.get_context(message)
    closing, release = asyncio.Event(), asyncio.Event()
    real_close = cog._webhook.close

    async def delayed_close():
        if asyncio.current_task().get_name() == "DeleteOwnerA":
            closing.set()
            await release.wait()
        await real_close()

    monkeypatch.setattr(cog._webhook, "close", delayed_close)
    deleting = asyncio.create_task(
        cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=owner_a.id),
        name="DeleteOwnerA",
    )
    await closing.wait()

    async def configure_owner_b():
        await cog.webhook_setup.callback(cog, context_b, port=unused_tcp_port, bind="127.0.0.1")
        await cog.webhook_enable.callback(cog, context_b)

    configuring = asyncio.create_task(configure_owner_b())
    await asyncio.sleep(0.02)
    assert not configuring.done()
    owner_b.send.assert_not_awaited()
    assert (await cog.config.webhook())["owner_id"] is None
    release.set()
    await deleting
    await configuring
    policy = await cog.config.webhook()
    assert policy["owner_id"] == owner_b.id and policy["enabled"]
    assert cog._webhook.runner is not None
    assert not cog._privacy.operations and not cog._privacy.deleting


async def test_webhook_failure_logs_only_typed_metadata_not_provider_text(hook, caplog):
    service, config, update, client, url = hook
    update.side_effect = RuntimeError("secret=do-not-log provider/private-url")
    body = json.dumps(push()).encode()
    async with client.post(url, data=body, headers=headers(body)) as response:
        assert response.status == 202
    await eventually(lambda: update.await_count == 1)
    await asyncio.sleep(0.02)
    record = next(record for record in caplog.records if record.name == "downloaderplus.webhook")
    assert record.notification_error == "RuntimeError"
    assert record.notification_stage == "Repository update"
    assert record.exc_info is None
    assert "do-not-log" not in caplog.text and "private-url" not in caplog.text
