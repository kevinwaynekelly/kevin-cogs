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

VOICE_REQUIREMENTS = {"PyNaCl": "PyNaCl>=1.5.0,<1.6", "davey": "davey>=0.1.6"}


def _voice_import(package):
    module = import_module("nacl" if package == "PyNaCl" else "davey")
    if package == "PyNaCl":
        import_module("nacl.secret")
        import_module("nacl.utils")
    return module


def voice_import_status():
    """Check imports separately from distribution metadata, without rebinding Discord."""
    statuses = {}
    for package in VOICE_REQUIREMENTS:
        try:
            _voice_import(package)
        except (ImportError, OSError, RuntimeError) as exc:
            missing = isinstance(exc, ModuleNotFoundError) and exc.name == (
                "nacl" if package == "PyNaCl" else "davey"
            )
            statuses[package] = "Missing" if missing else f"Import failed ({type(exc).__name__})"
        else:
            statuses[package] = "Ready"
    return statuses


def _voice_import_error(package, exc):
    # Raw native/pip errors can contain paths or configured index credentials.
    return MediaError(
        f"{package} could not be imported by Red ({type(exc).__name__}). "
        "Ask the bot owner to run audiorepair, then restart Red and run audiostatus. "
        f"Manual setup: install {VOICE_REQUIREMENTS[package]} in Red's Python environment. "
        "See AudioPlus's container setup guide."
    )


def _load_voice_libraries():
    """Initialize optional voice imports exposed after Discord.py's first import.

    Red imports Discord.py before adding Downloader's private dependency folder
    to sys.path. Bind the newly available libraries without reloading Discord
    modules or replacing classes already used by the running bot.
    """
    client = discord.voice_client
    if not getattr(client, "has_nacl", False):
        try:
            nacl = _voice_import("PyNaCl")
        except (ImportError, OSError, RuntimeError) as exc:
            raise _voice_import_error("PyNaCl", exc) from exc
        client.nacl = nacl
        client.has_nacl = True
    if not getattr(client, "has_dave", False) or not getattr(
        discord.voice_state, "has_dave", False
    ):
        try:
            davey = _voice_import("davey")
        except (ImportError, OSError, RuntimeError) as exc:
            raise _voice_import_error("davey", exc) from exc
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


async def diagnostics(*, voice_error=None, voice_guard=None):
    binaries = await asyncio.gather(
        executable_version("ffmpeg", "-version"),
        executable_version("deno"),
        executable_version("node"),
        executable_version("qjs"),
    )
    # Repair can start while executable probes are awaiting their subprocesses.
    if voice_guard:
        voice_error = voice_guard() or voice_error
    packages = {}
    for name in ("yt-dlp", "yt-dlp-ejs", "PyNaCl", "davey"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "missing"
    voice_packages = (
        dict.fromkeys(VOICE_REQUIREMENTS, "Restart required")
        if voice_error
        else voice_import_status()
    )
    runtimes = []
    for name, version, minimum in (("Deno", binaries[1], (2, 3)), ("Node", binaries[2], (22, 0))):
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
        if match and tuple(map(int, match.groups()[:2])) >= minimum:
            runtimes.append(name)
    if binaries[3] not in {"missing", "unavailable", "timed out"}:
        runtimes.append("QuickJS")
    if not voice_error:
        try:
            require_voice()
        except MediaError as exc:
            voice_error = str(exc)
        if not voice_error and any(status != "Ready" for status in voice_packages.values()):
            voice_error = (
                "Native voice imports are unavailable. Ask the bot owner to run audiorepair, "
                "then restart Red and run audiostatus."
            )
    ready = (
        not voice_error
        and all(value != "missing" for value in packages.values())
        and bool(runtimes)
    )
    return {
        "ready": ready,
        "voice_error": voice_error,
        "packages": packages,
        "voice_packages": voice_packages,
        "ffmpeg": binaries[0],
        "deno": binaries[1],
        "node": binaries[2],
        "quickjs": binaries[3],
        "runtimes": runtimes,
    }
