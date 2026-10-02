"""Explicit backup scope and strict validation without importing other cog packages."""

import json
import math
import re
from copy import deepcopy
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from redbot.core import commands

MAX_FILE = 256 * 1024
TARGETS = {
    "AudioPlus": ("music", "audioset setup"),
    "CommunityPlus": ("community", "community setup"),
    "LevelPlus": ("level", "level setup"),
    "LogPlus": ("log", "log setup"),
    "OwoPlus": ("owo", "owo setup"),
    "EmojiStealerPlus": ("emoji", "emoji"),
}
FIELDS = {
    "AudioPlus": ("music", "continuity"),
    "CommunityPlus": (
        "embeds",
        "autorole",
        "sticky",
        "welcome",
        "cya",
        "vcsolo",
        "seen",
        "community_tools",
        "features",
    ),
    "LevelPlus": (
        "curve",
        "multiplier",
        "max_level",
        "linear",
        "message",
        "reaction",
        "voice",
        "restrictions",
        "levelup",
        "rewards",
        "xp_features",
        "milestone_settings",
        "progress_settings",
    ),
    "LogPlus": (
        "log_channel",
        "fast_logs",
        "overrides",
        "message",
        "reactions",
        "server",
        "invites",
        "member",
        "voice",
        "sched",
        "commands",
        "rate",
        "style",
        "features",
        "history_settings",
        "alert_settings",
    ),
    "OwoPlus": ("enabled", "one_in", "owner_bypass", "haiku_enabled", "features", "fun_settings"),
    "EmojiStealerPlus": ("capture",),
}
EXCLUDED = {
    "CommunityPlus": ("features.role_menus", "features.summary.last_week"),
    "LevelPlus": ("xp_features.boosts",),
    "LogPlus": ("alert_settings.errors.recipient",),
    "OwoPlus": ("features.optouts",),
}
ROLE_LISTS = {
    "sticky.ignore",
    "features.self_roles",
    "features.solo_roles",
    "restrictions.no_roles",
}
ROLE_FIELDS = {
    "music.dj_role",
    "autorole.role_id",
    "community_tools.onboarding.role",
    "community_tools.birthdays.role",
}
MAP_PATHS = {
    "overrides",
    "rewards.roles",
    "features.routes",
    "features.words",
    "features.syllables",
    "features.channel_styles",
    "features.custom_styles",
    "progress_settings.goals",
}
ENUMS = {
    "curve": {"linear", "exponential", "constant"},
    "message.mode": {"none", "random", "perword"},
    "reaction.awards": {"none", "both", "author", "reactor"},
    "features.channel_mode": {"all", "allowlist"},
}
RANGES = {
    "music.max_seconds": (0, 86400),
    "music.per_member": (0, 100),
    "continuity.empty_grace": (10, 3600),
    "community_tools.birthdays.hour": (0, 23),
    "community_tools.native_events.minutes": (1, 1440),
    "progress_settings.daily_bonus": (0, 100),
    "progress_settings.max_bonus": (0, 1000),
    "alert_settings.digest.hour": (0, 23),
    "alert_settings.errors.threshold": (2, 50),
    "alert_settings.errors.window": (30, 3600),
    **{
        f"alert_settings.bursts.{metric}.{field}": bounds
        for metric in ("joins", "deletes", "permissions")
        for field, bounds in (("threshold", (2, 1000)), ("window", (10, 3600)))
    },
    "multiplier": (0.1, 10),
    "voice.min_members": (1, 99),
    "message.cooldown": (0, 3600),
    "reaction.cooldown": (0, 3600),
    "voice.cooldown": (1, 3600),
    "vcsolo.idle_seconds": (60, 604800),
    "features.warning_seconds": (0, 3600),
    "features.summary.weekday": (0, 6),
    "features.summary.hour": (0, 23),
    "features.intensity": (0, 5),
    "features.cooldown": (0, 3600),
    "one_in": (1, 1000000),
    "xp_features.repeat_seconds": (0, 3600),
    "xp_features.min_words": (0, 100),
    "xp_features.daily_cap": (0, 1000000000),
    "history_settings.days": (1, 90),
}


def fail(path):
    raise commands.BadArgument(
        f"Invalid backup setting: {path}. Use a fresh settings backup and current server IDs."
    )


