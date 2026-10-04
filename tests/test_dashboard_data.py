"""Dashboard records use real source storage, permissions and HTTP authentication."""

import importlib
import json
import time
from copy import copy
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import make_channel, make_guild, make_member
from test_dashboardplus import dashboard_runtime as dashboard_fixture
from test_dashboardplus import login
from test_dashboardplus import red_command_runtime as command_fixture

from dashboardplus.data import DATASETS
from introplus.resolver import Track

dashboard_runtime = dashboard_fixture
red_command_runtime = command_fixture

PACKAGES = (
    ("communityplus", "CommunityPlus"),
    ("levelplus", "LevelPlus"),
    ("logplus", "LogPlus"),
    ("owoplus", "OwoPlus"),
    ("emojistealerplus", "EmojiStealerPlus"),
    ("exportplus", "ExportPlus"),
    ("backupplus", "BackupPlus"),
    ("introplus", "IntroPlus"),
    ("presenceplus", "PresencePlus"),
    ("settingshub", "SettingsHub"),
    ("coreplus", "CorePlus"),
    ("downloaderplus", "DownloaderPlus"),
)


@pytest.fixture
async def data_runtime(dashboard_runtime, monkeypatch, tmp_path):
    """Load independently installable cogs without live media or Discord calls."""
    from redbot.cogs.downloader.downloader import Downloader
    from redbot.core._cog_manager import CogManagerUI

    bot, dashboard, audio, member, invoke, ctx, client = dashboard_runtime
    bot._uptime = datetime.utcnow()
    member.guild_permissions = discord.Permissions.all()
    member.guild.owner_id = 777
    monkeypatch.setattr(bot, "change_presence", AsyncMock())
    for package in ("introplus", "exportplus"):
        monkeypatch.setattr(
            f"{package}.cog.cog_data_path", lambda cog: tmp_path / type(cog).__name__
        )
    monkeypatch.setattr("introplus.cog.IntroPlus._warm_existing", AsyncMock())
    manager = SimpleNamespace(
        repos=(),
        repos_folder=tmp_path / "repos",
        get_repo=lambda name: None,
        initialize=AsyncMock(),
    )
    monkeypatch.setattr("redbot.cogs.downloader.downloader.cog_data_path", lambda cog: tmp_path)
    monkeypatch.setattr("redbot.cogs.downloader.downloader.RepoManager", lambda: manager)
    native = Downloader(bot)
    native._ready.set()
    await bot.add_cog(native)
    await bot.add_cog(CogManagerUI())
    loaded = []
    try:
        for package, name in PACKAGES:
            source = getattr(importlib.import_module(package), name)(bot)
            await bot.add_cog(source)
            loaded.append(source)
        intro = bot.get_cog("IntroPlus")
        monkeypatch.setattr(intro._cache, "schedule", Mock())
        monkeypatch.setattr(intro._resolver, "search", AsyncMock())
        monkeypatch.setattr(intro._resolver, "resolve", AsyncMock())
        await login(dashboard_runtime)
        yield dashboard_runtime
    finally:
        for source in reversed(loaded):
            await bot.remove_cog(source.qualified_name)
        await bot.remove_cog("CogManagerUI")
        await bot.remove_cog("Downloader")


async def records(runtime, cog, *, page=1, query="", status=200):
    _, _, _, member, _, ctx, client = runtime
    response = await client.get(
        f"/api/data/{member.guild.id}",
        params={"channel": str(ctx.channel.id), "cog": cog, "page": str(page), "query": query},
    )
    assert response.status == status, await response.text()
    return await response.json()


def text_records(data):
    return json.dumps(data["rows"], ensure_ascii=False)


def clip(title="A personal intro"):
    return {
        "track": asdict(
            Track(
                "https://www.youtube.com/watch?v=YE7VzlLtp-4",
                title,
                "Blender",
                120000,
                "youtube",
                False,
            )
        ),
        "duration": 7.5,
        "start": 12,
    }


