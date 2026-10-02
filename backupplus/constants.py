"""BackupPlus storage, job and data limits."""

DEFAULTS_GUILD = {
    "state": {
        "snapshots": {},
        "auto_hours": 0,
        "last_attempt": 0,
        "last_error": "",
        "last_restore": {},
    }
}
MAX_FILE = 2 * 1024 * 1024
MAX_MANUAL = 5
MAX_AUTO = 3
MAX_SAFETY = 2
MAX_STATE = 21 * 1024 * 1024
MAX_ROLES = 250
MAX_CHANNELS = 500
MAX_OVERWRITES = 10000
API_TIMEOUT = 20
RESTORE_TIMEOUT = 30 * 60
PREVIEW_TTL = 10 * 60

__red_end_user_data_statement__ = (
    "This cog stores bounded server structure snapshots containing role/channel names and IDs, "
    "role permissions and appearance, channel settings, and permission overwrites including "
    "member IDs. It stores no chat messages, attachments, member role assignments or credentials. "
    "At most five manual, three automatic and two pre-restore snapshots are retained per server, "
    "with a 2 MiB limit per snapshot and 21 MiB total server state limit. Automatic backups "
    "default off. Snapshots remain until deleted, rotated or the server is removed. Restore "
    "previews contain a requester ID only in memory for ten minutes and are cleared on unload. "
    "User-data exports return only that user's permission overwrite records. User-data deletion "
    "cancels pending backup work and removes snapshots containing that user's overwrites. Files already "
    "delivered by DM and restored Discord objects remain with their recipients or server."
)