def select_fields(name, record):
    result = {key: deepcopy(record[key]) for key in FIELDS[name]}
    for path in EXCLUDED.get(name, ()):
        parts = path.split(".")
        node = result
        for part in parts[:-1]:
            node = node[part]
        node.pop(parts[-1], None)
    return result


def merge_fields(current, incoming, path=""):
    """Keep member data and operational fields omitted by the backup scope."""
    result = deepcopy(current)
    for key, value in incoming.items():
        child_path = f"{path}.{key}".lstrip(".")
        result[key] = (
            merge_fields(result[key], value, child_path)
            if isinstance(value, dict)
            and isinstance(result.get(key), dict)
            and child_path not in MAP_PATHS
            else deepcopy(value)
        )
    return result


def parse_backup(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail("duplicate key " + key)
            result[key] = value
        return result

    if len(raw) > MAX_FILE:
        fail("file exceeds 256 KiB")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: fail(value))
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise commands.BadArgument("Upload a valid UTF-8 JSON settings backup.") from error
    if not isinstance(value, dict) or set(value) != {"schema", "guild_id", "cogs"}:
        fail("file structure")
    if (
        type(value["schema"]) is not int
        or value["schema"] != 1
        or type(value["guild_id"]) is not int
    ):
        fail("schema/server ID")
    if (
        not isinstance(value["cogs"], dict)
        or not value["cogs"]
        or set(value["cogs"]) - TARGETS.keys()
    ):
        fail("cogs")
    return value


def server_id(guild, value, path, *, role=False, grant=False, text=False):
    if type(value) is not int or not 0 < value < 2**64:
        fail(path)
    item = guild.get_role(value) if role else guild.get_channel_or_thread(value)
    if item is None or (text and not isinstance(item, discord.TextChannel)):
        fail(path)
    if grant:
        dangerous = {
            "administrator",
            "ban_members",
            "kick_members",
            "moderate_members",
            "mute_members",
            "deafen_members",
            "move_members",
            "mention_everyone",
            "view_audit_log",
            "view_guild_insights",
            "view_creator_monetization_analytics",
        }
        if (
            guild.me is None
            or item.is_default()
            or item.managed
            or item >= guild.me.top_role
            or any(
                enabled and (name.startswith("manage_") or name in dangerous)
                for name, enabled in item.permissions
            )
        ):
            fail(path + " unsafe role")


