"""Real Red command/configuration paths; only Discord gateway transport is mocked."""

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import make_member
from redbot.core import commands
from redbot.core._cli import parse_cli_flags
from redbot.core._events import init_events
from test_audio_hybrid import red_command_runtime as runtime_fixture
from test_cog_hybrid import invoke_slash
from test_native_audio import FakeVoice, eventually, track

from audioplus.player import GuildPlayer
from presenceplus import PresencePlus
from presenceplus.constants import (
    DEFAULT_COMMAND_HINT,
    DEFAULTS,
    LEGACY_COMMAND_HINT,
    __red_end_user_data_statement__,
)
from presenceplus.controller import PresenceController, signature
from presenceplus.profiles import effective_profile, minute, render, template, validate, weekdays

red_command_runtime = runtime_fixture


class PresenceMember(SimpleNamespace):
    @property
    def activity(self):
        return self.activities[0] if self.activities else None


@pytest.fixture
async def presence_runtime(red_command_runtime, monkeypatch):
    bot, audio, member, invoke = red_command_runtime
    init_events(bot, parse_cli_flags([]))
    monkeypatch.setattr(bot, "_delete_delay", AsyncMock())
    monkeypatch.setattr("presenceplus.controller.MIN_UPDATE_SECONDS", 0)
    guild = member.guild
    guild.me = PresenceMember(
        **vars(guild.me),
        status=discord.Status.idle,
        activities=(discord.Game("Before automation"),),
    )
    guild.member_count = 42
    bot._connection._guilds[guild.id] = guild
    bot.owner_ids.add(member.id)
    # Exercise AutoShardedClient's actual local presence cache updates without a socket.
    change = bot.change_presence
    monkeypatch.setattr(bot, "change_presence", AsyncMock(wraps=change))
    cog = PresencePlus(bot)
    await bot.add_cog(cog)
    try:
        yield bot, audio, cog, member, invoke
    finally:
        await bot.remove_cog("PresencePlus")


def scheduled():
    settings = deepcopy(DEFAULTS)
    settings["profiles"]["night"] = {
        "status": "idle",
        "entries": [{"kind": "watching", "text": "The moon"}],
    }
    settings["timezone"] = "America/Chicago"
    settings["schedules"]["friday"] = {
        "profile": "night",
        "start": minute("22:00"),
        "end": minute("07:00"),
        "days": weekdays("fri"),
    }
    return settings


@pytest.mark.parametrize(
    "value",
    [
        "{unknown}",
        "{song.title}",
        "{song[0]}",
        "{song!r}",
        "{servers:03}",
        "{",
        "",
        "line\nbreak",
        "x" * 129,
        "🎵" * 65,
    ],
)
def test_invalid_templates_fail_before_configuration_or_gateway_updates(value):
    with pytest.raises(ValueError):
        template(value)


def test_unicode_templates_literal_braces_and_untrusted_music_are_bounded():
    text = template("{{music}} {song} · {listeners}")
    rendered = render(text, {"song": "A\n" + "🎵" * 100, "listeners": 3})
    assert rendered.startswith("{music} A") and "\n" not in rendered
    assert len(rendered.encode("utf-16-le")) <= 256
    assert template("🎵" * 64) == "🎵" * 64


@pytest.mark.parametrize(
    "utc,expected",
    [
        ("2026-10-03T02:59:00+00:00", "default"),
        ("2026-10-03T03:00:00+00:00", "night"),
        ("2026-10-03T11:59:00+00:00", "night"),
        ("2026-10-03T12:00:00+00:00", "default"),
        ("2026-10-04T03:00:00+00:00", "default"),
    ],
)
def test_overnight_rules_use_the_start_day_and_exclusive_end(utc, expected):
    settings = validate(scheduled())
    assert effective_profile(settings, datetime.fromisoformat(utc))[0] == expected


