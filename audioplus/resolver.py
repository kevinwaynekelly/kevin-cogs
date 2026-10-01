"""Resolve media in cancellable yt-dlp processes, outside Red's event loop."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

MAX_TRACKS = 100
DIRECT_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac", ".webm"}


class MediaError(Exception):
    """A media failure safe to display without exposing stream URLs or credentials."""


@dataclass(frozen=True)
class Track:
    uri: str
    title: str
    author: str = "Unknown"
    length: int = 0
    source: str = "http"
    direct: bool = False


@dataclass(frozen=True)
class Stream:
    url: str
    headers: dict[str, str] = field(default_factory=dict)


def http_url(value: object) -> str:
    if not isinstance(value, str):
        raise MediaError("The source did not provide a playable HTTP stream.")
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError
        if parts.username or parts.password or any(ord(c) < 32 for c in value):
            raise ValueError
    except ValueError as exc:
        raise MediaError("Use an HTTP or HTTPS media URL without embedded credentials.") from exc
    return value


def normalize_query(query: str) -> str:
    query = query.strip()
    if not query:
        raise MediaError("Provide search terms or a media URL.")
    if query.lower().startswith(("http://", "https://")):
        return http_url(query)
    match = re.match(r"^([a-z][a-z0-9_-]*search\d*):(.*)$", query, re.I | re.S)
    if match:
        provider, terms = match.groups()
        if not terms.strip():
            raise MediaError("Provide search terms after the search prefix.")
        provider = provider.lower()
        # Retain the old public prefixes; music search uses yt-dlp's YouTube search.
        if provider in {"ytsearch", "ytsearch1", "ytmsearch", "ytmsearch1"}:
            return "ytsearch1:" + terms.strip()
        if provider in {"scsearch", "scsearch1"}:
            return "scsearch1:" + terms.strip()
        raise MediaError("Supported search prefixes are ytsearch:, ytmsearch:, and scsearch:.")
    if "://" in query:
        raise MediaError("Use an HTTP or HTTPS media URL.")
    return "ytsearch1:" + query


def media_failure(stderr: str) -> str:
    text = stderr.lower()
    if any(word in text for word in ("sign in", "login", "cookies", "confirm you're not a bot")):
        return "The provider requires authentication or rejected this server. Try a public track and check the provider's access requirements."
    if any(word in text for word in ("javascript", "js runtime", "signature", "challenge", "ejs")):
        return "YouTube extraction failed. Update yt-dlp and yt-dlp-ejs and check the JavaScript runtime with audio pingnode."
    if "unsupported url" in text:
        return "yt-dlp does not support that URL. Try a YouTube or SoundCloud URL or search."
    if "403" in text or "429" in text:
        return "The provider denied or rate-limited this server's request. Try again later or use another accessible source."
    if "no module named" in text:
        return "yt-dlp is unavailable in Red's Python environment. Install AudioPlus's dependencies and restart Red."
    return "The media source could not be loaded. Check audio pingnode, update yt-dlp, and try another public track."


class MediaResolver:
    def __init__(self, *, timeout: float = 45):
        self.timeout = timeout
        self._processes: set[asyncio.subprocess.Process] = set()
        self._slots = asyncio.Semaphore(2)
        self._closed = False

    def _command(self, query, *, flat):
        args = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-warnings",
            "--no-progress",
            "--dump-single-json",
            "--skip-download",
            "--no-cache-dir",
            "--socket-timeout",
            "15",
            "--retries",
            "1",
            "--extractor-retries",
            "1",
            "--js-runtimes",
            "deno",
            "--js-runtimes",
            "node",
            "--js-runtimes",
            "quickjs",
        ]
        if flat:
            args += ["--flat-playlist", "--playlist-end", str(MAX_TRACKS)]
        else:
            args += ["--no-playlist", "--format", "bestaudio/best"]
        return [*args, "--", query]

    @staticmethod
    async def _kill(process):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()

    async def _extract(self, query, *, flat):
        if self._closed:
            raise MediaError("AudioPlus is unloading. Try again after it reloads.")
        async with self._slots:
            if self._closed:
                raise MediaError("AudioPlus is unloading. Try again after it reloads.")
            spawning = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *self._command(query, flat=flat),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    # Downloader's private dependency path must reach child Python.
                    env={
                        **os.environ,
                        "PYTHONPATH": os.pathsep.join(
                            dict.fromkeys(os.path.abspath(path) for path in sys.path)
                        ),
                    },
                )
            )
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                # Cancellation between OS spawn and transport setup still owns the child.
                process = await spawning
                await self._kill(process)
                raise
            self._processes.add(process)
            try:
                if self._closed:
                    raise MediaError("AudioPlus is unloading.")
                stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout)
                if process.returncode:
                    raise MediaError(media_failure(stderr.decode("utf-8", errors="replace")))
                if len(stdout) > 8 * 1024 * 1024:
                    raise MediaError(
                        "The source returned too much metadata. Try a smaller playlist."
                    )
                try:
                    data = json.loads(stdout)
                except (ValueError, UnicodeError) as exc:
                    raise MediaError("The source returned invalid metadata.") from exc
                if not isinstance(data, dict):
                    raise MediaError("The source returned invalid metadata.")
                return data
            except asyncio.TimeoutError as exc:
                raise MediaError(
                    "Media lookup timed out. Try again or use a smaller playlist."
                ) from exc
            finally:
                if process.returncode is None:
                    await self._kill(process)
                self._processes.discard(process)

    async def search(self, query: str, *, limit: int = 1) -> list[Track]:
        query = normalize_query(query)
        if query.startswith(("ytsearch1:", "scsearch1:")):
            query = query.replace("search1:", f"search{max(1, min(10, limit))}:", 1)
        if query.startswith(("http://", "https://")):
            path = PurePosixPath(urlsplit(query).path)
            if path.suffix.lower() in DIRECT_EXTENSIONS:
                return [Track(query, unquote(path.name)[:200] or "Direct audio", direct=True)]
        data = await self._extract(query, flat=True)
        entries = data.get("entries") if "entries" in data else [data]
        tracks = []
        for entry in (entries or [])[:MAX_TRACKS]:
            if not isinstance(entry, dict):
                continue
            source = str(entry.get("extractor_key") or entry.get("ie_key") or "http").lower()
            uri = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
            if "youtube" in source and not str(uri).startswith(("http://", "https://")):
                uri = "https://www.youtube.com/watch?v=" + str(entry.get("id") or uri)
            try:
                uri = http_url(uri)
            except MediaError:
                continue
            duration = entry.get("duration")
            tracks.append(
                Track(
                    uri,
                    str(entry.get("title") or "Unknown track")[:300],
                    str(entry.get("uploader") or entry.get("channel") or "Unknown")[:200],
                    max(0, int(duration * 1000))
                    if isinstance(duration, (int, float)) and math.isfinite(duration)
                    else 0,
                    source,
                )
            )
        return tracks

    async def resolve(self, track: Track) -> Stream:
        if track.direct:
            return Stream(http_url(track.uri))
        data = await self._extract(track.uri, flat=False)
        url = http_url(data.get("url"))
        headers = {}
        for name, value in (data.get("http_headers") or {}).items():
            if (
                re.fullmatch(r"[A-Za-z0-9-]+", str(name))
                and isinstance(value, str)
                and not any(ord(c) < 32 for c in value)
            ):
                headers[str(name)] = value
        return Stream(url, headers)

    async def close(self):
        self._closed = True
        # The owning extraction coroutine drains its pipes and reaps the killed process.
        for process in tuple(self._processes):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        await asyncio.gather(*(process.wait() for process in tuple(self._processes)))
