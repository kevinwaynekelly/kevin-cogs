"""One owned daily update task, using local calendar time and durable claims."""

import asyncio
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("downloaderplus.daily")


def next_daily(now, clock, zone):
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock):
        raise ValueError("Use a daily time in HH:MM format, such as 04:00.")
    try:
        tz = ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Use a timezone such as America/Chicago or UTC.") from exc
    hour, minute = map(int, clock.split(":"))
    local = datetime.fromtimestamp(now, timezone.utc).astimezone(tz)
    # The first occurrence of a repeated time wins. A nonexistent time shifts
    # forward by the DST gap when converted back from its UTC timestamp.
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return int(candidate.timestamp())


class DailyUpdates:
    def __init__(self, config, update, *, now=time.time, poll=60, ready=None):
        self.config, self.update = config, update
        self.now, self.poll = now, poll
        self.ready = ready
        self.worker = None
        self.closed = True
        self.policy = {}
        self.run_token = ""

    async def start(self):
        self.policy = await self.config.daily()
        if not self.policy["enabled"]:
            return
        next_daily(self.now(), self.policy["time"], self.policy["timezone"])
        self.closed = False
        self.worker = asyncio.create_task(self._run(), name="DownloaderPlusDaily")

    async def close(self):
        self.closed = True
        task, self.worker = self.worker, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def active(self, policy):
        return policy["enabled"] and policy["generation"] == self.policy["generation"]

    async def _result(self, status, detail, *, after_reload=False):
        async with self.config.daily() as policy:
            if (
                not self.active(policy)
                or (self.closed and not after_reload)
                or policy["last_result"].get("token") != self.run_token
            ):
                return False
            policy["last_result"].update(status=status, detail=detail, at=int(self.now()))
        return True

    async def _run(self):
        if self.ready:
            await self.ready()
        while not self.closed:
            try:
                policy = await self.config.daily()
                if not self.active(policy):
                    return
                remaining = policy["next_run"] - self.now()
                if remaining > 0:
                    await asyncio.sleep(min(self.poll, remaining))
                    continue
                async with self.config.daily() as policy:
                    if self.closed or not self.active(policy) or policy["next_run"] > self.now():
                        continue
                    # Claim before starting Git/Pip so a restart cannot replay
                    # an uncertain installation repeatedly. Missed days coalesce.
                    policy["next_run"] = next_daily(self.now(), policy["time"], policy["timezone"])
                    self.run_token = secrets.token_hex(8)
                    policy["last_result"] = {
                        "status": "running",
                        "detail": "Updating repositories and cogs.",
                        "at": int(self.now()),
                        "token": self.run_token,
                    }
                status, detail, finish = await self.update(self.policy)
                if not await self._result("reloading" if finish else status, detail):
                    return
                if finish:
                    await finish()
                    await self._result(status, detail, after_reload=True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error(
                    "Daily repository/cog update failed.",
                    extra={
                        "notification_error": type(error).__name__,
                        "notification_stage": "Daily update",
                    },
                )
                try:
                    await self._result(
                        "failed",
                        "Daily update failed; check Red logs and the result channel.",
                        after_reload=True,
                    )
                except Exception:
                    # A storage outage must not silently kill the owned scheduler.
                    log.error("Could not persist the daily update failure status.")
                # Also bound retries if the persistent claim itself could not be saved.
                await asyncio.sleep(self.poll)
