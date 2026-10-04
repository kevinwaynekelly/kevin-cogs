"""Regression tests for bounded templates and isolated administrator recovery."""

import asyncio
import csv
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import forbidden, make_channel, make_context, make_member
from redbot.core import commands

from communityplus import CommunityPlus
from communityplus.templates import MAX_OUTPUT, render_template, validate_template


@pytest.mark.parametrize(
    "template",
    [
        "{user:999999999}",
        "{server:.999999999s}",
        "{count:999999999d}",
        "{user:９９９９９９９９９}",
        "{user:{count}}",
        "{user.__class__}",
        "{server[0]}",
        "{user!x}",
        "{user",
        "x" * 2001,
    ],
)
async def test_template_settings_reject_unsafe_costs_without_saving(bot, guild, template):
    cog = CommunityPlus(bot)
    ctx = make_context(guild)
    before = await cog.config.guild(guild).all()
    for command in (cog.cw_msg, cog.cc_msg):
        with pytest.raises(commands.BadArgument):
            await command.callback(cog, ctx, text=template)
    assert await cog.config.guild(guild).all() == before
    with pytest.raises(ValueError):
        validate_template(template)


def test_existing_unsafe_templates_never_reach_python_format(monkeypatch):
    import communityplus.templates as templates

    formatter = Mock()
    monkeypatch.setattr(templates._FORMATTER, "format_field", formatter)
    for value in ("{user:999999999}", "{count:{user}}", "{user.__class__}"):
        assert render_template(value, {"user": "Kevin", "count": 9}) == value
    formatter.assert_not_called()


def test_normal_templates_keep_escapes_conversions_widths_and_precision():
    assert (
        render_template(
            "{{Welcome}} {mention}, {user!s:.3s} of {server:<10}, #{count:03d}",
            {"user": "Kevin", "mention": "<@123>", "server": "Reach", "count": 7},
        )
        == "{Welcome} <@123>, Kev of Reach     , #007"
    )
    template = "{user:512}" * 50
    assert render_template(template, {"user": "Kevin"}) == template
    assert len(render_template("x" * 10000, {})) == MAX_OUTPUT


async def test_unsafe_saved_templates_are_bounded_in_preview_and_live_notices(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    member.created_at = member.joined_at = discord.utils.utcnow()
    guild.member_count = 1
    guild.me.top_role = SimpleNamespace(position=10)
    channel = make_channel(guild)
    ctx = make_context(guild, channel, member)
    unsafe = "{user:999999999}"
    for section in ("welcome", "cya"):
        await getattr(cog.config.guild(guild), section).message.set(unsafe)
        await getattr(cog.config.guild(guild), section).channel_id.set(channel.id)
    await cog.cw_prev.callback(cog, ctx, member)
    await cog.cc_prev.callback(cog, ctx, member)
    await cog.on_member_join(member)
    await cog.on_member_remove(member)
    assert all(call.kwargs["embed"].description == unsafe for call in ctx.send.await_args_list)
    assert all(call.kwargs["embed"].description == unsafe for call in channel.send.await_args_list)


def recovery_role(role_id, *, position, permissions=None, name="role"):
    role = Mock(spec=discord.Role)
    role.id, role.position, role.name = role_id, position, name
    role.permissions = permissions or discord.Permissions.none()
    role.managed = False
    role.is_default.return_value = False
    role.mention = f"<@&{role_id}>"
    role.__ge__ = Mock(side_effect=lambda other: role.position >= other.position)
    role.edit = AsyncMock(return_value=role)
    role.delete = AsyncMock()
    return role


def recovery_runtime(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    member.guild_permissions = discord.Permissions.all()
    guild.owner_id = member.id
    bot.is_owner.return_value = True
    bot.intents = SimpleNamespace(members=True)
    guild.member_count = 2
    top = recovery_role(99, position=10)
    guild.me.top_role = top
    guild.me.roles = [top]
    fresh = recovery_role(100, position=9)
    guild.create_role = AsyncMock(return_value=fresh)
    guild.fetch_roles = AsyncMock(return_value=[top, fresh])
    guild.roles = [top]

    async def fetch_members(*, limit):
        assert limit is None
        yield SimpleNamespace(id=member.id, _roles=[fresh.id])
        yield SimpleNamespace(id=guild.me.id, _roles=[top.id])

    guild.fetch_members = Mock(side_effect=fetch_members)
    return cog, member, make_context(guild, author=member), fresh, top


async def test_recovery_requires_bot_ownership(bot, guild):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)
    guild.owner_id = member.id
    bot.is_owner.return_value = False
    with pytest.raises(commands.CheckFailure):
        await cog.com_restore.callback(cog, ctx)
    guild.create_role.assert_not_awaited()
    member.add_roles.assert_not_awaited()


async def test_recovery_never_adopts_named_role_and_elevates_only_after_isolated_assignment(
    bot, guild
):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)
    hijacked = recovery_role(17, position=1, name="Restored Admin")
    guild.roles.insert(0, hijacked)
    # Even a matching named role already held by the requester does not establish origin
    # or a safe hierarchy and must not be treated as a reusable recovery role.
    forged = recovery_role(
        18,
        position=1,
        permissions=discord.Permissions(administrator=True),
        name=f"Restored Admin {member.id}",
    )
    member.roles = [forged]
    guild.owner_id = member.id + 1
    calls = []

    async def edit(**kwargs):
        calls.append(("edit", kwargs))
        if "permissions" in kwargs:
            assert member.add_roles.await_count == 1
            assert guild.fetch_roles.await_count == 2
        return fresh

    fresh.edit.side_effect = edit
    await cog.com_restore.callback(cog, ctx)
    creation = guild.create_role.await_args.kwargs
    assert creation["permissions"].value == 0
    assert creation["name"] == f"Restored Admin {member.id}"
    assert creation["mentionable"] is False
    assert calls[0][1]["position"] == top.position - 1
    assert calls[1][1]["permissions"].administrator
    member.add_roles.assert_awaited_once_with(
        fresh, reason=f"Bot owner recovery requested by {member.id}"
    )
    hijacked.edit.assert_not_awaited()
    forged.edit.assert_not_awaited()
    fresh.delete.assert_not_awaited()


