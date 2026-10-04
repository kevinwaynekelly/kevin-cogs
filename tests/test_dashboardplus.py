"""Real local HTTP/auth and Red command execution; Discord/media transport is mocked."""

import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer
from conftest import make_channel
from test_audio_hybrid import red_command_runtime as command_fixture
from test_cog_hybrid import invoke_slash
from test_native_audio import FakeSource, FakeVoice, eventually, track

from audioplus.player import GuildPlayer
from dashboardplus import DashboardPlus
from dashboardplus.auth import Auth, dashboard_url, digest, hostname, listener
from dashboardplus.constants import COOKIE, IDLE_SECONDS, MAX_SESSIONS, SESSION_SECONDS
from dashboardplus.schema import EDITORS, SETTINGS, music_arguments, public_url, track_card

red_command_runtime = command_fixture


@pytest.fixture
async def dashboard_runtime(red_command_runtime, monkeypatch):
    bot, audio, member, invoke = red_command_runtime
    bot.owner_ids.add(member.id)
    monkeypatch.setattr(bot, "on_command_error", AsyncMock())
    ctx = await invoke("!queue")
    guild = member.guild
    guild.text_channels = [ctx.channel]
    guild.member_count, guild.icon = 3, None
    ctx.channel.permissions_for.side_effect = None
    ctx.channel.permissions_for.return_value = discord.Permissions.all()
    bot._connection._guilds[guild.id] = guild
    monkeypatch.setattr(bot, "get_user", lambda uid: member if uid == member.id else None)
    cog = DashboardPlus(bot)
    await bot.add_cog(cog)
    client = TestClient(TestServer(cog.application()))
    await client.start_server()
    try:
        yield bot, cog, audio, member, invoke, ctx, client
    finally:
        await client.close()
        await bot.remove_cog("DashboardPlus")


async def login(runtime):
    bot, cog, audio, member, invoke, ctx, client = runtime
    origin = str(client.make_url("/")).rstrip("/")
    code = cog._auth.issue(member.id)
    response = await client.post("/api/login", json={"code": code}, headers={"Origin": origin})
    assert response.status == 200, await response.text()
    data = await response.json()
    return {"Origin": origin, "X-CSRF-Token": data["csrf"]}, code, response


def action_body(runtime, action, value=None, *, kind="music"):
    _, _, _, member, _, ctx, _ = runtime
    return {
        "guild": str(member.guild.id),
        "channel": str(ctx.channel.id),
        "kind": kind,
        "action": action,
        "value": value,
    }


async def test_assets_no_auth_and_private_api_single_use_cookie(dashboard_runtime):
    bot, cog, audio, member, invoke, ctx, client = dashboard_runtime
    for path in ("/", "/app.js", "/style.css"):
        response = await client.get(path)
        assert response.status == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert (await client.get("/api/overview")).status == 401
    assert (await client.get(f"/api/data/{member.guild.id}?cog=level.members")).status == 401
    headers, code, response = await login(dashboard_runtime)
    cookie = response.cookies[COOKIE]
    assert cookie["httponly"] and cookie["samesite"] == "Strict"
    assert code not in cog._auth.codes
    assert digest(code) not in cog._auth.codes
    assert (await client.post("/api/login", json={"code": code}, headers=headers)).status == 401
    response = await client.get("/api/overview")
    assert response.status == 200
    data = await response.json()
    assert data["bot"] == "Scarlet" and data["servers"][0]["id"] == str(member.guild.id)
    assert isinstance(data["servers"][0]["id"], str)
    assert not hasattr(cog, "_password")
    assert set(cog.config.defaults["GLOBAL"]) == {"settings"}


