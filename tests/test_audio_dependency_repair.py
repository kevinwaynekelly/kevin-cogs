"""Owner checks, runtime import validation, and real bounded subprocess cleanup."""

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import make_channel
from test_audio_hybrid import red_command_runtime as command_runtime_fixture

import audioplus.backend as backend
import audioplus.dependencies as dependencies
from audioplus import AudioPlus
from audioplus.dependencies import VoiceDependencyRepair
from audioplus.resolver import MediaError

red_command_runtime = command_runtime_fixture


@pytest.fixture
def repair(monkeypatch):
    monkeypatch.setattr(
        dependencies,
        "voice_import_status",
        lambda: {"PyNaCl": "Missing", "davey": "Ready"},
    )
    return VoiceDependencyRepair()


async def test_repair_installs_only_failing_libraries_in_bot_environment(repair, monkeypatch):
    run = AsyncMock(side_effect=[(0, "Installed"), (0, '{"PyNaCl":"Ready","davey":"Ready"}')])
    monkeypatch.setattr(repair, "_run", run)
    assert await repair.repair(AsyncMock())
    install, probe = [call.args[0] for call in run.await_args_list]
    assert install[:4] == [sys.executable, "-I", "-m", "pip"]
    assert "--only-binary=:all:" in install and "--force-reinstall" in install
    assert install[-1] == "PyNaCl>=1.5.0,<1.6" and "davey>=0.1.6" not in install
    assert "-t" not in install and "--target" not in install
    assert probe[0] == sys.executable and json.loads(probe[-1]) == sys.path
    assert repair.changed and not repair.running


async def test_working_libraries_are_not_reinstalled(repair, monkeypatch):
    monkeypatch.setattr(
        dependencies,
        "voice_import_status",
        lambda: dict.fromkeys(dependencies.VOICE_REQUIREMENTS, "Ready"),
    )
    run, started = AsyncMock(), AsyncMock()
    monkeypatch.setattr(repair, "_run", run)
    assert not await repair.repair(started)
    assert not repair.changed
    run.assert_not_awaited()
    started.assert_not_awaited()


@pytest.mark.parametrize(
    "output,expected",
    [
        (
            "ERROR: No matching distribution found for davey https://user:secret@example.invalid",
            "binary wheel",
        ),
        ("Permission denied: /private/path", "cannot write"),
        ("No module named pip", "pip is missing"),
        ("externally-managed-environment", "virtual environment"),
        ("Proxy connection failed: https://user:secret@example.invalid", "package index"),
    ],
)
async def test_install_errors_are_actionable_without_leaking_pip_output(
    repair, monkeypatch, output, expected
):
    monkeypatch.setattr(repair, "_run", AsyncMock(return_value=(1, output)))
    with pytest.raises(MediaError, match=expected) as error:
        await repair.repair(AsyncMock())
    assert "secret" not in str(error.value) and "/private" not in str(error.value)
    assert repair.changed and not repair.running


@pytest.mark.parametrize("output", ['{"PyNaCl":"ImportError","davey":"Ready"}', "not json", "[]"])
async def test_successful_pip_is_not_enough_when_runtime_imports_fail(repair, monkeypatch, output):
    run = AsyncMock(side_effect=[(0, "Installed"), (0, output)])
    monkeypatch.setattr(repair, "_run", run)
    with pytest.raises(MediaError, match="fresh native API checks still fail for PyNaCl"):
        await repair.repair(AsyncMock())
    assert run.await_count == 2


async def test_real_probe_checks_libraries_including_downloader_precedence(repair, tmp_path):
    code, output = await repair._run(
        [sys.executable, "-c", dependencies._PROBE, json.dumps(sys.path)], 10
    )
    assert code == 0 and json.loads(output) == {"PyNaCl": "Ready", "davey": "Ready"}
    # A successful bot-environment installation must not conceal a broken private copy.
    (tmp_path / "nacl.py").write_text("raise ImportError('https://user:secret@example.invalid')")
    code, output = await repair._run(
        [sys.executable, "-c", dependencies._PROBE, json.dumps([str(tmp_path), *sys.path])], 10
    )
    assert code == 0 and json.loads(output) == {
        "PyNaCl": "Import failed (ImportError)",
        "davey": "Ready",
    }
    assert "secret" not in output


