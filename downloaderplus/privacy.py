"""Cancel/wait configuring owner commands before erasing their webhook settings."""

import asyncio
from contextlib import asynccontextmanager
from functools import wraps

from redbot.core import commands


class ConfigurationBarrier:
    def __init__(self):
        self.operations = {}
        self.deleting = {}

    @asynccontextmanager
    async def operation(self, user_id):
        if user_id in self.deleting:
            raise commands.CommandError(
                "Your DownloaderPlus data is being deleted. Retry afterward."
            )
        task = asyncio.current_task()
        done = asyncio.get_running_loop().create_future()
        self.operations[task] = (user_id, done)
        try:
            yield
        finally:
            self.operations.pop(task, None)
            if not done.done():
                done.set_result(None)

    @asynccontextmanager
    async def deletion(self, user_id):
        while (previous := self.deleting.get(user_id)) is not None:
            if await asyncio.shield(previous):
                yield False
                return
        finished = asyncio.get_running_loop().create_future()
        self.deleting[user_id] = finished
        erased = False
        try:
            owned = [
                (task, done) for task, (owner, done) in self.operations.items() if owner == user_id
            ]
            for task, done in owned:
                task.cancel()
            await asyncio.gather(*(asyncio.shield(done) for task, done in owned))
            yield True
            erased = True
        finally:
            self.deleting.pop(user_id, None)
            if not finished.done():
                finished.set_result(erased)

    async def close(self):
        current = asyncio.current_task()
        tasks = [task for task in self.operations if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def configuration_request(callback):
    @wraps(callback)
    async def guarded(self, ctx, *args, **kwargs):
        if self._closing:
            raise commands.CommandError("DownloaderPlus is unloading.")
        async with self._privacy.operation(ctx.author.id):
            async with self._configuration_lock:
                return await callback(self, ctx, *args, **kwargs)

    return guarded