async def test_recovery_rejects_a_non_admin_role_manager_above_target_and_cleans_up(bot, guild):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)
    manager = recovery_role(3, position=11, permissions=discord.Permissions(manage_roles=True))
    guild.fetch_roles.return_value.append(manager)
    with pytest.raises(commands.CheckFailure):
        await cog.com_restore.callback(cog, ctx)
    member.add_roles.assert_not_awaited()
    assert fresh.edit.await_count == 1
    fresh.delete.assert_awaited_once()


async def test_shared_unmanaged_bot_role_cannot_be_a_higher_non_admin_role_manager(bot, guild):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)
    top.permissions = discord.Permissions(manage_roles=True)
    # The bot can have Administrator through another role, while this unmanaged role is
    # also held by humans. Its appearance in the bot's roles must not exempt it.
    with pytest.raises(commands.CheckFailure):
        await cog.com_restore.callback(cog, ctx)
    member.add_roles.assert_not_awaited()
    fresh.delete.assert_awaited_once()


async def test_dedicated_bot_integration_role_is_the_only_non_admin_manager_exemption(bot, guild):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)
    top.permissions = discord.Permissions(manage_roles=True)
    top.managed = True
    top.tags = SimpleNamespace(bot_id=guild.me.id)
    await cog.com_restore.callback(cog, ctx)
    assert fresh.edit.await_args.kwargs["permissions"].administrator
    fresh.delete.assert_not_awaited()


async def test_recovery_position_uses_fresh_roles_instead_of_a_delayed_gateway_cache(bot, guild):
    cog, member, ctx, fresh, cached_top = recovery_runtime(bot, guild)
    current_top = recovery_role(cached_top.id, position=cached_top.position + 1)
    fresh.position = current_top.position - 1
    guild.fetch_roles.return_value = [current_top, fresh]
    await cog.com_restore.callback(cog, ctx)
    assert fresh.edit.await_args_list[0].kwargs["position"] == current_top.position - 1
    assert fresh.edit.await_args.kwargs["permissions"].administrator


async def test_cancelled_recovery_cleans_up_the_unprivileged_role(bot, guild):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)
    assigned = asyncio.Event()

    async def waiting_assignment(*args, **kwargs):
        assigned.set()
        await asyncio.Event().wait()

    member.add_roles.side_effect = waiting_assignment
    task = asyncio.create_task(cog.com_restore.callback(cog, ctx))
    await assigned.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fresh.edit.await_count == 1
    fresh.delete.assert_awaited_once()


async def test_failed_recovery_assignment_does_not_leave_an_administrator_role(bot, guild):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)
    member.add_roles.side_effect = forbidden()
    with pytest.raises(discord.Forbidden):
        await cog.com_restore.callback(cog, ctx)
    assert fresh.edit.await_count == 1
    assert "permissions" not in fresh.edit.await_args.kwargs
    fresh.delete.assert_awaited_once()


async def test_recovery_rechecks_owner_after_discord_assignment(bot, guild):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)

    async def changed_owner(*args, **kwargs):
        bot.is_owner.return_value = False

    member.add_roles.side_effect = changed_owner
    with pytest.raises(commands.CheckFailure):
        await cog.com_restore.callback(cog, ctx)
    assert fresh.edit.await_count == 1
    fresh.delete.assert_awaited_once()


