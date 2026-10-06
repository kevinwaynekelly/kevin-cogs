"""Atomic, bounded alert handoff to a separately scheduled Unraid host script."""

import asyncio
import json
import logging
import os
import re
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .constants import (
    CAUSES,
    COGS,
    DEDUPE_SECONDS,
    DEFAULT_CAUSES,
    KINDS,
    MAX_EVENTS,
    MAX_FILE_BYTES,
    MAX_SEQUENCE,
    RETENTION_SECONDS,
)

log = logging.getLogger(__name__)


def identifier(value, fallback="unspecified"):
    """Only accept short developer-controlled stage/type identifiers."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_. -]{1,80}", value):
        return value
    return fallback


def safe_exception(error):
    """Never call str/repr on an exception or retain its arguments."""
    if error is None:
        return None
    if isinstance(error, TimeoutError):
        return "TimeoutError"
    name = type(error).__name__
    result = name if re.fullmatch(r"[A-Za-z0-9_]{1,80}", name) else "Exception"
    for attribute, lower, upper, label in (
        ("status", 100, 599, "HTTP"),
        ("errno", 1, 4095, "errno"),
    ):
        try:
            value = getattr(error, attribute, None)
        except Exception:
            continue
        if type(value) is int and lower <= value <= upper:
            result += f" ({label} {value})"
    return result


def instant(value):
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 32:
        raise ValueError("Invalid alert timestamp")
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    if result.utcoffset().total_seconds() != 0:
        raise ValueError("Invalid alert timestamp")
    return result


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def completed_task(task):
    """Wait for owned work even if cancellation is requested more than once."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve any failure before restoring the caller's cancellation.
        if not task.cancelled():
            try:
                task.result()
            except Exception:
                pass
        raise


async def completed_io(function, *arguments):
    """A cancelled caller still owns and waits for an already-started file write."""
    return await completed_task(asyncio.create_task(asyncio.to_thread(function, *arguments)))


def fresh_state(enabled=False):
    return {
        "schema": 1,
        "producer": uuid.uuid4().hex,
        "enabled": enabled,
        "sequence": 0,
        "events": [],
    }


