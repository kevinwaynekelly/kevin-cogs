"""Strict, same-server structural snapshots using public Discord.py objects."""

import hashlib
import json
import math
import re
import time
from copy import deepcopy

import discord
from redbot.core import commands

from .constants import MAX_CHANNELS, MAX_FILE, MAX_OVERWRITES, MAX_ROLES

KINDS = {0: "text", 2: "voice", 4: "category", 5: "news", 13: "stage", 15: "forum", 16: "media"}
ROLE_FIELDS = {
    "name",
    "permissions",
    "colour",
    "secondary_colour",
    "tertiary_colour",
    "hoist",
    "mentionable",
}
COMMON = {"name", "position", "category", "overwrites", "nsfw"}
TEXT = {"topic", "slowmode_delay", "default_auto_archive_duration", "default_thread_slowmode_delay"}
VOICE = {"bitrate", "user_limit", "rtc_region", "video_quality_mode"}
FORUM = TEXT | {
    "available_tags",
    "default_reaction_emoji",
    "default_sort_order",
    "default_layout",
    "require_tag",
}


def invalid(detail):
    raise commands.BadArgument("Invalid server backup: " + detail)


def name_key(value, *, reserved=False):
    value = value.lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", value):
        raise commands.BadArgument(
            "Use a backup name of 1–32 letters, numbers, underscores or hyphens."
        )
    if not reserved and value.startswith(("auto-", "before-")):
        raise commands.BadArgument(
            "The auto- and before- prefixes are reserved for system snapshots."
        )
    return value


def encode(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(encode(value)).hexdigest()


def portable(data, mappings):
    """Export recovered object IDs so re-imports do not recreate replacements."""
    result = deepcopy(data)
    role_ids, channel_ids = mappings["roles"], mappings["channels"]
    for row in result["roles"]:
        row["id"] = role_ids.get(row["id"], row["id"])
    for row in result["channels"]:
        row["id"] = channel_ids.get(row["id"], row["id"])
        if row["category"]:
            row["category"] = channel_ids.get(row["category"], row["category"])
        for overwrite in row["overwrites"]:
            if overwrite["kind"] == "role":
                overwrite["id"] = role_ids.get(overwrite["id"], overwrite["id"])
    return validate(result, int(data["guild_id"]))


def snowflake(value):
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or not 0 < int(value) < 2**64
    ):
        invalid("bad object ID")
    if str(int(value)) != value:
        invalid("noncanonical object ID")


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        invalid("integer outside its supported range")


def text(value, maximum, *, empty=True, optional=False):
    if value is None and optional:
        return
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value)
        or "\x00" in value
    ):
        invalid("invalid text field")


def boolean(value):
    if type(value) is not bool:
        invalid("invalid boolean field")


def keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        invalid("unexpected or missing fields")


def fields_for(kind):
    if kind == "category":
        return COMMON - {"nsfw"}
    return COMMON | (
        TEXT if kind in {"text", "news"} else VOICE if kind in {"voice", "stage"} else FORUM
    )


def _colour(value):
    return value.value if value is not None else None


def role_record(role):
    return {
        "id": str(role.id),
        "name": role.name,
        "permissions": role.permissions.value,
        "colour": role.colour.value,
        "secondary_colour": _colour(role.secondary_colour),
        "tertiary_colour": _colour(role.tertiary_colour),
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "position": role.position,
        "managed": role.managed,
    }


def emoji_record(emoji):
    if emoji is None:
        return None
    return {
        "id": str(emoji.id) if emoji.id else None,
        "name": emoji.name,
        "animated": emoji.animated,
    }