@pytest.mark.parametrize("utc", ["2026-11-01T06:30:00+00:00", "2026-11-01T07:30:00+00:00"])
def test_repeated_dst_hour_selects_one_profile_without_duplicate_jobs(utc):
    settings = scheduled()
    settings["schedules"]["friday"]["days"] = weekdays("sat")
    assert effective_profile(validate(settings), datetime.fromisoformat(utc))[0] == "night"


@pytest.mark.parametrize(
    "start,end,days",
    [("06:30", "08:00", "sat"), ("23:00", "01:00", "fri"), ("22:00", "22:00", "sun")],
)
def test_schedule_overlap_and_ambiguous_all_day_windows_are_rejected(start, end, days):
    settings = scheduled()
    settings["schedules"]["conflict"] = {
        "profile": "default",
        "start": minute(start),
        "end": minute(end),
        "days": weekdays(days),
    }
    with pytest.raises(ValueError):
        validate(settings)


def test_sunday_rollover_conflicts_with_monday_and_adjacent_windows_are_allowed():
    settings = scheduled()
    settings["schedules"]["friday"]["days"] = weekdays("sun")
    settings["schedules"]["monday"] = {
        "profile": "default",
        "start": minute("06:59"),
        "end": minute("08:00"),
        "days": weekdays("mon"),
    }
    with pytest.raises(ValueError, match="overlap"):
        validate(settings)
    settings["schedules"]["monday"]["start"] = minute("07:00")
    assert validate(settings)


@pytest.mark.parametrize(
    "field,value",
    [
        ("interval", 59),
        ("interval", 86401),
        ("interval", True),
        ("enabled", 1),
        ("timezone", "No/SuchPlace"),
        ("selected", "missing"),
    ],
)
def test_invalid_global_policy_is_rejected(field, value):
    settings = deepcopy(DEFAULTS)
    settings[field] = value
    with pytest.raises(ValueError):
        validate(settings)


async def test_prefix_slash_messages_profiles_and_restart_persistence(
    presence_runtime, monkeypatch
):
    bot, _, cog, member, invoke = presence_runtime
    assert not (await cog.config.settings())["enabled"]
    bot.change_presence.assert_not_awaited()
    for content in (
        "!presence set custom Hi from {servers} servers",
        "!presence add watching {members} members",
        "!presence interval 60",
        "!presence profile create night",
        "!presence profile use night",
        "!presence set listening Quiet music",
        "!presence status idle",
        "!presence profile use default",
    ):
        ctx = await invoke(content)
        assert not ctx.command_failed, content
    ctx = await invoke_slash(bot, invoke, monkeypatch, "presence timezone", timezone="UTC")
    assert not ctx.command_failed and ctx.defer.await_args.kwargs["ephemeral"]
    saved = await cog.config.settings()
    assert len(saved["profiles"]["default"]["entries"]) == 2
    assert saved["profiles"]["night"]["status"] == "idle"
    assert saved["timezone"] == "UTC"
    for path in (
        "!presence",
        "!presence show",
        "!presence list",
        "!presence profile",
        "!presence schedule",
        "!presence help",
    ):
        assert not (await invoke(path)).command_failed
    # Red copies each cog's hybrid group, including the fallback, on reload.
    await bot.remove_cog("PresencePlus")
    reloaded = PresencePlus(bot)
    await bot.add_cog(reloaded)
    assert await reloaded.config.settings() == saved
    ctx = await invoke_slash(bot, invoke, monkeypatch, "presence show")
    assert not ctx.command_failed and "default" in ctx.send.await_args.kwargs["embed"].description


