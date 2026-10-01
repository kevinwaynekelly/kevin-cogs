"""Member-selectable and automatic reward roles must not grant moderation."""

import importlib
from unittest.mock import Mock

import discord
import pytest
from redbot.core import commands


@pytest.mark.parametrize("package", ["communityplus", "levelplus"])
@pytest.mark.parametrize(
    "permission",
    [
        "manage_messages",
        "manage_threads",
        "manage_events",
        "manage_expressions",
        "manage_nicknames",
        "mute_members",
        "deafen_members",
        "move_members",
        "mention_everyone",
        "view_audit_log",
        "view_guild_insights",
    ],
)
def test_role_policy_rejects_privileged_roles(package, permission, guild):
    def role(uid, permissions, position):
        return discord.Role(
            guild=guild,
            state=Mock(),
            data={
                "id": str(uid),
                "name": "Role",
                "permissions": str(permissions),
                "position": position,
                "color": 0,
                "managed": False,
            },
        )

    guild.me.top_role = role(104, 0, 10)
    value = discord.Permissions(**{permission: True}).value
    unsafe = role(101, value, 1)
    with pytest.raises(commands.BadArgument):
        importlib.import_module(f"{package}.features").safe_role(guild, unsafe)
