"""Export reviewed cog policy fields from Red's JSON database, without modifying it.

Run with Python 3.10+; Red and the cogs do not need to be installed. With no
--output, print only database/policy sizes. The output is an inspection/export
format, NOT a replacement for Red's settings.json or a complete bot backup.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_EXPORT_BYTES = 16 * 1024 * 1024
SCALAR = object()


@dataclass(frozen=True)
class Map:
    item: object


@dataclass(frozen=True)
class List:
    item: object


def fields(names):
    return dict.fromkeys(names.split(), SCALAR)


IDS = List(SCALAR)
NAMESPACES = {
    "CorePlus": None,
    "DownloaderPlus": None,
    "AudioPlus": str(0xA10DEFAB),
    "BackupPlus": "702035011",
    "CommunityPlus": str(0xC0DE505),
    "EmojiStealerPlus": "702035010",
    "ExportPlus": None,  # Uses temporary files, not Red Config.
    "IntroPlus": "702035012",
    "LevelPlus": str(0x1EAF01),
    "LogPlus": str(0x51A7E11),
    "OwoPlus": str(0x5E0F1A),
    "PresencePlus": "702035013",
    "SettingsHub": "702034990",
}

# Explicit nested allowlists. Never copy an arbitrary Config dictionary as a
# policy: several sections mix settings with IDs, histories and scheduler state.
GUILD = {
    "AudioPlus": {
        "music": fields(
            "panel dj_role vote_skip fair_queue autoplay history max_seconds per_member "
            "session_summary summary_channel"
        ),
        "continuity": fields("recovery empty_pause empty_grace normalize"),
    },
    "BackupPlus": {"state": fields("auto_hours")},
    "CommunityPlus": {
        "embeds": fields("compact"),
        "autorole": fields("enabled role_id"),
        "sticky": {**fields("enabled"), "ignore": IDS},
        "welcome": fields("enabled channel_id message"),
        "cya": fields("enabled channel_id message"),
        "vcsolo": fields("enabled idle_seconds dm_notify"),
        "seen": fields("enabled"),
        "features": {
            "solo_channels": IDS,
            "solo_roles": IDS,
            "self_roles": IDS,
            **fields("warning_seconds"),
            "summary": fields("enabled channel weekday hour timezone"),
        },
        "community_tools": {
            **fields("voice_hub voice_category"),
            "native_events": fields("enabled channel minutes"),
            "onboarding": fields("enabled role rules"),
            "birthdays": fields("enabled channel role timezone hour"),
        },
    },
    "EmojiStealerPlus": {"capture": fields("enabled reactions notify channel")},
    "ExportPlus": {},
    "IntroPlus": fields("enabled volume cooldown channel"),
    "LevelPlus": {
        **fields("curve multiplier max_level"),
        "linear": fields("base inc"),
        "message": fields("enabled mode min max cooldown"),
        "reaction": fields("enabled awards min max cooldown"),
        "voice": fields("enabled min max cooldown min_members anti_afk"),
        "restrictions": {
            "no_channels": IDS,
            "no_roles": IDS,
            **fields("thread_xp forum_xp text_in_voice_xp slash_command_xp"),
        },
        "levelup": fields("enabled channel_id template image"),
        "rewards": {"roles": Map(SCALAR), **fields("stack")},
        "xp_features": fields("periods timezone repeat_seconds reaction_once daily_cap min_words"),
        "milestone_settings": {
            **fields("badges challenges"),
            "goals": {
                metric: fields("target reward") for metric in ("message", "reaction", "voice", "xp")
            },
        },
        "progress_settings": {
            **fields("streak daily_bonus max_bonus monthly announce_channel"),
            "goals": Map(fields("id metric target reward role")),
        },
    },
    "LogPlus": {
        **fields("log_channel fast_logs"),
        "overrides": Map(SCALAR),
        "message": {**fields("edit delete bulk_delete pins"), "exempt_channels": IDS},
        "reactions": fields("add remove clear"),
        "server": {
            **fields(
                "channel_create channel_delete channel_update role_create role_delete role_update "
                "server_update emoji_update sticker_update integrations_update webhooks_update "
                "thread_create thread_delete thread_update"
            ),
            "exempt_channels": IDS,
        },
        "invites": fields("create delete"),
        "member": fields("join leave roles_changed nick_changed ban unban timeout presence"),
        "voice": fields("join move leave mute deaf video stream"),
        "sched": fields("create update delete user_add user_remove"),
        "commands": fields("this_bot other_bots"),
        "rate": fields("seconds"),
        "style": fields("compact"),
        "features": {
            "routes": fields("message reactions server invites member voice sched commands"),
            **fields("retry"),
        },
        "history_settings": fields("enabled days"),
        "alert_settings": {
            **fields("channel"),
            "bursts": {
                metric: fields("enabled threshold window")
                for metric in ("joins", "deletes", "permissions")
            },
            "digest": fields("enabled timezone hour"),
            "errors": fields("enabled threshold window"),
        },
    },
    "OwoPlus": {
        **fields("enabled one_in owner_bypass haiku_enabled"),
        "features": {
            **fields("channel_mode keywords intensity cooldown"),
            "allowed": IDS,
            "excluded": IDS,
            "words": Map(SCALAR),
            "syllables": Map(SCALAR),
            "channel_styles": Map(fields("style expires")),
            "custom_styles": Map({"words": Map(SCALAR), **fields("prefix suffix uppercase")}),
        },
        "fun_settings": fields("undo"),
    },
    "PresencePlus": {},
    "SettingsHub": {
        "theme": {"colors": fields("info success warning error"), **fields("footer")},
        "snapshots": fields("enabled hours"),
        "audit_policy": fields("enabled days"),
    },
}
GLOBAL = {
    "AudioPlus": {"watchdog": fields("enabled guild_id channel_id video_url hour minute timezone")},
    "PresencePlus": {
        "settings": {
            **fields("enabled selected interval timezone"),
            "profiles": Map({**fields("status"), "entries": List(fields("kind text"))}),
            "schedules": Map({**fields("profile start end"), "days": IDS}),
            "music": fields("enabled guild_id text"),
        }
    },
}


class ExportError(ValueError):
    """A safe, value-free message suitable for command-line reporting."""


def select(value, rule, path):
    """Select only reviewed fields; unexpected known-field shapes fail closed."""
    if rule is SCALAR:
        if value is None or type(value) in (str, bool, int):
            return value
        if type(value) is float and math.isfinite(value):
            return value
    elif isinstance(rule, List):
        if isinstance(value, list):
            return [select(item, rule.item, path + "[]") for item in value]
    elif isinstance(value, dict):
        if isinstance(rule, Map):
            return {key: select(item, rule.item, path + ".*") for key, item in value.items()}
        if isinstance(rule, dict):
            return {
                key: select(value[key], child, path + "." + key)
                for key, child in rule.items()
                if key in value
            }
    raise ExportError(f"Unexpected JSON shape at {path}; no export was written.")


def project(name, database):
    """Project only the active namespace's GLOBAL/GUILD policies, never user scopes."""
    identifier = NAMESPACES[name]
    if not isinstance(database, dict):
        raise ExportError(f"{name}: expected a Red JSON database object.")
    if identifier is None or identifier not in database:
        return {}
    namespace = database[identifier]
    if not isinstance(namespace, dict):
        raise ExportError(f"{name}: invalid Config namespace.")
    result = {}
    if name in GLOBAL and "GLOBAL" in namespace:
        result["GLOBAL"] = select(namespace["GLOBAL"], GLOBAL[name], name + ".GLOBAL")
    if GUILD[name] and "GUILD" in namespace:
        guilds = namespace["GUILD"]
        if not isinstance(guilds, dict):
            raise ExportError(f"{name}: invalid server settings object.")
        result["GUILD"] = {}
        for key, record in guilds.items():
            if not key.isascii() or not key.isdigit() or len(key) > 20 or not 0 < int(key) < 2**64:
                raise ExportError(f"{name}: invalid server ID.")
            result["GUILD"][key] = select(record, GUILD[name], name + ".GUILD.*")
    return {identifier: result} if result else {}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExportError("Duplicate JSON key in a source database.")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ExportError("Non-finite JSON number in a source database.")


