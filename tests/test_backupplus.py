"""Actual Red commands/Config and SDK objects, with Discord transport mocked."""

import asyncio
import json
import time
from copy import deepcopy
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import forbidden
from redbot.core import commands
from redbot.core._cli import parse_cli_flags
from redbot.core._events import init_events
from test_audio_hybrid import red_command_runtime as runtime_fixture
from test_cog_hybrid import invoke_slash

from backupplus import BackupPlus
from backupplus.constants import MAX_FILE, __red_end_user_data_statement__
from backupplus.restore import build_plan, channel_kwargs, execute, permissions_for
from backupplus.snapshot import capture, encode, parse, portable

red_command_runtime = runtime_fixture


def role(guild, identifier, *, name="Member", permissions=0, position=1, managed=False):
    return discord.Role(
        guild=guild,
        state=Mock(),
        data={
            "id": str(identifier),
            "name": name,
            "permissions": str(permissions),
            "position": position,
            "colors": {"primary_color": 0x123456},
            "hoist": False,
            "mentionable": False,
            "managed": managed,
        },
    )


def sdk_channel(guild, identifier, *, kind=0, parent=None, overwrites=None):
    payload = {
        "id": str(identifier),
        "type": kind,
        "name": f"channel-{identifier}",
        "position": 0,
        "parent_id": str(parent) if parent else None,
        "permission_overwrites": overwrites or [],
        "topic": "Useful discussion",
        "nsfw": False,
        "rate_limit_per_user": 0,
        "default_auto_archive_duration": 1440,
        "default_thread_rate_limit_per_user": 0,
        "bitrate": 64000,
        "user_limit": 0,
        "rtc_region": None,
        "video_quality_mode": 1,
        "available_tags": [
            {
                "id": "6001",
                "name": "Solved",
                "moderated": False,
                "emoji_id": None,
                "emoji_name": "✅",
            }
        ],
        "default_sort_order": None,
        "default_forum_layout": 0,
        "default_reaction_emoji": {"emoji_name": "⭐", "emoji_id": None},
        "flags": 16 if kind in {15, 16} else 0,
    }
    cls = {
        0: discord.TextChannel,
        2: discord.VoiceChannel,
        4: discord.CategoryChannel,
        5: discord.TextChannel,
        13: discord.StageChannel,
        15: discord.ForumChannel,
        16: discord.ForumChannel,
    }[kind]
    return cls(state=Mock(), guild=guild, data=payload)


def structure(guild):
    guild.features = ["COMMUNITY"]
    guild.bitrate_limit = 96000
    guild.owner_id = 42
    guild.roles = [
        role(guild, guild.id, name="@everyone", position=0),
        role(guild, 201, permissions=discord.Permissions(send_messages=True).value),
        role(guild, 202, name="Staff", position=2),
        role(
            guild,
            200,
            name="Scarlet",
            permissions=discord.Permissions.all().value,
            position=10,
            managed=True,
        ),
    ]
    guild.default_role = guild.roles[0]
    guild.get_role.side_effect = lambda identifier: next(
        (item for item in guild.roles if item.id == identifier), None
    )
    guild.me.roles = [guild.default_role, guild.roles[-1]]
    guild.me.top_role = guild.roles[-1]
    guild.channels = [
        sdk_channel(guild, 5001, kind=4),
        sdk_channel(
            guild,
            5002,
            parent=5001,
            overwrites=[
                {
                    "id": str(guild.id),
                    "type": 0,
                    "allow": "0",
                    "deny": str(discord.Permissions(view_channel=True).value),
                },
                {
                    "id": "777777777777777777",
                    "type": 1,
                    "allow": str(discord.Permissions(view_channel=True).value),
                    "deny": "0",
                },
            ],
        ),
    ]
    guild.fetch_roles = AsyncMock(side_effect=lambda: list(guild.roles))
    guild.fetch_channels = AsyncMock(side_effect=lambda: list(guild.channels))
    guild.fetch_member = AsyncMock(side_effect=lambda identifier: guild.get_member(identifier))
    return guild


def snapshot(guild):
    return capture(guild, guild.roles, guild.channels)


def structure_with_unknown_permissions(guild):
    structure(guild)
    unknown_allow, unknown_deny = 1 << 47, 1 << 63
    assert not (unknown_allow | unknown_deny) & discord.Permissions.all().value
    guild.roles[1]._permissions |= unknown_allow | unknown_deny
    guild.channels[1]._fill_overwrites(
        {
            "permission_overwrites": [
                {
                    "id": str(guild.id),
                    "type": 0,
                    "allow": "0",
                    "deny": str(discord.Permissions(view_channel=True).value),
                },
                {
                    "id": "201",
                    "type": 0,
                    "allow": str(unknown_allow | discord.Permissions(send_messages=True).value),
                    "deny": str(unknown_deny),
                },
                {
                    "id": "777777777777777777",
                    "type": 1,
                    "allow": str(unknown_deny | discord.Permissions(view_channel=True).value),
                    "deny": str(unknown_allow),
                },
            ]
        }
    )
    return snapshot(guild)


