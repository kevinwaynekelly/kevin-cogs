"""Native PCM decoding with bounded clips and owned FFmpeg cleanup."""

import audioop
import os
import re
import shlex
import threading

import discord

from .media_network import REMOTE_OPTIONS, MediaRelay, NetworkPolicyError
from .resolver import MediaError, Stream


class DecoderError(MediaError):
    """A public, allowlisted failure; never retain FFmpeg's arbitrary text."""

    def __init__(self, message, *, retryable=False, http_status=None):
        super().__init__(message)
        self.retryable = retryable
        self.http_status = http_status


class _DecoderStderr:
    """Drain the child continuously while retaining at most 16 KiB in memory."""

    LIMIT = 16 * 1024

    def __init__(self):
        reader, writer = os.pipe()
        self.reader = os.fdopen(reader, "rb", buffering=0)
        self.writer = os.fdopen(writer, "wb", buffering=0)
        self._tail = bytearray()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, name="AudioDecoderStderr", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self.reader.close()
            self.writer.close()
            raise

    def _drain(self):
        try:
            with self.reader:
                while data := self.reader.read(8192):
                    with self._lock:
                        self._tail.extend(data)
                        del self._tail[: -self.LIMIT]
        except OSError:
            pass

    def failure(self, *, local):
        self._thread.join(timeout=2)
        with self._lock:
            text = self._tail.decode("utf-8", errors="replace").lower()
        # Only fixed messages and a numeric status can reach users or logs.
        status = re.search(
            r"\b(?:http error|http status(?: code)?|server returned)\s+([45]\d{2})\b", text
        )
        if not local and status:
            code = int(status[1])
            reason = "denied this stream" if code in {401, 403} else "rejected this stream"
            return DecoderError(
                f"FFmpeg: the audio server {reason} (HTTP {code}).",
                retryable=code in {401, 403, 404, 408, 410, 425, 429} or code >= 500,
                http_status=code,
            )
        if not local and any(
            value in text
            for value in (
                "connection timed out",
                "connection reset",
                "connection refused",
                "network is unreachable",
                "temporary failure in name resolution",
                "input/output error",
            )
        ):
            return DecoderError("FFmpeg: the audio connection failed or timed out.", retryable=True)
        if "not on whitelist" in text or "protocol not found" in text:
            return DecoderError("FFmpeg: this audio stream requires an unsupported protocol.")
        if "decoder" in text and any(value in text for value in ("not found", "unsupported")):
            return DecoderError("FFmpeg: the installed build cannot decode this audio format.")
        if local:
            return DecoderError("FFmpeg could not read the saved audio copy.")
        if "invalid data found" in text or "does not contain any stream" in text:
            return DecoderError("FFmpeg: the source returned unreadable audio data.")
        return DecoderError(
            "FFmpeg could not read this audio stream. Run /tone to test the local player."
        )

    def close(self):
        self.writer.close()
        self._thread.join(timeout=2)
        with self._lock:
            self._tail.clear()


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
        self._relay = None
        if not stream.local:
            try:
                self._relay = MediaRelay(stream.url, stream.headers)
            except NetworkPolicyError as error:
                raise MediaError(str(error)) from error
        before = (
            "-nostdin -protocol_whitelist file,pipe"
            if stream.local
            else "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 15000000 "
            + REMOTE_OPTIONS
        )
        if stream.headers and stream.local:
            headers = "".join(f"{key}: {value}\r\n" for key, value in stream.headers.items())
            before += " -headers " + shlex.quote(headers)
        if start:
            before += f" -ss {start / 1000:.3f}"
        self._stderr = None
        try:
            self._stderr = _DecoderStderr()
            self._audio = discord.FFmpegPCMAudio(
                self._relay.url if self._relay else stream.url,
                before_options=before,
                options="-vn -loglevel warning"
                + (f" -t {duration / 1000:.3f}" if duration else "")
                + (" -af loudnorm=I=-16:TP=-1.5:LRA=11" if normalize else ""),
                stderr=self._stderr.writer,
            )
        except BaseException:
            if self._stderr:
                self._stderr.close()
            if self._relay:
                self._relay.close()
            raise
        # The child owns its inherited write descriptor until it exits.
        self._stderr.writer.close()
        self._local = stream.local
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
                raise self._stderr.failure(local=self._local)
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
                if self._relay:
                    self._relay.close()
                self._cleaned = True
