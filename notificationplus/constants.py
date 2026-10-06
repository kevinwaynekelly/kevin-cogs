"""Bounded, private operational alert metadata."""

COGS = {
    "audioplus": "AudioPlus",
    "backupplus": "BackupPlus",
    "communityplus": "CommunityPlus",
    "coreplus": "CorePlus",
    "dashboardplus": "DashboardPlus",
    "downloaderplus": "DownloaderPlus",
    "emojistealerplus": "EmojiStealerPlus",
    "exportplus": "ExportPlus",
    "introplus": "IntroPlus",
    "levelplus": "LevelPlus",
    "logplus": "LogPlus",
    "owoplus": "OwoPlus",
    "presenceplus": "PresencePlus",
    "settingshub": "SettingsHub",
}
KINDS = {"daily", "playback", "command", "test", "notification"}
CAUSES = {
    "Daily YouTube playback check failed. Run audiostatus or audiocheck now.",
    "Playback failed after automatic recovery. Run audiostatus.",
    "Notification delivery failed.",
    "Unexpected command failure.",
    "Background task failed.",
    "Test notification requested.",
}
DEFAULT_CAUSES = {
    "daily": "Daily YouTube playback check failed. Run audiostatus or audiocheck now.",
    "playback": "Playback failed after automatic recovery. Run audiostatus.",
    "command": "Unexpected command failure.",
    "test": "Test notification requested.",
    "notification": "Background task failed.",
}
MAX_EVENTS = 64
MAX_FILE_BYTES = 128 * 1024
MAX_PENDING = 128
RETENTION_SECONDS = 7 * 24 * 60 * 60
DEDUPE_SECONDS = 10 * 60
MAX_SEQUENCE = 2**53 - 1

__red_end_user_data_statement__ = (
    "Stores a global enabled flag in Red Config and a bounded operational failure outbox in "
    "the cog's persistent alerts/unraid.json file. Outbox records contain a random producer "
    "identifier, monotonically increasing sequence, UTC timestamp, suite cog name, fixed "
    "failure category/stage, sanitized exception type and numeric HTTP/OS error code, and "
    "optional server ID. Member IDs, display names, messages, song titles, media URLs, "
    "credentials and tracebacks are never included. At most 64 events are kept, older "
    "than seven days are pruned on writes/status reads, and equivalent failures are "
    "suppressed for ten minutes. A separate Unraid host script reads the outbox and uses "
    "Unraid's configured notification recipients, which may receive these operational "
    "records by email. Disabling notifications clears queued events. No per-user data "
    "is collected; user data export and deletion hooks therefore return no user records."
)
