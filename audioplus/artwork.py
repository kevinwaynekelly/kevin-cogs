"""Public video artwork derived from saved source pages, never playback streams."""

import re
from urllib.parse import parse_qs, urlsplit

from .resolver import MediaError, http_url


def video_thumbnail(uri):
    """Return YouTube's public thumbnail without fetching or retaining metadata."""
    if not isinstance(uri, str) or len(uri) > 2048:
        return None
    try:
        parts = urlsplit(http_url(uri))
        host = parts.hostname.lower()
        path = parts.path.strip("/").split("/")
        if host in {"youtu.be", "www.youtu.be"} and len(path) == 1:
            identifier = path[0]
        elif host in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtube-nocookie.com",
            "www.youtube-nocookie.com",
        }:
            if path == ["watch"]:
                values = parse_qs(parts.query, max_num_fields=50).get("v", [])
                identifier = values[0] if len(values) == 1 else ""
            elif len(path) == 2 and path[0] in {"shorts", "embed", "live", "v"}:
                identifier = path[1]
            else:
                return None
        else:
            return None
    except (MediaError, ValueError):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
        return None
    return f"https://i.ytimg.com/vi/{identifier}/hqdefault.jpg"