@pytest.mark.parametrize("enabled", [False, True])
async def test_stock_hint_upgrade_preserves_settings_and_later_owner_edits(
    presence_runtime, enabled
):
    bot, _, cog, _, invoke = presence_runtime
    await bot.remove_cog("PresencePlus")
    settings = deepcopy(DEFAULTS)
    settings["enabled"] = enabled
    settings["interval"] = 600
    settings["profiles"]["default"]["status"] = "idle"
    settings["profiles"]["default"]["entries"] = [
        {"kind": "custom", "text": LEGACY_COMMAND_HINT},
        {"kind": "watching", "text": "Owner's custom message"},
    ]
    settings["profiles"]["personal"] = {
        "status": "dnd",
        "entries": [{"kind": "custom", "text": LEGACY_COMMAND_HINT}],
    }
    settings["schedules"]["night"] = {"profile": "personal", "start": 1320, "end": 420, "days": [4]}
    await cog.config.settings.set(settings)
    await cog.config.command_hint_version.set(0)
    upgraded = PresencePlus(bot)
    await bot.add_cog(upgraded)
    expected = deepcopy(settings)
    expected["profiles"]["default"]["entries"][0]["text"] = DEFAULT_COMMAND_HINT
    assert await upgraded.config.settings() == expected
    assert await upgraded.config.command_hint_version() == 1
    assert not (await invoke("!presence preview")).command_failed

    # Once the upgrade has run, an owner's exact choice of the old text is custom.
    assert not (await invoke(f"!presence set custom {LEGACY_COMMAND_HINT}")).command_failed
    chosen = await upgraded.config.settings()
    await bot.remove_cog("PresencePlus")
    reloaded = PresencePlus(bot)
    await bot.add_cog(reloaded)
    assert await reloaded.config.settings() == chosen


async def test_custom_default_hint_survives_first_upgrade(presence_runtime):
    bot, _, cog, _, _ = presence_runtime
    await bot.remove_cog("PresencePlus")
    settings = deepcopy(DEFAULTS)
    settings["profiles"]["default"]["entries"] = [
        {"kind": "custom", "text": "Kevin's chosen status"}
    ]
    await cog.config.settings.set(settings)
    await cog.config.command_hint_version.set(0)
    upgraded = PresencePlus(bot)
    await bot.add_cog(upgraded)
    assert await upgraded.config.settings() == settings
    assert await upgraded.config.command_hint_version() == 1


@pytest.mark.parametrize("slash", [False, True])
async def test_ordinary_users_and_administrators_cannot_control_global_presence(
    presence_runtime, monkeypatch, slash
):
    bot, _, cog, member, invoke = presence_runtime
    bot.owner_ids.discard(member.id)
    member.guild_permissions = discord.Permissions.all()
    before = await cog.config.settings()
    ctx = (
        await invoke_slash(bot, invoke, monkeypatch, "presence set", activity="playing", text="No")
        if slash
        else await invoke("!presence set playing No")
    )
    assert ctx.command_failed and await cog.config.settings() == before
    bot.change_presence.assert_not_awaited()
    if slash:
        ctx.defer.assert_not_awaited()


async def test_slash_parent_disable_is_checked_before_acknowledgement(
    presence_runtime, monkeypatch
):
    bot, _, cog, _, invoke = presence_runtime
    bot.get_command("presence").enabled = False
    ctx = await invoke_slash(bot, invoke, monkeypatch, "presence set", activity="custom", text="No")
    assert ctx.command_failed and not (await cog.config.settings())["enabled"]
    ctx.defer.assert_not_awaited()


async def test_slash_saved_parent_permission_denial_is_retained(presence_runtime, monkeypatch):
    bot, _, cog, _, invoke = presence_runtime

    # A global check is used to represent a current saved parent denial.
    async def deny(ctx):
        if ctx.command.qualified_name == "presence":
            raise commands.CheckFailure("Disabled for this context")
        return True

    parent = bot.get_command("presence")
    parent.add_check(deny)
    try:
        ctx = await invoke_slash(
            bot, invoke, monkeypatch, "presence profile create", profile="forbidden"
        )
        assert ctx.command_failed and "forbidden" not in (await cog.config.settings())["profiles"]
        ctx.defer.assert_not_awaited()
    finally:
        parent.remove_check(deny)