async def test_saved_intros_activity_and_level_records_are_fresh_and_read_only(data_runtime):
    bot, dashboard, audio, member, invoke, ctx, client = data_runtime
    intro = bot.get_cog("IntroPlus")
    community = bot.get_cog("CommunityPlus")
    level = bot.get_cog("LevelPlus")
    await intro.config.member(member).clip.set(clip())
    async with community.config.member(member).all() as data:
        data["stats"]["messages"] = 4321
        data["stats"]["voice_joins"] = 23
        data["activity_names"] = {"Stardew Valley": 17, "Minecraft": 4}
        data["participation"]["voice_seconds"] = 7200
        data["seen"]["any"] = 1791122400
    await level.config.guild(member.guild).xp.set({str(member.id): 9876})
    await level.config.guild(member.guild).names.set({str(member.id): "Stored alias"})
    before = {
        "intro": await intro.config.all_members(member.guild),
        "community": await community.config.all_members(member.guild),
        "level": await level.config.guild(member.guild).all(),
    }

    data = await records(data_runtime, "intro.clips")
    assert data["total"] == 1
    assert "A personal intro" in text_records(data)
    assert str(member.id) in text_records(data)
    assert "7.5" in text_records(data) and "12" in text_records(data)
    data = await records(data_runtime, "community.activity")
    assert data["total"] == 1
    assert "4321" in text_records(data)
    data = await records(data_runtime, "community.games")
    assert "Stardew Valley" in text_records(data)
    data = await records(data_runtime, "level.members")
    assert data["total"] == 1 and "9876" in text_records(data)

    assert before == {
        "intro": await intro.config.all_members(member.guild),
        "community": await community.config.all_members(member.guild),
        "level": await level.config.guild(member.guild).all(),
    }
    intro._cache.schedule.assert_not_called()
    intro._resolver.search.assert_not_awaited()
    intro._resolver.resolve.assert_not_awaited()
    assert not intro._workers and not intro._voices
    assert not audio._players
    ctx.channel.send.assert_not_awaited()

    await level.config.guild(member.guild).xp.set({str(member.id): 9999})
    assert "9999" in text_records(await records(data_runtime, "level.members"))
    await intro.red_delete_data_for_user(requester="user", user_id=member.id)
    assert (await records(data_runtime, "intro.clips"))["total"] == 0
    await community.red_delete_data_for_user(requester="user", user_id=member.id)
    assert (await records(data_runtime, "community.activity"))["total"] == 0
    await level.red_delete_data_for_user(requester="user", user_id=member.id)
    assert (await records(data_runtime, "level.members"))["total"] == 0


async def test_records_are_paginated_searchable_and_server_scoped(data_runtime):
    bot, dashboard, audio, member, invoke, ctx, client = data_runtime
    source = bot.get_cog("IntroPlus")
    guild = member.guild
    for index in range(73):
        person = make_member(guild, member.id + index + 1, name=f"Listener {index:02d}")
        await source.config.member(person).clip.set(clip(f"Entrance track {index:02d}"))
    other_guild = make_guild(guild.id + 1)
    other_member = make_member(other_guild, member.id, name="Other server listener")
    await source.config.member(other_member).clip.set(clip("Other server secret record"))
    first = await records(data_runtime, "intro.clips")
    assert first["total"] == 73
    assert len(first["rows"]) < first["total"] and first["pages"] > 1
    assert "Other server secret record" not in json.dumps(first)
    second = await records(data_runtime, "intro.clips", page=2)
    assert first["rows"] != second["rows"]
    assert not {json.dumps(row, sort_keys=True) for row in first["rows"]}.intersection(
        json.dumps(row, sort_keys=True) for row in second["rows"]
    )
    filtered = await records(data_runtime, "intro.clips", query="tRaCk 42")
    assert filtered["total"] == 1
    assert "Entrance track 42" in text_records(filtered)
    assert (await source.config.all_members(guild)).keys() == {
        member.id + index + 1 for index in range(73)
    }


async def test_data_api_rechecks_owner_membership_and_original_source_paths(data_runtime):
    bot, dashboard, audio, member, invoke, ctx, client = data_runtime
    guild = member.guild
    source = bot.get_cog("CommunityPlus")
    await source.config.member(member).stats.messages.set(88)
    assert (await records(data_runtime, "community.activity"))["total"] == 1
    bot.get_command("community stats").disable_in(guild)
    await records(data_runtime, "community.activity", status=403)
    bot.get_command("community stats").enable_in(guild)
    bot.get_command("activity").disable_in(guild)
    await records(data_runtime, "community.activity", status=403)
    bot.get_command("activity").enable_in(guild)
    await bot._disabled_cog_cache.disable_cog_in_guild("CommunityPlus", guild.id)
    await records(data_runtime, "community.activity", status=403)
    await bot._disabled_cog_cache.enable_cog_in_guild("CommunityPlus", guild.id)
    assert (await records(data_runtime, "community.activity"))["total"] == 1
    guild.members.remove(member)
    await records(data_runtime, "community.activity", status=403)
    guild.members.append(member)
    bot.owner_ids.remove(member.id)
    await records(data_runtime, "level.members", status=401)
    assert not dashboard._auth.sessions


