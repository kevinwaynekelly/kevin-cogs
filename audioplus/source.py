"""Native PCM decoding with bounded clips and owned FFmpeg cleanup."""

import audioop
import os
import shlex
import threading

import discord

from .resolver import MediaError, Stream


class NativeSource(discord.AudioSource):
    def __init__(
        self,
        stream: Stream,
        *,
        volume: int,
        start: int = 0,
        normalize: bool = False,
        duration: int = 0,
    ):
        before = (
            "-nostdin -protocol_whitelist file,pipe"
            if stream.local
            else "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 15000000 -protocol_whitelist http,https,tcp,tls,crypto,pipe"
        )
        if stream.headers:
            headers = "".join(f"{key}: {value}\r\n" for key, value in stream.headers.items())
            before += " -headers " + shlex.quote(headers)
        if start:
            before += f" -ss {start / 1000:.3f}"
        self._stderr = open(os.devnull, "wb")
        try:
            self._audio = discord.FFmpegPCMAudio(
                stream.url,
                before_options=before,
                options="-vn -loglevel error"
                + (f" -t {duration / 1000:.3f}" if duration else "")
                + (" -af loudnorm=I=-16:TP=-1.5:LRA=11" if normalize else ""),
                stderr=self._stderr,
            )
        except BaseException:
            self._stderr.close()
            raise
        self.volume = volume
        self.start = start
        self.frames = 0
        self.max_frames = duration // 20 if duration else None
        self._cleaned = False
        self._cleanup_lock = threading.Lock()

    @property
    def position(self):
        return self.start + self.frames * 20

    def read(self):
        if self.max_frames is not None and self.frames >= self.max_frames:
            return b""
        data = self._audio.read()
        if not data:
            # EOF can be a decoder/network failure rather than a natural track end.
            code = self._audio._process.wait(timeout=2)
            if code or not self.frames:
                raise MediaError(
                    "FFmpeg could not read this audio stream. Try another track or run audio tone to test the local player."
                )
            return b""
        self.frames += 1
        return audioop.mul(data, 2, self.volume / 100) if self.volume != 100 else data

    def is_opus(self):
        return False

    def cleanup(self):
        if getattr(self, "_cleaned", True):
            return
        # Discord's audio thread and the event loop can both request cleanup.
        # A completed flag must mean the child is reaped, not merely claimed.
        with self._cleanup_lock:
            if self._cleaned:
                return
            try:
                self._audio.cleanup()
            finally:
                self._stderr.close()
                self._cleaned = True
