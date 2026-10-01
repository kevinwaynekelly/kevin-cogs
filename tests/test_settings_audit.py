"""Task attribution and selected write interception with actual Red Config drivers."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_context, make_member, make_message
from test_cog_hybrid import invoke_slash
from test_settingshub import command_runtime as command_fixture
from test_settingshub import hub_runtime as hub_fixture
from test_settingshub import red_command_runtime as red_fixture

from audioplus.command_support import configuration_action
from settingshub import SettingsHub
from settingshub.audit import AUDIT_BYTES, Actor, changes, retained
from settingshub.cog import RestoreView

command_runtime = command_fixture
hub_runtime = hub_fixture
red_command_runtime = red_fixture


async def test_prefix_changes_record_actual_before_after_and_actor(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!audioset limits 600 3")
    assert not ctx.command_failed
    records = await hub._audit_records(member.guild.id)
    assert len(records) == 1
    record = records[0]
    assert record["actor"] == member.id and record["command"] == "audioset limits"
    assert {c["path"]: (c["before"], c["after"]) for c in record["changes"]} == {
        "music.max_seconds": (0, 600),
        "music.per_member": (0, 3),
    }
    await invoke("!audioset limits 600 3")
    assert len(await hub._audit_records(member.guild.id)) == 1
    ctx = await invoke("!settings history show " + record["id"])
    assert "Before: 0" in ctx.send.call_args.kwargs["embed"].description
    assert "After: 600" in ctx.send.call_args.kwargs["embed"].description


async def test_slash_theme_and_legacy_scalar_writes_are_tracked(hub_runtime, monkeypatch):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke_slash(bot, invoke, monkeypatch, "theme footer", text="Scarlet")
    assert not ctx.command_failed
    await invoke("!community welcome message Welcome aboard!")
    records = await hub._audit_records(member.guild.id)
    assert {r["cog"] for r in records} == {"SettingsHub", "CommunityPlus"}
    assert records[0]["changes"][0]["path"] == "theme.footer"
    assert records[1]["changes"][0]["after"] == "Welcome aboard!"


async def test_setup_component_attributes_the_clicking_member(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!audioset setup")
    view = ctx.send.call_args.kwargs["view"]
    selector = view.children[2]  # Vote skip toggle.
    selector._values = ["on"]
    message = make_message(member, ctx.channel)
    message._state = bot._connection
    interaction = SimpleNamespace(
        guild=ctx.guild,
        guild_id=ctx.guild.id,
        user=member,
        message=message,
        response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await selector.callback(interaction)
    record = (await hub._audit_records(member.guild.id))[0]
    assert record["actor"] == member.id and record["command"] == "audioset setup"
    assert record["changes"] == [{"path": "music.vote_skip", "before": False, "after": True}]


async def test_restore_button_writes_are_attributed_and_runtime_data_excluded(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    bundle = await hub._backup_bundle(ctx)
    bundle["cogs"] = {"AudioPlus": bundle["cogs"]["AudioPlus"]}
    bundle["cogs"]["AudioPlus"]["music"]["max_seconds"] = 700
    selected = await hub._validate_bundle(ctx, bundle)
    view = RestoreView(hub, ctx, bundle, selected)
    message = make_message(member, ctx.channel)
    message._state = bot._connection
    interaction = SimpleNamespace(
        guild=ctx.guild,
        guild_id=ctx.guild.id,
        user=member,
        message=message,
        response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await view.children[0].callback(interaction)
    records = await hub._audit_records(member.guild.id)
    assert len(records) == 1 and records[0]["command"] == "settings restore"
    assert records[0]["changes"] == [{"path": "music.max_seconds", "before": 0, "after": 700}]


async def test_concurrent_writers_keep_correct_actor_and_old_values(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    cog = bot.get_cog("AudioPlus")
    other = make_member(member.guild, 42)
    contexts = [make_context(member.guild, author=author) for author in (member, other)]
    for ctx in contexts:
        ctx.command = bot.get_command("audioset limits")

    async def write(ctx, value):
        async with configuration_action(cog, ctx):
            await asyncio.sleep(0)
            await cog.config.guild(ctx.guild).music.max_seconds.set(value)

    await asyncio.gather(write(contexts[0], 10), write(contexts[1], 20))
    records = await hub._audit_records(member.guild.id)
    assert [(r["actor"], r["changes"][0]["before"], r["changes"][0]["after"]) for r in records] == [
        (member.id, 0, 10),
        (42, 10, 20),
    ]


async def test_inherited_background_tasks_do_not_claim_command_actor(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    cog = bot.get_cog("AudioPlus")
    ctx = await invoke("!settings")
    ctx.command = bot.get_command("audioset autoplay")
    async with configuration_action(cog, ctx):
        task = asyncio.create_task(cog.config.guild(member.guild).music.autoplay.set(True))
        await task
    assert not await hub._audit_records(member.guild.id)


async def test_failed_writes_and_failed_checks_are_not_recorded(hub_runtime, monkeypatch):
    bot, hub, member, invoke = hub_runtime
    cog = bot.get_cog("AudioPlus")
    driver = cog.config._driver
    entry = hub._observed_drivers[driver]
    hub._unobserve_config(driver)
    original = entry[1]["set"][0]

    async def fail(identifier, *args, **kwargs):
        if identifier.identifiers == ("music", "max_seconds"):
            raise RuntimeError("storage unavailable")
        return await original(identifier, *args, **kwargs)

    monkeypatch.setattr(driver, "set", fail)
    ctx = await invoke("!settings")
    ctx.command = bot.get_command("audioset limits")
    async with configuration_action(cog, ctx):
        with pytest.raises(RuntimeError):
            await cog.config.guild(member.guild).music.max_seconds.set(12)
    assert not await hub._audit_records(member.guild.id)
    bot.owner_ids.discard(member.id)
    ctx = await invoke("!audioset limits 30 1")
    assert ctx.command_failed and not await hub._audit_records(member.guild.id)


async def test_global_secrets_personal_records_and_cursors_are_excluded(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    cog = bot.get_cog("AudioPlus")
    ctx = await invoke("!settings")
    ctx.command = bot.get_command("audioset limits")
    async with configuration_action(cog, ctx):
        await cog.config.password.set("do-not-retain-this")
        await cog.config.guild(member.guild).favorites.set({str(member.id): []})
        await cog.config.guild(member.guild).listening_history.set([])
        await hub.config.guild(member.guild).snapshots.last_at.set(100)
        community = bot.get_cog("CommunityPlus")
        await community.config.guild(member.guild).features.summary.last_week.set("cursor")
    assert not await hub._audit_records(member.guild.id)


async def test_paused_collection_read_visibility_and_data_deletion(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    await invoke("!audioset limits 600 3")
    record = (await hub._audit_records(member.guild.id))[0]
    bot.get_command("audioset setup").disable_in(member.guild)
    ctx = await invoke("!settings history")
    assert "No recorded" in ctx.send.call_args.kwargs["embed"].description
    exported = json.load((await hub.red_get_data_for_user(user_id=member.id))["settingshub.json"])
    assert exported[str(member.guild.id)][0]["id"] == record["id"]
    await hub.red_delete_data_for_user(requester="discord_deleted_user", user_id=member.id)
    assert not await hub._audit_records(member.guild.id)
    await hub.config.guild(member.guild).audit_policy.enabled.set(False)
    bot.get_command("audioset setup").enable_in(member.guild)
    await invoke("!audioset limits 700 3")
    assert not await hub._audit_records(member.guild.id)


async def test_unload_restores_driver_methods_and_reload_resumes_observation(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    audio = bot.get_cog("AudioPlus")
    await invoke("!audioset limits 600 3")
    await bot.remove_cog("SettingsHub")
    assert "set" not in audio.config._driver.__dict__
    assert not hub._observed_drivers
    await invoke("!audioset limits 800 3")
    new = SettingsHub(bot)
    await bot.add_cog(new)
    await invoke("!audioset limits 900 3")
    records = await new._audit_records(member.guild.id)
    assert records[-1]["changes"][0] == {"path": "music.max_seconds", "before": 800, "after": 900}


def test_audit_retention_and_values_are_bounded():
    now = time.time()
    records = [{"at": now, "data": "x" * 5000} for _ in range(250)]
    result = retained([{"at": now - 31 * 86400}, *records], 30, now)
    assert len(result) < 200 and len(json.dumps(result, ensure_ascii=False).encode()) <= AUDIT_BYTES
    delta = changes({"rules": "a" * 10000}, {"rules": "b" * 10000})
    assert delta[0]["before"]["truncated"] and delta[0]["after"]["truncated"]


async def test_bulk_write_audit_keeps_latest_entry_inside_budget(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    actor = Actor(member.guild.id, member.id, "settings restore", lambda: asyncio.current_task())
    delta = changes(
        {str(i): "a" * 9000 for i in range(500)}, {str(i): "b" * 9000 for i in range(500)}
    )
    await hub._append_audit(member.guild.id, actor, "SettingsHub", delta)
    records = await hub._audit_records(member.guild.id)
    assert len(records) == 1 and records[0]["omitted"] > 400
    assert len(json.dumps(records, ensure_ascii=False).encode()) <= AUDIT_BYTES


async def test_clear_records_return_to_merged_defaults(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    await invoke("!audioset limits 600 3")
    ctx = await invoke("!settings")
    ctx.command = bot.get_command("audioset limits")
    audio = bot.get_cog("AudioPlus")
    async with configuration_action(audio, ctx):
        await audio.config.guild(member.guild).music.max_seconds.clear()
    record = (await hub._audit_records(member.guild.id))[-1]
    assert record["changes"] == [{"path": "music.max_seconds", "before": 600, "after": 0}]


async def test_audit_storage_failure_preserves_successful_source_write(hub_runtime, monkeypatch):
    bot, hub, member, invoke = hub_runtime
    monkeypatch.setattr(
        hub, "_append_audit", AsyncMock(side_effect=RuntimeError("audit unavailable"))
    )
    ctx = await invoke("!audioset limits 600 3")
    assert not ctx.command_failed
    assert (await bot.get_cog("AudioPlus").config.guild(member.guild).music())["max_seconds"] == 600


async def test_prefix_and_slash_history_list_export_and_invalid_retention(hub_runtime, monkeypatch):
    bot, hub, member, invoke = hub_runtime
    await invoke("!theme footer Scarlet")
    ctx = await invoke_slash(bot, invoke, monkeypatch, "settings history list", page=1)
    assert (
        not ctx.command_failed and "theme.footer" in ctx.send.call_args.kwargs["embed"].description
    )
    ctx = await invoke("!settings history export")
    assert json.load(ctx.send.call_args.kwargs["file"].fp)[0]["actor"] == member.id
    before = await hub.config.guild(member.guild).audit_policy()
    ctx = await invoke("!settings history enabled True 91")
    assert ctx.command_failed and await hub.config.guild(member.guild).audit_policy() == before
