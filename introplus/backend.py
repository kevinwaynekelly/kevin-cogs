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
from .voice_libraries import (
    VOICE_REQUIREMENTS,
    IncompatibleVoiceLibrary,
    import_voice_library,
    validate_voice_library,
    voice_library_status,
)


def _voice_import(package):
    module = import_voice_library(package, importer=import_module)
    # Discord can retain a different module from its startup import. Check the
    # references actually used by its handshake, gateway, and encryption code.
    bindings = (
        (discord.voice_client,)
        if package == "PyNaCl"
        else (discord.voice_client, discord.voice_state, discord.gateway)
    )
    name = "nacl" if package == "PyNaCl" else "davey"
    for binding in bindings:
        bound = getattr(binding, name, None)
        if bound is not None and bound is not module:
            validate_voice_library(package, bound)
    return module


def voice_import_status():
    """Check imported and bound native APIs without rebinding Discord."""
    return voice_library_status(importer=_voice_import)


def _voice_import_error(package, exc):
    # Raw native/pip errors can contain paths or configured index credentials.
    problem = (
        f"{package} is incompatible with Discord.py: missing or invalid native API {exc.details}. "
        if isinstance(exc, IncompatibleVoiceLibrary)
        else f"{package} could not be imported by Red ({type(exc).__name__}). "
    )
    return MediaError(
        problem
        + "Ask the bot owner to install the native voice libraries and restart Red; AudioPlus also offers audiorepair when installed. "
        f"Manual setup: install {VOICE_REQUIREMENTS[package]} in Red's Python environment. "
        "See IntroPlus's container setup guide."
    )


def _load_voice_libraries():
    """Initialize optional voice imports exposed after Discord.py's first import.

    Red imports Discord.py before adding Downloader's private dependency folder
    to sys.path. Bind the newly available libraries without reloading Discord
    modules or replacing classes already used by the running bot.
    """
    client = discord.voice_client
    libraries = {}
    for package in VOICE_REQUIREMENTS:
        try:
            libraries[package] = _voice_import(package)
        except (ImportError, OSError, RuntimeError) as exc:
            raise _voice_import_error(package, exc) from exc
    if not getattr(client, "has_nacl", False) or getattr(client, "nacl", None) is None:
        nacl = libraries["PyNaCl"]
        client.nacl = nacl
        client.has_nacl = True
    if not getattr(client, "has_dave", False) or not getattr(
        discord.voice_state, "has_dave", False
    ):
        for module in (client, discord.voice_state, discord.gateway):
            module.davey = libraries["davey"]
        discord.voice_state.has_dave = True
        client.has_dave = True
    else:
        for module in (client, discord.voice_state, discord.gateway):
            if getattr(module, "davey", None) is None:
                module.davey = libraries["davey"]


def require_voice():
    if not shutil.which("ffmpeg"):
        raise MediaError(
            "FFmpeg is missing from the Red container. Install it, then run intro diagnostics."
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
                "The Opus library is missing. Install libopus in the Red container, then run intro diagnostics."
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
