"""Pure profile, template and weekly-window validation."""

import json
import re
import string
from copy import deepcopy
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .constants import (
    KINDS,
    MAX_CONFIG_BYTES,
    MAX_ENTRIES,
    MAX_PROFILES,
    MAX_SCHEDULES,
    PLACEHOLDERS,
    STATUSES,
)
from .presentation import clip, units

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def name(value):
    if not isinstance(value, str):
        raise ValueError("Use a profile or rule name containing letters, numbers, - or _.")
    value = value.lower().strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,23}", value):
        raise ValueError(
            "Names must be 1-24 letters, numbers, - or _, starting with a letter/number."
        )
    return value


def template(value):
    if not isinstance(value, str) or not value.strip() or units(value) > 128:
        raise ValueError("Status text must contain 1-128 UTF-16 units, including placeholders.")
    if any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
        raise ValueError("Status text must be a single line without control characters.")
    try:
        for _, field, spec, conversion in string.Formatter().parse(value):
            if field is not None and (field not in PLACEHOLDERS or spec or conversion):
                raise ValueError
    except ValueError as error:
        raise ValueError(
            "Use only {servers}, {members}, {uptime}, {song} and {listeners}. "
            "Use {{ and }} for literal braces."
        ) from error
    return value.strip()


def zone(value):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("Use an IANA timezone such as America/Chicago or UTC.")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(
            "Use an available IANA timezone such as America/Chicago or UTC."
        ) from error


def minute(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Use a 24-hour time such as 22:00 or 07:30.")
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def weekdays(value):
    if value.lower().strip() == "all":
        return list(range(7))
    parts = [part.strip().lower() for part in value.split(",")]
    if not parts or any(part not in DAY_NAMES for part in parts):
        raise ValueError("Use all, or comma-separated weekdays such as mon,tue,wed.")
    return sorted({DAY_NAMES.index(part) for part in parts})


def weekly_windows(rule):
    """Split overnight windows, including Sunday rollover, into weekly intervals."""
    length = (rule["end"] - rule["start"]) % 1440
    for day in rule["days"]:
        start = day * 1440 + rule["start"]
        end = start + length
        yield start, min(end, 10080)
        if end > 10080:
            yield 0, end - 10080


def validate(settings):
    """Return a detached, bounded config; reject before a caller commits changes."""
    value = deepcopy(settings)
    try:
        if type(value["enabled"]) is not bool:
            raise ValueError("Enabled must be true or false.")
        if type(value["interval"]) is not int or not 60 <= value["interval"] <= 86400:
            raise ValueError("Rotation interval must be 60-86400 seconds.")
        zone(value["timezone"])
        profiles = value["profiles"]
        if not isinstance(profiles, dict) or not 1 <= len(profiles) <= MAX_PROFILES:
            raise ValueError(f"Keep 1-{MAX_PROFILES} profiles.")
        if value["selected"] not in profiles:
            raise ValueError("The selected profile is missing.")
        for key, profile in profiles.items():
            if name(key) != key or profile["status"] not in STATUSES:
                raise ValueError("A profile name or availability is invalid.")
            entries = profile["entries"]
            if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
                raise ValueError(f"A profile supports at most {MAX_ENTRIES} messages.")
            for entry in entries:
                if entry["kind"] not in KINDS:
                    raise ValueError("Use custom, playing, listening, watching or competing.")
                entry["text"] = template(entry["text"])
        rules = value["schedules"]
        if not isinstance(rules, dict) or len(rules) > MAX_SCHEDULES:
            raise ValueError(f"Keep at most {MAX_SCHEDULES} schedule rules.")
        windows = []
        for key, rule in rules.items():
            if name(key) != key or rule["profile"] not in profiles:
                raise ValueError("A schedule name or target profile is invalid.")
            if any(
                type(rule[field]) is not int or not 0 <= rule[field] < 1440
                for field in ("start", "end")
            ):
                raise ValueError("Schedule times must be valid minutes of the day.")
            if rule["start"] == rule["end"]:
                raise ValueError(
                    "Schedule start and end must differ; use the base profile for all day."
                )
            days = rule["days"]
            if (
                not isinstance(days, list)
                or not days
                or len(days) > 7
                or any(type(day) is not int or not 0 <= day <= 6 for day in days)
                or len(set(days)) != len(days)
            ):
                raise ValueError("A schedule needs unique weekdays from Monday through Sunday.")
            for start, end in weekly_windows(rule):
                if any(
                    start < other_end and end > other_start for other_start, other_end in windows
                ):
                    raise ValueError(
                        "Schedule windows overlap. Shorten or remove the conflicting rule."
                    )
                windows.append((start, end))
        music = value["music"]
        if (
            type(music["enabled"]) is not bool
            or type(music["guild_id"]) is not int
            or not 0 <= music["guild_id"] < 2**64
        ):
            raise ValueError("Music settings need true/false and a valid source server ID.")
        if music["enabled"] and not music["guild_id"]:
            raise ValueError("Select a source server before enabling music status.")
        music["text"] = template(music["text"])
        if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_CONFIG_BYTES:
            raise ValueError("Saved presence settings exceed 64 KiB.")
    except (KeyError, TypeError, AttributeError, UnicodeError) as error:
        raise ValueError(
            "Saved presence settings are invalid. Use presence reset true to start again."
        ) from error
    return value


def effective_profile(settings, now):
    local = now.astimezone(zone(settings["timezone"]))
    clock = local.hour * 60 + local.minute
    for key, rule in settings["schedules"].items():
        if rule["start"] < rule["end"]:
            match = local.weekday() in rule["days"] and rule["start"] <= clock < rule["end"]
        else:
            match = (local.weekday() in rule["days"] and clock >= rule["start"]) or (
                (local - timedelta(days=1)).weekday() in rule["days"] and clock < rule["end"]
            )
        if match:
            return rule["profile"], key
    return settings["selected"], None


def render(value, fields):
    text = value.format_map(fields)
    text = " ".join(
        "".join(char for char in text if ord(char) >= 32 and not 127 <= ord(char) < 160).split()
    )
    return clip(text, 128)


def describe_rule(rule):
    def clock(value):
        return f"{value // 60:02d}:{value % 60:02d}"

    days = ",".join(DAY_NAMES[day] for day in rule["days"])
    return f"{rule['profile']} · {clock(rule['start'])}-{clock(rule['end'])} · {days}"
