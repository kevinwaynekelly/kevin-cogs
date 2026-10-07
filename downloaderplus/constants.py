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

DAILY_DEFAULTS = {
    "enabled": False,
    "time": "04:00",
    "timezone": "America/Chicago",
    "owner_id": None,
    "channel_id": None,
    "generation": "",
    "next_run": 0,
    "last_result": {},
}

SYNC_DEFAULTS = {"fingerprint": "", "last_attempt": 0, "last_success": 0}

DISCORD_DEFAULTS = {
    "enabled": False,
    "webhook_id": None,
    "guild_id": None,
    "channel_id": None,
    "owner_id": None,
    "generation": "",
    "pending": False,
    "last_message_id": 0,
    "last_result": {},
}

__red_end_user_data_statement__ = (
    "DownloaderPlus optionally stores a private webhook secret, listener settings, the "
    "configuring bot owner's ID and notification channel, up to 256 delivery IDs/body "
    "digests, a pending-update flag and the latest update status. A private trigger "
    "credential is derived from the secret. Optional daily updates store the time, "
    "timezone, configuring owner/channel IDs, schedule generation, next-run timestamp "
    "and latest result. Optional Discord webhook triggers store only the approved webhook, "
    "server, channel and configuring owner IDs, a generation marker, latest accepted "
    "message ID, pending flag and latest result; the webhook URL/token and message contents "
    "are not stored. Slash sync stores a command-schema fingerprint and two "
    "timestamps to avoid redundant uploads. It stores no webhook "
    "payloads or new repository copies. It delegates to bundled Red Downloader, whose existing "
    "repository, installed-cog and data policies continue to apply. Results are sent "
    "to the invoking Discord channel or configured automation result channel. Owner IDs "
    "and channel settings are included in that owner's data export; secrets and replay "
    "digests are excluded. Deleting that owner's data disables and clears their webhook, "
    "daily schedule and Discord trigger, without erasing another owner's automation."
)