async def test_origin_csrf_host_json_and_action_allowlist(dashboard_runtime):
    bot, cog, audio, member, invoke, ctx, client = dashboard_runtime
    headers, _, _ = await login(dashboard_runtime)
    body = action_body(dashboard_runtime, "pause")
    for invalid in (
        {},
        {"Origin": "https://elsewhere.invalid", "X-CSRF-Token": headers["X-CSRF-Token"]},
        {"Origin": headers["Origin"]},
    ):
        assert (await client.post("/api/action", json=body, headers=invalid)).status == 403
    assert (await client.get("/", headers={"Host": "evil.invalid"})).status == 403
    assert (await client.post("/api/action", data="x", headers=headers)).status == 400
    for data in (
        {**body, "command": "shutdown"},
        {**body, "action": "shutdown"},
        {**body, "kind": "raw-config"},
        {**body, "action": []},
    ):
        assert (await client.post("/api/action", json=data, headers=headers)).status == 400
    response = await client.post(
        "/api/action", data='{"value":NaN}', headers={**headers, "Content-Type": "application/json"}
    )
    assert response.status == 400
    response = await client.post("/api/login", json={"code": "x" * 17000}, headers=headers)
    assert response.status == 413


async def test_owner_revocation_missing_membership_and_dashboard_disabled(dashboard_runtime):
    bot, cog, audio, member, invoke, ctx, client = dashboard_runtime
    headers, _, _ = await login(dashboard_runtime)
    bot.owner_ids.remove(member.id)
    assert (await client.get("/api/session")).status == 401
    assert not cog._auth.sessions
    bot.owner_ids.add(member.id)
    headers, _, _ = await login(dashboard_runtime)
    member.guild.members.remove(member)
    assert (await client.get(f"/api/guild/{member.guild.id}")).status == 403
    member.guild.members.append(member)
    await bot._disabled_cog_cache.disable_cog_in_guild("DashboardPlus", member.guild.id)
    assert (await client.get(f"/api/guild/{member.guild.id}")).status == 403


async def test_reviewed_settings_actual_commands_and_source_disable(dashboard_runtime):
    bot, cog, audio, member, invoke, ctx, client = dashboard_runtime
    headers, _, _ = await login(dashboard_runtime)
    await audio.config.guild(member.guild).favorites.set(
        {str(member.id): [{"secret": "never-export-this"}]}
    )
    response = await client.get(f"/api/guild/{member.guild.id}")
    assert response.status == 200, await response.text()
    data = await response.json()
    assert "never-export-this" not in json.dumps(data)
    assert len(data["settings"]) == 8
    assert next(item for item in data["settings"] if item["id"] == "music.limits")["value"] == {
        "max_seconds": 0,
        "per_member": 0,
    }
    body = action_body(
        dashboard_runtime, "music.limits", {"max_seconds": 180, "per_member": 3}, kind="setting"
    )
    response = await client.post("/api/action", json=body, headers=headers)
    assert response.status == 200, await response.text()
    policy = await audio.config.guild(member.guild).music()
    assert (policy["max_seconds"], policy["per_member"]) == (180, 3)
    source = bot.get_command("audioset limits")
    source.disable_in(member.guild)
    assert (await client.post("/api/action", json=body, headers=headers)).status == 403
    await bot._disabled_cog_cache.disable_cog_in_guild("AudioPlus", member.guild.id)
    data = await (await client.get(f"/api/guild/{member.guild.id}")).json()
    assert data["settings"] == [] and not data["music"]["available"]
    assert not next(item for item in data["cogs"] if item["name"] == "AudioPlus")["enabled"]


