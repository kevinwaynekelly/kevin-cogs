"""Bounded automatic emoji capture defaults."""

DEFAULTS_GUILD = {
    "capture": {"enabled": True, "reactions": True, "notify": False, "channel": None},
    "copied": {},
    "last_error": None,
}
MAX_IMAGE = 256 * 1024
MAX_COPIES = 1000
__red_end_user_data_statement__ = (
    "This cog stores server capture settings and up to 1000 mappings of external emoji IDs "
    "to server emoji IDs, names, animation flags and image fingerprints. It does not store "
    "member IDs, message text or image files. Pending emoji/channel IDs are held only in "
    "bounded memory until processed or unloaded. Copied images become ordinary Discord "
    "server emojis and remain until removed there. User-data hooks return no personal records."
)