async def test_fresh_probe_rejects_an_importable_incomplete_downloader_copy(repair, tmp_path):
    (tmp_path / "davey.py").write_text(
        "# Import succeeds but the required native APIs are absent.\n"
    )
    code, output = await repair._run(
        [sys.executable, "-c", dependencies._PROBE, json.dumps([str(tmp_path), *sys.path])], 10
    )
    statuses = json.loads(output)
    assert code == 0 and statuses["PyNaCl"] == "Ready"
    assert statuses["davey"].startswith("Incompatible (DAVE_PROTOCOL_VERSION")


async def test_incompatible_bound_dave_library_blocks_connection_and_is_repaired(
    red_command_runtime, monkeypatch
):
    import davey

    bot, cog, member, invoke = red_command_runtime
    broken = ModuleType("davey")
    broken.__dict__.update(davey.__dict__)
    del broken.DAVE_PROTOCOL_VERSION
    monkeypatch.setattr(backend.discord.voice_state, "davey", broken)
    voice_channel = make_channel(member.guild, kind=backend.discord.VoiceChannel)
    member.voice = SimpleNamespace(channel=voice_channel)
    connect, lookup = AsyncMock(), AsyncMock()
    monkeypatch.setattr(cog, "_connect_voice", connect)
    monkeypatch.setattr(cog._resolver, "search", lookup)

    play = await invoke("!play roar")
    assert "DAVE_PROTOCOL_VERSION" in play.send.await_args.kwargs["embed"].description
    connect.assert_not_awaited()
    lookup.assert_not_awaited()
    assert cog._voice_repair.changed is False

    bot.owner_ids.add(member.id)
    run = AsyncMock(side_effect=[(0, "Installed"), (0, '{"PyNaCl":"Ready","davey":"Ready"}')])
    monkeypatch.setattr(cog._voice_repair, "_run", run)
    repaired = await invoke("!audiorepair")
    install = run.await_args_list[0].args[0]
    assert "davey>=0.1.6" in install and "PyNaCl>=1.5.0,<1.6" not in install
    assert "native API checks" in repaired.send.await_args.kwargs["embed"].description
    assert bot._audioplus_voice_restart_required is True


@pytest.mark.parametrize("during_spawn", [False, True])
async def test_unload_kills_installer_even_during_spawn(repair, monkeypatch, during_spawn):
    original = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        spawned.set()
        if during_spawn:
            await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(repair.repair(AsyncMock()))
    await asyncio.wait_for(spawned.wait(), 2)
    close = asyncio.create_task(repair.close())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(close, 2)
    assert task.cancelled() and processes[0].returncode is not None
    assert not repair.running
    with pytest.raises(MediaError, match="unloading"):
        await repair.repair(AsyncMock())


async def test_installer_timeout_kills_child_and_output_is_bounded(repair, monkeypatch):
    original = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(MediaError, match="timed out"):
        await repair._run([sys.executable, "-c", "import time; time.sleep(60)"], 0.1)
    assert processes[0].returncode is not None
    code, output = await repair._run([sys.executable, "-c", "print('x' * 1000000)"], 5)
    assert code == 0 and len(output.encode()) <= 32768


async def test_repair_is_owner_only_and_blocks_playback_until_restart(
    red_command_runtime, monkeypatch
):
    bot, cog, member, invoke = red_command_runtime
    run = AsyncMock(side_effect=[(0, "Installed"), (0, '{"PyNaCl":"Ready","davey":"Ready"}')])
    monkeypatch.setattr(cog._voice_repair, "_run", run)
    monkeypatch.setattr(
        dependencies, "voice_import_status", lambda: {"PyNaCl": "Missing", "davey": "Ready"}
    )
    # Production Red binds its error handler during startup, outside this fixture.
    on_error = AsyncMock()
    monkeypatch.setattr(bot, "on_command_error", on_error)
    await invoke("!audiorepair")
    run.assert_not_awaited()
    on_error.assert_awaited_once()
    bot.owner_ids.add(member.id)
    ctx = await invoke("!audiorepair")
    assert "fresh Python process" in ctx.send.await_args.kwargs["embed"].description
    assert bot._audioplus_voice_restart_required is True
    member.voice = SimpleNamespace(channel=make_channel(member.guild))
    play = await invoke("!play roar")
    assert "Restart Red" in play.send.await_args.kwargs["embed"].description
    await bot.remove_cog("AudioPlus")
    new = AudioPlus(bot)
    await bot.add_cog(new)
    with pytest.raises(MediaError, match="Restart Red"):
        new._require_voice()
    again = await invoke("!audiorepair")
    assert "Restart Red" in again.send.await_args.kwargs["embed"].description
    assert run.await_count == 2


