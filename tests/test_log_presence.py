"""Retired presence controls cannot restart collection or survive an upgrade."""

from copy import deepcopy
from unittest.mock import AsyncMock

from conftest import make_context

from logplus import LogPlus
from logplus.cog import EVENT_SWITCHES
from settingshub.schema import select_fields


def test_presence_listener_and_toggle_are_not_registered(bot):
    cog = LogPlus(bot)
    assert "on_presence_update" not in dict(cog.get_listeners())
    root = next(command for command in cog.get_commands() if command.name == "log")
    assert root.get_command("toggle").get_command("member").get_command("presence") is None
    assert "member.presence" not in EVENT_SWITCHES


async def test_load_removes_saved_presence_flags_and_preserves_other_guild_settings(bot, guild):
    legacy = LogPlus(bot).config
    legacy.register_guild(member={"presence": True})
    cog = LogPlus(bot)
    assert cog.config is legacy
    assert "presence" not in cog.config.defaults["GUILD"]["member"]
    original = {}
    # Include a saved server that is absent from the bot's current guild cache.
    for guild_id, enabled in ((guild.id, True), (guild.id + 1, False)):
        conf = cog.config.guild_from_id(guild_id)
        member = await conf.member()
        member.update(presence=enabled, join=False)
        await conf.member.set(member)
        await conf.log_channel.set(123456789)
        await conf.features.retry.set(False)
        original[guild_id] = await conf.all()
    await cog._settings(guild)
    await cog.cog_load()
    try:
        for guild_id, before in original.items():
            expected = deepcopy(before)
            expected["member"].pop("presence")
            assert await cog.config.guild_from_id(guild_id).all() == expected
        assert "presence" not in (await cog._settings(guild))["member"]
        # Repeating the migration is harmless and does not restore the setting.
        await cog._migrate_removed_presence()
        assert await cog.config.guild_from_id(guild.id).all() == {
            **original[guild.id],
            "member": {k: v for k, v in original[guild.id]["member"].items() if k != "presence"},
        }
    finally:
        await cog.cog_unload()


async def test_removed_switch_is_rejected_and_legacy_policy_export_omits_it(bot, guild):
    cog = LogPlus(bot)
    cog._reply = AsyncMock()
    await LogPlus.event.callback(cog, make_context(guild), "member.presence", True)
    assert cog._reply.await_args.args[1].startswith("Unknown event.")
    assert "member.presence" not in cog._reply.await_args.args[1]
    assert "presence" not in await cog.config.guild(guild).member()
    legacy = await cog.config.guild(guild).all()
    legacy["member"]["presence"] = True
    projected = select_fields("LogPlus", legacy)
    assert "presence" not in projected["member"]
    assert legacy["member"]["presence"] is True
