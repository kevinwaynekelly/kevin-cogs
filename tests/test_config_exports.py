"""Review the whole export scope against actual cog defaults and Red JSON storage."""

import importlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from redbot.core import Config
from redbot.core._drivers.json import JsonDriver

from scripts import red_settings_export as exporter
from settingshub.schema import FIELDS, select_fields

NON_POLICY_GUILD = {
    "DashboardPlus": set(),
    "DownloaderPlus": set(),
    "NotificationPlus": set(),
    "AudioPlus": {"playlists", "favorites", "listening_history", "recovery", "server_playlists"},
    "BackupPlus": set(),
    "CommunityPlus": {"social", "voice_rooms"},
    "EmojiStealerPlus": {"copied", "last_error"},
    "ExportPlus": set(),
    "IntroPlus": set(),
    "LevelPlus": {
        "xp",
        "names",
        "period_xp",
        "earned_today",
        "milestones",
        "progress",
        "season_calendar",
    },
    "LogPlus": {"history_records", "moderation_summary", "incident_cases"},
    "OwoPlus": {"user_probs", "poetry"},
    "PresencePlus": set(),
    "SettingsHub": {"configuration_history"},
}
NON_POLICY_GLOBAL = {
    "DownloaderPlus": {"slash_sync"},
    "AudioPlus": {"host", "port", "password", "secure", "resume_timeout"},
    "PresencePlus": {"command_hint_version"},
}
NON_POLICY_NESTED = {
    "DownloaderPlus": tuple(
        "GLOBAL.webhook." + key
        for key in ("secret", "owner_id", "channel_id", "pending", "deliveries", "last_result")
    )
    + tuple(
        "GLOBAL.daily." + key
        for key in ("owner_id", "channel_id", "generation", "next_run", "last_result")
    ),
    "AudioPlus": tuple(
        "GLOBAL.watchdog." + key
        for key in (
            "recipient_id",
            "last_check_day",
            "retry_at",
            "last_result",
            "pending_alert",
            "alert_retry_at",
            "last_alert_error",
        )
    ),
    "BackupPlus": tuple(
        "GUILD.state." + key for key in ("snapshots", "last_attempt", "last_error", "last_restore")
    ),
    "CommunityPlus": ("GUILD.features.role_menus", "GUILD.features.summary.last_week"),
    "LevelPlus": ("GUILD.xp_features.boosts",),
    "LogPlus": ("GUILD.alert_settings.errors.recipient",),
    "OwoPlus": ("GUILD.features.optouts",),
    "SettingsHub": ("GUILD.snapshots.last_at", "GUILD.snapshots.records"),
}


@pytest.mark.parametrize("name", exporter.NAMESPACES)
def test_scope_reviews_every_registered_default_and_matches_hub(name, bot, monkeypatch, tmp_path):
    package = name.lower()
    for module_name in ("introplus", "exportplus", "notificationplus"):
        monkeypatch.setattr(
            f"{module_name}.cog.cog_data_path", lambda cog: tmp_path / type(cog).__name__
        )
    cog = getattr(importlib.import_module(package), name)(bot)
    if exporter.NAMESPACES[name] is None:
        assert not hasattr(cog, "config")
        assert exporter.NAMESPACES[name] is None
        return
    defaults = cog.config.defaults
    assert int(cog.config.unique_identifier.split(":")[0]) == int(exporter.NAMESPACES[name])
    assert set(defaults.get("GUILD", {})) == set(exporter.GUILD[name]) | NON_POLICY_GUILD[name]
    assert set(defaults.get("GLOBAL", {})) == set(
        exporter.GLOBAL.get(name, {})
    ) | NON_POLICY_GLOBAL.get(name, set())
    raw = deepcopy(defaults)
    guild_defaults = raw.pop("GUILD", {})
    if guild_defaults:
        raw["GUILD"] = {"123": guild_defaults}
    selected = exporter.project(name, {exporter.NAMESPACES[name]: raw})
    policies = {
        "GUILD": {
            key: deepcopy(value)
            for key, value in guild_defaults.items()
            if key not in NON_POLICY_GUILD[name]
        },
        "GLOBAL": {
            key: deepcopy(value)
            for key, value in defaults.get("GLOBAL", {}).items()
            if key not in NON_POLICY_GLOBAL.get(name, set())
        },
    }
    for path in NON_POLICY_NESTED.get(name, ()):
        *parents, key = path.split(".")
        node = policies
        for part in parents:
            node = node[part]
        node.pop(key)
    expected = {
        scope: ({"123": record} if scope == "GUILD" else record)
        for scope, record in policies.items()
        if record
    }
    assert selected[exporter.NAMESPACES[name]] == expected
    if name in FIELDS:
        assert selected[exporter.NAMESPACES[name]]["GUILD"]["123"] == select_fields(
            name, guild_defaults
        )


