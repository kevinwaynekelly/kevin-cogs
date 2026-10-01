"""Local native voice requirements and bounded executable diagnostics."""

from __future__ import annotations

import asyncio
import ctypes.util
import importlib.metadata
import re
import shutil
from importlib import import_module

import discord

from .resolver import MediaError


def _load_voice_libraries():
    """Initialize optional voice imports exposed after Discord.py's first import.

    Red imports Discord.py before adding Downloader's private dependency folder
    to sys.path. Bind the newly available libraries without reloading Discord
    modules or replacing classes already used by the running bot.
    """
    client = discord.voice_client
    if not getattr(client, "has_nacl", False):
        try:
            nacl = import_module("nacl")
            import_module("nacl.secret")
            import_module("nacl.utils")
        except (ImportError, OSError) as exc:
            raise MediaError(
                "PyNaCl could not be imported by Red. Reinstall AudioPlus's Python "
                "dependencies and restart Red, then run audio pingnode."
            ) from exc
        client.nacl = nacl
        client.has_nacl = True
    if not getattr(client, "has_dave", False) or not getattr(
        discord.voice_state, "has_dave", False
    ):
        try:
            davey = import_module("davey")
        except (ImportError, OSError) as exc:
            raise MediaError(
                "davey could not be imported by Red. Reinstall AudioPlus's Python "
                "dependencies and restart Red, then run audio pingnode."
            ) from exc
        for module in (client, discord.voice_state, discord.gateway):
            module.davey = davey
        discord.voice_state.has_dave = True
        client.has_dave = True


def require_voice():
    if not shutil.which("ffmpeg"):
        raise MediaError(
            "FFmpeg is missing from the Red container. Install it, then run audio pingnode."
        )
    _load_voice_libraries()
    if not discord.opus.is_loaded():
        library = ctypes.util.find_library("opus")
        try:
            if library:
                discord.opus.load_opus(library)
        except OSError:
            pass
        if not discord.opus.is_loaded():
            raise MediaError(
                "The Opus library is missing. Install libopus in the Red container, then run audio pingnode."
            )


async def executable_version(name, option="--version"):
    path = shutil.which(name)
    if not path:
        return "missing"
    process = await asyncio.create_subprocess_exec(
        path, option, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 5)
        line = output.decode("utf-8", errors="replace").splitlines()
        return line[0][:160] if process.returncode == 0 and line else "unavailable"
    except asyncio.TimeoutError:
        return "timed out"
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


async def diagnostics():
    binaries = await asyncio.gather(
        executable_version("ffmpeg", "-version"),
        executable_version("deno"),
        executable_version("node"),
        executable_version("qjs"),
    )
    packages = {}
    for name in ("yt-dlp", "yt-dlp-ejs", "PyNaCl", "davey"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "missing"
    runtimes = []
    for name, version, minimum in (("Deno", binaries[1], (2, 3)), ("Node", binaries[2], (22, 0))):
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
        if match and tuple(map(int, match.groups()[:2])) >= minimum:
            runtimes.append(name)
    if binaries[3] not in {"missing", "unavailable", "timed out"}:
        runtimes.append("QuickJS")
    try:
        require_voice()
        voice_error = None
    except MediaError as exc:
        voice_error = str(exc)
    ready = (
        not voice_error
        and all(value != "missing" for value in packages.values())
        and bool(runtimes)
    )
    return {
        "ready": ready,
        "voice_error": voice_error,
        "packages": packages,
        "ffmpeg": binaries[0],
        "deno": binaries[1],
        "node": binaries[2],
        "quickjs": binaries[3],
        "runtimes": runtimes,
    }
