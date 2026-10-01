"""Opt-in collection, deduplication, retention, privacy, and safe exports."""

import asyncio
import json
import time

from conftest import forbidden, make_channel, make_context, make_member

from logplus import LogPlus
from logplus.history import MAX_BYTES, MAX_RECORDS, export_history, prune_history


async def configured(bot, guild):
    cog = LogPlus(bot)
    channel = make_channel(guild)
    await cog.config.guild(guild).log_channel.set(channel.id)
    return cog, channel


async def test_history_optin_persistence_and_retry_deduplication(bot, guild):
    cog, channel = await configured(bot, guild)
    member = make_member(guild)
    embed = await cog._E(
        guild, "Member joined", description=f"{member.mention} hello", etype="member_joined"
    )
    await cog._send(guild, embed)
    assert not await cog.config.guild(guild).history_records()
    await cog._set_history_policy(guild, "enabled", True)
    cog._start_retries = lambda gid: None
    channel.send.side_effect = forbidden()
    await cog._send(guild, embed)
    assert len(await cog.config.guild(guild).history_records()) == 1
    channel.send.side_effect = None
    await cog._deliver_log(guild, cog._retry_queues[guild.id][0])
    fresh = LogPlus(bot)
    assert (
        len(
            await fresh._history_query(guild, member_id=member.id, query="HELLO", category="member")
        )
        == 1
    )
    assert not await fresh._history_query(guild, member_id=42)
    assert not await fresh._history_query(guild, category="message")
    await cog._set_history_policy(guild, "enabled", False)
    await cog._send(guild, embed)
    assert len(await cog.config.guild(guild).history_records()) == 1


async def test_history_obeys_routes_event_switches_exemptions_and_disable(bot, guild):
    cog, channel = await configured(bot, guild)
    await cog._set_history_policy(guild, "enabled", True)
    embed = await cog._E(guild, "Deleted", description="message", etype="message_deleted")
    await cog.config.guild(guild).message.exempt_channels.set([channel.id])
    cog._settings_cache.clear()
    await cog._send(guild, embed, channel.id)
    assert not await cog.config.guild(guild).history_records()
    await cog.config.guild(guild).message.exempt_channels.set([])
    await cog.config.guild(guild).message.delete.set(False)
    cog._settings_cache.clear()
    await cog._send(guild, embed, channel.id)
    assert not await cog.config.guild(guild).history_records()
    await cog.config.guild(guild).message.delete.set(True)
    cog._settings_cache.clear()
    bot.cog_disabled_in_guild.return_value = True
    await cog._send(guild, embed, channel.id)
    assert not await cog.config.guild(guild).history_records()
    bot.cog_disabled_in_guild.return_value = False
    await cog.config.guild(guild).log_channel.set(None)
    cog._settings_cache.clear()
    await cog._send(guild, embed, channel.id)
    assert not await cog.config.guild(guild).history_records()


async def test_concurrent_writes_and_user_erasure(bot, guild):
    cog, channel = await configured(bot, guild)
    member = make_member(guild)
    await cog._set_history_policy(guild, "enabled", True)
    embed = await cog._E(guild, "Member", description=member.mention, etype="member_joined")
    await asyncio.gather(*(cog._send(guild, embed) for _ in range(20)))
    assert len(await cog._history_query(guild, member_id=member.id)) == 20
    data = await cog.red_get_data_for_user(user_id=member.id)
    assert len(json.load(data["logplus-history.json"])[str(guild.id)]) == 20
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    assert not await cog.config.guild(guild).history_records()


def test_pruning_enforces_age_count_and_byte_limits():
    now = time.time()
    row = {"time": now, "users": [], "title": "Event", "description": "x" * 2000, "fields": []}
    rows = [{**row, "time": now - 10 * 86400}, *[dict(row) for _ in range(2000)]]
    prune_history(rows, 7, now)
    assert 0 < len(rows) <= MAX_RECORDS
    assert sum(len(json.dumps(r, ensure_ascii=False).encode()) for r in rows) <= MAX_BYTES
    prune_history(rows, 1, now + 2 * 86400)
    assert not rows


async def test_inactive_history_cleanup_and_owned_maintenance(bot, guild):
    cog, channel = await configured(bot, guild)
    row = {
        "time": time.time() - 8 * 86400,
        "users": [],
        "title": "Expired",
        "description": "",
        "fields": [],
    }
    await cog.config.guild(guild).history_records.set([row])
    await cog._history_tick()
    assert not await cog.config.guild(guild).history_records()
    await cog.cog_load()
    task = cog._history_task
    await cog.cog_unload()
    assert task.done() and cog._history_task is None


def test_csv_neutralizes_formulas_and_json_keeps_values():
    row = {
        "time": 1,
        "event": "=FORMULA()",
        "category": "message",
        "source": 1,
        "users": [123],
        "title": " \t@SUM(1,2)",
        "description": "private, text\nnext line",
        "fields": [],
    }
    text = export_history([row], "csv").getvalue().decode("utf-8-sig")
    assert "'=FORMULA()" in text and "' \t@SUM" in text
    assert json.load(export_history([row], "json"))[0]["event"] == "=FORMULA()"


async def test_retention_changes_prune_now_and_routing_cache_excludes_history(bot, guild):
    cog, channel = await configured(bot, guild)
    old = {
        "time": time.time() - 2 * 86400,
        "users": [],
        "title": "Old",
        "description": "",
        "fields": [],
    }
    await cog.config.guild(guild).history_records.set([old])
    await cog.history_retention.callback(cog, make_context(guild), days=1)
    assert not await cog.config.guild(guild).history_records()
    assert "history_records" not in await cog._settings(guild)