def fake_mutations(guild, monkeypatch):
    """Network substitutes mutate current server objects, retaining SDK interfaces."""
    counter = [8000]
    calls = []

    async def edit_role(item, **kwargs):
        calls.append(("role-edit", item.id, kwargs))
        for key, value in kwargs.items():
            if key == "reason":
                continue
            if key == "permissions":
                item._permissions = value.value
            elif key in {"colour", "secondary_colour", "tertiary_colour"}:
                setattr(item, "_" + key, value.value if value is not None else None)
            else:
                setattr(item, key, value)
        return item

    async def create_role(**kwargs):
        counter[0] += 1
        item = role(
            guild,
            counter[0],
            name=kwargs["name"],
            permissions=kwargs["permissions"].value,
            position=1,
        )
        guild.roles.append(item)
        calls.append(("role-create", item.id, kwargs))
        return item

    async def edit_channel(item, **kwargs):
        calls.append(("channel-edit", item.id, kwargs))
        for key, value in kwargs.items():
            if key in {"reason", "overwrites", "require_tag", "type"}:
                continue
            if key == "category":
                item.category_id = value.id if value else None
            else:
                setattr(item, key, value)
        return item

    async def create_channel(kind, **kwargs):
        counter[0] += 1
        overwrites = [
            {
                "id": str(target.id),
                "type": 0 if isinstance(target, discord.Role) else 1,
                "allow": str(overwrite.pair()[0].value),
                "deny": str(overwrite.pair()[1].value),
            }
            for target, overwrite in kwargs["overwrites"].items()
        ]
        item = sdk_channel(
            guild,
            counter[0],
            kind=kind,
            parent=kwargs.get("category").id if kwargs.get("category") else None,
            overwrites=overwrites,
        )
        item.name = kwargs["name"]
        guild.channels.append(item)
        calls.append(("channel-create", item.id, kwargs))
        return item

    async def reorder(*, positions, reason):
        calls.append(("order", 0, positions))
        for item, position in positions.items():
            item.position = position
        return list(guild.roles)

    monkeypatch.setattr(discord.Role, "edit", edit_role)
    for cls in (
        discord.TextChannel,
        discord.CategoryChannel,
        discord.VoiceChannel,
        discord.StageChannel,
        discord.ForumChannel,
    ):
        monkeypatch.setattr(cls, "edit", edit_channel)
    guild.create_role = AsyncMock(side_effect=create_role)
    guild.edit_role_positions = AsyncMock(side_effect=reorder)
    for method, kind in (
        ("create_category", 4),
        ("create_text_channel", 0),
        ("create_voice_channel", 2),
        ("create_stage_channel", 13),
        ("create_forum", 15),
    ):

        async def create(*, _kind=kind, **kwargs):
            return await create_channel(_kind, **kwargs)

        setattr(guild, method, AsyncMock(side_effect=create))
    return calls


@pytest.fixture
async def backup_runtime(red_command_runtime, monkeypatch):
    bot, audio, member, invoke = red_command_runtime
    init_events(bot, parse_cli_flags([]))
    monkeypatch.setattr(bot, "_delete_delay", AsyncMock())
    bot.owner_ids.add(member.id)
    bot._connection._guilds[member.guild.id] = member.guild
    structure(member.guild)
    member.guild_permissions = discord.Permissions.all()
    received = []

    async def dm(*args, **kwargs):
        upload = kwargs.get("file")
        received.append(
            (
                kwargs.get("embed"),
                upload.filename if upload else None,
                upload.fp.read() if upload else None,
            )
        )
        return SimpleNamespace(edit=AsyncMock())

    member.send = AsyncMock(side_effect=dm)
    cog = BackupPlus(bot)
    await bot.add_cog(cog)
    try:
        yield bot, cog, member, invoke, received
    finally:
        await bot.remove_cog("BackupPlus")


def test_capture_sdk_objects_preserves_uncached_overwrites_and_all_supported_kinds(guild):
    structure(guild)
    for kind in (2, 5, 13, 15, 16):
        guild.channels.append(sdk_channel(guild, 7000 + kind, kind=kind, parent=5001))
    data = snapshot(guild)
    assert len(data["roles"]) == 4 and len(data["channels"]) == 7
    assert {row["kind"] for row in data["channels"]} == {
        "category",
        "text",
        "news",
        "voice",
        "stage",
        "forum",
        "media",
    }
    assert data["channels"][1]["overwrites"][0]["id"] == "777777777777777777"
    assert parse(encode(data), guild.id) == data
    assert "messages" not in data
    member_target = next(
        target for target in guild.channels[1].overwrites if isinstance(target, discord.Object)
    )
    assert member_target.type in {discord.abc.User, discord.User}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(guild_id="456"),
        lambda data: data.update(schema=True),
        lambda data: data["roles"].append(data["roles"][0]),
        lambda data: data["channels"][1].update(category="9999"),
        lambda data: data["channels"][1]["overwrites"][0].update(allow=True),
        lambda data: data["roles"][1].update(permissions=1 << 64),
        lambda data: data["roles"][1].update(permissions=-1),
        lambda data: data["roles"][1].update(permissions=True),
        lambda data: data["channels"][1]["overwrites"][0].update(allow=-1),
        lambda data: data["channels"][1]["overwrites"][0].update(deny=1 << 64),
        lambda data: data["channels"][1]["overwrites"][0].update(allow=1 << 63, deny=1 << 63),
        lambda data: data["channels"][1].update(url="https://evil.invalid"),
        lambda data: data.update(created_at=float("nan")),
    ],
)
def test_import_rejects_invalid_structure(guild, mutation):
    structure(guild)
    data = snapshot(guild)
    mutation(data)
    raw = json.dumps(data).encode()
    with pytest.raises(commands.BadArgument):
        parse(raw, guild.id)


