"""Exercise real voice libraries made available after Discord.py startup."""

import asyncio
import sys
import textwrap

import pytest


async def run_isolated(script):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        textwrap.dedent(script),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 15)
        assert process.returncode == 0, output.decode(errors="replace")
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


async def test_downloader_voice_libraries_initialize_without_replacing_discord_classes():
    await run_isolated(
        """
        import asyncio
        import importlib.abc
        import sys
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock

        class StartupImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.')[0] in {'nacl', 'davey'}:
                    raise ModuleNotFoundError('Downloader folder not yet visible')

        startup = StartupImports()
        sys.meta_path.insert(0, startup)
        import discord
        assert not discord.voice_client.has_nacl
        assert not discord.voice_client.has_dave
        client_class = discord.Client
        voice_class = discord.VoiceClient
        websocket_class = discord.gateway.DiscordVoiceWebSocket

        # Red exposes Downloader's dependencies after importing Discord.py.
        sys.meta_path.remove(startup)
        from audioplus.backend import require_voice
        require_voice()
        require_voice()
        assert discord.Client is client_class
        assert discord.VoiceClient is voice_class
        assert discord.gateway.DiscordVoiceWebSocket is websocket_class

        async def check():
            client = SimpleNamespace(_connection=SimpleNamespace(
                loop=asyncio.get_running_loop(), user=SimpleNamespace(id=123)
            ))
            channel = SimpleNamespace(id=456, guild=SimpleNamespace(id=789))
            voice = discord.VoiceClient(client, channel)
            state = voice._connection
            try:
                # Construct and use the actual NaCl cipher, not just a readiness flag.
                state.secret_key = list(range(32))
                header = bytes(12)
                payload = b'local voice packet'
                encrypted = voice._encrypt_aead_xchacha20_poly1305_rtpsize(header, payload)
                assert encrypted.startswith(header)
                assert len(encrypted) > len(header) + len(payload)

                # Initialize a real DAVE session and generate a real MLS key package.
                state.ws = SimpleNamespace(send_binary=AsyncMock())
                state.dave_protocol_version = state.max_dave_protocol_version
                assert state.dave_protocol_version > 0
                await state.reinit_dave_session()
                assert state.dave_session is not None
                assert state.ws.send_binary.await_args.args[1]

                # Gateway processing also needs its originally missing davey import.
                state.dave_session = SimpleNamespace(process_proposals=Mock(return_value=None))
                websocket = object.__new__(discord.gateway.DiscordVoiceWebSocket)
                websocket._connection = state
                message = bytes([0, 1, websocket.MLS_PROPOSALS, 0]) + b'proposal'
                await websocket.received_binary_message(message)
                state.dave_session.process_proposals.assert_called_once()
            finally:
                state._socket_reader.stop()
                state._socket_reader.join(timeout=2)
                assert not state._socket_reader.is_alive()

        asyncio.run(check())
        """
    )


@pytest.mark.parametrize("package,label", [("nacl", "PyNaCl"), ("davey", "davey")])
async def test_unimportable_voice_library_still_blocks_playback(package, label):
    await run_isolated(
        f"""
        import importlib.abc
        import sys

        class UnavailableLibrary(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.')[0] == {package!r}:
                    raise ImportError('Missing native binding')

        sys.meta_path.insert(0, UnavailableLibrary())
        from audioplus.backend import require_voice
        from audioplus.resolver import MediaError
        try:
            require_voice()
        except MediaError as exc:
            assert {label!r} in str(exc)
            assert 'could not be imported' in str(exc)
            assert 'Missing native binding' not in str(exc)
        else:
            raise AssertionError('Playback was enabled without the native dependency')
        """
    )
