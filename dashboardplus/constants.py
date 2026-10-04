"""Local listener settings and bounded transient authentication policies."""

DEFAULTS = {"enabled": False, "bind": "127.0.0.1", "port": 8765, "hosts": []}
CODE_SECONDS = 300
SESSION_SECONDS = 8 * 3600
IDLE_SECONDS = 30 * 60
MAX_SESSIONS = 32
COOKIE = "dashboardplus_session"

__red_end_user_data_statement__ = (
    "Stores only bot-wide listener enabled/bind/port and explicitly allowed hostnames in "
    "Red Config. Single-use login code hashes and session hashes, CSRF tokens, owner IDs "
    "and expiry/last-use times are held only in bounded memory. Codes expire in five "
    "minutes; sessions expire after eight hours or thirty idle minutes. No Discord bot "
    "token, password, raw Config export, member activity database or web access history "
    "is collected. Authenticated owners read selected server status, public track metadata "
    "and reviewed configuration fields, and use existing checked cog commands. Original "
    "cogs retain their own data policies and optional SettingsHub audit records. Owner "
    "privacy deletion revokes codes/sessions; export reports transient session metadata "
    "without credentials. Stop, unload or restart revokes all logins."
)