def test_import_rejects_duplicate_keys_malformed_and_large_files(guild):
    for raw in (b'{"schema":1,"schema":1}', b"\xff", b"{" * 2000, b" " * (MAX_FILE + 1)):
        with pytest.raises(commands.BadArgument):
            parse(raw, guild.id)


def test_snapshot_preserves_raw_masks_and_unknown_targets_without_loss(guild):
    data = structure_with_unknown_permissions(guild)
    assert data["roles"][1]["permissions"] == guild.roles[1].permissions.value
    assert data["roles"][1]["permissions"] & (1 << 63)
    rows = {item["id"]: item for item in data["channels"][1]["overwrites"]}
    assert rows["201"]["allow"] & (1 << 47) and rows["201"]["deny"] == 1 << 63
    assert rows["777777777777777777"]["allow"] & (1 << 63)
    assert rows["777777777777777777"]["deny"] == 1 << 47
    assert parse(encode(data), guild.id) == data
    mappings = {"roles": {}, "channels": {}}
    assert portable(data, mappings) == data
    plan = build_plan(guild, data, snapshot(guild), mappings)
    assert not plan.actions and not plan.blockers


@pytest.mark.parametrize("problem", ["missing_records", "unknown_target", "missing_mask"])
def test_capture_refuses_unavailable_or_unrecognized_raw_overwrite_records(guild, problem):
    structure(guild)
    channel = guild.channels[1]
    if problem == "missing_records":
        del channel._overwrites
    elif problem == "unknown_target":
        channel._overwrites[0].type = 2
    else:
        del channel._overwrites[0].allow
    with pytest.raises(commands.BadArgument):
        snapshot(guild)


@pytest.mark.parametrize("operation", ["create", "edit"])
async def test_actual_sdk_restore_requests_preserve_unknown_masks(guild, operation):
    data = structure_with_unknown_permissions(guild)
    if operation == "create":
        guild.roles.pop(1)
        guild.channels.pop(1)
    else:
        guild.roles[1]._permissions = discord.Permissions(send_messages=True).value
        guild.channels[1]._fill_overwrites({"permission_overwrites": []})
    mappings = {"roles": {}, "channels": {}}
    plan = build_plan(guild, data, snapshot(guild), mappings)
    assert not plan.blockers
    role_id, channel_id = (8001, 8002) if operation == "create" else (201, 5002)

    def role_response(*args, **kwargs):
        return {
            "id": str(role_id),
            "name": "Member",
            "permissions": kwargs["permissions"],
            "position": 1,
            "colors": {"primary_color": 0x123456},
            "hoist": False,
            "mentionable": False,
            "managed": False,
        }

    def channel_response(*args, **kwargs):
        return {
            "id": str(channel_id),
            "type": 0,
            "name": "channel-5002",
            "position": 0,
            "parent_id": "5001",
            "permission_overwrites": kwargs["permission_overwrites"],
        }

    http = SimpleNamespace(
        create_role=AsyncMock(side_effect=role_response),
        edit_role=AsyncMock(side_effect=role_response),
        create_channel=AsyncMock(side_effect=channel_response),
        edit_channel=AsyncMock(side_effect=channel_response),
    )
    guild._state = SimpleNamespace(http=http)
    guild._channels = {}
    guild._create_channel = MethodType(discord.Guild._create_channel, guild)
    guild.create_text_channel = MethodType(discord.Guild.create_text_channel, guild)
    guild.create_role = MethodType(discord.Guild.create_role, guild)
    guild.edit_role_positions = AsyncMock()
    for item in [*guild.roles, *guild.channels]:
        item._state = guild._state
    roles = {str(item.id): item for item in guild.roles}
    channels = {str(item.id): item for item in guild.channels}

    async def remember(kind, source, target, **kwargs):
        if target is not None:
            mappings[kind + "s"][source] = target

    await execute(guild, plan, roles, channels, AsyncMock(), remember, AsyncMock(), reason="test")
    role_call = http.create_role if operation == "create" else http.edit_role
    channel_call = http.create_channel if operation == "create" else http.edit_channel
    assert int(role_call.await_args.kwargs["permissions"]) == data["roles"][1]["permissions"]
    restored = capture(guild, list(roles.values()), list(channels.values()))
    expected = portable(data, mappings)
    restored_role = next(row for row in restored["roles"] if row["id"] == str(role_id))
    assert restored_role["permissions"] == data["roles"][1]["permissions"]
    restored_channel = next(row for row in restored["channels"] if row["id"] == str(channel_id))
    assert restored_channel["overwrites"] == expected["channels"][1]["overwrites"]
    assert len(channel_call.await_args.kwargs["permission_overwrites"]) == 3


