"""Immediate repost admission and bounded Discord attachment downloads."""

from __future__ import annotations

import asyncio
import io
from collections import Counter
from contextlib import contextmanager
from functools import wraps
from urllib.parse import urlsplit

import aiohttp
import discord

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_REPOSTS = 4
MAX_GUILD_REPOSTS = 2
DOWNLOAD_SECONDS = 15


class RepostBudget:
    """Reserve work synchronously instead of accumulating semaphore waiters."""

    def __init__(self):
        self.tasks = {}
        self.guilds = Counter()
        self.closing = False

    @contextmanager
    def slot(self, guild_id):
        task = asyncio.current_task()
        if task in self.tasks:
            yield self.tasks[task] == guild_id and not self.closing
            return
        if (
            self.closing
            or len(self.tasks) >= MAX_REPOSTS
            or self.guilds[guild_id] >= MAX_GUILD_REPOSTS
        ):
            yield False
            return
        self.tasks[task] = guild_id
        self.guilds[guild_id] += 1
        try:
            yield True
        finally:
            self.tasks.pop(task, None)
            self.guilds[guild_id] -= 1
            if not self.guilds[guild_id]:
                del self.guilds[guild_id]

    async def close(self):
        self.closing = True
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def bounded_repost(listener):
    @wraps(listener)
    async def wrapped(self, message, *args, **kwargs):
        guild = message.guild
        if guild is None:
            return
        with self._repost_budget.slot(guild.id) as admitted:
            if admitted:
                return await listener(self, message, *args, **kwargs)

    return wrapped


def attachment_size(attachment):
    size = getattr(attachment, "size", None)
    if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
        raise ValueError("Attachment exceeds the repost byte budget.")
    return size


def attachment_url(attachment):
    url = getattr(attachment, "url", None)
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError("Missing Discord attachment URL.")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"cdn.discordapp.com", "media.discordapp.net"}
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/attachments/")
        or parsed.fragment
    ):
        raise ValueError("Attachments must come directly from Discord's CDN.")
    return url


async def download_attachment(session, attachment, remaining):
    """Read only bounded chunks; never call Attachment.read/to_file on untrusted sizes."""
    attachment_size(attachment)
    url = attachment_url(attachment)
    if session is None or remaining < 0:
        raise ValueError("Attachment downloads are unavailable.")
    limit = min(MAX_FILE_BYTES, remaining)
    buffer = io.BytesIO()
    try:
        async with session.get(
            url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_SECONDS)
        ) as response:
            if response.status != 200:
                raise ValueError("Discord could not provide this attachment.")
            if response.content_length is not None and response.content_length > limit:
                raise ValueError("Attachment exceeds the repost byte budget.")
            count = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                count += len(chunk)
                if count > limit:
                    raise ValueError("Attachment exceeds the repost byte budget.")
                buffer.write(chunk)
        buffer.seek(0)
        file = discord.File(
            buffer,
            filename=attachment.filename,
            description=attachment.description,
            spoiler=attachment.is_spoiler(),
        )
        return file, count
    except BaseException:
        buffer.close()
        raise
