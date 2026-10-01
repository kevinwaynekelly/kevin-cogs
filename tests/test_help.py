"""Exercise Red's native help formatter so cog command listings cannot go blank."""

from unittest.mock import AsyncMock

import pytest
from conftest import make_context
from redbot.core.commands.help import HelpSettings, RedHelpFormatter

from audioplus import AudioPlus
from communityplus import CommunityPlus
from levelplus import LevelPlus
from logplus import LogPlus
from owoplus import OwoPlus

COGS = [AudioPlus, CommunityPlus, LevelPlus, LogPlus, OwoPlus]


def help_context(bot, guild, *, embeds):
    ctx = make_context(guild)
    ctx.bot = bot
    ctx.me = guild.me
    bot.description = "Red V3"
    bot.embed_requested = AsyncMock(return_value=embeds)
    bot.cogs = {cls.__name__: cls(bot) for cls in COGS}
    bot.commands = []
    for cog in bot.cogs.values():
        # Command injection normally attaches the cog when Red loads the package.
        for command in cog.walk_commands():
            command.cog = cog
        bot.commands.extend(cog.get_commands())
    return ctx


@pytest.mark.parametrize("cls", COGS)
def test_all_command_groups_and_subcommands_have_help(bot, guild, cls):
    cog = cls(bot)
    ctx = make_context(guild)
    ctx.bot = bot
    for command in cog.walk_commands():
        summary = command.format_shortdoc_for_context(ctx)
        assert summary.strip(), command.qualified_name
        assert summary in command.format_help_for_context(ctx), command.qualified_name
        assert "[p]" not in summary, command.qualified_name


@pytest.mark.parametrize("embeds", [True, False])
async def test_native_bot_help_lists_cog_descriptions(bot, guild, embeds):
    ctx = help_context(bot, guild, embeds=embeds)
    formatter = RedHelpFormatter()
    formatter.make_and_send_embeds = AsyncMock()
    formatter.send_pages = AsyncMock()
    settings = HelpSettings(verify_checks=False)
    await formatter.format_bot_help(ctx, help_settings=settings)
    if embeds:
        payload = formatter.make_and_send_embeds.await_args.args[1]
        output = "\n".join(field.value for field in payload["fields"])
    else:
        output = "\n".join(formatter.send_pages.await_args.args[1])
    for cog in bot.cogs.values():
        root = cog.get_commands()[0]
        assert root.name in output
        assert root.short_doc in output


@pytest.mark.parametrize("embeds", [True, False])
async def test_native_group_and_leaf_help_show_descriptions_and_syntax(bot, guild, embeds):
    ctx = help_context(bot, guild, embeds=embeds)
    formatter = RedHelpFormatter()
    formatter.make_and_send_embeds = AsyncMock()
    formatter.send_pages = AsyncMock()
    settings = HelpSettings(verify_checks=False, verify_exists=True)
    root = bot.cogs["CommunityPlus"].get_commands()[0]
    for command in (root, root.get_command("welcome"), root.get_command("welcome channel")):
        await formatter.format_command_help(ctx, command, help_settings=settings)
        if embeds:
            payload = formatter.make_and_send_embeds.await_args.args[1]
            output = (
                payload["embed"]["description"]
                + "\n"
                + "\n".join(field.name + "\n" + field.value for field in payload["fields"])
            )
        else:
            output = "\n".join(formatter.send_pages.await_args.args[1])
        assert command.short_doc in output
        assert f"!{command.qualified_name}" in output
        if command is root:
            assert root.get_command("autorole").short_doc in output
        elif command.name == "welcome":
            assert root.get_command("welcome preview").short_doc in output
        else:
            assert "Omit the channel" in output


async def test_native_help_keeps_hidden_commands_and_aliases_filtered(bot, guild):
    ctx = help_context(bot, guild, embeds=True)
    for command in bot.cogs["LogPlus"].get_commands():
        command.hidden = True
    formatter = RedHelpFormatter()
    formatter.make_and_send_embeds = AsyncMock()
    await formatter.format_bot_help(ctx, help_settings=HelpSettings(verify_checks=False))
    payload = formatter.make_and_send_embeds.await_args.args[1]
    assert not any("LogPlus" in field.name for field in payload["fields"])
    community = bot.cogs["CommunityPlus"].get_commands()[0]
    await formatter.format_command_help(
        ctx, community, help_settings=HelpSettings(verify_checks=False, verify_exists=True)
    )
    fields = formatter.make_and_send_embeds.await_args.args[1]["fields"]
    subcommands = "\n".join(field.value for field in fields if "Subcommands" in field.name)
    assert subcommands.count("**help**") == 1
    assert "**commands**" not in subcommands
    assert "**?**" not in subcommands