def test_plan_readonly_role_hierarchy_and_missing_managed_overwrites(guild):
    structure(guild)
    original = snapshot(guild)
    desired = deepcopy(original)
    desired["roles"][1]["name"] = "New name"
    desired["roles"][-1]["name"] = "Protected bot"
    plan = build_plan(guild, desired, original, {"roles": {}, "channels": {}})
    assert not plan.blockers and len(plan.actions) == 1
    assert "left unchanged" in plan.warnings[0]
    assert guild.roles[1].name == "Member" and desired["roles"][1]["name"] == "New name"
    missing = role(guild, 203, managed=True, position=3)
    guild.roles.append(missing)
    saved = snapshot(guild)
    saved["channels"][1]["overwrites"].append(
        {"id": "203", "kind": "role", "allow": 0, "deny": 1024}
    )
    guild.roles.remove(missing)
    plan = build_plan(guild, saved, snapshot(guild), {"roles": {}, "channels": {}})
    assert any("missing managed role" in row for row in plan.blockers)


def test_category_propagation_reapplies_saved_children_and_blocks_extra_synced_channels(guild):
    structure(guild)
    guild.channels[1] = sdk_channel(guild, 5002, parent=5001)
    live = snapshot(guild)
    desired = deepcopy(live)
    desired["channels"][0]["overwrites"] = [
        {"id": str(guild.id), "kind": "role", "allow": 0, "deny": 1024}
    ]
    plan = build_plan(guild, desired, live, {"roles": {}, "channels": {}})
    assert not plan.blockers and len(plan.actions) == 2
    assert "overwrites" in plan.actions[1]["changes"]
    guild.channels.append(sdk_channel(guild, 5003, parent=5001))
    plan = build_plan(guild, desired, snapshot(guild), {"roles": {}, "channels": {}})
    assert any("outside this snapshot" in row for row in plan.blockers)


def test_bot_access_calculation_and_permission_grant_blocks(guild):
    structure(guild)
    row = snapshot(guild)["channels"][1]
    permissions = permissions_for(
        discord.Permissions(manage_channels=True, manage_roles=True, view_channel=True).value,
        row,
        guild.me.id,
        {str(guild.id)},
        guild.id,
    )
    assert not permissions.view_channel
    assert permissions_for(
        discord.Permissions(administrator=True).value, row, guild.me.id, set(), guild.id
    ).view_channel
    guild.roles[-1]._permissions = discord.Permissions(
        manage_roles=True, manage_channels=True, view_channel=True
    ).value
    live = snapshot(guild)
    desired = deepcopy(live)
    desired["roles"][1]["permissions"] = discord.Permissions(administrator=True).value
    plan = build_plan(guild, desired, live, {"roles": {}, "channels": {}})
    assert any("cannot grant" in row for row in plan.blockers)


async def test_real_prefix_and_slash_commands_save_privately_and_honor_parent_checks(
    backup_runtime, monkeypatch
):
    bot, cog, member, invoke, received = backup_runtime
    ctx = await invoke("!backup create baseline")
    assert not ctx.command_failed
    state = await cog.config.guild(member.guild).state()
    assert set(state["snapshots"]) == {"baseline"}
    ctx = await invoke("!backup download baseline")
    assert not ctx.command_failed and received[-1][1] == "server-backup-baseline.json"
    assert parse(received[-1][2], member.guild.id)["guild_id"] == str(member.guild.id)
    assert all("file" not in call.kwargs for call in ctx.send.await_args_list)
    ctx = await invoke_slash(bot, invoke, monkeypatch, "backup create", name="slashcopy")
    assert not ctx.command_failed and ctx.defer.await_args.kwargs["ephemeral"]
    member.guild_permissions = discord.Permissions(manage_guild=True)
    ctx = await invoke("!backup create denied")
    assert ctx.command_failed
    assert "denied" not in (await cog.config.guild(member.guild).state())["snapshots"]
    member.guild_permissions = discord.Permissions.all()
    cog.backup.disable_in(member.guild)
    ctx = await invoke_slash(bot, invoke, monkeypatch, "backup create", name="disabled")
    assert ctx.command_failed
    assert "disabled" not in (await cog.config.guild(member.guild).state())["snapshots"]