async def test_invalid_changes_leave_saved_configuration_untouched(presence_runtime):
    _, _, cog, _, invoke = presence_runtime
    await invoke("!presence profile create night")
    await invoke("!presence schedule add late night 22:00 07:00 fri")
    before = await cog.config.settings()
    for content in (
        "!presence interval 1",
        "!presence set custom {password}",
        "!presence schedule add overlap default 06:00 08:00 sat",
        "!presence profile delete night",
        "!presence profile delete default",
        "!presence remove 0",
        "!presence timezone Invalid/Zone",
        "!presence schedule add bad default 25:00 07:00",
    ):
        ctx = await invoke(content)
        assert ctx.command_failed, content
        assert await cog.config.settings() == before


async def test_concurrent_writers_keep_both_updates_and_recheck_owner_after_wait(presence_runtime):
    bot, _, cog, member, invoke = presence_runtime
    await asyncio.gather(
        invoke("!presence add watching Servers {servers}"), invoke("!presence add listening Music")
    )
    assert len((await cog.config.settings())["profiles"]["default"]["entries"]) == 3
    async with cog.config.settings.get_lock():
        task = asyncio.create_task(invoke("!presence interval 120"))
        await eventually(lambda: bool(cog._commands))
        bot.owner_ids.discard(member.id)
    ctx = await task
    assert ctx.command_failed and (await cog.config.settings())["interval"] == 300


async def test_preview_does_not_publish_or_advance_and_reset_requires_true(presence_runtime):
    bot, _, cog, _, invoke = presence_runtime
    await invoke("!presence profile create night")
    saved = await cog.config.settings()
    state = cog._controller.phase, cog._controller.phase_start, cog._controller.offset
    await invoke("!presence preview")
    await invoke("!presence reset")
    assert await cog.config.settings() == saved
    assert (cog._controller.phase, cog._controller.phase_start, cog._controller.offset) == state
    bot.change_presence.assert_not_awaited()
    await invoke("!presence reset true")
    assert await cog.config.settings() == DEFAULTS


async def test_slash_schedule_and_music_use_real_conversion_paths(presence_runtime, monkeypatch):
    bot, _, cog, member, invoke = presence_runtime
    await invoke("!presence profile create night")
    ctx = await invoke_slash(
        bot,
        invoke,
        monkeypatch,
        "presence schedule add",
        rule="overnight",
        profile="night",
        start="22:00",
        end="07:00",
        days="fri,sat",
    )
    assert not ctx.command_failed
    assert (await cog.config.settings())["schedules"]["overnight"]["days"] == [4, 5]
    ctx = await invoke_slash(
        bot, invoke, monkeypatch, "presence music", enabled=True, server_id=str(member.guild.id)
    )
    assert (
        not ctx.command_failed
        and (await cog.config.settings())["music"]["guild_id"] == member.guild.id
    )
    await invoke("!presence music false")
    assert not (await cog.config.settings())["music"]["enabled"]


async def test_unload_cancels_waiting_writers_before_they_commit(presence_runtime):
    bot, _, cog, _, invoke = presence_runtime
    before = await cog.config.settings()
    async with cog.config.settings.get_lock():
        task = asyncio.create_task(invoke("!presence interval 120"))
        await eventually(lambda: bool(cog._commands))
        await bot.remove_cog("PresencePlus")
        # Discord.py converts callback cancellation into command_failed.
        assert task.done() and (await task).command_failed
    assert await cog.config.settings() == before
    assert cog._controller.closed and cog._controller.task is None


