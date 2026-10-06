"""Observe suite errors without formatting log messages or keeping tracebacks."""

import logging
import re
import threading

from .constants import COGS, MAX_PENDING
from .outbox import identifier


def namespace(name):
    if not isinstance(name, str):
        return None
    parts = name.lower().split(".")
    # A notification transport error must never report itself recursively.
    if "notificationplus" in parts:
        return None
    return next((COGS[part] for part in parts if part in COGS), None)


def source(record):
    if "notificationplus" in record.name.lower().split("."):
        return None
    name = namespace(record.name)
    if name:
        return name
    traceback = record.exc_info[2] if record.exc_info else None
    frames = 0
    while traceback is not None and frames < 64:
        name = namespace(traceback.tb_frame.f_globals.get("__name__"))
        if name:
            return name
        traceback = traceback.tb_next
        frames += 1
    return None


def sanitized_error(record):
    """Build a detached exception with only a safe type and numeric error code."""
    original = record.exc_info[1] if record.exc_info else None
    name = (
        type(original).__name__
        if original is not None
        else getattr(record, "notification_error", None)
    )
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,80}", name):
        return None
    safe = type(name, (Exception,), {})()
    if original is not None:
        for attribute, lower, upper in (("status", 100, 599), ("errno", 1, 4095)):
            try:
                value = getattr(original, attribute, None)
            except Exception:
                continue
            if type(value) is int and lower <= value <= upper:
                setattr(safe, attribute, value)
    return safe


class SuiteErrors(logging.Handler):
    """Admission is bounded before scheduling callbacks from foreign threads."""

    def __init__(self, cog, loop):
        super().__init__(logging.WARNING)
        self.cog = cog
        self.loop = loop
        self._admission = threading.Lock()
        self._pending = 0
        self.dropped = 0
        self.accepting = True

    def emit(self, record):
        if not self.accepting or record.levelno < logging.WARNING:
            return
        owner = source(record)
        if owner is None:
            return
        stage = identifier(getattr(record, "notification_stage", record.funcName))
        guild_id = getattr(record, "notification_guild_id", None)
        guild_id = guild_id if type(guild_id) is int and 0 < guild_id < 2**64 else None
        event = (owner, stage, guild_id, sanitized_error(record), self.cog._generation)
        with self._admission:
            if not self.accepting:
                return
            if self._pending >= MAX_PENDING:
                self.dropped += 1
                return
            self._pending += 1
        try:
            self.loop.call_soon_threadsafe(self._enqueue, event)
        except RuntimeError:
            self.completed()

    def _enqueue(self, event):
        if not self.accepting or self.cog._closing:
            self.completed()
            return
        try:
            self.cog._pending.put_nowait(event)
        except Exception:
            self.completed()

    def completed(self):
        with self._admission:
            self._pending = max(0, self._pending - 1)

    def close(self):
        with self._admission:
            self.accepting = False
        super().close()