@pytest.mark.parametrize("slash", [False, True])
async def test_create_and_download_unknown_permission_masks(backup_runtime, monkeypatch, slash):
    bot, cog, member, invoke, received = backup_runtime
    expected = structure_with_unknown_permissions(member.guild)
    if slash:
        ctx = await invoke_slash(bot, invoke, monkeypatch, "backup create", name="baseline")
    else:
        ctx = await invoke("!backup create baseline")
    assert not ctx.command_failed
    record = (await cog.config.guild(member.guild).state())["snapshots"]["baseline"]
    assert record["data"]["roles"] == expected["roles"]
    assert record["data"]["channels"] == expected["channels"]
    ctx = await invoke("!backup download baseline")
    assert not ctx.command_failed
    exported = parse(received[-1][2], member.guild.id)
    assert exported == record["data"]


async def test_preview_is_private_readonly_and_restore_requires_same_requester_and_fresh_server(
    backup_runtime,
):
    bot, cog, member, invoke, received = backup_runtime
    await invoke("!backup create baseline")
    member.guild.roles[1].name = "Changed"
    before = await cog.config.guild(member.guild).state()
    ctx = await invoke("!backup preview baseline")
    assert not ctx.command_failed and received[-1][1] == "restore-plan-baseline.json"
    assert len(json.loads(received[-1][2])["actions"]) == 1
    assert await cog.config.guild(member.guild).state() == before
    preview = cog._previews[(member.guild.id, member.id)]
    member.guild.roles[1].name = "Changed again"
    ctx = await invoke(f"!backup restore baseline {preview['token']}")
    assert ctx.command_failed and not cog._active
    assert await cog.config.guild(member.guild).state() == before
    ctx = await invoke("!backup restore baseline 😀")
    assert ctx.command_failed
    preview["expires"] = time.monotonic() - 1
    ctx = await invoke(f"!backup restore baseline {preview['token']}")
    assert ctx.command_failed


async def test_restore_edits_roles_saves_safety_snapshot_and_keeps_unbacked_objects(
    backup_runtime, monkeypatch
):
    bot, cog, member, invoke, received = backup_runtime
    calls = fake_mutations(member.guild, monkeypatch)
    await invoke("!backup create baseline")
    member.guild.roles[1].name = "Changed"
    extra = sdk_channel(member.guild, 5010)
    member.guild.channels.append(extra)
    await invoke("!backup preview baseline")
    token = cog._previews[(member.guild.id, member.id)]["token"]
    ctx = await invoke(f"!backup restore baseline {token}")
    assert not ctx.command_failed, ctx.send.await_args
    state = await cog.config.guild(member.guild).state()
    assert state["last_restore"]["state"] == "complete" and state["last_restore"]["completed"] == 1
    assert member.guild.roles[1].name == "Member" and extra in member.guild.channels
    safety = state["snapshots"][state["last_restore"]["safety"]]["data"]
    assert safety["roles"][1]["name"] == "Changed"
    assert calls[0][0] == "role-edit" and not cog._previews


async def test_missing_roles_and_categories_are_mapped_before_private_channel_creation(
    backup_runtime, monkeypatch
):
    bot, cog, member, invoke, received = backup_runtime
    calls = fake_mutations(member.guild, monkeypatch)
    await invoke("!backup create baseline")
    member.guild.roles.pop(1)
    member.guild.channels = []
    await invoke("!backup preview baseline")
    token = cog._previews[(member.guild.id, member.id)]["token"]
    ctx = await invoke(f"!backup restore baseline {token}")
    assert not ctx.command_failed, ctx.send.await_args
    state = await cog.config.guild(member.guild).state()
    record = state["snapshots"]["baseline"]
    assert record["mappings"]["roles"]["201"] and set(record["mappings"]["channels"]) == {
        "5001",
        "5002",
    }
    assert not record["pending"]
    create = next(
        item
        for item in calls
        if item[0] == "channel-create" and item[2].get("category") is not None
    )
    assert create[2]["category"].id == int(record["mappings"]["channels"]["5001"])
    everyone = next(
        overwrite
        for target, overwrite in create[2]["overwrites"].items()
        if target.id == member.guild.id
    )
    assert everyone.view_channel is False
    assert any(
        isinstance(target, discord.Object) and target.type is discord.User
        for target in create[2]["overwrites"]
    )
    exported = portable(record["data"], record["mappings"])
    assert exported["roles"][1]["id"] == record["mappings"]["roles"]["201"]
    assert exported["channels"][1]["category"] == record["mappings"]["channels"]["5001"]


async def test_unknown_create_result_blocks_retries_and_known_rejections_can_retry(
    backup_runtime, monkeypatch
):
    bot, cog, member, invoke, received = backup_runtime
    fake_mutations(member.guild, monkeypatch)
    await invoke("!backup create baseline")
    member.guild.roles.pop(1)
    member.guild.create_role.side_effect = asyncio.TimeoutError("secret provider payload")
    await invoke("!backup preview baseline")
    token = cog._previews[(member.guild.id, member.id)]["token"]
    ctx = await invoke(f"!backup restore baseline {token}")
    state = await cog.config.guild(member.guild).state()
    assert state["last_restore"]["state"] == "partial"
    assert state["snapshots"]["baseline"]["pending"] == {"role:201": True}
    assert "secret" not in str(ctx.send.await_args)
    await invoke("!backup preview baseline")
    assert not cog._previews
    replacement = role(member.guild, 9000)
    member.guild.roles.append(replacement)
    ctx = await invoke("!backup bind baseline role 201 9000")
    assert not ctx.command_failed
    state = await cog.config.guild(member.guild).state()
    assert not state["snapshots"]["baseline"]["pending"]
    assert state["snapshots"]["baseline"]["mappings"]["roles"]["201"] == "9000"
    before = member.guild.create_role.await_count
    await invoke("!backup preview baseline")
    token = cog._previews[(member.guild.id, member.id)]["token"]
    await invoke(f"!backup restore baseline {token}")
    assert member.guild.create_role.await_count == before


