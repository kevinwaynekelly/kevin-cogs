"""Intro policies, clip bounds and Red data disclosure."""

DEFAULTS_GUILD = {"enabled": True, "volume": 70, "cooldown": 60, "channel": 0}
DEFAULTS_MEMBER = {"clip": {}}
MAX_DURATION = 30
MAX_START = 86400
MAX_PENDING = 5
MAX_GUILD_WORKERS = 100
QUEUE_TTL = 120
__red_end_user_data_statement__ = (
    "Stores each member's chosen public YouTube video URL, title, author, source duration, "
    "intro duration and start offset in per-server Red Config. Server settings store automatic "
    "playback, volume, cooldown and an optional voice-channel restriction. Queued joins, member "
    "IDs, cooldowns and the latest sanitized server result are bounded transient memory and "
    "clear on unload; user-data hooks export/delete that member's saved clip and pending work. "
    "Leaving a server clears its intro settings and clips; member removal clears that member's "
    "clip. No downloaded audio, signed stream URLs, cookies, OAuth tokens or cipher passwords "
    "are persisted. YouTube and Discord receive requests necessary for playback."
)
