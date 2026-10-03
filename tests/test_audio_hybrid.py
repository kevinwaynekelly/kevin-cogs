"""Register hybrid commands and exercise Red's message and slash entry points."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import make_channel, make_member, make_message
from discord.ext.commands import Bot
from redbot.core import Config, commands
from redbot.core._cli import parse_cli_flags
from redbot.core._drivers.json import JsonDriver
from redbot.core.bot import Red
from redbot.core.core_commands import Core
from redbot.core.tree import RedTree
from test_audioplus import audio_runtime as native_runtime_fixture
from test_native_audio import FakeSource, FakeVoice, eventually, track

from audioplus import AudioPlus
from audioplus.player import GuildPlayer

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
    "seek",
    "remove",
    "move",
    "recoverqueue",
    "replay",
}
NEW_GROUPS = {
    "search",
    "audioset",
    "playlist",
    "favorite",
    "serverplaylist",
    "history",
    "audiocache",
}


@pytest.fixture
async def red_command_runtime(monkeypatch, tmp_path, guild):
    core_config = Config(
        "Core",
        str(tmp_path),
        JsonDriver("Core", "0", data_path_override=tmp_path / "Core"),
        False,
    )
    monkeypatch.setattr(Config, "get_core_conf", staticmethod(lambda **kwargs: core_config))
    monkeypatch.setattr(
        "redbot.core._cog_manager.cog_data_path", lambda cog: tmp_path / type(cog).__name__
    )
    bot = Red(
        command_prefix="!",
        cli_flags=parse_cli_flags([]),
        intents=discord.Intents.none(),
        owner_ids={888},
    )
    await bot._async_setup_hook()
    # Mock event transport and RPC; command parsing, checks, and invocation stay real.
    monkeypatch.setattr(bot, "dispatch", Mock())
    monkeypatch.setattr(bot, "register_rpc_handler", Mock())
    bot._red_ready.set()
    bot._connection.user = discord.ClientUser(
        state=bot._connection,
        data={"id": "999", "username": "Scarlet", "discriminator": "0", "avatar": None},
    )
    guild.me.display_name = "Scarlet"
    member = make_member(guild)
    channel = make_channel(guild)
    channel.category = None
    channel.permissions_for.side_effect = lambda target: (
        discord.Permissions.all() if target is guild.me else discord.Permissions.none()
    )
    contexts = []
    get_context = bot.get_context

    async def capture_context(message, **kwargs):
        ctx = await get_context(message, **kwargs)
        ctx.send = AsyncMock()
        ctx.embed_requested = AsyncMock(return_value=True)
        contexts.append(ctx)
        return ctx

    monkeypatch.setattr(bot, "get_context", capture_context)
    await bot.add_cog(Core(bot))
    cog = AudioPlus(bot)
    await bot.add_cog(cog)

    async def invoke(content):
        message = make_message(member, channel, content=content)
        message._state = bot._connection
        await asyncio.wait_for(bot.process_commands(message), timeout=2)
        return contexts[-1]

    try:
        yield bot, cog, member, invoke
    finally:
        await bot.remove_cog("AudioPlus")
        await bot.remove_cog("Core")
        await discord.Client.close(bot)


@pytest.mark.parametrize("owner", [False, True])
async def test_red_messages_reply_for_owner_and_ordinary_member(red_command_runtime, owner):
    bot, cog, member, invoke = red_command_runtime
    if owner:
        bot.owner_ids.add(member.id)
    for content, expected in (
        ("!queue", "Not connected."),
        ("!play roar", "No available voice channel."),
        ("!play", "query is a required argument"),
        ("!audio queue", "Not connected."),
    ):
        ctx = await invoke(content)
        assert isinstance(ctx, commands.Context) and ctx.valid
        assert ctx.command.cog is cog
        assert ctx.send.await_count == 1
        assert expected in ctx.send.await_args.kwargs["embed"].description


async def test_red_reload_keeps_direct_commands_invokable(red_command_runtime):
    bot, old_cog, member, invoke = red_command_runtime
    original_leave = bot.get_command("leave")
    assert (await invoke("!queue")).valid
    await bot.remove_cog("AudioPlus")
    assert bot.get_command("play") is None
    assert old_cog._closing
    replacement = AudioPlus(bot)
    await bot.add_cog(replacement)
    for name in CONTROLS:
        assert bot.get_command(name).cog is replacement
        assert bot.get_command(name).requires.ready_event.is_set()
    ctx = await invoke("!queue")
    assert ctx.command is replacement.queue
    assert "Not connected." in ctx.send.await_args.kwargs["embed"].description
    assert bot.get_command("leave") is original_leave


async def test_red_parses_shortcut_and_controls_the_player(red_command_runtime):
    bot, cog, member, invoke = red_command_runtime
    guild = member.guild
    voice_channel = make_channel(guild, kind=discord.VoiceChannel)
    voice = FakeVoice(guild.id)
    voice.guild, voice.channel = guild, voice_channel
    guild.voice_client = voice
    member.voice = SimpleNamespace(channel=voice_channel)
    player = GuildPlayer(
        voice, cog._resolver, cog._report_playback_failure, source_factory=FakeSource
    )
    cog._players[guild.id] = player
    cog._resolver.search = AsyncMock(return_value=[track("selected song")])
    cog._resolver.resolve = AsyncMock(
        side_effect=lambda selected: SimpleNamespace(url=selected.uri)
    )
    ctx = await invoke("!p artist and song")
    await eventually(lambda: player.playing)
    assert ctx.command is cog.play and not ctx.command_failed
    cog._resolver.search.assert_awaited_once_with("ytsearch1:artist and song")
    assert "selected song" in ctx.send.await_args.kwargs["embed"].description
    await invoke("!pause")
    assert player.paused
    await invoke("!volume 250")
    assert player.volume == 250
    await invoke("!resume")
    assert player.playing and not player.paused
    await invoke("!disconnect")
    assert player.closed and not voice.is_connected()


async def test_real_red_tree_registers_controls_without_replacing_core_leave():
    bot = Bot(command_prefix="!", intents=discord.Intents.none(), tree_cls=RedTree)
    original_leave = Core.leave.copy()
    bot.add_command(original_leave)
    cog = AudioPlus(bot)
    try:
        await bot.add_cog(cog)
        assert bot.get_command("leave") is original_leave
        assert set(bot.tree._disabled_global_commands) == CONTROLS | NEW_GROUPS
        for name in NEW_GROUPS:
            bot.get_command(name).app_command.to_dict(bot.tree)
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
