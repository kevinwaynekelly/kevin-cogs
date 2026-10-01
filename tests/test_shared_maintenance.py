"""Permission-aware discovery, bounded snapshots and optional theme protocol."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from conftest import make_context
from test_settingshub import command_runtime as command_fixture
from test_settingshub import hub_runtime as runtime_fixture
from test_settingshub import red_command_runtime as audio_fixture

from settingshub.maintenance import differences
from settingshub.presentation import COLORS, Presentation

hub_runtime = runtime_fixture
command_runtime = command_fixture
red_command_runtime = audio_fixture


def test_snapshot_diff_reports_nested_changes_and_removals():
    assert differences({"cog": {"a": 1, "b": True}}, {"cog": {"a": 2}}) == ["cog.a", "cog.b"]


async def test_shared_theme_is_optional_and_keeps_semantic_tones(guild):
    ctx = make_context(guild)
    ctx.bot = SimpleNamespace(
        _kevin_cogs_themes={guild.id: {"colors": {"error": 0x123456}, "footer": "Scarlet"}}
    )
    await Presentation("Music", "play").send(ctx, "Failed", tone="error")
    card = ctx.send.call_args.kwargs["embed"]
    assert card.color.value == 0x123456
    assert card.footer.text.startswith("Scarlet")
    ctx.bot._kevin_cogs_themes.clear()
    await Presentation("Music", "play").send(ctx, "Failed", tone="error")
    assert ctx.send.call_args.kwargs["embed"].color.value == COLORS["error"]


async def test_snapshots_skip_unchanged_and_bound_retention(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    ctx = await invoke("!settings")
    bundle = await hub._backup_bundle(ctx)
    assert await hub._store_snapshot(member.guild, bundle, now=100)
    assert not await hub._store_snapshot(member.guild, bundle, now=101)
    for index in range(15):
        changed = deepcopy(bundle)
        changed["cogs"]["OwoPlus"]["one_in"] = index + 2
        assert await hub._store_snapshot(member.guild, changed, now=200 + index)
    state = await hub.config.guild(member.guild).snapshots()
    assert len(state["records"]) == 10
    assert state["last_at"] == 214
    assert len({r["id"] for r in state["records"]}) == 10


async def test_automatic_snapshot_is_opt_in_and_excludes_member_records(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    bot._connection._guilds[member.guild.id] = member.guild
    hub._capture_settings = AsyncMock(wraps=hub._capture_settings)
    await hub._snapshot_tick(now=999999)
    hub._capture_settings.assert_not_called()
    await invoke("!snapshots auto true 1")
    await hub._snapshot_tick(now=999999)
    state = await hub.config.guild(member.guild).snapshots()
    assert len(state["records"]) == 1
    assert "xp" not in state["records"][0]["bundle"]["cogs"]["LevelPlus"]
    await invoke("!snapshots auto false")
    await hub._snapshot_tick(now=1009999)
    assert hub._capture_settings.await_count == 1


async def test_theme_and_browser_use_real_red_commands(hub_runtime):
    bot, hub, member, invoke = hub_runtime
    await invoke("!theme color error #123456")
    assert bot._kevin_cogs_themes[member.guild.id]["colors"]["error"] == 0x123456
    await invoke("!theme footer Scarlet")
    ctx = await invoke("!commandbrowser birthday")
    assert "No available commands" in ctx.send.call_args.kwargs["embed"].description
    ctx = await invoke("!commandbrowser level show")
    assert "level show" in ctx.send.call_args.kwargs["embed"].description
    assert "Usage:" in ctx.send.call_args.kwargs["embed"].description
    await invoke("!theme reset")
    assert bot._kevin_cogs_themes[member.guild.id]["colors"] == COLORS