async def test_storage_limits_auto_rotation_and_failed_import_leave_originals(backup_runtime):
    bot, cog, member, invoke, received = backup_runtime
    for number in range(5):
        assert not (await invoke(f"!backup create save{number}")).command_failed
    original = await cog.config.guild(member.guild).state()
    assert (await invoke("!backup create overflow")).command_failed
    assert await cog.config.guild(member.guild).state() == original
    assert (await invoke("!backup create auto-fake")).command_failed
    await invoke("!backup auto 24")
    for _ in range(4):
        state = await cog.config.guild(member.guild).state()
        state["last_attempt"] = 0
        await cog._save(member.guild, state)
        await cog._automatic_once()
    state = await cog.config.guild(member.guild).state()
    assert sum(record["category"] == "auto" for record in state["snapshots"].values()) == 3
    assert sum(record["category"] == "manual" for record in state["snapshots"].values()) == 5
    assert (await invoke("!backup auto 5")).command_failed
    await invoke("!backup auto 0")
    assert not (await cog.config.guild(member.guild).state())["auto_hours"]


async def test_private_failure_rechecks_admin_and_never_posts_snapshot_publicly(backup_runtime):
    bot, cog, member, invoke, received = backup_runtime
    await invoke("!backup create baseline")
    member.send.side_effect = forbidden()
    ctx = await invoke("!backup preview baseline")
    assert ctx.command_failed and not cog._previews
    assert all("file" not in call.kwargs for call in ctx.send.await_args_list)
    member.guild_permissions = discord.Permissions.none()
    ctx = await invoke("!backup download baseline")
    assert ctx.command_failed


async def test_user_data_hooks_are_scoped_and_remove_whole_affected_snapshots(backup_runtime):
    bot, cog, member, invoke, received = backup_runtime
    await invoke("!backup create baseline")
    identifier = 777777777777777777
    data = await cog.red_get_data_for_user(user_id=identifier)
    rows = json.loads(data["backupplus-overwrites.json"].getvalue())
    assert len(rows) == 1 and rows[0]["id"] == str(identifier)
    assert not await cog.red_get_data_for_user(user_id=member.id)
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=identifier)
    assert not (await cog.config.guild(member.guild).state())["snapshots"]
    assert (
        json.loads(Path("backupplus/info.json").read_text())["end_user_data_statement"]
        == __red_end_user_data_statement__
    )


async def test_actual_sdk_role_edit_clears_colours_with_supported_api_payload(guild):
    structure(guild)
    current = guild.roles[1]
    saved = snapshot(guild)
    current._secondary_colour = 0xABCDEF
    current._tertiary_colour = 0xFEDCBA
    response = {
        "id": str(current.id),
        "name": current.name,
        "permissions": str(current.permissions.value),
        "position": current.position,
        "colors": {
            "primary_color": current.colour.value,
            "secondary_color": None,
            "tertiary_color": None,
        },
    }
    http = SimpleNamespace(edit_role=AsyncMock(return_value=response))
    current._state = SimpleNamespace(http=http)
    plan = build_plan(guild, saved, snapshot(guild), {"roles": {}, "channels": {}})
    await execute(
        guild,
        plan,
        {str(item.id): item for item in guild.roles},
        {str(item.id): item for item in guild.channels},
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        reason="test",
    )
    assert http.edit_role.await_args.kwargs["colors"] == {
        "secondary_color": None,
        "tertiary_color": None,
    }


def test_forum_tag_recovery_uses_existing_ids_and_voice_limit_validation(guild):
    structure(guild)
    forum = sdk_channel(guild, 5015, kind=15, parent=5001)
    guild.channels.append(forum)
    data = snapshot(guild)
    row = next(item for item in data["channels"] if item["id"] == "5015")
    forum.available_tags[0].id = 7001
    plan = build_plan(guild, data, snapshot(guild), {"roles": {}, "channels": {}})
    assert not plan.actions
    values = channel_kwargs(
        row,
        {"roles": {}, "channels": {}},
        {str(item.id): item for item in guild.roles},
        {str(item.id): item for item in guild.channels},
        current=forum,
    )
    assert values["available_tags"][0].id == 7001
    assert isinstance(values["default_layout"], discord.ForumLayoutType)
    assert values["require_tag"]
    guild.channels.append(sdk_channel(guild, 5016, kind=2))
    data = snapshot(guild)
    next(item for item in data["channels"] if item["id"] == "5016")["user_limit"] = 100
    with pytest.raises(commands.BadArgument):
        parse(encode(data), guild.id)


