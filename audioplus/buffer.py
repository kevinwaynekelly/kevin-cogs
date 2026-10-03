"""Bounded PCM read-ahead without blocking Discord's packet-sending thread."""

from __future__ import annotations

import asyncio
import audioop
import threading
import time
from collections import deque

import discord

from .source import DecoderError

BUFFER_FRAMES = 6000
PREFILL_FRAMES = 150
STARTUP_TIMEOUT = 10.0
STALL_TIMEOUT = 15.0
FRAME_SECONDS = 0.02
SILENCE = bytes(3840)


class BufferedSource(discord.AudioSource):
    def __init__(self, decoder):
        self.decoder = decoder
        self.volume = decoder.volume
        # Apply gain when a frame is consumed, including previously buffered audio.
        decoder.volume = 100
        self.start = decoder.start
        self.frames = 0
        self.underruns = 0
        self.silence_frames = 0
        self._max_read_ms = 0.0
        self._read_started = 0.0
        self._frames = deque()
        self._condition = threading.Condition()
        self._cleanup_lock = threading.Lock()
        self._stopped = False
        self._cleaned = False
        self._done = False
        self._error = None
        self._refilling = False
        self._refill_started = 0.0
        self._thread = threading.Thread(
            target=self._produce, name="AudioPlusPCMBuffer", daemon=True
        )
        try:
            self._thread.start()
        except BaseException:
            self.cleanup()
            raise

    @property
    def position(self):
        return self.start + self.frames * 20

    def _produce(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._stopped or len(self._frames) < BUFFER_FRAMES
                    )
                    if self._stopped:
                        return
                    started = self._read_started = time.monotonic()
                frame = self.decoder.read()
                elapsed = (time.monotonic() - started) * 1000
                with self._condition:
                    if self._stopped:
                        return
                    self._max_read_ms = max(self._max_read_ms, elapsed)
                    self._read_started = 0.0
                    if not frame:
                        self._done = True
                        self._condition.notify_all()
                        return
                    if len(frame) != len(SILENCE):
                        raise DecoderError("The audio decoder returned an invalid PCM frame.")
                    self._frames.append(frame)
                    self._condition.notify_all()
        except Exception as error:
            with self._condition:
                if not self._stopped:
                    if self._read_started:
                        self._max_read_ms = max(
                            self._max_read_ms, (time.monotonic() - self._read_started) * 1000
                        )
                    self._read_started = 0.0
                    self._error = error
                    self._done = True
                    self._condition.notify_all()

    def _timeout_error(self, *, startup=False):
        reason = "did not become ready" if startup else "stalled while refilling"
        return DecoderError(
            f"The audio supply {reason}. Run /playerstate for buffer diagnostics.",
            retryable=not getattr(self.decoder, "_local", False),
        )

    def _wait_ready(self):
        deadline = time.monotonic() + STARTUP_TIMEOUT
        with self._condition:
            while not self._stopped and not self._done and len(self._frames) < PREFILL_FRAMES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._timeout_error(startup=True)
                self._condition.wait(timeout=remaining)
            if self._stopped:
                raise DecoderError("Audio preparation was interrupted.")
            if not self._frames and self._error:
                raise self._error

    async def prepare(self):
        """Prefill outside the event loop; the player owns cancellation/cleanup."""
        await asyncio.to_thread(self._wait_ready)

    def read(self):
        # This function never waits for FFmpeg or a network response.
        with self._condition:
            if self._stopped:
                return b""
            if self._refilling and (len(self._frames) >= PREFILL_FRAMES or self._done):
                self._refilling = False
            if not self._refilling and self._frames:
                data = self._frames.popleft()
                self.frames += 1
                self._condition.notify_all()
            elif self._done and not self._frames:
                if self._error:
                    raise self._error
                return b""
            else:
                now = time.monotonic()
                if not self._refilling:
                    self._refilling = True
                    self._refill_started = now
                    self.underruns += 1
                if now - self._refill_started >= STALL_TIMEOUT:
                    raise self._timeout_error()
                self.silence_frames += 1
                return SILENCE
        return audioop.mul(data, 2, self.volume / 100) if self.volume != 100 else data

    def diagnostics(self):
        with self._condition:
            ongoing = (time.monotonic() - self._read_started) * 1000 if self._read_started else 0.0
            return {
                "buffer_seconds": len(self._frames) * FRAME_SECONDS,
                "target_seconds": BUFFER_FRAMES * FRAME_SECONDS,
                "prefill_seconds": PREFILL_FRAMES * FRAME_SECONDS,
                "refilling": self._refilling,
                "underruns": self.underruns,
                "silence_ms": self.silence_frames * 20,
                "max_read_ms": max(self._max_read_ms, ongoing),
            }

    def is_opus(self):
        return False

    def cleanup(self):
        with self._cleanup_lock:
            if self._cleaned:
                return
            with self._condition:
                if self._read_started:
                    self._max_read_ms = max(
                        self._max_read_ms, (time.monotonic() - self._read_started) * 1000
                    )
                self._stopped = True
                self._condition.notify_all()
            try:
                # Terminating the decoder unblocks a pending pipe read.
                self.decoder.cleanup()
            finally:
                if (
                    self._thread.ident is not None
                    and self._thread is not threading.current_thread()
                ):
                    self._thread.join(timeout=3)
                with self._condition:
                    self._frames.clear()
                    self._error = None
                    self._read_started = 0.0
                    self._cleaned = True
