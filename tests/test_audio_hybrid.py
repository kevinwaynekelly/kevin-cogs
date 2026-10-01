"""Register real hybrid commands and exercise both playback entry points."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext.commands import Bot
from redbot.core import commands
from redbot.core.core_commands import Core
from redbot.core.tree import RedTree
from test_audioplus import audio_runtime as native_runtime_fixture
from test_native_audio import eventually, track

from audioplus import AudioPlus

audio_runtime = native_runtime_fixture
CONTROLS = {
    "play",
    "join",
    "disconnect",
    "skip",
    "stop",
    "pause",
    "resume",
    "volume",
    "np",
    "queue",
    "shuffle",
    "repeat",
    "tone",
    "audiostatus",
    "playerstate",
    "debugvc",
    "speak",
    "undeafen",
    "fixvoice",
    "rejoin",
}


async def test_real_red_tree_registers_controls_without_replacing_core_leave():
    bot = Bot(command_prefix="!", intents=discord.Intents.none(), tree_cls=RedTree)
    original_leave = Core.leave.copy()
    bot.add_command(original_leave)
    cog = AudioPlus(bot)
    try:
        await bot.add_cog(cog)
        assert bot.get_command("leave") is original_leave
        assert set(bot.tree._disabled_global_commands) == CONTROLS
        for name in CONTROLS:
            command = bot.get_command(name)
            assert isinstance(command, commands.HybridCommand)
            assert command.cog is cog
            assert command.app_command.guild_only
            assert command.short_doc and command.app_command.description
            command.app_command.to_dict(bot.tree)
            with pytest.raises(commands.NoPrivateMessage):
                await command.checks[0](SimpleNamespace(guild=None))
        assert bot.get_command("p") is bot.get_command("play")
        assert bot.get_command("dc") is bot.get_command("disconnect")
        assert bot.get_command("audio play") is cog.audio_play
        await bot.remove_cog("AudioPlus")
        assert not bot.tree._disabled_global_commands
        assert bot.get_command("play") is None
        assert bot.get_command("leave") is original_leave
    finally:
        await bot.close()


async def test_direct_prefix_controls_operate_the_same_player(audio_runtime):
    cog, player, ctx = audio_runtime
    ctx.defer = AsyncMock()
    cog._resolver.search = AsyncMock(return_value=[track("selected song")])
    await cog.play.callback(cog, ctx, query="song")
    await eventually(lambda: player.playing)
    assert player.current.title == "selected song"
    assert "selected song" in ctx.send.await_args.kwargs["embed"].description
    await cog.pause.callback(cog, ctx)
    assert player.paused
    await cog.volume.callback(cog, ctx, value=250)
    assert player.volume == player.source.volume == 250
    await cog.resume.callback(cog, ctx)
    assert player.playing and not player.paused
    await cog.stop.callback(cog, ctx)
    await eventually(lambda: player._runner.done())
    assert not player.playing and not player.queue
    ctx.defer.assert_not_awaited()


async def test_slash_play_defers_before_lookup_and_uses_the_same_track_display(audio_runtime):
    cog, player, ctx = audio_runtime
    calls = []
    ctx.interaction = SimpleNamespace(response=SimpleNamespace(is_done=lambda: False))
    ctx.clean_prefix = "/"
    ctx.command = cog.play
    ctx.defer = AsyncMock(side_effect=lambda: calls.append("defer"))

    async def lookup(query):
        calls.append("lookup")
        return [track("slash song")]

    cog._resolver.search = AsyncMock(side_effect=lookup)
    app_command = cog.play.app_command
    arguments = await app_command._transform_arguments(
        ctx.interaction, SimpleNamespace(query="slash song")
    )
    await app_command._do_call(ctx, arguments)
    await eventually(lambda: player.playing)
    assert calls == ["defer", "lookup"]
    assert player.current.title == "slash song" and len(player.voice.starts) == 1
    output = ctx.send.await_args.kwargs["embed"]
    assert "slash song" in output.description
    assert output.footer.text == "Kevin's Cogs · Use /play to queue music"


async def test_slash_optional_arguments_and_repeat_choices(audio_runtime):
    cog, player, ctx = audio_runtime
    ctx.interaction = SimpleNamespace(response=SimpleNamespace(is_done=lambda: True))
    ctx.defer = AsyncMock()
    volume = cog.volume.app_command
    arguments = await volume._transform_arguments(ctx.interaction, SimpleNamespace())
    await volume._do_call(ctx, arguments)
    assert "Volume: 100%" in ctx.send.await_args.kwargs["embed"].description
    arguments = await volume._transform_arguments(ctx.interaction, SimpleNamespace(value=350))
    await volume._do_call(ctx, arguments)
    assert player.volume == 350
    repeat = cog.repeat.app_command
    assert [choice.value for choice in repeat.parameters[0].choices] == ["off", "track", "queue"]
    arguments = await repeat._transform_arguments(ctx.interaction, SimpleNamespace(mode="queue"))
    await repeat._do_call(ctx, arguments)
    assert player.repeat == "queue"
    ctx.defer.assert_not_awaited()