def test_nested_allowlist_removes_records_secrets_and_future_fields():
    secret = "private-record-must-never-be-exported"
    examples = {
        "AudioPlus": {
            "GLOBAL": {
                "password": secret,
                "watchdog": {
                    "enabled": True,
                    "guild_id": 123,
                    "recipient_id": secret,
                    "last_result": secret,
                    "pending_alert": secret,
                    "retry_at": 99,
                },
            },
            "GUILD": {
                "123": {
                    "music": {"panel": True, "secret": secret},
                    "listening_history": [secret],
                    "playlists": {secret: []},
                    "favorites": {secret: []},
                    "recovery": secret,
                }
            },
        },
        "BackupPlus": {
            "GUILD": {
                "123": {"state": {"auto_hours": 24, "snapshots": secret, "last_restore": secret}}
            }
        },
        "CommunityPlus": {
            "GUILD": {
                "123": {
                    "features": {
                        "self_roles": [456],
                        "role_menus": secret,
                        "summary": {"timezone": "UTC", "last_week": secret},
                    },
                    "social": secret,
                    "voice_rooms": secret,
                }
            }
        },
        "EmojiStealerPlus": {
            "GUILD": {"123": {"capture": {"enabled": True}, "copied": secret, "last_error": secret}}
        },
        "IntroPlus": {"GUILD": {"123": {"enabled": True, "cooldown": 60}}},
        "LevelPlus": {
            "GUILD": {
                "123": {
                    "curve": "linear",
                    "xp": secret,
                    "names": secret,
                    "milestones": secret,
                    "xp_features": {"daily_cap": 100, "boosts": secret},
                    "progress": secret,
                    "progress_settings": {
                        "goals": {
                            "chatty": {
                                "id": "goal",
                                "metric": "message",
                                "target": 100,
                                "reward": 5,
                                "role": None,
                                "history": secret,
                            }
                        }
                    },
                }
            }
        },
        "LogPlus": {
            "GUILD": {
                "123": {
                    "log_channel": 456,
                    "history_records": secret,
                    "incident_cases": secret,
                    "alert_settings": {
                        "errors": {"enabled": True, "threshold": 5, "recipient": secret}
                    },
                }
            }
        },
        "OwoPlus": {
            "GUILD": {
                "123": {
                    "enabled": True,
                    "user_probs": secret,
                    "poetry": secret,
                    "features": {
                        "optouts": secret,
                        "custom_styles": {
                            "space": {"words": {"hello": "greetings"}, "history": secret}
                        },
                    },
                }
            }
        },
        "PresencePlus": {
            "GLOBAL": {
                "command_hint_version": 1,
                "settings": {
                    "enabled": True,
                    "profiles": {
                        "default": {
                            "status": "online",
                            "entries": [{"kind": "custom", "text": "Use /play", "token": secret}],
                            "history": secret,
                        }
                    },
                    "schedules": {
                        "night": {
                            "profile": "default",
                            "days": [1],
                            "start": 60,
                            "end": 90,
                            "actor": secret,
                        }
                    },
                },
            }
        },
        "SettingsHub": {
            "GUILD": {
                "123": {
                    "snapshots": {"enabled": True, "hours": 6, "records": secret, "last_at": 99},
                    "configuration_history": secret,
                    "theme": {"footer": "Scarlet"},
                }
            }
        },
    }
    for name, namespace in examples.items():
        original = deepcopy(namespace)
        namespace.update(
            {scope: {secret: secret} for scope in ("MEMBER", "USER", "CHANNEL", "ROLE", "CUSTOM")}
        )
        namespace["future_records"] = secret
        database = {
            exporter.NAMESPACES[name]: namespace,
            "inactive": {"GLOBAL": {"password": secret}},
        }
        saved = deepcopy(database)
        selected = exporter.project(name, database)
        encoded = json.dumps(selected)
        assert secret not in encoded, name
        assert "inactive" not in selected
        assert selected, name
        assert database == saved
        assert "GLOBAL" in original or "GUILD" in original
    assert exporter.project("ExportPlus", {"unused": {"GUILD": {"123": {"chats": secret}}}}) == {}


