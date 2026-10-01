"""Useful playback errors without arbitrary exception text or signed media URLs."""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from contextlib import contextmanager
from pathlib import PurePath

import discord
from redbot.core import commands

from .resolver import MediaError

log = logging.getLogger(__name__)


def _identifier(value):
    return (
        isinstance(value, str)
        and len(value) <= 100
        and all(part.isidentifier() for part in value.split("."))
    )


def safe_exception(error):
    """Use structured exception fields, never arbitrary messages/objects/HTTP bodies."""
    name = type(error).__name__
    if isinstance(error, discord.HTTPException) and all(
        isinstance(value, int) for value in (error.status, error.code)
    ):
        return f"{name}: HTTP {error.status}, Discord error {error.code}."
    if isinstance(error, discord.ConnectionClosed) and isinstance(error.code, int):
        return f"{name}: Discord WebSocket close code {error.code}."
    if isinstance(error, asyncio.TimeoutError):
        return f"{name}: the operation timed out."
    if isinstance(error, AttributeError) and _identifier(error.name):
        return f"{name}: missing attribute `{error.name}` on `{type(error.obj).__name__}`."
    if isinstance(error, (ImportError, NameError)) and _identifier(error.name):
        return f"{name}: unavailable name/module `{error.name}`."
    if isinstance(error, OSError) and isinstance(error.errno, int):
        return f"{name}: errno {error.errno} ({os.strerror(error.errno)})."
    if isinstance(error, RuntimeError):
        # These exact Discord.py messages contain no runtime input.
        known = {
            "PyNaCl library needed in order to use voice": "PyNaCl is unavailable. Run audiorepair and restart Red.",
            "davey library needed in order to use voice": "davey is unavailable. Run audiorepair and restart Red.",
            "davey library needed in order to use E2EE voice": "davey is unavailable. Run audiorepair and restart Red.",
        }
        if str(error) in known:
            return f"{name}: {known[str(error)]}"
    return name


def _locations(error):
    return [
        f"{PurePath(frame.f_code.co_filename).name}:{line} ({frame.f_code.co_name})"
        for frame, line in list(traceback.walk_tb(error.__traceback__))[-12:]
    ]


def log_failure(stage, error, *, guild_id=None):
    """Keep traceback locations useful while excluding source lines and exception text."""
    locations = " -> ".join(_locations(error))
    context = f" in guild {guild_id}" if isinstance(guild_id, int) else ""
    log.warning(
        "AudioPlus failed at %s%s: %s; traceback: %s",
        stage,
        context,
        safe_exception(error),
        locations,
    )


class PlaybackFailure(MediaError):
    def __init__(self, stage, error):
        reason = (
            str(error)
            if isinstance(error, (MediaError, commands.CommandError))
            else safe_exception(error)
        )
        detail = f"**Stage** · {stage}\n**Cause** · {reason}"
        locations = _locations(error)
        if not isinstance(error, (MediaError, commands.CommandError)) and locations:
            location = locations[-1].replace("`", "")[:200]
            detail += f"\n**Location** · `{location}`"
        super().__init__(detail)


@contextmanager
def playback_stage(stage):
    try:
        yield
    except PlaybackFailure:
        raise
    except Exception as error:
        log_failure(stage, error)
        raise PlaybackFailure(stage, error) from error
