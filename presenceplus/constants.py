"""Global presence defaults and bounded policies."""

DEFAULTS = {
    "enabled": False,
    "selected": "default",
    "interval": 300,
    "timezone": "America/Chicago",
    "profiles": {
        "default": {
            "status": "online",
            "entries": [{"kind": "custom", "text": "Use !help for commands"}],
        }
    },
    "schedules": {},
    "music": {"enabled": False, "guild_id": 0, "text": "{song}"},
}
MAX_PROFILES = 10
MAX_ENTRIES = 20
MAX_SCHEDULES = 20
MAX_CONFIG_BYTES = 65536
MIN_UPDATE_SECONDS = 15
POLL_SECONDS = 15
UPDATE_TIMEOUT = 10
KINDS = {"custom", "playing", "listening", "watching", "competing"}
STATUSES = {"online", "idle", "dnd", "invisible"}
PLACEHOLDERS = {"servers", "members", "uptime", "song", "listeners"}

__red_end_user_data_statement__ = (
    "Stores bot-owner configured global status profiles, availability, rotation interval, "
    "timezone, weekly schedules and an optional AudioPlus source server ID and music template "
    "in Red Config. Profiles and templates store the text the owner supplies; do not include "
    "private personal information. No member-specific records, listening histories or tokens "
    "are collected. Dynamic member counts and the selected server's current song/listener "
    "count are read from memory and not persisted. Enabled status text is sent to Discord "
    "and is visible across the bot's servers. Runtime rotation, previous presence and safe "
    "error types are transient and clear on unload. User-data hooks return no per-user data; "
    "the bot owner can clear all saved settings using presence reset true."
)
