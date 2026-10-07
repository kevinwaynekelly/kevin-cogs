"""A bounded Discord webhook listener with one durable, coalescing update worker."""

import asyncio
import logging
import math
import secrets
import time

log = logging.getLogger("downloaderplus.discord_trigger")
MAX_REQUESTS = 8
IDENTITY_FIELDS = ("webhook_id", "guild_id", "channel_id", "owner_id")
TRIGGERS = {"updateall", "!updateall"}


class DiscordTrigger:
    def __init__(self, config, update, *, coalesce=3, interval=30, ready=None):
        self.config, self.update = config, update
        self.coalesce, self.interval, self.ready = coalesce, interval, ready
        self.worker = None
        self.closed = True
        self.policy = {}
        self._event = asyncio.Event()
        self._requests = set()
        self._accept_lock = asyncio.Lock()
        self._run_token = ""

    async def start(self):
        if self.worker is not None:
            return
        self.policy = await self.config.discord_trigger()
        if (
            not self.policy["enabled"]
            or not self.policy["generation"]
            or any(
                not isinstance(self.policy[key], int) or self.policy[key] <= 0
                for key in IDENTITY_FIELDS
            )
        ):
            return
        self.closed = False
        self._event.clear()
        if self.policy["pending"] or self.policy["last_result"].get("status") == "running":
            self._event.set()
        self.worker = asyncio.create_task(self._run(), name="DownloaderPlusDiscordTrigger")

    async def close(self):
        self.closed = True
        current = asyncio.current_task()
        tasks = [task for task in [self.worker, *self._requests] if task and task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.worker = None

    def _active(self, policy):
        return (
            policy["enabled"]
            and policy["generation"] == self.policy["generation"]
            and all(policy[key] == self.policy[key] for key in IDENTITY_FIELDS)
        )

    def _matches(self, message):
        return (
            not self.closed
            and getattr(message, "edited_at", None) is None
            and getattr(message, "webhook_id", None) == self.policy["webhook_id"]
            and getattr(getattr(message, "guild", None), "id", None) == self.policy["guild_id"]
            and getattr(getattr(message, "channel", None), "id", None) == self.policy["channel_id"]
            and isinstance(getattr(message, "id", None), int)
            and message.id > self.policy["last_message_id"]
            and isinstance(getattr(message, "content", None), str)
            and message.content.strip().casefold() in TRIGGERS
        )

    async def accept(self, message):
        # Most messages return before any await or Config access. Admission is
        # bounded even while persistent storage is slow; no task is created here.
        if not self._matches(message) or len(self._requests) >= MAX_REQUESTS:
            return False
        task = asyncio.current_task()
        self._requests.add(task)
        try:
            async with self._accept_lock:
                # Construct Red's context only after acquiring its public lock;
                # cancellation while waiting must not abandon its read coroutine.
                async with (
                    self.config.discord_trigger.get_lock(),
                    self.config.discord_trigger(acquire_lock=False) as policy,
                ):
                    if (
                        self.closed
                        or not self._active(policy)
                        or message.id <= policy["last_message_id"]
                    ):
                        return False
                    policy["last_message_id"] = message.id
                    policy["pending"] = True
                self.policy["last_message_id"] = message.id
                self._event.set()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._error(error, "Discord trigger receipt")
            return False
        finally:
            self._requests.discard(task)

    @staticmethod
    def _error(error, stage):
        log.error(
            "Discord-triggered repository/cog update failed.",
            extra={"notification_error": type(error).__name__, "notification_stage": stage},
        )

    async def _result(self, status, detail, *, after_reload=False):
        async with (
            self.config.discord_trigger.get_lock(),
            self.config.discord_trigger(acquire_lock=False) as policy,
        ):
            if (
                (self.closed and not after_reload)
                or not self._active(policy)
                or policy["last_result"].get("token") != self._run_token
            ):
                return False
            policy["last_result"].update(status=status, detail=detail, at=int(time.time()))
        return True

    async def _claim(self):
        async with self._accept_lock:
            async with (
                self.config.discord_trigger.get_lock(),
                self.config.discord_trigger(acquire_lock=False) as policy,
            ):
                if self.closed or not self._active(policy):
                    return False
                self._run_token = secrets.token_hex(8)
                policy["last_result"] = {
                    "status": "running",
                    "detail": "Refreshing all repositories and unpinned cogs.",
                    "at": int(time.time()),
                    "started_at": time.time(),
                    "token": self._run_token,
                }
                policy["pending"] = False
            # Keep admission locked until the durable pending bit and event agree.
            self._event.clear()
        return True

    async def _run(self):
        if self.ready:
            try:
                await self.ready()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._error(error, "Discord trigger readiness")
                self.closed = True
                return
        previous = self.policy["last_result"]
        started = previous.get("started_at", previous.get("at", 0))
        elapsed = (
            max(0, time.time() - started)
            if isinstance(started, (int, float)) and math.isfinite(started)
            else self.interval
        )
        last_start = time.monotonic() - min(self.interval, elapsed)
        while not self.closed:
            reloading = False
            claimed = False
            try:
                await self._event.wait()
                await asyncio.sleep(
                    max(self.coalesce, last_start + self.interval - time.monotonic())
                )
                if not await self._claim():
                    return
                claimed = True
                last_start = time.monotonic()
                status, detail, finish = await self.update(self.policy)
                if not await self._result("reloading" if finish else status, detail):
                    return
                if finish:
                    # The callback may unload this cog. close() leaves its own
                    # worker alive; generation and run tokens fence old results.
                    reloading = True
                    await finish()
                    await self._result(status, detail, after_reload=True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._error(error, "Discord automatic reload" if reloading else "Discord update")
                if claimed:
                    try:
                        await self._result(
                            "failed",
                            "Update failed; check Red logs and the result channel.",
                            after_reload=reloading,
                        )
                    except Exception as storage_error:
                        self._error(storage_error, "Discord trigger result storage")
                # A storage outage must not cause an unbounded retry loop.
                await asyncio.sleep(max(self.coalesce, self.interval, 0.01))
