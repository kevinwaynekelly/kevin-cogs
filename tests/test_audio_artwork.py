"""Artwork follows public tracks across playback, saved records and Discord cards."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_audio_hybrid import red_command_runtime as red_fixture
from test_audioplus import audio_runtime as native_fixture
from test_cog_hybrid import invoke_slash
from test_native_audio import eventually
from test_presentation import assert_limits

from audioplus.artwork import video_thumbnail
from audioplus.features import saved_track
from audioplus.resolver import Track

audio_runtime = native_fixture
red_command_runtime = red_fixture
VIDEO = "YE7VzlLtp-4"
OTHER = "dQw4w9WgXcQ"
THUMBNAIL = f"https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg"
OTHER_THUMBNAIL = f"https://i.ytimg.com/vi/{OTHER}/hqdefault.jpg"
SONG = Track(f"https://www.youtube.com/watch?v={VIDEO}", "Video", "Artist", 180000, "youtube")
NEXT = Track(f"https://youtu.be/{OTHER}", "Next", "Artist", 180000, "youtube")
DIRECT = Track("https://media.invalid/song.mp3?token=stream-secret", "Direct", direct=True)


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VIDEO}&list=playlist&t=30",
        f"https://youtube.com/watch?v={VIDEO}",
        f"https://m.youtube.com/watch?v={VIDEO}",
        f"https://music.youtube.com/watch?v={VIDEO}",
        f"https://youtu.be/{VIDEO}?si=share-code",
        f"https://www.youtube.com/shorts/{VIDEO}",
        f"https://www.youtube.com/live/{VIDEO}",
        f"https://www.youtube.com/embed/{VIDEO}",
        f"https://www.youtube-nocookie.com/embed/{VIDEO}",
        f"http://youtube.com/v/{VIDEO}",
    ],
)
def test_public_video_formats_produce_only_the_canonical_image(url):
    assert video_thumbnail(url) == THUMBNAIL


@pytest.mark.parametrize(
    "url",
    [
        None,
        123,
        "",
        "https://www.youtube.com/playlist?list=abc",
        "https://www.youtube.com/watch?v=too-short",
        "https://www.youtube.com/watch?v=../../private",
        f"https://www.youtube.com/watch?v={VIDEO}&v={OTHER}",
        f"https://www.youtube.com.evil.invalid/watch?v={VIDEO}",
        f"https://notyoutube.com/watch?v={VIDEO}",
        f"https://www.youtube.com@evil.invalid/watch?v={VIDEO}",
        f"https://user:password@www.youtube.com/watch?v={VIDEO}",
        f"file://www.youtube.com/watch?v={VIDEO}",
        f"https://www.youtube.com/watch?v={VIDEO}\n",
        "https://[invalid-host/watch?v=" + VIDEO,
        "https://media.invalid/audio?token=stream-secret",
        "https://i.ytimg.com/vi/" + VIDEO + "/hqdefault.jpg",
        f"https://www.youtube.com/watch?v={VIDEO}&" + "a=b&" * 100,
        f"https://www.youtube.com/watch?v={VIDEO}&x=" + "a" * 2048,
    ],
)
def test_invalid_private_and_unrelated_sources_do_not_become_thumbnails(url):
    assert video_thumbnail(url) is None


@pytest.mark.parametrize("slash", [False, True])
@pytest.mark.parametrize("embeds", [False, True])
async def test_play_prefix_and_slash_show_artwork_without_an_extra_lookup(
    audio_runtime, slash, embeds
):
    cog, player, ctx = audio_runtime
    ctx.command = cog.play if slash else cog.audio_play
    ctx.embed_requested = AsyncMock(return_value=embeds)
    cog._resolver._extract = AsyncMock(
        return_value={
            "entries": [{"id": VIDEO, "url": VIDEO, "ie_key": "Youtube", "title": "Video"}]
        }
    )
    cog._resolver.resolve = AsyncMock(return_value=SimpleNamespace(url=DIRECT.uri))
    if slash:
        ctx.interaction = SimpleNamespace(response=SimpleNamespace(is_done=lambda: False))
        ctx.defer = AsyncMock()
        arguments = await cog.play.app_command._transform_arguments(
            ctx.interaction, SimpleNamespace(query="video")
        )
        await cog.play.app_command._do_call(ctx, arguments)
        ctx.defer.assert_awaited_once()
    else:
        await cog.audio_play.callback(cog, ctx, query="video")
    await eventually(lambda: player.playing)
    cog._resolver._extract.assert_awaited_once_with("ytsearch1:video", flat=True)
    if embeds:
        embed = ctx.send.await_args.kwargs["embed"]
        assert embed.thumbnail.url == THUMBNAIL
        assert_limits(embed)
        output = str(embed.to_dict())
    else:
        output = ctx.send.await_args.args[0]
        assert "embed" not in ctx.send.await_args.kwargs
    assert player.current.uri in output and "Video" in output
    assert "stream-secret" not in output and "media.invalid" not in output


async def test_panel_thumbnail_follows_tracks_and_clears_for_direct_and_idle(audio_runtime):
    cog, player, ctx = audio_runtime
    message = SimpleNamespace(edit=AsyncMock())
    ctx.channel.send.return_value = message
    cog.bot._kevin_cogs_themes = {ctx.guild.id: {"colors": {"info": 0x123456}, "footer": "Scarlet"}}
    await player.enqueue([SONG, NEXT, DIRECT], ctx)
    await eventually(lambda: player.playing)
    await cog._update_panel(player)
    first = ctx.channel.send.await_args.kwargs["embed"]
    assert first.thumbnail.url == THUMBNAIL
    assert first.color.value == 0x123456 and first.footer.text == "Scarlet"
    for expected, artwork in [(NEXT, OTHER_THUMBNAIL), (DIRECT, None), (None, None)]:
        player.voice.finish()
        await eventually(lambda: player.current is expected and not player.preparing)
        await cog._update_panel(player)
        updated = message.edit.await_args.kwargs["embed"]
        assert updated.thumbnail.url == artwork
        assert updated.color.value == 0x123456 and updated.footer.text == "Scarlet"
        assert_limits(updated)


@pytest.mark.parametrize(
    "command,arguments,artwork",
    [
        ("audio_nowplaying", {}, THUMBNAIL),
        ("audio_playerstate", {}, THUMBNAIL),
        ("audio_pause", {}, THUMBNAIL),
        ("audio_resume", {}, THUMBNAIL),
        ("audio_volume", {"value": 75}, THUMBNAIL),
        ("audio_repeat", {"mode": "track"}, THUMBNAIL),
        ("audio_stop", {}, THUMBNAIL),
        ("audio_skip", {}, THUMBNAIL),
        ("audio_leave", {}, THUMBNAIL),
        ("audio_queue", {}, OTHER_THUMBNAIL),
        ("seek", {"position": "30"}, THUMBNAIL),
        ("remove", {"position": 1}, OTHER_THUMBNAIL),
        ("move", {"source": 1, "destination": 2}, OTHER_THUMBNAIL),
    ],
)
async def test_controls_show_the_track_the_reply_refers_to(
    audio_runtime, command, arguments, artwork
):
    cog, player, ctx = audio_runtime
    await player.enqueue([SONG, NEXT, DIRECT], ctx)
    await eventually(lambda: player.playing)
    if command == "audio_resume":
        player.pause()
    selected = getattr(cog, command)
    ctx.command = selected
    await selected.callback(cog, ctx, **arguments)
    assert ctx.send.await_args.kwargs["embed"].thumbnail.url == artwork


@pytest.mark.parametrize("command", ["favorite", "playlist", "server_playlist_show"])
async def test_existing_saved_records_display_images_without_rewriting_them(audio_runtime, command):
    cog, player, ctx = audio_runtime
    group = cog.config.guild(ctx.guild)
    old = saved_track(SONG)
    await group.favorites.set({str(ctx.author.id): [old]})
    await group.playlists.set({str(ctx.author.id): {"saved": [old]}})
    await group.server_playlists.set(
        {"saved": {"tracks": [{"user": ctx.author.id, "track": old}], "suggestions": []}}
    )
    records = [{"id": "abcdef", "at": int(time.time()), "requester": ctx.author.id, "track": old}]
    await group.listening_history.set(records)
    selected = getattr(cog, command)
    ctx.command = selected
    await selected.callback(
        cog, ctx, **({"name": "saved"} if command == "server_playlist_show" else {})
    )
    assert ctx.send.await_args.kwargs["embed"].thumbnail.url == THUMBNAIL
    assert await group.favorites() == {str(ctx.author.id): [old]}
    assert await group.playlists() == {str(ctx.author.id): {"saved": [old]}}
    assert await group.listening_history() == records
    cog._resolver.resolve.assert_not_awaited()


@pytest.mark.parametrize("slash", [False, True])
async def test_history_prefix_and_slash_keep_old_records_and_show_artwork(
    red_command_runtime, monkeypatch, slash
):
    bot, cog, member, invoke = red_command_runtime
    records = [
        {"id": "abcdef", "at": int(time.time()), "requester": member.id, "track": saved_track(SONG)}
    ]
    await cog.config.guild(member.guild).listening_history.set(records)
    ctx = (
        await invoke_slash(bot, invoke, monkeypatch, "history list", page=1)
        if slash
        else await invoke("!history")
    )
    assert not ctx.command_failed
    assert ctx.send.await_args.kwargs["embed"].thumbnail.url == THUMBNAIL
    assert await cog.config.guild(member.guild).listening_history() == records


async def test_paginated_cards_keep_artwork_theme_and_discord_limits(audio_runtime):
    cog, player, ctx = audio_runtime
    ctx.command = cog.favorite
    cog.bot._kevin_cogs_themes = {ctx.guild.id: {"colors": {"info": 0x123456}, "footer": "Scarlet"}}
    record = {**saved_track(SONG), "title": "Long title 😀 " * 50}
    await cog.config.guild(ctx.guild).favorites.set({str(ctx.author.id): [record] * 100})
    await cog.favorite.callback(cog, ctx)
    assert ctx.send.await_count > 1
    for call in ctx.send.await_args_list:
        embed = call.kwargs["embed"]
        assert embed.thumbnail.url == THUMBNAIL and embed.color.value == 0x123456
        assert embed.footer.text.startswith("Scarlet · ")
        assert_limits(embed)


async def test_queue_preview_and_failure_use_public_artwork_not_stream_metadata(audio_runtime):
    cog, player, ctx = audio_runtime
    await cog._reply_queued(ctx, [DIRECT, NEXT])
    assert ctx.send.await_args.kwargs["embed"].thumbnail.url == OTHER_THUMBNAIL
    player.current, player.context = SONG, ctx
    await cog._report_playback_failure(player, SONG, "Decoder unavailable")
    assert ctx.send.await_args.kwargs["embed"].thumbnail.url == THUMBNAIL
    assert "Decoder unavailable" in ctx.send.await_args.kwargs["embed"].description


async def test_search_and_session_summary_have_representative_artwork(audio_runtime):
    cog, player, ctx = audio_runtime
    ctx.channel.id = 457
    cog._resolver.search = AsyncMock(return_value=[SONG, NEXT])
    await cog.search.callback(cog, ctx, query="video")
    assert ctx.send.await_args.kwargs["embed"].thumbnail.url == THUMBNAIL
    assert len(ctx.send.await_args.kwargs["view"].children[0].options) == 2
    await cog.config.guild(ctx.guild).music.session_summary.set(True)
    player.context = ctx
    for current in [SONG, NEXT]:
        player.current = current
        await cog._session_started(player)
    await cog._finish_music_session(player)
    summary = ctx.channel.send.await_args.kwargs["embed"]
    assert summary.thumbnail.url == OTHER_THUMBNAIL
    assert summary.title == "AudioPlus · Music session summary"


async def test_daily_failure_dm_has_the_configured_test_video_artwork(audio_runtime):
    cog, player, ctx = audio_runtime
    user = SimpleNamespace(send=AsyncMock())
    cog.bot.get_user = lambda uid: user
    await cog._notify_check_failure(
        {"recipient_id": ctx.author.id, "guild_id": ctx.guild.id, "video_url": SONG.uri},
        {"at": time.time(), "detail": "Playback failed"},
    )
    assert user.send.await_args.kwargs["embed"].thumbnail.url == THUMBNAIL