def validate(state):
    if (
        not isinstance(state, dict)
        or set(state) != {"schema", "producer", "enabled", "sequence", "events"}
        or type(state.get("schema")) is not int
        or state["schema"] != 1
    ):
        raise ValueError("Invalid alert schema")
    if not isinstance(state.get("producer"), str) or not re.fullmatch(
        r"[0-9a-f]{32}", state["producer"]
    ):
        raise ValueError("Invalid alert producer")
    if type(state.get("enabled")) is not bool:
        raise ValueError("Invalid alert flag")
    sequence = state.get("sequence")
    if type(sequence) is not int or not 0 <= sequence <= MAX_SEQUENCE:
        raise ValueError("Invalid alert sequence")
    events = state.get("events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError("Invalid alert count")
    previous = 0
    for event in events:
        if not isinstance(event, dict) or set(event) != {
            "seq",
            "at",
            "cog",
            "kind",
            "stage",
            "cause",
            "guild_id",
        }:
            raise ValueError("Invalid alert event")
        if type(event["seq"]) is not int or not previous < event["seq"] <= sequence:
            raise ValueError("Invalid alert order")
        previous = event["seq"]
        instant(event["at"])
        if (
            not isinstance(event["cog"], str)
            or not isinstance(event["kind"], str)
            or (
                event["cog"] not in {*COGS.values(), "NotificationPlus"}
                or event["kind"] not in KINDS
            )
        ):
            raise ValueError("Invalid alert source")
        if event["cog"] == "NotificationPlus" and event["kind"] != "test":
            raise ValueError("Invalid recursive alert")
        if identifier(event["stage"], None) is None:
            raise ValueError("Invalid alert stage")
        # Exception summaries are constructed by report(), never restored from
        # arbitrary strings that could smuggle URLs into later email delivery.
        cause = event["cause"]
        if not isinstance(cause, str) or not (
            cause in CAUSES
            or re.fullmatch(
                r"[A-Za-z0-9_]{1,80}(?: \(HTTP [1-5][0-9]{2}\))?(?: \(errno [0-9]{1,4}\))?", cause
            )
        ):
            raise ValueError("Invalid alert cause")
        guild = event["guild_id"]
        if guild is not None and (
            not isinstance(guild, str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", guild)
            or int(guild) >= 2**64
        ):
            raise ValueError("Invalid alert server")
    return deepcopy(state)


class Outbox:
    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.state = None
        self.lock = asyncio.Lock()
        self.last_error = None

    def _read(self):
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Alert outbox exceeds size limit")
            return validate(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return fresh_state()

    def _write(self, state):
        payload = json.dumps(state, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError("Alert outbox exceeds size limit")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=".unraid-", suffix=".json", dir=self.path.parent
        )
        try:
            with os.fdopen(handle, "wb") as destination:
                os.fchmod(destination.fileno(), 0o600)
                destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def prune(self):
        now = self.clock()
        previous = self.state["events"]
        self.state["events"] = [
            event
            for event in previous
            if 0 <= (now - instant(event["at"])).total_seconds() < RETENTION_SECONDS
        ][-MAX_EVENTS:]
        return previous != self.state["events"]

    async def load(self, enabled):
        async with self.lock:
            try:
                self.state = await completed_io(self._read)
            except (ValueError, UnicodeError, json.JSONDecodeError):
                # Never forward corrupt records to the host. Changing producer
                # tells its cursor that this is a new, empty stream.
                log.warning("Invalid notification outbox replaced; queued alerts discarded")
                self.state = fresh_state()
            self.state["enabled"] = enabled
            self.prune()
            if not enabled:
                self.state["events"] = []
            await self._save()

    async def _save(self):
        try:
            await completed_io(self._write, deepcopy(self.state))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = safe_exception(error)
            log.error("Could not write notification outbox (%s)", self.last_error)
            return False
        self.last_error = None
        return True

    async def configure(self, enabled):
        async with self.lock:
            previous = deepcopy(self.state)
            self.state["enabled"] = enabled
            if not enabled:
                self.state["events"] = []
            self.prune()
            saved = await self._save()
            if not saved:
                self.state = previous
            return saved

    async def snapshot(self):
        async with self.lock:
            if self.prune() or self.last_error:
                await self._save()
            return deepcopy(self.state)

    async def report(
        self, cogname, stage, guild_id=None, *, error=None, kind="notification", cause=None
    ):
        if cogname not in COGS.values() and not (cogname == "NotificationPlus" and kind == "test"):
            return False
        if kind not in KINDS:
            return False
        stage = identifier(stage)
        cause = (
            cause
            if isinstance(cause, str) and cause in CAUSES
            else safe_exception(error) or DEFAULT_CAUSES[kind]
        )
        guild_id = str(guild_id) if type(guild_id) is int and 0 < guild_id < 2**64 else None
        async with self.lock:
            if not self.state["enabled"]:
                return False
            self.prune()
            now = self.clock()
            key = (cogname, stage, guild_id, kind, cause)
            if kind != "test" and any(
                (event["cog"], event["stage"], event["guild_id"], event["kind"], event["cause"])
                == key
                and (now - instant(event["at"])).total_seconds() < DEDUPE_SECONDS
                for event in self.state["events"]
            ):
                if self.last_error:
                    await self._save()
                return False
            if self.state["sequence"] >= MAX_SEQUENCE:
                self.state = fresh_state(enabled=True)
            self.state["sequence"] += 1
            self.state["events"].append(
                {
                    "seq": self.state["sequence"],
                    "at": timestamp(now),
                    "cog": cogname,
                    "kind": kind,
                    "stage": stage,
                    "cause": cause,
                    "guild_id": guild_id,
                }
            )
            self.state["events"] = self.state["events"][-MAX_EVENTS:]
            return await self._save()
