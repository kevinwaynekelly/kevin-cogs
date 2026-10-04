"""Bounded command admission and an in-flight privacy deletion barrier."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from functools import wraps

from redbot.core import commands

MAX_REQUESTS = 32
MAX_GUILD_REQUESTS = 16


class RequestBudget:
    """Reject overload synchronously, before creating work or joining a lock queue."""

    def __init__(self):
        self.tasks = {}
        self.guilds = {}

    @contextmanager
    def admit(self, guild_id):
        task = asyncio.current_task()
        if task in self.tasks:
            yield
            return
        if len(self.tasks) >= MAX_REQUESTS or self.guilds.get(guild_id, 0) >= MAX_GUILD_REQUESTS:
            raise commands.CommandError(
                "AudioPlus is handling too many requests. Try again shortly."
            )
        self.tasks[task] = guild_id
        self.guilds[guild_id] = self.guilds.get(guild_id, 0) + 1
        try:
            yield
        finally:
            self.tasks.pop(task, None)
            count = self.guilds.get(guild_id, 1) - 1
            if count:
                self.guilds[guild_id] = count
            else:
                self.guilds.pop(guild_id, None)


@dataclass(eq=False)
class Operation:
    task: asyncio.Task
    done: asyncio.Future
    users: set = field(default_factory=set)
    invalidated: bool = False


class PrivacyInterrupted(commands.CommandError):
    """A participant's erasure stopped optional delivery, not its cleanup caller."""


class PrivacyBarrier:
    """Deletion cancels admitted old work; no lasting user tombstones are needed."""

    def __init__(self):
        self.operations = {}
        self.users = {}
        self.deleting = {}
        self.lock = asyncio.Lock()

    def associate(self, operation, user_ids):
        user_ids = {uid for uid in user_ids if type(uid) is int and uid > 0}
        if any(uid in self.deleting for uid in user_ids):
            raise commands.CommandError(
                "Your AudioPlus data is being deleted. Try again afterward."
            )
        for uid in user_ids - operation.users:
            operation.users.add(uid)
            self.users.setdefault(uid, set()).add(operation)

    @asynccontextmanager
    async def operation(self, user_id):
        task = asyncio.current_task()
        if task in self.operations:
            self.associate(self.operations[task], [user_id])
            yield self.operations[task]
            return
        operation = Operation(task, asyncio.get_running_loop().create_future())
        self.associate(operation, [user_id])
        self.operations[task] = operation
        try:
            yield operation
        finally:
            self.operations.pop(task, None)
            for uid in operation.users:
                owned = self.users.get(uid)
                if owned is not None:
                    owned.discard(operation)
                    if not owned:
                        self.users.pop(uid, None)
            if not operation.done.done():
                operation.done.set_result(None)

    def related(self, user_ids):
        operation = self.operations.get(asyncio.current_task())
        if operation is not None:
            self.associate(operation, user_ids)

    @asynccontextmanager
    async def deletion(self, user_id):
        while (previous := self.deleting.get(user_id)) is not None:
            if await asyncio.shield(previous):
                yield False
                return
            # An interrupted erasure is not success. The waiter must retry it.
        finished = asyncio.get_running_loop().create_future()
        self.deleting[user_id] = finished
        erased = False
        try:
            pending = tuple(self.users.get(user_id, ()))
            for operation in pending:
                operation.invalidated = True
                operation.task.cancel()
            # The operation's finally block completes before stored data is erased.
            await asyncio.gather(*(asyncio.shield(op.done) for op in pending))
            async with self.lock:
                yield True
                erased = True
        finally:
            self.deleting.pop(user_id, None)
            if not finished.done():
                finished.set_result(erased)


@asynccontextmanager
async def personal_context(cog, guild_id, user_id):
    if cog._closing:
        raise commands.CommandError("AudioPlus is unloading.")
    with cog._requests.admit(guild_id):
        async with cog._privacy.operation(user_id) as operation:
            yield operation


def personal_request(callback):
    @wraps(callback)
    async def guarded(self, ctx, *args, **kwargs):
        async with personal_context(self, getattr(ctx.guild, "id", 0), ctx.author.id):
            return await callback(self, ctx, *args, **kwargs)

    return guarded


def media_request(callback):
    @wraps(callback)
    async def guarded(self, context, *args, **kwargs):
        with self._requests.admit(getattr(context.guild, "id", 0)):
            return await callback(self, context, *args, **kwargs)

    return guarded


def participant_request(callback):
    @wraps(callback)
    async def guarded(self, player, *args, **kwargs):
        async with personal_context(self, player.guild.id, 0) as operation:
            try:
                return await callback(self, player, *args, **kwargs)
            except asyncio.CancelledError:
                if operation.invalidated:
                    raise PrivacyInterrupted(
                        "Music summary cancelled for user-data deletion."
                    ) from None
                raise

    return guarded


def privacy_write(callback):
    @wraps(callback)
    async def guarded(self, *args, **kwargs):
        async with self._privacy.lock:
            return await callback(self, *args, **kwargs)

    return guarded
