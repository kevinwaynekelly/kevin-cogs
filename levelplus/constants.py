"""Persistent defaults and presentation constants."""

from __future__ import annotations

import re

__red_end_user_data_statement__ = (
    "This cog stores per-guild leveling settings, per-user XP totals, and a last-known display name. "
    "Data persists across leaves/joins to preserve user progress. Admins may export or erase specific users via commands."
)

DEFAULTS_GUILD = {
    "curve": "linear",
    "multiplier": 1.0,
    "max_level": 0,
    "linear": {"base": 83.2, "inc": 100.433},  # Arcane-like scale
    "message": {"enabled": True, "mode": "perword", "min": 1, "max": 1, "cooldown": 60},
    "reaction": {
        "enabled": True,
        "awards": "both",
        "min": 25,
        "max": 25,
        "cooldown": 300,
    },
    "voice": {
        "enabled": True,
        "min": 15,
        "max": 40,
        "cooldown": 180,
        "min_members": 1,
        "anti_afk": False,
    },
    "restrictions": {
        "no_channels": [],
        "no_roles": [],
        "thread_xp": True,
        "forum_xp": True,
        "text_in_voice_xp": True,
        "slash_command_xp": True,
    },
    "levelup": {
        "enabled": True,
        "channel_id": None,
        "template": "{user.mention} has reached level **{user.level}**! GG!",
        "image": False,
    },
    "xp": {},  # {user_id(str): int}
    "names": {},  # {user_id(str): alias}
}

WORD_RE = re.compile(r"\b\w+\b", re.UNICODE)