@pytest.fixture
def controlled(monkeypatch):
    clock = [1000.0]
    now = [datetime(2026, 10, 3, 10, tzinfo=timezone.utc)]
    bot = SimpleNamespace(
        guilds=[],
        status=discord.Status.idle,
        activity=discord.Game("Before"),
        get_guild=lambda gid: None,
        get_cog=lambda name: None,
        wait_until_red_ready=AsyncMock(),
        cog_disabled_in_guild=AsyncMock(return_value=False),
    )

    async def change(*, status, activity):
        bot.status, bot.activity = status, activity

    bot.change_presence = AsyncMock(side_effect=change)
    cog = PresencePlus(bot)
    controller = PresenceController(cog, clock=lambda: now[0], monotonic=lambda: clock[0])
    cog._controller = controller
    return bot, cog, controller, clock, now


async def enable(cog, *, entries=None):
    settings = deepcopy(DEFAULTS)
    settings["enabled"] = True
    if entries is not None:
        settings["profiles"]["default"]["entries"] = entries
    await cog.config.settings.set(settings)
    return settings


async def test_gateway_throttle_dedup_rotation_and_forced_resume(controlled):
    bot, cog, controller, clock, _ = controlled
    await enable(
        cog, entries=[{"kind": "playing", "text": "One"}, {"kind": "watching", "text": "Two"}]
    )
    await controller.tick()
    assert bot.activity.name == "One"
    await controller.tick()
    assert bot.change_presence.await_count == 1
    controller.request(force=True)
    clock[0] += 14
    await controller.tick()
    assert bot.change_presence.await_count == 1
    clock[0] += 1
    await controller.tick()
    assert bot.change_presence.await_count == 2
    clock[0] += 285
    await controller.tick()
    assert bot.activity.name == "Two"
    clock[0] += 300
    await controller.tick()
    assert bot.activity.name == "One"
    clock[0] += 15
    await controller.close()
    assert bot.activity.name == "Before" and bot.status is discord.Status.idle


async def test_availability_without_activity_and_naive_red_uptime(controlled):
    bot, cog, controller, _, now = controlled
    settings = await enable(cog, entries=[])
    settings["profiles"]["default"]["status"] = "invisible"
    bot.uptime = (now[0] - timedelta(days=2, hours=3)).replace(tzinfo=None)
    await cog.config.settings.set(settings)
    await controller.tick()
    assert bot.activity is None and bot.status is discord.Status.invisible
    assert controller.uptime(now[0]) == "2d 3h 0m"


async def test_disable_restores_baseline_but_respects_later_core_manual_status(controlled):
    bot, cog, controller, clock, _ = controlled
    settings = await enable(cog)
    await controller.tick()
    settings["enabled"] = False
    await cog.config.settings.set(settings)
    clock[0] += 15
    await controller.tick()
    assert bot.activity.name == "Before" and controller.baseline is None
    settings["enabled"] = True
    await cog.config.settings.set(settings)
    clock[0] += 15
    await controller.tick()
    bot.activity = discord.Game("Changed using Core")
    calls = bot.change_presence.await_count
    await controller.close()
    assert bot.activity.name == "Changed using Core" and bot.change_presence.await_count == calls


async def test_disable_does_not_restore_until_gateway_cooldown_expires(controlled):
    bot, cog, controller, clock, _ = controlled
    settings = await enable(cog)
    await controller.tick()
    settings["enabled"] = False
    await cog.config.settings.set(settings)
    await controller.tick()
    assert bot.activity.name == DEFAULT_COMMAND_HINT
    clock[0] += 15
    await controller.tick()
    assert bot.activity.name == "Before"


async def test_schedule_transitions_reset_rotation_and_preview_keeps_phase(controlled):
    _, cog, controller, clock, now = controlled
    settings = scheduled()
    settings["enabled"] = True
    await cog.config.settings.set(settings)
    await controller.tick()
    assert controller.last_display.profile == "night"
    phase = controller.phase, controller.phase_start
    now[0] = datetime(2026, 10, 3, 13, tzinfo=timezone.utc)
    assert (await controller.display(settings, preview=True)).profile == "default"
    assert (controller.phase, controller.phase_start) == phase
    clock[0] += 15
    await controller.tick()
    assert controller.last_display.profile == "default" and controller.last_display.index == 0