async def test_staged_role_acquired_during_initial_creation_is_never_elevated(bot, guild):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)
    attacker = SimpleNamespace(id=member.id + 1, _roles=[fresh.id])
    guild.member_count = 3

    async def fetch_members(*, limit):
        assert fresh.edit.await_count == 1
        assert member.add_roles.await_count == 1
        # The attacker's cached Member.roles deliberately omits the fresh role. Only the
        # new REST row's raw role IDs expose assignment during the harmless staging window.
        yield SimpleNamespace(id=member.id, _roles=[fresh.id])
        yield attacker
        yield SimpleNamespace(id=guild.me.id, _roles=[top.id])

    guild.fetch_members.side_effect = fetch_members
    with pytest.raises(commands.CheckFailure, match="Another member acquired"):
        await cog.com_restore.callback(cog, ctx)
    assert fresh.edit.await_count == 1
    fresh.delete.assert_awaited_once()


@pytest.mark.parametrize(
    "mode", ["short", "missing_requester", "missing_role", "duplicate", "error"]
)
async def test_incomplete_or_failed_fresh_member_enumeration_aborts_recovery(bot, guild, mode):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)

    async def fetch_members(*, limit):
        if mode == "error":
            raise forbidden()
        if mode != "missing_requester":
            yield SimpleNamespace(id=member.id, _roles=[] if mode == "missing_role" else [fresh.id])
        if mode == "duplicate":
            yield SimpleNamespace(id=member.id, _roles=[fresh.id])
        if mode != "short":
            yield SimpleNamespace(id=guild.me.id, _roles=[top.id])

    guild.fetch_members.side_effect = fetch_members
    with pytest.raises(commands.CheckFailure):
        await cog.com_restore.callback(cog, ctx)
    assert fresh.edit.await_count == 1
    fresh.delete.assert_awaited_once()


async def test_recovery_requires_member_intent_before_creating_a_role(bot, guild):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)
    bot.intents.members = False
    with pytest.raises(commands.CheckFailure, match="Server Members intent"):
        await cog.com_restore.callback(cog, ctx)
    guild.create_role.assert_not_awaited()


async def test_slow_member_enumeration_is_bounded_and_leaves_no_privileged_role(
    bot, guild, monkeypatch
):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)
    monkeypatch.setattr("communityplus.cog.RECOVERY_CHECK_SECONDS", 0.01)

    async def fetch_members(*, limit):
        yield SimpleNamespace(id=member.id, _roles=[fresh.id])
        await asyncio.Event().wait()

    guild.fetch_members.side_effect = fetch_members
    with pytest.raises(commands.CheckFailure, match="could not complete"):
        await cog.com_restore.callback(cog, ctx)
    assert fresh.edit.await_count == 1
    fresh.delete.assert_awaited_once()


async def test_recovery_rechecks_bot_owner_after_full_member_verification(bot, guild):
    cog, member, ctx, fresh, top = recovery_runtime(bot, guild)

    async def fetch_members(*, limit):
        yield SimpleNamespace(id=member.id, _roles=[fresh.id])
        yield SimpleNamespace(id=guild.me.id, _roles=[top.id])
        bot.is_owner.return_value = False

    guild.fetch_members.side_effect = fetch_members
    with pytest.raises(commands.CheckFailure, match="during member verification"):
        await cog.com_restore.callback(cog, ctx)
    assert fresh.edit.await_count == 1
    fresh.delete.assert_awaited_once()


async def test_explicit_lifetime_activity_history_is_preserved_not_silently_pruned(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    records = {f"Game {index}": index for index in range(1500)}
    records["A" * 300] = 3
    await cog.config.member(member).activity_names.set(records)
    member.activities = [SimpleNamespace(type=discord.ActivityType.playing, name="New Game")]
    await cog._handle_presence_update_logic(SimpleNamespace(activities=[]), member)
    assert await cog.config.member(member).activity_names() == {**records, "New Game": 1}


@pytest.mark.parametrize("name", ["=SUM(1,2)", "+1+2", "-1+2", "@SUM(1,2)", "\t=1+1", "  =1+1"])
async def test_seen_csv_neutralizes_formulas_without_changing_saved_or_plain_text_names(
    bot, guild, name
):
    cog = CommunityPlus(bot)
    member = make_member(guild, name=name)
    member.__str__ = Mock(return_value=name)
    ctx = make_context(guild, author=member)
    await cog.com_seenlist_csv.callback(cog, ctx)
    file = ctx.send.await_args.kwargs["file"]
    file.fp.seek(0)
    rows = list(csv.DictReader(io.StringIO(file.fp.read().decode("utf-8"))))
    assert rows[0]["display"] == "'" + name
    assert rows[0]["member_id"] == str(member.id)
    assert member.name == name


async def test_seen_csv_leaves_ordinary_member_names_unchanged(bot, guild):
    cog = CommunityPlus(bot)
    member = make_member(guild)
    member.__str__ = Mock(return_value="Kevin, Kelly")
    ctx = make_context(guild, author=member)
    await cog.com_seenlist_csv.callback(cog, ctx)
    file = ctx.send.await_args.kwargs["file"]
    file.fp.seek(0)
    rows = list(csv.DictReader(io.StringIO(file.fp.read().decode("utf-8"))))
    assert rows[0]["display"] == "Kevin, Kelly"
