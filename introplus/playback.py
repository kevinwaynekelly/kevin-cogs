"""Finite intros and PCM overlays without changing music queues or progress."""

import asyncio
import audioop
import threading

import discord

from .resolver import MediaError


class OverlaySource(discord.AudioSource):
    def __init__(self, base, clip, complete):
        self.base, self.clip, self.complete = base, clip, complete
        self._finished = False
        self._cleaned = False
        self._lock = threading.RLock()

    def _finish(self, error=None):
        if not self._finished:
            self._finished = True
            try:
                self.clip.cleanup()
            except Exception:
                error = error or MediaError("The intro decoder could not finish cleanup.")
            finally:
                self.complete(error)

    def read(self):
        with self._lock:
            music = self.base.read()
            if not music:
                self._finish(MediaError("The music track ended before the intro finished."))
                return b""
            if self._finished:
                return music
            try:
                intro = self.clip.read()
            except Exception:
                self._finish(MediaError("The intro stream failed; music continues."))
                return music
            if not intro:
                self._finish()
                return music
            if len(intro) != len(music):
                self._finish(MediaError("The intro returned an invalid audio frame."))
                return music
            return audioop.add(audioop.mul(music, 2, 0.3), intro, 2)

    def is_opus(self):
        return False

    def detach(self):
        with self._lock:
            # AudioSource.__del__ invokes cleanup. A detached overlay no longer
            # owns the music source, including when its last reference vanishes.
            self._cleaned = True
            self._finish()

    def cleanup(self):
        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
            try:
                self._finish(MediaError("Intro playback was interrupted."))
            finally:
                self.base.cleanup()


def completion():
    """Discord audio callbacks arrive on a separate thread."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def deliver(error):
        if not future.done():
            future.set_result(error)

    def complete(error):
        if not loop.is_closed():
            loop.call_soon_threadsafe(deliver, error)

    return future, complete


async def play_overlay(voice, source, duration):
    base = voice.source
    if base is None or base.is_opus() or not voice.is_playing() or voice.is_paused():
        source.cleanup()
        raise MediaError("The existing voice player is idle, paused or uses incompatible audio.")
    future, complete = completion()
    overlay = OverlaySource(base, source, complete)
    try:
        voice.source = overlay
        error = await asyncio.wait_for(future, duration + 5)
        if error:
            raise error
    finally:
        # Stop/skip can already have installed another source. Never replace it.
        if voice.source is overlay:
            paused = voice.is_paused()
            try:
                voice.source = base
                if paused:
                    voice.pause()
            except (ValueError, discord.ClientException):
                pass
        await asyncio.to_thread(overlay.detach)


async def play_standalone(voice, source, duration):
    future, complete = completion()
    started = False
    try:
        voice.play(source, after=complete)
        started = True
        error = await asyncio.wait_for(future, duration + 5)
        if error:
            raise error
    finally:
        if started and voice.source is source:
            voice.stop()
        await asyncio.to_thread(source.cleanup)