async def test_native_database_with_large_member_activity_exports_only_policy(tmp_path):
    source = tmp_path / "red-data"
    identifier = exporter.NAMESPACES["CommunityPlus"]
    driver = JsonDriver(
        "ExportAuditCommunity", identifier, data_path_override=source / "CommunityPlus"
    )
    conf = Config("ExportAuditCommunity", identifier, driver, True)
    conf.register_guild(seen={"enabled": True}, welcome={"channel_id": None, "message": "Welcome"})
    conf.register_member(activity_names={}, stats={"messages": 0})
    await conf.guild_from_id(123).seen.enabled.set(False)
    await conf.guild_from_id(123).welcome.message.set("Hello {mention}")
    private = "private-activity-record-" * 25000
    for uid in (456, 457, 458):
        await conf.member_from_ids(123, uid).activity_names.set({private: 5})
    path = source / "CommunityPlus" / "settings.json"
    before = path.read_bytes()
    assert len(before) > 1_690_000
    bundle, report = exporter.collect(source)
    selected = bundle["cogs"]["CommunityPlus"][identifier]
    assert selected == {
        "GUILD": {"123": {"seen": {"enabled": False}, "welcome": {"message": "Hello {mention}"}}}
    }
    assert report[0][1] == len(before)
    assert report[0][2] < 200
    output = tmp_path / "repo" / "kevin-cogs-settings.json"
    exporter.write_export(output, bundle, source)
    assert path.read_bytes() == before
    assert output.stat().st_size < 500
    assert json.loads(output.read_text()) == bundle
    assert "MEMBER" not in output.read_text()
    assert not list(output.parent.glob(".settings-export-*"))


@pytest.mark.parametrize(
    "raw",
    [
        '{"same": 1, "same": "secret-do-not-print"}',
        '{"number": NaN}',
        '{"number": Infinity}',
        '{"not": "finished',
        "[1, 2]",
    ],
)
def test_invalid_source_preserves_previous_export_and_never_echoes_values(tmp_path, capsys, raw):
    source = tmp_path / "cogs"
    path = source / "CommunityPlus" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(raw)
    output = tmp_path / "config.json"
    output.write_text("previous-good-export")
    with pytest.raises(SystemExit) as error:
        exporter.main(["--source", str(source), "--output", str(output)])
    assert error.value.code == 1
    assert output.read_text() == "previous-good-export"
    assert "secret-do-not-print" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "record",
    [
        {"seen": []},
        {"welcome": {"message": {"private": "secret"}}},
        {"features": {"self_roles": {"private": "secret"}}},
    ],
)
def test_wrong_known_field_shape_fails_closed(record):
    with pytest.raises(exporter.ExportError, match="Unexpected JSON shape") as error:
        exporter.project(
            "CommunityPlus", {exporter.NAMESPACES["CommunityPlus"]: {"GUILD": {"123": record}}}
        )
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("guild_id", ["0", "-1", "9" * 4301, str(2**64), "private-member-name"])
def test_invalid_server_ids_are_rejected_without_echoing_them(guild_id):
    with pytest.raises(exporter.ExportError, match="invalid server ID"):
        exporter.project(
            "IntroPlus",
            {exporter.NAMESPACES["IntroPlus"]: {"GUILD": {guild_id: {"enabled": True}}}},
        )