def channel_record(channel):
    kind = KINDS.get(channel.type.value)
    if kind is None:
        return None
    overwrites = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        is_role = isinstance(target, discord.Role) or (
            isinstance(target, discord.Object) and target.type is discord.Role
        )
        overwrites.append(
            {
                "id": str(target.id),
                "kind": "role" if is_role else "member",
                "allow": allow.value,
                "deny": deny.value,
            }
        )
    row = {
        "id": str(channel.id),
        "kind": kind,
        "name": channel.name,
        "position": channel.position,
        "category": str(channel.category_id) if channel.category_id else None,
        "overwrites": sorted(overwrites, key=lambda item: (item["kind"], int(item["id"]))),
    }
    if kind != "category":
        row["nsfw"] = channel.nsfw
    if kind in {"text", "news", "forum", "media"}:
        for field in TEXT:
            row[field] = getattr(channel, field)
    if kind in {"voice", "stage"}:
        for field in VOICE:
            row[field] = getattr(channel, field)
        row["video_quality_mode"] = channel.video_quality_mode.value
    if kind in {"forum", "media"}:
        row.update(
            available_tags=[
                {
                    "id": str(tag.id),
                    "name": tag.name,
                    "moderated": tag.moderated,
                    "emoji": emoji_record(tag.emoji),
                }
                for tag in channel.available_tags
            ],
            default_reaction_emoji=emoji_record(channel.default_reaction_emoji),
            default_sort_order=channel.default_sort_order.value
            if channel.default_sort_order is not None
            else None,
            default_layout=channel.default_layout.value,
            require_tag=channel.flags.require_tag,
        )
    return row


def capture(guild, roles, channels):
    result = {
        "schema": 1,
        "guild_id": str(guild.id),
        "guild_name": guild.name,
        "created_at": time.time(),
        "roles": sorted(
            [role_record(role) for role in roles], key=lambda row: (row["position"], int(row["id"]))
        ),
        "channels": [],
        "warnings": [],
    }
    for channel in sorted(channels, key=lambda item: (item.position, item.id)):
        row = channel_record(channel)
        if row is None:
            result["warnings"].append(
                f"Unsupported channel type {channel.type.value}, ID {channel.id}, omitted."
            )
        else:
            result["channels"].append(row)
    return validate(result, guild.id)


def validate_emoji(row):
    if row is None:
        return
    keys(row, {"id", "name", "animated"})
    if row["id"] is not None:
        snowflake(row["id"])
    text(row["name"], 100)
    boolean(row["animated"])


