"""Persistent defaults and presentation constants."""

from __future__ import annotations

import discord

__red_end_user_data_statement__ = (
    "This cog stores per-guild settings for autoroles, sticky-role preferences, welcome/cya message targets, "
    "solo-voice idle settings, and compact-embed preference. It also stores, per member, last-seen timestamps for "
    "message/voice/join/leave/presence, presence status history (last online/offline), and counters for messages sent, "
    "voice joins/moves/leaves, stream/video starts, activity starts (playing/streaming/listening/watching/competing/custom), "
    "and per-game launch counts. It also stores lifetime voice seconds and bounded daily participation totals, "
    "self-role menu IDs, solo exemptions, and weekly digest settings. No message contents are stored."
)

DEFAULTS_GUILD = {
    "embeds": {"compact": True},
    "autorole": {"enabled": True, "role_id": None},
    "sticky": {"enabled": True, "ignore": []},
    "welcome": {
        "enabled": True,
        "channel_id": None,
        "message": "Welcome {mention}! You’re member #{count} of **{server}**.",
    },
    "cya": {
        "enabled": True,
        "channel_id": None,
        "message": "Cya {user} 👋",
    },
    "vcsolo": {"enabled": True, "idle_seconds": 900, "dm_notify": True},
    "seen": {"enabled": True},
}

DEFAULTS_MEMBER = {
    "ever_seen": False,
    "sticky_roles": [],
    "seen": {
        "any": 0,
        "kind": "",
        "where": 0,
        "message": 0,
        "message_ch": 0,
        "voice": 0,
        "voice_ch": 0,
        "join": 0,
        "leave": 0,
        "presence": {
            "status": "",
            "since": 0,
            "last_online": 0,
            "last_offline": 0,
            "desktop": "unknown",
            "mobile": "unknown",
            "web": "unknown",
        },
    },
    "stats": {
        "messages": 0,
        "voice_joins": 0,
        "voice_moves": 0,
        "voice_leaves": 0,
        "stream_starts": 0,
        "video_starts": 0,
        "game_launches": 0,
        "activity_starts": {
            "playing": 0,
            "streaming": 0,
            "listening": 0,
            "watching": 0,
            "competing": 0,
            "custom": 0,
        },
        "status_changes": {
            "online": 0,
            "offline": 0,
            "idle": 0,
            "dnd": 0,
            "unknown": 0,
        },
    },
    "activity_names": {},
}

EVENT_COLOR = {
    "ok": discord.Color.green(),
    "info": discord.Color.blurple(),
    "warn": discord.Color.orange(),
    "err": discord.Color.red(),
}

__red_end_user_data_statement__ += " Stores bounded poll/event records, titles/options, creator and announcement IDs, times, member votes/RSVPs, private reminder opt-ins, and sent markers. User-data hooks export/delete personal choices and creator attribution; already delivered Discord messages remain."
