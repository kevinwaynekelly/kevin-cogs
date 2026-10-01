"""Persistent defaults and presentation constants."""

from __future__ import annotations

import re
from typing import Dict

__red_end_user_data_statement__ = (
    "This cog stores per-guild preferences for webhook-based message transformation "
    "(enable flag, 1-in-N owo probability, per-user overrides, owner bypass, and haiku toggle). "
    "It also stores channel scopes, personal opt-out member IDs, custom word replacements, "
    "syllable corrections, and intensity/cooldown preferences. It does not store message contents."
)

DEFAULTS_GUILD = {
    "enabled": False,
    "one_in": 1000,
    "user_probs": {},
    "owner_bypass": True,
    "haiku_enabled": True,
}

KEY_MAP: Dict[str, str] = {"now": "meow", "bro": "bwo", "dude": "duwde", "bud": "bwud"}

KEY_RX = re.compile(r"\b(" + "|".join(map(re.escape, KEY_MAP.keys())) + r")\b", re.IGNORECASE)

TARGETS = sorted({v for v in KEY_MAP.values()})

CODE_SPLIT = re.compile(r"(```[\s\S]*?```|`[^`]*?`)", re.MULTILINE)

OWO_FACES = ["uwu", "owo", ">w<", "^w^", "x3", "~", "nya~", "(⁄˘⁄⁄ ω⁄ ⁄˘⁄)♡"]

HAIKU_SUFFIX = " 🌸"

EMO = {
    "ok": "✅",
    "bad": "⚠️",
    "core": "🛠️",
    "msg": "💬",
    "prob": "🎲",
    "diag": "🧪",
    "spark": "✨",
}
