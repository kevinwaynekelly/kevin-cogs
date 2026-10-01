"""Persistent defaults and presentation constants."""

from __future__ import annotations

import re
from typing import Dict

__red_end_user_data_statement__ = (
    "This cog stores per-guild preferences for webhook-based message transformation "
    "(enable flag, 1-in-N owo probability, per-user overrides, owner bypass, and haiku toggle). "
    "It also stores channel scopes, personal opt-out member IDs, custom word replacements, "
    "syllable corrections, custom style dictionaries/decorations, channel style/expiry settings, and intensity/cooldown preferences. "
    "Explicit haiku submissions, author/approver/contest-creator IDs, member votes, deadlines and winner references are stored "
    "for up to 90 days, with up to 100 hall submissions, ten contests of 50 entries/500 voters, and a combined 1 MiB limit. "
    "Original transformed-message text is retained only in memory for two minutes, up to 50 active Undo records. "
    "Ordinary chat history is not persisted. Webhook copies and announcements remain in Discord until removed there. "
    "Red data hooks export personal settings, haiku/votes/attribution and active Undo text; deletion removes personal "
    "records and Undo text and anonymizes attribution on others' submissions."
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
