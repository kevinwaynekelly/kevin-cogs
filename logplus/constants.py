"""Persistent defaults and presentation constants."""

from __future__ import annotations

from typing import Dict

import discord

__red_end_user_data_statement__ = (
    "This cog stores per-guild settings for logging preferences, the destination log channel, and optional "
    "per-channel/category routing overrides and retry preferences. It does not persist message contents in Config. "
    "Unsent event records temporarily hold event details and message text in a bounded memory retry queue. "
    "User-data hooks export/delete queued records identifying that user; posted logs remain managed in Discord."
)

EVENT_STYLE: Dict[str, Dict[str, object]] = {
    "message_edited": {"emoji": "✏️", "color": discord.Color.gold()},
    "message_deleted": {"emoji": "🗑️", "color": discord.Color.red()},
    "bulk_delete": {"emoji": "🧹", "color": discord.Color.red()},
    "pins_updated": {"emoji": "📌", "color": discord.Color.blurple()},
    "reaction_added": {"emoji": "➕", "color": discord.Color.green()},
    "reaction_removed": {"emoji": "➖", "color": discord.Color.orange()},
    "reaction_cleared": {"emoji": "♻️", "color": discord.Color.orange()},
    "channel_created": {"emoji": "📺", "color": discord.Color.green()},
    "channel_deleted": {"emoji": "📺", "color": discord.Color.red()},
    "channel_updated": {"emoji": "📺", "color": discord.Color.blurple()},
    "role_created": {"emoji": "🛡️", "color": discord.Color.green()},
    "role_deleted": {"emoji": "🛡️", "color": discord.Color.red()},
    "role_updated": {"emoji": "🛡️", "color": discord.Color.blurple()},
    "server_updated": {"emoji": "🏠", "color": discord.Color.blurple()},
    "emoji_updated": {"emoji": "😃", "color": discord.Color.blurple()},
    "sticker_updated": {"emoji": "🏷️", "color": discord.Color.blurple()},
    "integrations_updated": {"emoji": "🧩", "color": discord.Color.blurple()},
    "webhooks_updated": {"emoji": "🪝", "color": discord.Color.blurple()},
    "thread_created": {"emoji": "🧵", "color": discord.Color.green()},
    "thread_deleted": {"emoji": "🧵", "color": discord.Color.red()},
    "thread_updated": {"emoji": "🧵", "color": discord.Color.blurple()},
    "invite_created": {"emoji": "🔗", "color": discord.Color.green()},
    "invite_deleted": {"emoji": "❌", "color": discord.Color.red()},
    "member_joined": {"emoji": "➕", "color": discord.Color.green()},
    "member_left": {"emoji": "➖", "color": discord.Color.red()},
    "member_kicked": {"emoji": "🥾", "color": discord.Color.dark_red()},
    "roles_changed": {"emoji": "🎭", "color": discord.Color.blurple()},
    "nick_changed": {"emoji": "✍️", "color": discord.Color.blurple()},
    "timeout_updated": {"emoji": "⏳", "color": discord.Color.orange()},
    "user_banned": {"emoji": "🔨", "color": discord.Color.dark_red()},
    "user_unbanned": {"emoji": "✅", "color": discord.Color.green()},
    "voice_join": {"emoji": "🎤", "color": discord.Color.green()},
    "voice_move": {"emoji": "🎤", "color": discord.Color.blurple()},
    "voice_leave": {"emoji": "🎤", "color": discord.Color.red()},
    "voice_mute": {"emoji": "🔇", "color": discord.Color.orange()},
    "voice_deaf": {"emoji": "🙉", "color": discord.Color.orange()},
    "voice_video": {"emoji": "🎥", "color": discord.Color.blurple()},
    "voice_stream": {"emoji": "📺", "color": discord.Color.blurple()},
    "sched_created": {"emoji": "📅", "color": discord.Color.green()},
    "sched_updated": {"emoji": "📅", "color": discord.Color.blurple()},
    "sched_deleted": {"emoji": "📅", "color": discord.Color.red()},
    "sched_user_add": {"emoji": "➕", "color": discord.Color.green()},
    "sched_user_rem": {"emoji": "➖", "color": discord.Color.red()},
    "cmd_thisbot": {"emoji": "🤖", "color": discord.Color.green()},
    "cmd_otherbot": {"emoji": "🤖", "color": discord.Color.blurple()},
}

_UI = {
    "core": "🛠️",
    "style": "🎨",
    "toggles": "🎚️",
    "diag": "🧪",
    "ok": "✅",
    "warn": "⚠️",
}

DEFAULTS_GUILD = {
    "log_channel": None,
    "fast_logs": True,
    "overrides": {},
    "message": {
        "edit": True,
        "delete": True,
        "bulk_delete": True,
        "pins": True,
        "exempt_channels": [],
    },
    "reactions": {"add": True, "remove": True, "clear": True},
    "server": {
        "channel_create": True,
        "channel_delete": True,
        "channel_update": True,
        "role_create": True,
        "role_delete": True,
        "role_update": True,
        "server_update": True,
        "emoji_update": True,
        "sticker_update": True,
        "integrations_update": True,
        "webhooks_update": True,
        "thread_create": True,
        "thread_delete": True,
        "thread_update": True,
        "exempt_channels": [],
    },
    "invites": {"create": True, "delete": True},
    "member": {
        "join": True,
        "leave": True,
        "roles_changed": True,
        "nick_changed": True,
        "ban": True,
        "unban": True,
        "timeout": True,
        "presence": True,
    },
    "voice": {
        "join": True,
        "move": True,
        "leave": True,
        "mute": True,
        "deaf": True,
        "video": True,
        "stream": True,
    },
    "sched": {
        "create": True,
        "update": True,
        "delete": True,
        "user_add": True,
        "user_remove": True,
    },
    "commands": {"this_bot": True, "other_bots": True},
    "rate": {"seconds": 2.0},
    "style": {"compact": True},
}