def validate_map(guild, path, value):
    if path == "features.custom_styles":
        if len(value) > 10:
            fail(path)
        for name, item in value.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", name) or name in {
                "owo",
                "pirate",
                "robot",
            }:
                fail(path)
            if not isinstance(item, dict) or set(item) != {
                "words",
                "prefix",
                "suffix",
                "uppercase",
            }:
                fail(path)
            if (
                not isinstance(item["words"], dict)
                or len(item["words"]) > 50
                or type(item["uppercase"]) is not bool
            ):
                fail(path)
            for text in (item["prefix"], item["suffix"]):
                if (
                    not isinstance(text, str)
                    or len(text) > 80
                    or any(ord(char) < 32 for char in text)
                ):
                    fail(path)
            validate_map(guild, "features.words", item["words"])
            if any(word is None for word in item["words"].values()):
                fail(path)
        return
    if path == "progress_settings.goals":
        if len(value) > 25:
            fail(path)
        for name, goal in value.items():
            if (
                not re.fullmatch(r"[a-z0-9_-]{1,32}", name)
                or not isinstance(goal, dict)
                or set(goal) != {"id", "metric", "target", "reward", "role"}
            ):
                fail(path)
            if (
                not isinstance(goal["id"], str)
                or not re.fullmatch(r"[a-f0-9]{12}", goal["id"])
                or goal["metric"] not in {"xp", "message", "reaction", "voice", "level", "streak"}
            ):
                fail(path)
            if (
                type(goal["target"]) is not int
                or not 1 <= goal["target"] <= 1000000000
                or type(goal["reward"]) is not int
                or not 0 <= goal["reward"] <= 10000
            ):
                fail(path)
            if goal["role"] is not None:
                server_id(guild, goal["role"], path, role=True, grant=True)
        return
    if len(value) > (500 if path == "features.syllables" else 100):
        fail(path)
    for key, item in value.items():
        if path in {"overrides", "rewards.roles", "features.channel_styles"}:
            if not re.fullmatch(r"[1-9]\d{0,19}", key):
                fail(path)
        if path == "overrides":
            server_id(guild, int(key), path)
            server_id(guild, item, path, text=True)
        elif path == "rewards.roles":
            if type(item) is not int or not 1 <= item <= 100000:
                fail(path)
            server_id(guild, int(key), path, role=True, grant=True)
        elif path == "features.routes":
            if key not in {
                "message",
                "reactions",
                "server",
                "invites",
                "member",
                "voice",
                "sched",
                "commands",
            }:
                fail(path)
            server_id(guild, item, path, text=True)
        elif path == "features.channel_styles":
            server_id(guild, int(key), path)
            if (
                not isinstance(item, dict)
                or set(item) != {"style", "expires"}
                or not isinstance(item["style"], str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", item["style"])
                or type(item["expires"]) not in {int, float}
                or not math.isfinite(item["expires"])
                or not 0 <= item["expires"] < 2**53
            ):
                fail(path)
        elif path in {"features.words", "features.syllables"}:
            if len(key) > 40 or not re.fullmatch(r"[a-z]+(?:'[a-z]+)?", key):
                fail(path)
            if path.endswith("syllables"):
                if type(item) is not int or not 1 <= item <= 10:
                    fail(path)
            elif item is not None and (
                not isinstance(item, str)
                or not 1 <= len(item) <= 60
                or any(ord(char) < 32 for char in item)
            ):
                fail(path)
        else:
            fail(path)


def validate_fields(guild, expected, incoming, path=""):
    if isinstance(expected, dict):
        if not isinstance(incoming, dict):
            fail(path)
        if not expected:
            validate_map(guild, path, incoming)
        else:
            if set(expected) != set(incoming):
                fail(path)
            if path == "features" and "custom_styles" in incoming:
                if not isinstance(incoming["custom_styles"], dict) or not isinstance(
                    incoming["channel_styles"], dict
                ):
                    fail("features.custom_styles")
                valid_styles = {"owo", "pirate", "robot", *incoming["custom_styles"]}
                if any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("style"), str)
                    or item.get("style") not in valid_styles
                    for item in incoming["channel_styles"].values()
                ):
                    fail("features.channel_styles")
            for key in expected:
                validate_fields(guild, expected[key], incoming[key], f"{path}.{key}".lstrip("."))
    elif isinstance(expected, list):
        if (
            not isinstance(incoming, list)
            or len(incoming) > (25 if path == "features.self_roles" else 500)
            or len(set(map(str, incoming))) != len(incoming)
        ):
            fail(path)
        for value in incoming:
            server_id(
                guild, value, path, role=path in ROLE_LISTS, grant=path == "features.self_roles"
            )
    elif expected is None:
        if incoming is not None:
            channel_types = {
                "community_tools.voice_hub": discord.VoiceChannel,
                "community_tools.voice_category": discord.CategoryChannel,
                "community_tools.native_events.channel": discord.VoiceChannel,
            }
            if path in channel_types and not isinstance(
                guild.get_channel(incoming), channel_types[path]
            ):
                fail(path)
            server_id(
                guild,
                incoming,
                path,
                role=path in ROLE_FIELDS,
                grant=path
                in {
                    "autorole.role_id",
                    "community_tools.onboarding.role",
                    "community_tools.birthdays.role",
                },
                text=path
                in {
                    "log_channel",
                    "music.summary_channel",
                    "capture.channel",
                    "welcome.channel_id",
                    "cya.channel_id",
                    "levelup.channel_id",
                    "features.summary.channel",
                    "community_tools.birthdays.channel",
                    "progress_settings.announce_channel",
                    "alert_settings.channel",
                },
            )
    elif isinstance(expected, bool):
        if type(incoming) is not bool:
            fail(path)
    elif type(expected) in {int, float}:
        low, high = RANGES.get(path, (0, 1000000000))
        if path.endswith(".target"):
            low, high = 1, 100000
        elif path.endswith(".reward"):
            low, high = 0, 10000
        if (
            type(incoming) not in ({int} if type(expected) is int else {int, float})
            or not math.isfinite(incoming)
            or not low <= incoming <= high
        ):
            fail(path)
    elif isinstance(expected, str):
        if not isinstance(incoming, str) or len(incoming) > 2000:
            fail(path)
        if path in ENUMS and incoming not in ENUMS[path]:
            fail(path)
        if path.endswith("timezone"):
            try:
                ZoneInfo(incoming)
            except (ZoneInfoNotFoundError, ValueError):
                fail(path)
    else:
        fail(path)
