"""Reward scenarios stay read-only and use the actual reconciliation plan."""

from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import make_member
from redbot.core import commands
from test_audio_hybrid import red_command_runtime as audio_fixture
from test_cog_hybrid import command_runtime as cog_fixture
from test_cog_hybrid import invoke_slash

from levelplus import LevelPlus
from levelplus.reward_preview import build_preview, preview_policy, reward_changes

command_runtime = cog_fixture
red_command_runtime = audio_fixture


def roles(guild):
    values = [
        discord.Role(
            guild=guild,
            state=Mock(),
            data={"id": str(uid), "name": f"Role {uid}", "position": position, "permissions": "0"},
        )
        for uid, position in ((1001, 1), (1002, 2), (1003, 10))
    ]
    guild.me.top_role = values[-1]
    guild.get_role.side_effect = lambda uid: next((r for r in values if r.id == uid), None)
    return values[:2]


async def test_formula_preview_reports_gains_losses_without_any_writes(bot, guild):
    cog = LevelPlus(bot)
    low, high = roles(guild)
    member = make_member(guild)
    member.roles = [high]
    member.remove_roles = AsyncMock()
    group = cog.config.guild(guild)
    await group.linear.set({"base": 100, "inc": 0})
    await group.multiplier.set(1)
    await group.xp.set({str(member.id): 1200})
    await group.rewards.set({"roles": {str(low.id): 5, str(high.id): 10}, "stack": False})
    before = await group.all()
    current = await cog._settings(guild)
    proposed = preview_policy(current, base=200)
    report = await build_preview(cog, guild, current, proposed)
    assert report["rows"] == [
        {"member": member.id, "old": 12, "new": 6, "add": [low.id], "remove": [high.id]}
    ]
    assert await group.all() == before and current["linear"]["base"] == 100
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()
    actual = reward_changes(member, 6, proposed["rewards"])
    assert actual[:2] == ([low], [high])


async def test_custom_earned_reward_protects_overlapping_role(bot, guild):
    cog = LevelPlus(bot)
    low, high = roles(guild)
    member = make_member(guild)
    member.roles = [high]
    group = cog.config.guild(guild)
    await group.rewards.roles.set({str(high.id): 10})
    await group.progress_settings.goals.set(
        {"keep": {"id": "abcdef123456", "metric": "xp", "target": 10, "reward": 0, "role": high.id}}
    )
    await group.progress.set({str(member.id): {"earned": ["abcdef123456"]}})
    current = await cog._settings(guild)
    report = await build_preview(cog, guild, current, current, member)
    assert not report["rows"][0]["remove"]


async def test_removed_definition_retains_assignments_and_blocked_roles_are_reported(bot, guild):
    cog = LevelPlus(bot)
    low, high = roles(guild)
    member = make_member(guild)
    member.roles = [high]
    await cog.config.guild(guild).rewards.roles.set({str(high.id): 10, "888": 1})
    current = await cog._settings(guild)
    proposed = preview_policy(current, role=high, threshold=0)
    report = await build_preview(cog, guild, current, proposed, member)
    assert not report["rows"][0]["remove"] and report["blocked"] == {888}


@pytest.mark.parametrize(
    "options",
    [
        {"multiplier": float("nan")},
        {"base": float("inf")},
        {"curve": "unknown"},
        {"increment": -1},
        {"max_level": -1},
        {"threshold": 2},
    ],
)
async def test_invalid_candidates_fail_without_saving(bot, guild, options):
    cog = LevelPlus(bot)
    before = await cog.config.guild(guild).all()
    with pytest.raises(commands.BadArgument):
        preview_policy(await cog._settings(guild), **options)
    assert await cog.config.guild(guild).all() == before


async def test_preview_prefix_slash_and_original_admin_checks(command_runtime, monkeypatch):
    bot, loaded, member, invoke = command_runtime
    cog = bot.get_cog("LevelPlus")
    bot.owner_ids.add(member.id)
    before = await cog.config.guild(member.guild).all()
    ctx = await invoke("!level rewards preview")
    assert not ctx.command_failed and "Dry run" in ctx.send.await_args.kwargs["embed"].description
    ctx = await invoke_slash(
        bot, invoke, monkeypatch, "level rewards preview", curve="linear", base=200.0
    )
    assert not ctx.command_failed and await cog.config.guild(member.guild).all() == before
    bot.owner_ids.discard(member.id)
    ctx = await invoke("!level rewards preview")
    assert ctx.command_failed