async def test_native_music_play_pause_resume_volume_and_web_only_replies(dashboard_runtime):
    bot, cog, audio, member, invoke, ctx, client = dashboard_runtime
    headers, _, _ = await login(dashboard_runtime)
    guild = member.guild
    channel = make_channel(guild, kind=discord.VoiceChannel)
    voice = FakeVoice(guild.id)
    voice.guild, voice.channel = guild, channel
    guild.voice_client = voice
    player = GuildPlayer(
        voice, audio._resolver, audio._report_playback_failure, source_factory=FakeSource
    )
    audio._players[guild.id] = player
    audio._resolver.search = AsyncMock(return_value=[track("selected song")])
    audio._resolver.resolve = AsyncMock(
        side_effect=lambda selected: SimpleNamespace(url=selected.uri)
    )
    response = await client.post(
        "/api/action", json=action_body(dashboard_runtime, "play", 'Artist "song"'), headers=headers
    )
    assert response.status == 200, await response.text()
    await eventually(lambda: player.playing)
    audio._resolver.search.assert_awaited_once_with('ytsearch1:Artist "song"')
    assert "selected song" in json.dumps(await response.json())
    for command, value in (("pause", None), ("volume", 75), ("resume", None)):
        response = await client.post(
            "/api/action", json=action_body(dashboard_runtime, command, value), headers=headers
        )
        assert response.status == 200, await response.text()
        if command == "pause":
            assert player.paused
    assert player.volume == 75 and player.playing and not player.paused
    data = await (await client.get(f"/api/guild/{guild.id}")).json()
    assert data["music"]["current"]["title"] == "selected song"
    ctx.channel.send.assert_not_awaited()
    bot.get_command("audio pause").disable_in(guild)
    response = await client.post(
        "/api/action", json=action_body(dashboard_runtime, "pause"), headers=headers
    )
    assert response.status == 403
    assert not player.paused


async def test_private_login_prefix_slash_and_owner_checks(dashboard_runtime, monkeypatch):
    bot, cog, audio, member, invoke, ctx, client = dashboard_runtime
    cog._runner = SimpleNamespace(cleanup=AsyncMock())
    try:
        ctx = await invoke("!dashboard login")
        assert not ctx.command_failed
        member.send.assert_awaited_once()
        secret = member.send.call_args.kwargs["embed"].description.split("`")[1]
        assert digest(secret) in cog._auth.codes
        assert secret not in str(ctx.send.call_args)
        assert "private" not in str(ctx.send.call_args).lower()
        ctx = await invoke_slash(bot, invoke, monkeypatch, "dashboard login")
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        assert ctx.send.call_args.kwargs["ephemeral"]
        bot.owner_ids.remove(member.id)
        ctx = await invoke("!dashboard login")
        assert ctx.command_failed and member.send.await_count == 1
    finally:
        cog._runner = None


async def test_advertised_url_survives_reload_and_private_login(dashboard_runtime, monkeypatch):
    bot, cog, audio, member, invoke, ctx, client = dashboard_runtime
    ctx = await invoke("!dashboard url http://10.10.1.200:8765/")
    assert not ctx.command_failed, bot.on_command_error.call_args
    assert cog._url() == "http://10.10.1.200:8765"
    assert (await cog.config.settings())["url"] == cog._url()
    # Changing a displayed URL never changes the actual listening or Host policy.
    assert cog._listener["bind"] == "127.0.0.1"
    assert not cog._host_allowed("10.10.1.200:8765")
    cog._runner = SimpleNamespace(cleanup=AsyncMock())
    try:
        await invoke("!dashboard login")
        assert "http://10.10.1.200:8765" in member.send.call_args.kwargs["embed"].description
    finally:
        cog._runner = None
    await bot.remove_cog("DashboardPlus")
    replacement = DashboardPlus(bot)
    await bot.add_cog(replacement)
    await replacement._startup
    assert replacement._url() == "http://10.10.1.200:8765"
    ctx = await invoke_slash(bot, invoke, monkeypatch, "dashboard url", address="")
    assert not ctx.command_failed, bot.on_command_error.call_args
    assert replacement._url() == "http://127.0.0.1:8765"
    bot.owner_ids.remove(member.id)
    ctx = await invoke("!dashboard url http://10.10.1.200:8765")
    assert ctx.command_failed
    assert (await replacement.config.settings())["url"] == ""


async def test_unexpected_errors_do_not_echo_secrets(dashboard_runtime, monkeypatch):
    bot, cog, audio, member, invoke, ctx, client = dashboard_runtime
    headers, _, _ = await login(dashboard_runtime)
    monkeypatch.setattr(
        bot.get_command("pause"),
        "_callback",
        AsyncMock(side_effect=RuntimeError("private-stream-token")),
    )
    response = await client.post(
        "/api/action", json=action_body(dashboard_runtime, "pause"), headers=headers
    )
    assert response.status == 500
    assert "private-stream-token" not in await response.text()
    assert cog._error == "RuntimeError"