@pytest.mark.parametrize("busy", ["voice", "lookup", "connection"])
async def test_busy_voice_or_lookup_prevents_package_changes(
    red_command_runtime, monkeypatch, busy
):
    bot, cog, member, invoke = red_command_runtime
    bot.owner_ids.add(member.id)
    repair = AsyncMock()
    monkeypatch.setattr(cog._voice_repair, "repair", repair)
    if busy == "voice":
        bot._connection._voice_clients[member.guild.id] = Mock(is_connected=Mock(return_value=True))
    else:
        tasks = cog._lookups if busy == "lookup" else cog._connections
        tasks.add(asyncio.create_task(asyncio.sleep(60)))
    ctx = await invoke("!audiorepair")
    assert "Disconnect voice sessions" in ctx.send.await_args.kwargs["embed"].description
    repair.assert_not_awaited()
    bot._connection._voice_clients.clear()


async def test_diagnostics_does_not_import_during_repairs(monkeypatch):
    imports = Mock(side_effect=AssertionError("Do not import while replacing files"))
    monkeypatch.setattr(backend, "voice_import_status", imports)
    monkeypatch.setattr(backend, "require_voice", imports)
    monkeypatch.setattr(backend, "executable_version", AsyncMock(return_value="missing"))
    state = await backend.diagnostics(voice_error="Restart Red")
    assert not state["ready"] and state["voice_error"] == "Restart Red"
    assert state["voice_packages"]["PyNaCl"] == "Restart required"
    imports.assert_not_called()


def test_metadata_presence_does_not_imply_importable_native_packages(monkeypatch):
    def import_module(name):
        if name == "nacl":
            raise ModuleNotFoundError("No nacl", name="nacl")
        raise OSError("Private path or credential")

    monkeypatch.setattr(backend, "import_module", import_module)
    assert backend.voice_import_status() == {
        "PyNaCl": "Missing",
        "davey": "Import failed (OSError)",
    }


async def test_diagnostics_rechecks_maintenance_after_executable_probes(monkeypatch):
    running = False

    async def version(*args):
        nonlocal running
        running = True
        return "missing"

    imports = Mock(side_effect=AssertionError("Repair started during binary probes"))
    monkeypatch.setattr(backend, "executable_version", version)
    monkeypatch.setattr(backend, "voice_import_status", imports)
    state = await backend.diagnostics(voice_guard=lambda: "Repair running" if running else None)
    assert not state["ready"] and state["voice_error"] == "Repair running"
    imports.assert_not_called()


async def test_failed_imports_cannot_report_ready_even_with_stale_discord_flags(monkeypatch):
    monkeypatch.setattr(backend, "executable_version", AsyncMock(return_value="v22.1.0"))
    monkeypatch.setattr(
        backend, "voice_import_status", lambda: {"PyNaCl": "Import failed", "davey": "Ready"}
    )
    monkeypatch.setattr(backend, "require_voice", lambda: None)
    state = await backend.diagnostics()
    assert not state["ready"] and "imports are unavailable" in state["voice_error"]


async def test_failed_install_blocks_playback_before_reply_is_sent(
    red_command_runtime, monkeypatch
):
    bot, cog, member, invoke = red_command_runtime
    bot.owner_ids.add(member.id)
    monkeypatch.setattr(
        dependencies, "voice_import_status", lambda: {"PyNaCl": "Missing", "davey": "Ready"}
    )
    monkeypatch.setattr(
        cog._voice_repair, "_run", AsyncMock(return_value=(1, "No matching distribution"))
    )
    reply = cog._reply

    async def send(ctx, content=None, **kwargs):
        if kwargs.get("tone") == "error":
            assert bot._audioplus_voice_restart_required is True
            with pytest.raises(MediaError, match="Restart Red"):
                cog._require_voice()
        return await reply(ctx, content, **kwargs)

    monkeypatch.setattr(cog, "_reply", send)
    ctx = await invoke("!audiorepair")
    assert "binary wheel" in ctx.send.await_args.kwargs["embed"].description
    assert "!restart" in ctx.send.await_args.kwargs["embed"].description