async def test_worker_recovers_gateway_failure_without_logging_private_details(
    controlled, monkeypatch, caplog
):
    bot, cog, controller, clock, _ = controlled
    await enable(cog)
    normal = bot.change_presence.side_effect
    bot.change_presence.side_effect = RuntimeError("private status text and secret URL")
    controller.start()
    try:
        await eventually(lambda: controller.last_error == "RuntimeError")
        assert "secret URL" not in caplog.text and "RuntimeError" in caplog.text
        bot.change_presence.side_effect = normal
        clock[0] += 15
        controller.request()
        await eventually(lambda: controller.last_display is not None)
        assert controller.last_error is None
    finally:
        clock[0] += 15
        await controller.close()


async def test_unload_cancels_pending_gateway_work_without_late_updates(controlled):
    bot, cog, controller, _, _ = controlled
    await enable(cog)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    bot.change_presence.side_effect = blocked
    controller.start()
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(controller.close(), 1)
    assert cancelled.is_set() and controller.task is None
    controller.request(force=True)
    await controller.tick()
    assert bot.change_presence.await_count == 1


async def test_music_snapshot_is_scoped_read_only_and_returns_on_pause_or_disable(presence_runtime):
    bot, audio, cog, member, invoke = presence_runtime
    guild = member.guild
    voice = FakeVoice(guild.id)
    voice.guild = guild
    listener = make_member(guild, 321)
    deaf = make_member(guild, 322)
    deaf.voice = SimpleNamespace(deaf=True, self_deaf=False)
    listener.voice = SimpleNamespace(deaf=False, self_deaf=False)
    machine = make_member(guild, 323, bot=True)
    voice.channel.members = [listener, deaf, machine]
    guild.voice_client = voice
    player = GuildPlayer(voice, audio._resolver, AsyncMock())
    player.current = track("Current song")
    voice.source = object()
    audio._players[guild.id] = player
    try:
        assert audio.music_presence(guild.id) == {"song": "Current song", "listeners": 1}
        assert not player.queue and voice.starts == []
        await invoke("!presence set watching Normal status")
        await invoke("!presence music true")
        display = await cog._controller.display(await cog.config.settings(), preview=True)
        assert display.music and display.kind == "listening" and display.text == "Current song"
        voice.paused = True
        display = await cog._controller.display(await cog.config.settings(), preview=True)
        assert not display.music and display.text == "Normal status"
        voice.paused = False
        bot.cog_disabled_in_guild = AsyncMock(return_value=True)
        assert not (await cog._controller.display(await cog.config.settings(), preview=True)).music
        bot.cog_disabled_in_guild.assert_awaited_with(audio, guild)
        assert audio.music_presence(guild.id + 1) is None
    finally:
        audio._players.pop(guild.id)
        guild.voice_client = None


def test_activity_payloads_are_supported_and_invisible_matches_cached_offline():
    from presenceplus.controller import Display

    for kind, expected in [
        ("custom", 4),
        ("playing", 0),
        ("listening", 2),
        ("watching", 3),
        ("competing", 5),
    ]:
        activity = Display("default", None, "online", kind, "Test", 0, 1).activity()
        payload = activity.to_dict()
        assert payload["type"] == expected
        assert payload.get("state", payload["name"]) == "Test"
    assert signature(discord.Status.offline, None) == signature("invisible", None)


async def test_metadata_privacy_and_independent_registration(controlled):
    _, cog, _, _, _ = controlled
    metadata = json.loads(Path("presenceplus/info.json").read_text())
    assert metadata["end_user_data_statement"] == __red_end_user_data_statement__
    assert metadata["requirements"] == []
    assert await cog.red_get_data_for_user(user_id=123) == {}
    before = await cog.config.settings()
    await cog.red_delete_data_for_user(requester="user", user_id=123)
    assert await cog.config.settings() == before
    for command in cog.walk_commands():
        assert command.help and "plus" not in command.name
