"""Real Red storage/commands with the Discord scheduled-event API mocked."""

import asyncio
from copy import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import make_channel, make_context, make_member
from redbot.core import commands
from test_audio_hybrid import red_command_runtime as red_fixture
from test_cog_hybrid import command_runtime as command_fixture
from test_cog_hybrid import invoke_slash

from communityplus import CommunityPlus
from communityplus.native_events import marker

command_runtime = command_fixture
red_command_runtime = red_fixture


class RemoteEvent:
    def __init__(self, guild, identifier, arguments, owner=999):
        self.guild, self.id, self.creator_id = guild, identifier, owner
        self.status = discord.EventStatus.scheduled
        self.name = arguments["name"]
        self.description = arguments["description"]
        self.start_time = arguments["start_time"]
        self.end_time = arguments["end_time"]
        self.channel_id = getattr(arguments["channel"], "id", None)
        self.calls = []
        self.edit = AsyncMock(side_effect=self._edit)

    async def _edit(self, **arguments):
        self.calls.append(arguments)
        result = copy(self)  # discord.py returns a new object after every edit.
        for key in ("name", "description", "start_time", "end_time", "status"):
            if key in arguments:
                setattr(result, key, arguments[key])
        if "channel" in arguments:
            result.channel_id = getattr(arguments["channel"], "id", None)
        self.guild._remote[self.id] = result
        return result


def transport(guild, owner=999):
    guild._remote = {}
    guild.fetch_scheduled_events = AsyncMock(side_effect=lambda **kw: list(guild._remote.values()))

    async def create(**arguments):
        remote = RemoteEvent(guild, 8000 + len(guild._remote), arguments, owner)
        guild._remote[remote.id] = remote
        return remote

    guild.create_scheduled_event = AsyncMock(side_effect=create)
    return create


@pytest.fixture
async def native(bot, guild):
    cog = CommunityPlus(bot)
    cog._now_ts = lambda: 1000
    ctx = make_context(guild, make_channel(guild), make_member(guild))
    ctx.send.return_value = SimpleNamespace(id=555, edit=AsyncMock())
    ctx.channel.fetch_message.return_value = ctx.send.return_value
    transport(guild)
    yield cog, ctx
    await cog.cog_unload()


async def local(cog, ctx, title="Game night"):
    await cog.event_create.callback(cog, ctx, title, "1600", 15)
    return next(reversed(await cog.config.guild(ctx.guild).social.events()))


async def test_opt_in_native_creation_voice_permissions_and_idempotence(native):
    cog, ctx = native
    key = await local(cog, ctx)
    ctx.guild.create_scheduled_event.assert_not_awaited()
    voice = make_channel(ctx.guild, 765, kind=discord.VoiceChannel)
    await cog.event_native.callback(cog, ctx, key, voice, 30)
    await cog.event_native.callback(cog, ctx, key, voice, 30)
    arguments = ctx.guild.create_scheduled_event.call_args.kwargs
    assert arguments["entity_type"] is discord.EntityType.voice and arguments["channel"] is voice
    assert arguments["privacy_level"] is discord.PrivacyLevel.guild_only
    assert arguments["start_time"] == datetime.fromtimestamp(1600, timezone.utc)
    assert arguments["end_time"] == datetime.fromtimestamp(3400, timezone.utc)
    assert ctx.guild.create_scheduled_event.await_count == 1
    assert (await cog._social_record(ctx.guild, "events", key))["native"]["id"] == 8000
    voice.permissions_for.return_value.connect = False
    with pytest.raises(commands.CheckFailure):
        await cog.event_native.callback(cog, ctx, key, voice, 30)


async def test_future_edits_update_the_existing_mirror_and_own_echo_preserves_long_title(native):
    cog, ctx = native
    title = "a" * 200
    key = await local(cog, ctx, title)
    await cog.event_native.callback(cog, ctx, key)
    first = ctx.guild._remote[8000]
    await cog.on_scheduled_event_update(first, first)
    assert (await cog._social_record(ctx.guild, "events", key))["title"] == title
    await cog.event_edit.callback(cog, ctx, key, "Changed", "2200", 10)
    assert ctx.guild.create_scheduled_event.await_count == 1
    updated = ctx.guild._remote[8000]
    assert updated.name == "Changed" and int(updated.start_time.timestamp()) == 2200
    assert (
        updated.calls[-1]["location"]
        == f"https://discord.com/channels/{ctx.guild.id}/{ctx.channel.id}"
    )
    assert not (await cog._social_record(ctx.guild, "events", key))["native"]["archives"]