def test_audit_only_and_stdlib_cli_output_is_deterministic(tmp_path, capsys):
    source = tmp_path / "cogs"
    path = source / "IntroPlus" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                exporter.NAMESPACES["IntroPlus"]: {
                    "GUILD": {"123": {"volume": 70}},
                    "MEMBER": {"secret": "must-not-print"},
                }
            }
        )
    )
    assert exporter.main(["--source", str(source)]) == 0
    stdout = capsys.readouterr().out
    assert "Audit only" in stdout and "IntroPlus" in stdout
    assert "must-not-print" not in stdout
    output = tmp_path / "settings-only.json"
    args = [
        sys.executable,
        "-I",
        str(Path(exporter.__file__).resolve()),
        "--source",
        str(source),
        "--output",
        str(output),
    ]
    completed = subprocess.run(args, capture_output=True, text=True, check=True)
    assert "Settings-only export saved" in completed.stdout
    before = output.read_bytes()
    subprocess.run(args, capture_output=True, text=True, check=True)
    assert output.read_bytes() == before
    assert path.read_text().find("must-not-print") >= 0


def test_export_cannot_overwrite_source_or_follow_symlinks(tmp_path, monkeypatch):
    source = tmp_path / "cogs"
    path = source / "IntroPlus" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    with pytest.raises(exporter.ExportError, match="separate export folder"):
        exporter.write_export(path, {}, source)
    with pytest.raises(exporter.ExportError, match="separate export folder"):
        exporter.write_export(tmp_path / "settings.json", {}, source)
    linked_output = tmp_path / "linked.json"
    linked_output.symlink_to(path)
    with pytest.raises(exporter.ExportError):
        exporter.write_export(linked_output, {}, source)
    linked_cog = tmp_path / "linked-cogs"
    linked_cog.mkdir()
    (linked_cog / "IntroPlus").symlink_to(path.parent)
    with pytest.raises(exporter.ExportError, match="symlinks"):
        exporter.collect(linked_cog)
    path.unlink()
    path.symlink_to(tmp_path / "other.json")
    (tmp_path / "other.json").write_text("{}")
    with pytest.raises(exporter.ExportError, match="symlinks"):
        exporter.collect(source)


def test_limits_and_failed_atomic_replace_keep_previous_output(tmp_path, monkeypatch):
    source = tmp_path / "cogs"
    source.mkdir()
    output = tmp_path / "settings-only.json"
    output.write_text("previous")
    monkeypatch.setattr(exporter, "MAX_EXPORT_BYTES", 1)
    with pytest.raises(exporter.ExportError, match="output limit"):
        exporter.write_export(output, {"schema": 1}, source)
    assert output.read_text() == "previous"
    monkeypatch.setattr(exporter, "MAX_EXPORT_BYTES", 1024)

    def fail_replace(*args):
        raise OSError("private-native-error")

    monkeypatch.setattr(exporter.os, "replace", fail_replace)
    with pytest.raises(exporter.ExportError, match="Cannot save") as error:
        exporter.write_export(output, {"schema": 1}, source)
    assert "private-native-error" not in str(error.value)
    assert output.read_text() == "previous"
    assert not list(tmp_path.glob(".settings-export-*"))
    path = source / "IntroPlus" / "settings.json"
    path.parent.mkdir()
    path.write_text("{}")
    monkeypatch.setattr(exporter, "MAX_SOURCE_BYTES", 1)
    with pytest.raises(exporter.ExportError, match="read limit"):
        exporter.collect(source)


def test_no_supported_databases_is_an_error(tmp_path):
    (tmp_path / "Core").mkdir()
    (tmp_path / "Core" / "settings.json").write_text('{"token": "private-bot-token"}')
    with pytest.raises(exporter.ExportError, match="No supported cog"):
        exporter.collect(tmp_path)