def read_database(path):
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise ExportError("Source databases must be regular files, without cog/file symlinks.")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ExportError("Source database exceeds the 256 MiB read limit.")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ExportError("Cannot read a source database as valid UTF-8 JSON.") from error
    return value, len(raw)


def collect(source):
    if not source.is_dir():
        raise ExportError("Use the directory containing Red's cog data folders.")
    cogs, report = {}, []
    for name, identifier in NAMESPACES.items():
        path = source / name / "settings.json"
        if not path.exists() and not path.is_symlink():
            continue
        database, source_bytes = read_database(path)
        selected = project(name, database)
        policy_bytes = len(json.dumps(selected, ensure_ascii=False, sort_keys=True).encode())
        cogs[name] = selected
        skipped = sum(key != identifier for key in database)
        report.append((name, source_bytes, policy_bytes, skipped))
    if not report:
        raise ExportError("No supported cog settings.json files found in the source directory.")
    return {"format": "kevin-cogs/settings-only", "schema": 1, "cogs": cogs}, report


def write_export(output, bundle, source):
    """Commit one complete export atomically outside the live database directory."""
    destination = output.resolve()
    if (
        output.is_symlink()
        or destination.is_relative_to(source.resolve())
        or output.name == "settings.json"
    ):
        raise ExportError(
            "Write to a separate export folder, using a name other than settings.json."
        )
    raw = (json.dumps(bundle, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()
    if len(raw) > MAX_EXPORT_BYTES:
        raise ExportError("Settings export exceeds the 16 MiB output limit.")
    temporary = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".settings-export-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    except OSError as error:
        raise ExportError("Cannot save the settings-only export.") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Red's cog data directory")
    parser.add_argument("--output", type=Path, help="Separate settings-only JSON file")
    args = parser.parse_args(argv)
    try:
        bundle, report = collect(args.source)
        if args.output is not None:
            write_export(args.output, bundle, args.source)
    except (ExportError, OSError, RecursionError) as error:
        message = str(error) if isinstance(error, ExportError) else "Cannot complete the export."
        parser.exit(1, message + "\n")
    print(f"{'Cog':<20} {'Database bytes':>16} {'Policy bytes':>14}")
    for name, original, selected, skipped in report:
        print(f"{name:<20} {original:>16,} {selected:>14,}")
        if skipped:
            print(f"  Ignored {skipped} inactive/unsupported Config namespace(s).")
    if args.output is None:
        print("Audit only. Use --output to save a settings-only export.")
    else:
        print("Settings-only export saved. Source databases were not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