async def test_private_data_routes_reject_invalid_queries(data_runtime):
    _, _, _, member, _, ctx, client = data_runtime
    path = f"/api/data/{member.guild.id}"
    base = {"channel": str(ctx.channel.id), "cog": "intro.clips", "page": "1"}
    for overrides in (
        {"page": "0"},
        {"page": "-1"},
        {"page": "1.5"},
        {"query": "x" * 201},
    ):
        response = await client.get(path, params={**base, **overrides})
        assert response.status == 400, await response.text()
    response = await client.get(path, params={**base, "channel": "not-a-channel"})
    assert response.status == 403
    response = await client.get(path, params={**base, "cog": "raw-config"})
    assert response.status in {400, 404}
    assert "token" not in await response.text()


async def test_every_cog_has_an_authenticated_data_view(data_runtime):
    bot, dashboard, audio, member, invoke, ctx, client = data_runtime
    response = await client.get(f"/api/guild/{member.guild.id}")
    assert response.status == 200, await response.text()
    catalog = (await response.json())["datasets"]
    assert {item["id"] for item in catalog} == set(DATASETS)
    names = {item["cog"] for item in catalog}
    assert names == {"AudioPlus", "DashboardPlus", *(name for _, name in PACKAGES)}
    for item in catalog:
        data = await records(data_runtime, item["id"])
        assert data["page"] == 1
        assert isinstance(data["columns"], list) and data["columns"]
        assert isinstance(data["rows"], list)
        assert data["total"] >= len(data["rows"])
        assert data["pages"] >= 1


async def test_record_views_exclude_credentials_local_paths_and_hidden_log_contents(data_runtime):
    bot, dashboard, audio, member, invoke, ctx, client = data_runtime
    now = int(time.time())
    guild = member.guild
    await audio.config.password.set("DO-NOT-EXPOSE-NODE-PASSWORD")
    await audio.config.guild(guild).listening_history.set(
        [
            {
                "id": "played-song",
                "at": now,
                "requester": member.id,
                "track": clip("Dashboard listening history")["track"],
                "stream": "https://media.invalid/?token=DO-NOT-EXPOSE-STREAM",
            }
        ]
    )
    intro = bot.get_cog("IntroPlus")
    saved = clip("Dashboard intro record")
    saved["cache_path"] = "/data/private/DO-NOT-EXPOSE-LOCAL-PATH.pcm"
    await intro.config.member(member).clip.set(saved)
    log = bot.get_cog("LogPlus")
    hidden = make_channel(guild, ctx.channel.id + 1)
    hidden.permissions_for.return_value = discord.Permissions.none()
    await log.config.guild(guild).history_records.set(
        [
            {
                "time": now,
                "event": "message_delete",
                "category": "messages",
                "source": channel.id,
                "users": [member.id],
                "title": "Deleted message",
                "description": content,
                "fields": [],
            }
            for channel, content in (
                (ctx.channel, "Visible retained log"),
                (hidden, "DO-NOT-EXPOSE-HIDDEN-CHANNEL-CONTENT"),
            )
        ]
    )
    await log.config.guild(guild).history_settings.set({"enabled": True, "days": 7})
    secret_code = dashboard._auth.issue(member.id)
    dataset_values = []
    catalog = (await (await client.get(f"/api/guild/{guild.id}")).json())["datasets"]
    for item in catalog:
        dataset_values.append(await records(data_runtime, item["id"]))
    serialized = json.dumps(dataset_values)
    for secret in (
        "DO-NOT-EXPOSE-NODE-PASSWORD",
        "DO-NOT-EXPOSE-STREAM",
        "DO-NOT-EXPOSE-LOCAL-PATH",
        "DO-NOT-EXPOSE-HIDDEN-CHANNEL-CONTENT",
        secret_code,
    ):
        assert secret not in serialized
    assert "Dashboard listening history" in serialized
    assert "Dashboard intro record" in serialized
    assert "Visible retained log" in serialized