async def test_timeout_after_discord_create_is_reused_and_reload_keeps_mapping(native):
    cog, ctx = native
    key = await local(cog, ctx)
    create = ctx.guild.create_scheduled_event.side_effect

    async def timeout(**arguments):
        await create(**arguments)
        raise asyncio.TimeoutError

    ctx.guild.create_scheduled_event.side_effect = timeout
    await cog.event_native.callback(cog, ctx, key)
    assert (await cog._social_record(ctx.guild, "events", key))["native"]["error"] == "TimeoutError"
    ctx.guild.create_scheduled_event.side_effect = create
    await cog._native_tick(ctx.guild)
    assert ctx.guild.create_scheduled_event.await_count == 1
    replacement = CommunityPlus(cog.bot)
    replacement._now_ts = cog._now_ts
    try:
        await replacement._native_tick(ctx.guild)
        assert ctx.guild.create_scheduled_event.await_count == 1
        assert (await replacement._social_record(ctx.guild, "events", key))["native"]["id"] == 8000
    finally:
        await replacement.cog_unload()


async def test_native_lifecycle_starts_and_finishes_at_configured_duration(native):
    cog, ctx = native
    key = await local(cog, ctx)
    await cog.event_native.callback(cog, ctx, key, minutes=1)
    cog._now_ts = lambda: 1600
    await cog._social_tick(ctx.guild)
    assert ctx.guild._remote[8000].status is discord.EventStatus.active
    assert (await cog._social_record(ctx.guild, "events", key))["closed"]
    cog._now_ts = lambda: 1660
    await cog._social_tick(ctx.guild)
    assert ctx.guild._remote[8000].status is discord.EventStatus.completed
    ctx.guild.fetch_scheduled_events.reset_mock()
    await cog._social_tick(ctx.guild)
    ctx.guild.fetch_scheduled_events.assert_not_awaited()


async def test_cancelling_mirror_keeps_local_rsvps_and_failed_cancellation_retries(native):
    cog, ctx = native
    key = await local(cog, ctx)
    await cog.event_native.callback(cog, ctx, key)
    await cog._social_choice(ctx, "events", key, "yes")
    ctx.guild.me.guild_permissions.create_events = False
    ctx.guild.me.guild_permissions.manage_events = False
    await cog.event_nativeoff.callback(cog, ctx, key)
    row = await cog._social_record(ctx.guild, "events", key)
    assert row["native"]["error"] == "CheckFailure" and not row["closed"]
    assert row["rsvps"] == {str(ctx.author.id): "yes"}
    ctx.guild.me.guild_permissions.create_events = True
    await cog._native_tick(ctx.guild)
    assert ctx.guild._remote[8000].status is discord.EventStatus.cancelled
    assert (await cog._social_record(ctx.guild, "events", key))["native"]["id"] is None
    await cog.event_native.callback(cog, ctx, key)
    await cog.event_cancel.callback(cog, ctx, key)
    assert ctx.guild._remote[8001].status is discord.EventStatus.cancelled


async def test_repeating_native_events_keep_current_occurrence_until_its_end(native):
    cog, ctx = native
    await cog.event_nativeset.callback(cog, ctx, True, minutes=30)
    key = await local(cog, ctx)
    await cog.event_policy.callback(cog, ctx, key, 5, 1)
    cog._now_ts = lambda: 1600
    await cog._social_tick(ctx.guild)
    row = await cog._social_record(ctx.guild, "events", key)
    assert row["at"] == 88000 and row["native"]["id"] == 8001
    assert row["native"]["archives"] == [{"id": 8000, "at": 1600, "end": 3400}]
    assert ctx.guild._remote[8000].status is discord.EventStatus.active
    cog._now_ts = lambda: 3400
    await cog._social_tick(ctx.guild)
    assert ctx.guild._remote[8000].status is discord.EventStatus.completed
    assert not (await cog._social_record(ctx.guild, "events", key))["native"]["archives"]
    await cog.event_nativeset.callback(cog, ctx, False)
    assert (await cog._social_record(ctx.guild, "events", key))["native"]["enabled"]