def validate(data, guild_id):
    keys(data, {"schema", "guild_id", "guild_name", "created_at", "roles", "channels", "warnings"})
    if type(data["schema"]) is not int or data["schema"] != 1:
        invalid("unsupported schema")
    snowflake(data["guild_id"])
    if data["guild_id"] != str(guild_id):
        invalid("this backup belongs to another server")
    text(data["guild_name"], 100, empty=False)
    if (
        type(data["created_at"]) not in {int, float}
        or not math.isfinite(data["created_at"])
        or not 0 <= data["created_at"] < 1e11
    ):
        invalid("invalid capture timestamp")
    for key, limit in (
        ("roles", MAX_ROLES),
        ("channels", MAX_CHANNELS),
        ("warnings", MAX_CHANNELS),
    ):
        if not isinstance(data[key], list) or len(data[key]) > limit:
            invalid("object count limit exceeded")
    for warning in data["warnings"]:
        text(warning, 200)
    roles = {}
    for row in data["roles"]:
        keys(row, ROLE_FIELDS | {"id", "position", "managed"})
        snowflake(row["id"])
        if row["id"] in roles:
            invalid("duplicate role ID")
        roles[row["id"]] = row
        text(row["name"], 100, empty=False)
        integer(row["position"], 0, 1000)
        integer(row["permissions"], 0, 2**64 - 1)
        if row["permissions"] & ~discord.Permissions.all().value:
            invalid("unsupported role permission bits")
        for field in ("colour", "secondary_colour", "tertiary_colour"):
            if row[field] is not None:
                integer(row[field], 0, 0xFFFFFF)
            elif field == "colour":
                invalid("missing primary colour")
        for field in ("hoist", "mentionable", "managed"):
            boolean(row[field])
    if str(guild_id) not in roles or roles[str(guild_id)]["managed"]:
        invalid("missing @everyone role")
    if roles[str(guild_id)]["name"] != "@everyone" or roles[str(guild_id)]["position"] != 0:
        invalid("invalid @everyone role")
    channels, targets = {}, 0
    for row in data["channels"]:
        if not isinstance(row, dict) or row.get("kind") not in set(KINDS.values()):
            invalid("unsupported channel kind")
        kind = row["kind"]
        keys(row, fields_for(kind) | {"id", "kind"})
        snowflake(row["id"])
        if row["id"] in channels:
            invalid("duplicate channel ID")
        channels[row["id"]] = row
        text(row["name"], 100, empty=False)
        integer(row["position"], 0, 1000)
        if row["category"] is not None:
            snowflake(row["category"])
        if not isinstance(row["overwrites"], list) or len(row["overwrites"]) > 1000:
            invalid("too many channel overwrites")
        seen = set()
        for overwrite in row["overwrites"]:
            keys(overwrite, {"id", "kind", "allow", "deny"})
            snowflake(overwrite["id"])
            if (
                overwrite["kind"] not in {"role", "member"}
                or (overwrite["kind"], overwrite["id"]) in seen
            ):
                invalid("invalid or duplicate overwrite target")
            seen.add((overwrite["kind"], overwrite["id"]))
            if overwrite["kind"] == "role" and overwrite["id"] not in roles:
                invalid("overwrite references a missing role")
            for field in ("allow", "deny"):
                integer(overwrite[field], 0, 2**64 - 1)
                if overwrite[field] & ~discord.Permissions.all().value:
                    invalid("unsupported overwrite permission bits")
            if overwrite["allow"] & overwrite["deny"]:
                invalid("overwrite allows and denies the same permission")
            targets += 1
        if kind != "category":
            boolean(row["nsfw"])
        if kind in {"text", "news", "forum", "media"}:
            text(row["topic"], 4096 if kind in {"forum", "media"} else 1024, optional=True)
            for field in ("slowmode_delay", "default_thread_slowmode_delay"):
                integer(row[field], 0, 21600)
            if type(row["default_auto_archive_duration"]) is not int or row[
                "default_auto_archive_duration"
            ] not in {60, 1440, 4320, 10080}:
                invalid("invalid archive duration")
        if kind in {"voice", "stage"}:
            integer(row["bitrate"], 8000, 384000)
            integer(row["user_limit"], 0, 10000)
            text(row["rtc_region"], 100, optional=True)
            integer(row["video_quality_mode"], 1, 2)
        if kind in {"forum", "media"}:
            integer(row["default_layout"], 0, 2)
            if row["default_sort_order"] is not None:
                integer(row["default_sort_order"], 0, 1)
            boolean(row["require_tag"])
            validate_emoji(row["default_reaction_emoji"])
            if not isinstance(row["available_tags"], list) or len(row["available_tags"]) > 20:
                invalid("invalid forum tags")
            tag_ids = set()
            for tag in row["available_tags"]:
                keys(tag, {"id", "name", "moderated", "emoji"})
                snowflake(tag["id"])
                if tag["id"] in tag_ids:
                    invalid("duplicate forum tag ID")
                tag_ids.add(tag["id"])
                text(tag["name"], 20, empty=False)
                boolean(tag["moderated"])
                validate_emoji(tag["emoji"])
    if targets > MAX_OVERWRITES:
        invalid("permission overwrite count limit exceeded")
    for row in channels.values():
        if row["category"] is not None and (
            row["category"] not in channels
            or channels[row["category"]]["kind"] != "category"
            or row["kind"] == "category"
        ):
            invalid("invalid category relationship")
    if len(encode(data)) > MAX_FILE:
        invalid("snapshot exceeds the 2 MiB limit")
    return data


def parse(raw, guild_id):
    if len(raw) > MAX_FILE:
        invalid("file exceeds the 2 MiB limit")

    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                invalid("duplicate JSON key")
            result[key] = value
        return result

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        return validate(data, guild_id)
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        invalid("malformed JSON")
