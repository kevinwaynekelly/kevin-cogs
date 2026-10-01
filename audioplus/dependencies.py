"""Explicit owner repair of native libraries in the running bot's Python environment."""

from __future__ import annotations

import asyncio
import json
import sys

from .backend import VOICE_REQUIREMENTS, voice_import_status
from .resolver import MediaError

_PROBE = """
import importlib, json, sys
sys.path = json.loads(sys.argv[1])
statuses = {}
for package, modules in {
    'PyNaCl': ('nacl.secret', 'nacl.utils'), 'davey': ('davey',)
}.items():
    try:
        for module in modules:
            importlib.import_module(module)
    except (ImportError, OSError, RuntimeError) as exc:
        statuses[package] = type(exc).__name__
    else:
        statuses[package] = 'Ready'
print(json.dumps(statuses))
"""


def _install_failure(output):
    text = output.lower()
    if "no module named pip" in text:
        return "pip is missing from Red's Python environment."
    if "permission denied" in text or "not writeable" in text or "not writable" in text:
        return "Red cannot write to its Python environment. Use the container setup guide."
    if "externally-managed-environment" in text:
        return "This Python environment is externally managed. Use Red's virtual environment."
    if "no matching distribution" in text or "could not find a version" in text:
        return "No compatible binary wheel was available for this Python/container architecture."
    if any(term in text for term in ("connection", "timed out", "ssl", "proxy")):
        return "pip could not reach its package index. Check the Red container's network access."
    return "pip could not install the native voice libraries. Use the container setup guide."


class VoiceDependencyRepair:
    """Own and bound installer/probe processes, including cancellation during spawn."""

    def __init__(self):
        self._task = None
        self._closed = False
        self.changed = False

    @property
    def running(self):
        return self._task is not None

    @staticmethod
    async def _finish(process):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()

    async def _run(self, args, timeout):
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
        )
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            process = await spawning
            await self._finish(process)
            raise
        except OSError as exc:
            raise MediaError("Red could not start its Python dependency installer.") from exc

        async def capture():
            output = bytearray()
            while chunk := await process.stdout.read(8192):
                output.extend(chunk)
                del output[:-32768]
            await process.wait()
            return output.decode("utf-8", errors="replace")

        try:
            output = await asyncio.wait_for(capture(), timeout)
            return process.returncode, output
        except asyncio.TimeoutError as exc:
            raise MediaError(
                "Voice dependency repair timed out. Restart Red before trying again."
            ) from exc
        finally:
            await self._finish(process)

    async def repair(self, on_start):
        if self._closed:
            raise MediaError("AudioPlus is unloading. Try again after it reloads.")
        if self.running:
            raise MediaError("A voice dependency repair is already running.")
        self._task = asyncio.current_task()
        try:
            requirements = [
                VOICE_REQUIREMENTS[name]
                for name, state in voice_import_status().items()
                if state != "Ready"
            ]
            if not requirements:
                return False
            await on_start()
            self.changed = True  # Even a failed/cancelled pip process may change installed files.
            code, output = await self._run(
                [
                    sys.executable,
                    "-I",  # Ignore Downloader/PYTHONPATH when installing into the bot environment.
                    "-m",
                    "pip",
                    "--disable-pip-version-check",
                    "install",
                    "--no-input",
                    "--only-binary=:all:",
                    "--force-reinstall",
                    "--no-cache-dir",
                    "--retries",
                    "1",
                    "--timeout",
                    "15",
                    *requirements,
                ],
                180,
            )
            if code:
                raise MediaError(_install_failure(output))
            # Match Red's actual import precedence, including existing Downloader packages.
            code, output = await self._run([sys.executable, "-c", _PROBE, json.dumps(sys.path)], 15)
            try:
                statuses = json.loads(output)
                valid = code == 0 and isinstance(statuses, dict)
                failed = [name for name in VOICE_REQUIREMENTS if statuses.get(name) != "Ready"]
            except (ValueError, AttributeError):
                valid, failed = False, list(VOICE_REQUIREMENTS)
            if not valid or failed:
                raise MediaError(
                    "Packages were installed, but fresh imports still fail for "
                    + ", ".join(failed or VOICE_REQUIREMENTS)
                    + ". Restart Red and run audiostatus. A broken Downloader copy may shadow "
                    "the bot environment; use the container setup guide if the error remains."
                )
            return True
        finally:
            self._task = None

    async def close(self):
        self._closed = True
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