async def test_all_editor_paths_keys_and_toggles_use_existing_cogs(
    dashboard_runtime, monkeypatch, tmp_path
):
    bot, cog, audio, member, invoke, ctx, client = dashboard_runtime
    loaded = []
    monkeypatch.setattr("introplus.cog.cog_data_path", lambda cog: tmp_path / "IntroPlus")
    try:
        for package, name in (
            ("communityplus", "CommunityPlus"),
            ("levelplus", "LevelPlus"),
            ("logplus", "LogPlus"),
            ("owoplus", "OwoPlus"),
            ("emojistealerplus", "EmojiStealerPlus"),
            ("introplus", "IntroPlus"),
            ("presenceplus", "PresencePlus"),
        ):
            source = getattr(importlib.import_module(package), name)(bot)
            await bot.add_cog(source)
            loaded.append(source)
        headers, _, _ = await login(dashboard_runtime)
        response = await client.get(f"/api/guild/{member.guild.id}")
        assert response.status == 200, await response.text()
        data = await response.json()
        assert {item["id"] for item in data["settings"]} == set(SETTINGS)
        assert all(len(item["description"]) > 30 for item in data["settings"])
        for spec in EDITORS:
            command = bot.get_command(spec.path)
            assert command and command.cog.qualified_name == spec.cog
            if spec.off:
                assert bot.get_command(spec.off).cog is command.cog
        for action, value in (
            ("community.sticky", False),
            ("log.channel", "0"),
            ("level.message", False),
            ("intro.volume", 40),
            ("presence.interval", 120),
        ):
            response = await client.post(
                "/api/action",
                json=action_body(dashboard_runtime, action, value, kind="setting"),
                headers=headers,
            )
            assert response.status == 200, await response.text()
        assert not await bot.get_cog("CommunityPlus").config.guild(member.guild).sticky.enabled()
        assert await bot.get_cog("LogPlus").config.guild(member.guild).log_channel() is None
        assert not await bot.get_cog("LevelPlus").config.guild(member.guild).message.enabled()
        assert await bot.get_cog("IntroPlus").config.guild(member.guild).volume() == 40
        assert (await bot.get_cog("PresencePlus").config.settings())["interval"] == 120
    finally:
        for source in reversed(loaded):
            await bot.remove_cog(source.qualified_name)


async def test_real_listener_lifecycle_reboot_policy_port_conflict_and_privacy(
    dashboard_runtime, unused_tcp_port
):
    bot, cog, audio, member, invoke, ctx, client = dashboard_runtime
    await invoke(f"!dashboard bind 127.0.0.1 {unused_tcp_port}")
    ctx = await invoke("!dashboard start")
    assert not ctx.command_failed, bot.on_command_error.call_args
    assert cog._runner is not None and (await cog.config.settings())["enabled"]
    async with ClientSession() as http:
        async with http.get(f"http://127.0.0.1:{unused_tcp_port}/") as response:
            assert response.status == 200
    cog._auth.issue(member.id)
    _, session = cog._auth.create(member.id)
    exported = await cog.red_get_data_for_user(user_id=member.id)
    assert json.loads(exported["dashboardplus.json"].getvalue())["active_sessions"] == 1
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    assert not cog._auth.codes and not cog._auth.sessions
    conflict = DashboardPlus(bot)
    with pytest.raises(OSError):
        await conflict._start_server()
    assert conflict._runner is None
    await bot.remove_cog("DashboardPlus")
    assert cog._runner is None and not cog._requests
    replacement = DashboardPlus(bot)
    await bot.add_cog(replacement)
    await replacement._startup
    assert replacement._runner and (await replacement.config.settings())["enabled"]
    await invoke("!dashboard stop")
    assert replacement._runner is None and not (await replacement.config.settings())["enabled"]