async def test_discord_updates_reverse_sync_but_do_not_write_interested_users(native):
    cog, ctx = native
    key = await local(cog, ctx)
    await cog.event_native.callback(cog, ctx, key)
    await cog._social_choice(ctx, "events", key, "yes")
    before = ctx.guild._remote[8000]
    after = await before.edit(
        name="From Discord", start_time=datetime.fromtimestamp(2000, timezone.utc)
    )
    await cog.on_scheduled_event_update(before, after)
    row = await cog._social_record(ctx.guild, "events", key)
    assert row["at"] == 2000 and row["title"] == "From Discord"
    assert row["rsvps"] == {str(ctx.author.id): "yes"}
    cancelled = await after.edit(status=discord.EventStatus.cancelled)
    await cog.on_scheduled_event_update(after, cancelled)
    assert (await cog._social_record(ctx.guild, "events", key))["closed"]


async def test_manual_deletion_disable_and_unowned_events_are_never_modified(native):
    cog, ctx = native
    key = await local(cog, ctx)
    await cog.event_native.callback(cog, ctx, key)
    remote = ctx.guild._remote.pop(8000)
    await cog.on_scheduled_event_delete(remote)
    await cog._native_tick(ctx.guild)
    assert ctx.guild.create_scheduled_event.await_count == 1
    row = await cog._social_record(ctx.guild, "events", key)
    assert not row["native"]["enabled"] and not row["closed"]
    remote.creator_id = 1234
    remote.description = marker(key, 1600)
    ctx.guild._remote[8000] = remote
    async with cog.config.guild(ctx.guild).social() as data:
        data["events"][key]["native"].update(id=8000, enabled=True)
    await cog._native_tick(ctx.guild)
    remote.edit.assert_not_awaited()
    assert (await cog._social_record(ctx.guild, "events", key))["native"]["error"] == "CheckFailure"
    cog.bot.cog_disabled_in_guild.return_value = True
    ctx.guild.fetch_scheduled_events.reset_mock()
    await cog._native_tick(ctx.guild)
    ctx.guild.fetch_scheduled_events.assert_not_awaited()


async def test_natural_native_end_does_not_cancel_a_recurring_series(native):
    cog, ctx = native
    key = await local(cog, ctx)
    await cog.event_native.callback(cog, ctx, key, minutes=1)
    await cog.event_policy.callback(cog, ctx, key, 0, 1)
    cog._now_ts = lambda: 1660
    before = ctx.guild._remote[8000]
    after = await before.edit(status=discord.EventStatus.completed)
    await cog.on_scheduled_event_update(before, after)
    assert not (await cog._social_record(ctx.guild, "events", key))["closed"]
    await cog._social_tick(ctx.guild)
    assert (await cog._social_record(ctx.guild, "events", key))["native"]["id"] == 8001


async def test_prefix_and_slash_native_commands_preserve_admin_checks(command_runtime, monkeypatch):
    bot, loaded, member, invoke = command_runtime
    community = bot.get_cog("CommunityPlus")
    transport(member.guild, bot.user.id)
    community._reply = AsyncMock(return_value=SimpleNamespace(id=555, edit=AsyncMock()))
    bot.owner_ids.add(member.id)
    ctx = await invoke("!event create Night 2026-12-01T18:00:00Z")
    assert not ctx.command_failed
    key = next(iter(await community.config.guild(member.guild).social.events()))
    bot.owner_ids.discard(member.id)
    ctx = await invoke(f"!event native {key}")
    assert ctx.command_failed and not member.guild._remote
    bot.owner_ids.add(member.id)
    ctx = await invoke_slash(bot, invoke, monkeypatch, "event native", event_id=key, minutes=30)
    assert not ctx.command_failed and len(member.guild._remote) == 1
    bot.get_command("event native").disable_in(member.guild)
    ctx = await invoke_slash(bot, invoke, monkeypatch, "event native", event_id=key)
    assert ctx.command_failed and len(member.guild._remote) == 1
