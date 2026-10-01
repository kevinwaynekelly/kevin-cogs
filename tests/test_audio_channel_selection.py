"""Use command callbacks and the real player with Discord connections mocked."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import make_channel, make_context, make_member
from redbot.core import commands
from test_native_audio import FakeSource, FakeVoice, eventually, track

import audioplus.cog as audio_module
from audioplus import AudioPlus
from audioplus.player import GuildPlayer
from audioplus.resolver import Stream


@pytest.fixture
async def automatic_audio(bot, guild, monkeypatch):
    cog = AudioPlus(bot)
    await cog.cog_load()
    monkeypatch.setattr(audio_module, "require_voice", lambda: None)
    monkeypatch.setattr(
        audio_module,
        "GuildPlayer",
        lambda voice, resolver, report, **kwargs: GuildPlayer(
            voice, resolver, report, source_factory=FakeSource, **kwargs
        ),
    )
    cog._resolver.search = AsyncMock(return_value=[track()])
    cog._resolver.resolve = AsyncMock(side_effect=lambda selected: Stream(selected.uri))
    member = make_member(guild)
    ctx = make_context(guild, make_channel(guild), member)
    ctx.bot = bot

    def voice_channel(channel_id, people, *, bots=0):
        channel = make_channel(guild, channel_id, discord.VoiceChannel)
        channel.members = [SimpleNamespace(bot=False) for _ in range(people)] + [
            SimpleNamespace(bot=True) for _ in range(bots)
        ]

        async def connect(**kwargs):
            voice = FakeVoice(guild.id)
            voice.guild, voice.channel = guild, channel
            guild.voice_client = voice
            return voice

        channel.connect = AsyncMock(side_effect=connect)
        return channel

    try:
        yield cog, ctx, voice_channel
    finally:
        await cog.cog_unload()


@pytest.mark.parametrize("entry", ["prefix", "slash", "legacy"])
async def test_play_without_user_in_voice_joins_the_busiest_available_channel(
    automatic_audio, entry
):
    cog, ctx, channel = automatic_audio
    bot_room = channel(10, 2, bots=10)
    busy = channel(20, 4)
    private = channel(30, 8)
    private.permissions_for.return_value.connect = False
    afk = channel(40, 20)
    ctx.guild.afk_channel = afk
    full = channel(50, 8)
    full.user_limit = 8
    full.permissions_for.return_value.move_members = False
    if entry == "slash":
        ctx.interaction = SimpleNamespace(response=SimpleNamespace(is_done=lambda: False))
        ctx.defer = AsyncMock()
    callback = AudioPlus.audio_play if entry == "legacy" else AudioPlus.play
    await callback.callback(cog, ctx, query="roar")
    player = cog._get_player(ctx.guild)
    await eventually(lambda: player.playing)
    assert player.voice.channel is busy and player.current.title == "one"
    busy.connect.assert_awaited_once()
    for skipped in (bot_room, private, afk, full):
        skipped.connect.assert_not_awaited()
    if entry == "slash":
        ctx.defer.assert_awaited_once()


async def test_play_prefers_the_users_channel_over_a_busier_one(automatic_audio):
    cog, ctx, channel = automatic_audio
    own = channel(10, 1)
    busy = channel(20, 10)
    ctx.author.voice = SimpleNamespace(channel=own)
    await AudioPlus.play.callback(cog, ctx, query="roar")
    assert cog._get_player(ctx.guild).voice.channel is own
    own.connect.assert_awaited_once()
    busy.connect.assert_not_awaited()


@pytest.mark.parametrize("people", [0, 3])
async def test_tied_and_empty_channels_follow_server_order(automatic_audio, people):
    cog, ctx, channel = automatic_audio
    first = channel(10, people)
    second = channel(20, people)
    # A voice state whose channel is None also takes the automatic route.
    ctx.author.voice = SimpleNamespace(channel=None)
    await AudioPlus.play.callback(cog, ctx, query="roar")
    assert cog._get_player(ctx.guild).voice.channel is first
    first.connect.assert_awaited_once()
    second.connect.assert_not_awaited()


@pytest.mark.parametrize("restriction", ["missing", "view_channel", "connect", "speak", "afk"])
async def test_no_eligible_channel_reports_an_error_before_lookup_or_connection(
    automatic_audio, restriction
):
    cog, ctx, channel = automatic_audio
    target = None
    if restriction != "missing":
        target = channel(10, 5)
        if restriction == "afk":
            ctx.guild.afk_channel = target
        else:
            setattr(target.permissions_for.return_value, restriction, False)
    with pytest.raises(commands.UserInputError, match="No available voice channel"):
        await AudioPlus.play.callback(cog, ctx, query="roar")
    assert not cog._players
    cog._resolver.search.assert_not_awaited()
    if target:
        target.connect.assert_not_awaited()


async def test_full_channel_is_available_with_move_members_permission(automatic_audio):
    cog, ctx, channel = automatic_audio
    busy = channel(10, 5)
    busy.user_limit = 5
    assert busy.permissions_for.return_value.move_members
    other = channel(20, 2)
    await AudioPlus.play.callback(cog, ctx, query="roar")
    assert cog._get_player(ctx.guild).voice.channel is busy
    other.connect.assert_not_awaited()


async def test_already_connected_channel_stays_available_when_it_is_full(automatic_audio):
    cog, ctx, channel = automatic_audio
    busy = channel(10, 5)
    busy.user_limit = 6
    busy.permissions_for.return_value.move_members = False
    await AudioPlus.play.callback(cog, ctx, query="roar")
    player = cog._get_player(ctx.guild)
    await eventually(lambda: player.playing)
    busy.members.append(SimpleNamespace(bot=True))
    other = channel(20, 2)
    await AudioPlus.play.callback(cog, ctx, query="another song")
    assert cog._get_player(ctx.guild) is player and player.voice.channel is busy
    busy.connect.assert_awaited_once()
    other.connect.assert_not_awaited()
    assert len(player.queue) == 1