async def test_previous_listener_policy_keeps_values_with_new_url_default(dashboard_runtime):
    bot, cog, audio, member, invoke, ctx, client = dashboard_runtime
    previous = {"enabled": False, "bind": "0.0.0.0", "port": 9876, "hosts": ["aria.home"]}
    await cog.config.settings.set(previous)
    await bot.remove_cog("DashboardPlus")
    replacement = DashboardPlus(bot)
    await bot.add_cog(replacement)
    await replacement._startup
    assert await replacement.config.settings() == {**previous, "url": ""}
    assert replacement._url() == "http://127.0.0.1:9876"


def test_bounded_auth_expiry_rates_revocation_and_validation():
    now = [0]
    auth = Auth(clock=lambda: now[0])
    code = auth.issue(1)
    assert auth.redeem(code) == 1 and auth.redeem(code) is None
    code = auth.issue(1)
    now[0] = 301
    assert auth.redeem(code) is None
    cookie, _ = auth.create(1)
    now[0] += IDLE_SECONDS
    assert auth.get(cookie) is None
    cookie, _ = auth.create(1)
    for _ in range(16):
        now[0] += 1799
        auth.get(cookie)
    now[0] += SESSION_SECONDS
    assert auth.get(cookie) is None
    for owner_id in range(50):
        auth.issue(owner_id)
        auth.create(owner_id)
    assert len(auth.codes) == len(auth.sessions) == MAX_SESSIONS
    auth.revoke(49)
    assert not any(item.owner_id == 49 for item in auth.sessions.values())
    assert all(auth.allow("address", limit=3) for _ in range(3))
    assert not auth.allow("address", limit=3)
    now[0] += 61
    assert auth.allow("address", limit=3)
    assert listener("0.0.0.0", 8765) == ("0.0.0.0", 8765)
    for bind, port in (("example.com", 8765), ("127.0.0.1", 80), ("::", True)):
        with pytest.raises(ValueError):
            listener(bind, port)
    for host in ("https://example.com", "x/y", "x:8765", "x..y"):
        with pytest.raises(ValueError):
            hostname(host)
    assert hostname("Aria.Home.") == "aria.home"
    assert dashboard_url("http://10.10.1.200:8765/") == "http://10.10.1.200:8765"
    assert dashboard_url("https://Aria.Home/") == "https://aria.home"
    assert dashboard_url("http://[::1]:8765") == "http://[::1]:8765"
    assert dashboard_url("") == ""
    for value in (
        "javascript:alert(1)",
        "https://user:secret@example.com/",
        "https://example.com/path",
        "https://example.com/?token=secret",
        "https://example.com/#code",
        "http://example.com:65536/",
        "http://0.0.0.0:8765/",
        "http://[::]:8765/",
        "http://elsewhere.invalid\\@example.com",
        "http://example.com\n",
    ):
        with pytest.raises(ValueError):
            dashboard_url(value)


def test_action_validation_and_public_track_artwork():
    from redbot.core import commands

    from audioplus.resolver import Track

    for value in (True, -1, 1001, "100"):
        with pytest.raises(commands.BadArgument):
            music_arguments("volume", value)
    with pytest.raises(commands.BadArgument):
        music_arguments("play", "line\nbreak")
    with pytest.raises(commands.BadArgument):
        SETTINGS["music.limits"].validate({"max_seconds": 5, "per_member": True})
    assert SETTINGS["log.channel"].command("0") == "log clearchannel"
    assert (
        public_url("javascript:alert(1)") == ""
        and public_url("https://user:password@host/path") == ""
    )
    card = track_card(Track("https://www.youtube.com/watch?v=YE7VzlLtp-4", "song", length=120000))
    assert card["thumbnail"] == "https://i.ytimg.com/vi/YE7VzlLtp-4/hqdefault.jpg"
    assert set(card) == {"title", "author", "url", "thumbnail", "duration"}