async def test_definitive_create_rejection_removes_uncertainty_and_preserves_original(
    backup_runtime, monkeypatch
):
    bot, cog, member, invoke, received = backup_runtime
    fake_mutations(member.guild, monkeypatch)
    await invoke("!backup create baseline")
    member.guild.roles.pop(1)
    member.guild.create_role.side_effect = forbidden()
    await invoke("!backup preview baseline")
    token = cog._previews[(member.guild.id, member.id)]["token"]
    await invoke(f"!backup restore baseline {token}")
    state = await cog.config.guild(member.guild).state()
    assert not state["snapshots"]["baseline"]["pending"]
    assert state["last_restore"]["state"] == "partial"
    assert state["snapshots"]["baseline"]["data"]["roles"][1]["id"] == "201"
    await invoke("!backup preview baseline")
    assert cog._previews


async def test_restore_rechecks_admin_between_mutations(backup_runtime, monkeypatch):
    bot, cog, member, invoke, received = backup_runtime
    calls = fake_mutations(member.guild, monkeypatch)
    original_edit = discord.Role.edit

    async def revoke(item, **kwargs):
        result = await original_edit(item, **kwargs)
        member.guild_permissions = discord.Permissions.none()
        return result

    monkeypatch.setattr(discord.Role, "edit", revoke)
    await invoke("!backup create baseline")
    member.guild.roles[1].name = "First changed"
    member.guild.roles[2].name = "Second changed"
    await invoke("!backup preview baseline")
    token = cog._previews[(member.guild.id, member.id)]["token"]
    await invoke(f"!backup restore baseline {token}")
    state = await cog.config.guild(member.guild).state()
    assert state["last_restore"]["state"] == "partial" and state["last_restore"]["completed"] == 1
    assert len(calls) == 1 and member.guild.roles[2].name == "Second changed"


