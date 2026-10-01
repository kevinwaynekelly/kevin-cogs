"""Components repeat current Red checks for the actual clicking member."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_guild, make_member, make_message
from redbot.core import commands
from test_audio_hybrid import red_command_runtime as command_fixture

from audioplus.interactive import SetupView, component_context

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


def test_interactive_helpers_match_in_every_independent_cog():
    sources = [
        Path(importlib.import_module(f"{package}.interactive").__file__).read_text()
        for package in ("audioplus", "communityplus", "levelplus", "logplus", "owoplus")
    ]
    assert len(set(sources)) == 1


async def test_setup_panel_rejects_a_different_server(red_command_runtime, monkeypatch):
    bot, cog, member, invoke = red_command_runtime
    ctx = await invoke("!queue")
    update = AsyncMock()
    view = SetupView(cog, ctx, "audioset setup", [("panel", "Player panel", "toggle")], update)
    checked = AsyncMock(return_value=ctx)
    monkeypatch.setattr("audioplus.interactive.component_context", checked)
    interaction = SimpleNamespace(
        guild=make_guild(member.guild.id + 1),
        user=member,
        response=SimpleNamespace(is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    selector = view.children[0]
    selector._values = ["on"]
    await selector.callback(interaction)
    checked.assert_not_awaited()
    update.assert_not_awaited()
    assert "another server" in interaction.followup.send.await_args.args[0]
    view.stop()
    await view.on_timeout()
    assert view not in cog._views
