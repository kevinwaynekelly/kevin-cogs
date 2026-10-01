"""Queue edits, resumed seeking, saved data isolation, voting, and panel lifetimes."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import make_member
from redbot.core import commands
from test_audioplus import audio_runtime as native_fixture
from test_native_audio import eventually, track

from audioplus.features import check_control
from audioplus.resolver import Track

audio_runtime = native_fixture


async def test_queue_edits_and_seek_preserve_paused_track_and_queue(audio_runtime):
    cog, player, ctx = audio_runtime
    song = Track("https://example.invalid/song", "Song", length=180000)
    await player.enqueue([song, track("two"), track("three"), track("four")], ctx)
    await eventually(lambda: player.playing)
    await cog.move.callback(cog, ctx, source=3, destination=1)
    assert [song.title for song in player.queue] == ["four", "two", "three"]
    await cog.remove.callback(cog, ctx, position=2)
    player.voice.pause()
    await cog.seek.callback(cog, ctx, position="1:23")
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.source.position == 83000 and player.paused
    assert player.current is song and [song.title for song in player.queue] == ["four", "three"]
    with pytest.raises(commands.CommandError):
        await cog.seek.callback(cog, ctx, position="180")
    with pytest.raises(commands.BadArgument):
        await cog.seek.callback(cog, ctx, position="1:99")
    with pytest.raises(commands.CommandError):
        await cog.remove.callback(cog, ctx, position=0)


async def test_collections_are_private_bounded_and_exportable(audio_runtime):
    cog, player, ctx = audio_runtime
    await player.enqueue([track("one"), track("two")], ctx)
    await eventually(lambda: player.playing)
    await cog.playlist_save.callback(cog, ctx, name="MY_LIST")
    await cog.favorite_add.callback(cog, ctx)
    await cog.favorite_add.callback(cog, ctx)
    group = cog.config.guild(ctx.guild)
    assert len((await group.favorites())[str(ctx.author.id)]) == 1
    export = json.load((await cog.red_get_data_for_user(user_id=ctx.author.id))["audioplus.json"])
    assert export["collections"][str(ctx.guild.id)]["playlists"]["my_list"][0]["title"] == "one"
    assert "password" not in str(export)
    other = make_member(ctx.guild, 456)
    ctx.author = other
    with pytest.raises(commands.BadArgument):
        await cog.playlist_play.callback(cog, ctx, name="my_list")
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=123456789012345678)
    assert not await group.playlists() and not await group.favorites()


async def test_vote_skip_requires_unique_current_listeners(audio_runtime):
    cog, player, ctx = audio_runtime
    members = [ctx.author, *[make_member(ctx.guild, uid) for uid in (2, 3, 4)]]
    for member in members:
        member.voice = SimpleNamespace(channel=player.voice.channel)
        member.guild_permissions = discord.Permissions.none()
    player.voice.channel.members = members
    await cog.config.guild(ctx.guild).music.vote_skip.set(True)
    await player.enqueue([track("one"), track("two")], ctx)
    await eventually(lambda: player.playing)
    await cog.audio_skip.callback(cog, ctx)
    await cog.audio_skip.callback(cog, ctx)
    assert len(player.voice.starts) == 1
    ctx.author = members[1]
    await cog.audio_skip.callback(cog, ctx)
    await eventually(lambda: len(player.voice.starts) == 2)
    assert player.current.title == "two"
    ctx.author.voice = None
    with pytest.raises(commands.CheckFailure):
        await check_control(cog, ctx)


@pytest.mark.parametrize("embeds", [True, False])
async def test_panel_tracks_changes_respects_embeds_and_closes(audio_runtime, embeds):
    cog, player, ctx = audio_runtime
    ctx.embed_requested = AsyncMock(return_value=embeds)
    message = SimpleNamespace(edit=AsyncMock())
    ctx.channel.send.return_value = message
    player.on_start = cog._track_started
    await player.enqueue([track("one"), track("two")], ctx)
    await eventually(lambda: player.guild.id in cog._panels)
    assert bool(ctx.channel.send.await_args.kwargs["embed"]) == embeds
    assert len(ctx.channel.send.await_args.kwargs["view"].children) == 4
    player.voice.finish()
    await eventually(lambda: message.edit.await_count > 0)
    output = message.edit.await_args.kwargs
    assert "two" in (output["embed"].description if embeds else output["content"])
    await cog.cog_unload()
    assert (
        not cog._panels
        and not cog._panel_tasks
        and message.edit.await_args.kwargs == {"view": None}
    )
