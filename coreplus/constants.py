"""CorePlus has no persistent database or replacement Core configuration."""

__red_end_user_data_statement__ = (
    "CorePlus stores no persistent settings or member records. Help controls hold the "
    "requester's ID, channel and selected page in memory for up to three minutes, then "
    "discard them on expiry or unload. Commands delegate to Red Core, whose existing "
    "configuration and data policies still apply. Replies and help cards are sent to "
    "the invoking Discord channel. User-data deletion closes that user's help controls."
)