async def test_other_cog_records_include_saved_content_and_operational_inventories(
    data_runtime, tmp_path
):
    from test_backupplus import snapshot, structure

    from exportplus.cog import ExportJob

    bot, dashboard, audio, member, invoke, ctx, client = data_runtime
    guild, now = member.guild, int(time.time())
    emoji = bot.get_cog("EmojiStealerPlus")
    await emoji.config.guild(guild).copied.set(
        {
            "100000000000000001": {
                "emoji": 100000000000000002,
                "name": "captured_dance",
                "animated": True,
                "hash": "private-image-fingerprint",
            }
        }
    )
    backup = bot.get_cog("BackupPlus")
    source_guild = make_guild(guild.id)
    structure(source_guild)
    await backup.config.guild(guild).state.snapshots.set(
        {"baseline": backup._record(snapshot(source_guild), "manual")}
    )
    owo = bot.get_cog("OwoPlus")
    await owo.config.guild(guild).user_probs.set({str(member.id): 321})
    await owo.config.guild(guild).features.custom_styles.set(
        {"space": {"words": {"hello": "Greetings Earthling"}, "prefix": "", "suffix": ""}}
    )
    await owo.config.guild(guild).poetry.hall.set(
        {
            "poem": {
                "text": "Autumn leaf drifts down",
                "author": member.id,
                "at": now,
                "approved": True,
                "approver": member.id,
            }
        }
    )
    presence = bot.get_cog("PresencePlus")
    async with presence.config.settings() as settings:
        settings["profiles"]["default"]["entries"] = [
            {"kind": "custom", "text": "Dashboard source presence"}
        ]
        settings["schedules"]["work"] = {
            "profile": "default",
            "days": [0, 1, 2, 3, 4],
            "start": 540,
            "end": 1020,
        }
    hub = bot.get_cog("SettingsHub")
    await hub.config.guild(guild).configuration_history.set(
        [
            {
                "id": "changed-setting",
                "at": now,
                "actor": member.id,
                "cog": "AudioPlus",
                "command": "audioset panel true",
                "changes": [{"path": "music.panel", "before": False, "after": True}],
                "omitted": 0,
            }
        ]
    )
    await hub.config.guild(guild).snapshots.records.set(
        [{"id": "checkpoint", "at": now, "bundle": {"cogs": {"AudioPlus": {}}}}]
    )
    source = bot.get_cog("Downloader")
    repository = SimpleNamespace(
        name="kevin",
        branch="main",
        commit="abc123",
        available_modules=(),
        folder_path=tmp_path / "repo",
        url="https://user:DO-NOT-EXPOSE-REPO-CREDENTIAL@github.com/example/repo.git",
    )
    repository.folder_path.mkdir()
    source._repo_manager.repos = (repository,)
    source._repo_manager.get_repo = lambda name: repository if name == repository.name else None
    await source.config.installed_cogs.set(
        {
            "kevin": {
                "dashboardplus": {
                    "repo_name": "kevin",
                    "module_name": "dashboardplus",
                    "commit": "abc123",
                }
            }
        }
    )
    export = bot.get_cog("ExportPlus")
    job_ctx = copy(ctx)
    job_ctx.command = bot.get_command("export server")
    job = ExportJob(job_ctx, tmp_path / "export-job", None, None, True, True)
    job.messages, job.state, job.finished = 5678, "complete", now
    job.channels = [{"id": str(ctx.channel.id), "messages": 5678}]
    export._jobs[guild.id] = job

    expected = {
        "emoji.copied": "captured_dance",
        "backup.snapshots": "baseline",
        "owo.members": "321",
        "owo.styles": "Greetings Earthling",
        "owo.haiku": "Autumn leaf drifts down",
        "presence.profiles": "Dashboard source presence",
        "presence.schedules": "work",
        "settings.history": "audioset panel true",
        "settings.snapshots": "checkpoint",
        "download.installed": "dashboardplus",
        "download.repos": "kevin",
        "export.jobs": "5678",
        "core.cogs": "IntroPlus",
        "core.status": "Discord.py",
        "dashboard.status": "8765",
    }
    results = {}
    for identifier, content in expected.items():
        data = await records(data_runtime, identifier)
        assert content in text_records(data), identifier
        results[identifier] = data
    emoji_row = results["emoji.copied"]["rows"][0]
    assert emoji_row["image"].endswith("100000000000000002.gif")
    values = results["backup.snapshots"]["rows"][0]["values"]
    assert values["roles"] == len(source_guild.roles)
    assert values["channels"] == len(source_guild.channels)
    serialized = json.dumps(results)
    assert "DO-NOT-EXPOSE-REPO-CREDENTIAL" not in serialized
    assert "private-image-fingerprint" not in serialized
    assert str(job.root) not in serialized
    assert export._jobs[guild.id] is job and job.messages == 5678
    bot.change_presence.assert_not_awaited()


async def test_hidden_source_root_and_backup_admin_requirements_are_preserved(data_runtime):
    bot, dashboard, audio, member, invoke, ctx, client = data_runtime
    bot.get_command("log").disable_in(member.guild)
    await records(data_runtime, "log.history", status=403)
    catalog = (await (await client.get(f"/api/guild/{member.guild.id}")).json())["datasets"]
    assert "log.history" not in {item["id"] for item in catalog}
    bot.get_command("log").enable_in(member.guild)
    assert (await records(data_runtime, "log.history"))["total"] == 0
    member.guild_permissions = discord.Permissions.none()
    await records(data_runtime, "backup.snapshots", status=403)
