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
    "clip. Stores the selected audio segment as local PCM in Red's IntroPlus data directory, "
    "associated with server/member IDs and clip timing, bounded to 256 clips and 128 MiB. "
    "Ready copies survive reloads/restarts; changing or clearing a clip, member/server removal "
    "and user-data deletion remove its cached audio and cancel pending downloads. User-data "
    "export returns saved choices/timing, not audio files. Signed stream URLs, cookies, OAuth "
    "tokens and cipher passwords are not persisted. YouTube and Discord receive requests "
    "necessary for preparation and playback."
)
