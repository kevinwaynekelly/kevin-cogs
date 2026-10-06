"""Private webhook policy; installed packages remain owned by Red Downloader."""

WEBHOOK_DEFAULTS = {
    "enabled": False,
    "bind": "127.0.0.1",
    "port": 8766,
    "secret": "",
    "owner_id": None,
    "channel_id": None,
    "pending": False,
    "deliveries": [],
    "last_result": {},
}

__red_end_user_data_statement__ = (
    "DownloaderPlus optionally stores a private webhook secret, listener settings, the "
    "configuring bot owner's ID and notification channel, up to 256 delivery IDs/body "
    "digests, a pending-update flag and the latest update status. It stores no webhook "
    "payloads or new repository copies. It delegates to bundled Red Downloader, whose existing "
    "repository, installed-cog and data policies continue to apply. Results are sent "
    "to the invoking Discord channel or configured webhook result channel. Owner IDs "
    "and channel settings are included in that owner's data export; secrets and replay "
    "digests are excluded. Deleting that owner's data disables and clears their webhook."
)