async def test_cancel_and_reload_preserve_uncertain_creates_and_clear_owned_tasks(
    backup_runtime, monkeypatch
):
    bot, cog, member, invoke, received = backup_runtime
    fake_mutations(member.guild, monkeypatch)
    await invoke("!backup create baseline")
    member.guild.roles.pop(1)
    entered = asyncio.Event()

    async def pending(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    member.guild.create_role.side_effect = pending
    await invoke("!backup preview baseline")
    token = cog._previews[(member.guild.id, member.id)]["token"]
    restore = asyncio.create_task(invoke(f"!backup restore baseline {token}"))
    await asyncio.wait_for(entered.wait(), 1)
    assert cog._active
    assert not (await invoke("!backup cancel")).command_failed
    await asyncio.gather(restore, return_exceptions=True)
    state = await cog.config.guild(member.guild).state()
    assert state["last_restore"]["state"] == "cancelled" and state["snapshots"]["baseline"][
        "pending"
    ] == {"role:201": True}
    assert not cog._active and not cog._tasks
    state["last_restore"]["state"] = "running"
    await cog._save(member.guild, state)
    await bot.remove_cog("BackupPlus")
    assert cog._automatic.done()
    replacement = BackupPlus(bot)
    await bot.add_cog(replacement)
    state = await replacement.config.guild(member.guild).state()
    assert state["last_restore"]["state"] == "interrupted"
    assert state["snapshots"]["baseline"]["pending"] == {"role:201": True}
    assert not (await invoke("!backup status")).command_failed


async def test_cancel_staging_before_safety_capture_does_not_start_mutations(
    backup_runtime, monkeypatch
):
    bot, cog, member, invoke, received = backup_runtime
    calls = fake_mutations(member.guild, monkeypatch)
    await invoke("!backup create baseline")
    member.guild.roles[1].name = "Changed"
    await invoke("!backup preview baseline")
    token = cog._previews[(member.guild.id, member.id)]["token"]
    entered = asyncio.Event()
    original_store = cog._store

    async def staging(*args, **kwargs):
        if kwargs.get("category") == "safety":
            entered.set()
            await asyncio.Event().wait()
        return await original_store(*args, **kwargs)

    monkeypatch.setattr(cog, "_store", staging)
    restore = asyncio.create_task(invoke(f"!backup restore baseline {token}"))
    await asyncio.wait_for(entered.wait(), 1)
    await invoke("!backup cancel")
    await asyncio.gather(restore, return_exceptions=True)
    assert not calls and not cog._active and not cog._tasks
    assert not (await cog.config.guild(member.guild).state())["last_restore"]


@pytest.mark.parametrize("cleanup", ["unload", "user", "guild"])
async def test_cleanup_cancels_pending_private_transfers(backup_runtime, cleanup):
    bot, cog, member, invoke, received = backup_runtime
    await invoke("!backup create baseline")
    entered = asyncio.Event()
    uploads = []

    async def pending(*args, **kwargs):
        uploads.append(kwargs["file"])
        entered.set()
        await asyncio.Event().wait()

    member.send.side_effect = pending
    download = asyncio.create_task(invoke("!backup download baseline"))
    await asyncio.wait_for(entered.wait(), 1)
    if cleanup == "unload":
        await bot.remove_cog("BackupPlus")
    elif cleanup == "user":
        await cog.red_delete_data_for_user(requester="user", user_id=777777777777777777)
    else:
        await cog.on_guild_remove(member.guild)
    await asyncio.gather(download, return_exceptions=True)
    assert download.done() and uploads[0].fp.closed and not cog._tasks
    if cleanup != "unload":
        assert not (await cog.config.guild(member.guild).state())["snapshots"]


async def test_auto_failure_cursor_and_byte_budget_do_not_overwrite_saved_records(
    backup_runtime, monkeypatch
):
    bot, cog, member, invoke, received = backup_runtime
    await invoke("!backup create baseline")
    before = await cog.config.guild(member.guild).state()
    monkeypatch.setattr("backupplus.cog.MAX_STATE", 1)
    ctx = await invoke("!backup create overflow")
    assert ctx.command_failed and await cog.config.guild(member.guild).state() == before
    monkeypatch.setattr("backupplus.cog.MAX_STATE", 21 * 1024 * 1024)
    await invoke("!backup auto 24")
    state = await cog.config.guild(member.guild).state()
    state["last_attempt"] = 0
    await cog._save(member.guild, state)
    member.guild.fetch_channels.side_effect = RuntimeError("secret error payload")
    await cog._automatic_once()
    state = await cog.config.guild(member.guild).state()
    assert state["last_error"] == "RuntimeError" and state["last_attempt"] > 0
    assert set(state["snapshots"]) == {"baseline"}
    attempts = member.guild.fetch_channels.await_count
    await cog._automatic_once()
    assert member.guild.fetch_channels.await_count == attempts


async def test_slash_import_attachment_validation_and_current_server_scope(
    backup_runtime, monkeypatch
):
    bot, cog, member, invoke, received = backup_runtime
    raw = encode(snapshot(member.guild))
    attachment = discord.Attachment(
        data={
            "id": "777777777777777778",
            "size": len(raw),
            "filename": "backup.json",
            "url": "https://cdn.discordapp.com/mock",
            "proxy_url": "https://media.discordapp.net/mock",
        },
        state=bot._connection,
    )
    monkeypatch.setattr(discord.Attachment, "read", AsyncMock(return_value=raw))
    ctx = await invoke_slash(
        bot, invoke, monkeypatch, "backup import", name="imported", attachment=attachment
    )
    assert not ctx.command_failed
    before = await cog.config.guild(member.guild).state()
    data = snapshot(member.guild)
    data["guild_id"] = "456"
    monkeypatch.setattr(discord.Attachment, "read", AsyncMock(return_value=encode(data)))
    ctx = await invoke_slash(
        bot, invoke, monkeypatch, "backup import", name="wrongserver", attachment=attachment
    )
    assert ctx.command_failed and await cog.config.guild(member.guild).state() == before


@pytest.mark.parametrize("method", ["auto", "delete"])
async def test_settings_writes_recheck_admin_after_waiting_for_server_lock(backup_runtime, method):
    bot, cog, member, invoke, received = backup_runtime
    await invoke("!backup create baseline")
    before = await cog.config.guild(member.guild).state()
    ctx = await invoke("!help")
    lock = cog._locks[member.guild.id]
    await lock.acquire()
    if method == "auto":
        task = asyncio.create_task(cog.backup_auto.callback(cog, ctx, 24))
    else:
        task = asyncio.create_task(cog.backup_delete.callback(cog, ctx, "baseline"))
    await asyncio.sleep(0)
    member.guild_permissions = discord.Permissions.none()
    lock.release()
    with pytest.raises(commands.CheckFailure):
        await task
    assert await cog.config.guild(member.guild).state() == before


async def test_confirmation_cannot_be_reused_by_another_administrator(backup_runtime):
    bot, cog, member, invoke, received = backup_runtime
    await invoke("!backup create baseline")
    member.guild.roles[1].name = "Changed"
    await invoke("!backup preview baseline")
    token = cog._previews[(member.guild.id, member.id)]["token"]
    ctx = await invoke("!help")
    ctx.author = SimpleNamespace(id=987654321012345678, guild_permissions=discord.Permissions.all())
    with pytest.raises(commands.CheckFailure):
        await cog.backup_restore.callback(cog, ctx, "baseline", token)
    assert member.guild.roles[1].name == "Changed"


async def test_capture_failure_cancels_and_awaits_sibling_discovery_request(bot, guild):
    structure(guild)
    entered, finished = asyncio.Event(), asyncio.Event()

    async def channels():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async def roles():
        await entered.wait()
        raise RuntimeError("transport failed")

    guild.fetch_roles.side_effect = roles
    guild.fetch_channels.side_effect = channels
    cog = BackupPlus(bot)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(cog._fetch(guild), 1)
    assert finished.is_set()
    assert not any(task.get_name().startswith("BackupPlus fetch") for task in asyncio.all_tasks())
