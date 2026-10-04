"""ExportPlus limits and data disclosure. Exports are temporary, never Config records."""

TEXT_BYTES = 1024 * 1024
VOLUME_BYTES = 7 * 1024 * 1024
MAX_EXPORT_BYTES = 256 * 1024 * 1024
MAX_STORAGE_BYTES = 1024 * 1024 * 1024
MAX_CHANNELS = 10000
MAX_JOBS = 2
MAX_RETAINED = 16
RETENTION = 24 * 60 * 60
JOB_TIMEOUT = 4 * 60 * 60

__red_end_user_data_statement__ = (
    "On an administrator's explicit request this cog reads accessible server message history. "
    "Temporary files contain message text, author names and IDs, timestamps, channel IDs/names, "
    "reply references, reactions, embeds, stickers, polls and attachment metadata/links. "
    "Attachment binaries are not downloaded. Files are privately sent to the requester and "
    "cached on the bot for at most 24 hours, or until cleared, replaced, reloaded or unloaded. "
    "Disk use is bounded. No chat data or bot credentials are stored in Red Config. "
    "User-data exports return only that author's retained message records; a user-data deletion "
    "request cancels and clears only exports requested by that user or containing their authored rows. In-flight unaffected exports omit that author after deletion; temporary exclusion IDs are bounded and erased with each job. Copies already downloaded or delivered "
    "to Discord remain with their recipients."
)
