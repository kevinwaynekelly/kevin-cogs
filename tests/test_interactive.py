"""Components repeat current Red checks for the actual clicking member."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_member, make_message
from redbot.core import commands
from test_audio_hybrid import red_command_runtime as command_fixture

from audioplus.interactive import component_context

red_command_runtime = command_fixture


async def test_component_uses_clicker_and_checks_saved_command_permission(red_command_runtime):
    bot, cog, member, invoke = red_command_runtime
    ctx = await invoke("!queue")
    another = make_member(member.guild, 444)
    message = make_message(another, ctx.channel, content="panel")
    message._state = bot._connection
    interaction = SimpleNamespace(
        guild=member.guild,
        user=member,
        message=message,
        response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock()),
    )
    checked = await component_context(cog, interaction, "queue")
    assert checked.author is member and message.author is another
    cog.queue.enabled = False
    with pytest.raises(commands.DisabledCommand):
        await component_context(cog, interaction, "queue")
    cog.queue.enabled = True
    with pytest.raises(commands.CheckFailure):
        await component_context(cog, interaction, "audioset setup")
    with pytest.raises(commands.CheckFailure):
        await component_context(cog, interaction, "queue", owner_id=another.id)
