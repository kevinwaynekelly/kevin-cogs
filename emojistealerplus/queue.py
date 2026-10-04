"""A bounded asyncio queue with round-robin server scheduling."""

import asyncio
from collections import deque

MAX_PENDING = 100
MAX_GUILD_PENDING = 10


class CaptureQueue(asyncio.Queue):
    def _init(self, maxsize):
        self._guilds = {}
        self._rotation = deque()

    def qsize(self):
        return sum(len(queue) for queue in self._guilds.values())

    def empty(self):
        return not self._rotation

    def _put(self, item):
        guild_id = item[0]
        if guild_id not in self._guilds:
            self._guilds[guild_id] = deque()
            self._rotation.append(guild_id)
        self._guilds[guild_id].append(item)

    def _get(self):
        guild_id = self._rotation.popleft()
        queue = self._guilds[guild_id]
        item = queue.popleft()
        if queue:
            self._rotation.append(guild_id)
        else:
            del self._guilds[guild_id]
        return item
