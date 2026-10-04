"""Verify actual network dial boundaries, redirects, manifests and child ownership."""

import asyncio
import os
import socket
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from audioplus import media_network
from audioplus.media_network import MediaRelay, NetworkPolicyError, PublicConnector, PublicResolver
from audioplus.resolver import MediaError, MediaResolver, Stream, normalize_query
from audioplus.source import DecoderError, NativeSource


def allow_test_loopback(monkeypatch):
    """Opt in the known loopback media fixtures only, never a production setting."""
    for name in ("audioplus", "introplus"):
        module = __import__(f"{name}.media_network", fromlist=["public_address"])
        original = module.public_address

        def permit(value, original=original):
            if str(value) in {"127.0.0.1", "::1"}:
                return str(value)
            return original(value)

        monkeypatch.setattr(module, "public_address", permit)


def allow_test_worker_loopback(monkeypatch, resolver):
    """Run the real worker/yt-dlp with only fixture socket destinations allowed."""
    command = resolver._command

    def fixture_command(query, *, flat):
        args = command(query, flat=flat)
        script = args[1]
        program = (
            "import sys,runpy; "
            f"sys.path.insert(0,{str(Path(script).parent)!r}); "
            "import media_network as m; original=m.public_address; "
            "m.public_address=lambda host: str(host) if str(host) in {'127.0.0.1','::1'} "
            "else original(host); "
            f"sys.argv={args[1:]!r}; "
            f"runpy.run_path({script!r},run_name='__main__')"
        )
        return [args[0], "-c", program]

    monkeypatch.setattr(resolver, "_command", fixture_command)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.mp3",
        "http://10.10.1.200/a.mp3",
        "http://192.168.1.1/a.mp3",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/a.mp3",
        "http://[::ffff:127.0.0.1]/a.mp3",
        "http://localhost/a.mp3",
        "http://service.internal/a.mp3",
        "http://[64:ff9b::a00:1]/a.mp3",
        "http://168.63.129.16/a.mp3",
        "file:///etc/passwd",
        "http://user:pass@example.org/a.mp3",
    ],
)
def test_member_media_queries_reject_private_literals_names_and_credentials(url):
    with pytest.raises(MediaError):
        normalize_query(url)


@pytest.mark.parametrize("address", ["0.0.0.0", "224.0.0.1", "100.64.0.1", "ff02::1", "127.1"])
def test_public_dial_rejects_reserved_multicast_and_noncanonical_addresses(address):
    with pytest.raises(NetworkPolicyError):
        media_network.public_address(address)


async def test_public_dns_rejects_all_mixed_private_answers_before_any_connection(monkeypatch):
    loop = asyncio.get_running_loop()
    results = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.10.1.200", 443)),
    ]

    async def resolution(*args, **kwargs):
        return results

    monkeypatch.setattr(loop, "getaddrinfo", resolution)
    with pytest.raises(NetworkPolicyError):
        await PublicResolver().resolve("public.example", 443)


async def test_resolver_returns_literal_pinned_addresses_and_connector_never_redoes_dns(
    monkeypatch,
):
    loop = asyncio.get_running_loop()
    calls = []

    async def resolution(host, port, **kwargs):
        calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(loop, "getaddrinfo", resolution)
    addresses = await PublicResolver().resolve("public.example", 443)
    assert addresses[0]["host"] == "8.8.8.8"
    assert addresses[0]["flags"] & socket.AI_NUMERICHOST
    connector = PublicConnector(resolver=PublicResolver())
    try:
        with pytest.raises(NetworkPolicyError):
            await connector._wrap_create_connection(None, "public.example", 443)
        with pytest.raises(NetworkPolicyError):
            await connector._wrap_create_connection(None, "127.1", 443)
    finally:
        await connector.close()
    assert calls == [("public.example", 443)]


@pytest.fixture
async def attack_server():
    app = web.Application()
    requests = []

    async def redirect(request):
        requests.append(request.path)
        raise web.HTTPFound("http://169.254.169.254/never-request-this")

    async def playlist(request):
        requests.append(request.path)
        return web.Response(
            text="#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n"
            f"http://{request.headers['Host']}/private-segment\n#EXT-X-ENDLIST\n",
            content_type="application/vnd.apple.mpegurl",
        )

    async def private_segment(request):
        requests.append(request.path)
        return web.Response(body=b"never fetched")

    app.router.add_get("/redirect", redirect)
    app.router.add_get("/playlist", playlist)
    app.router.add_get("/private-segment", private_segment)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    root = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        yield root, requests
    finally:
        await runner.cleanup()


async def test_relay_blocks_redirect_private_hop_and_capability_guessing(
    attack_server, monkeypatch
):
    root, requests = attack_server
    allow_test_loopback(monkeypatch)
    relay = MediaRelay(root + "/redirect")
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.get(relay.url.rsplit("/", 1)[0] + "/wrong-token") as response:
                assert response.status == 404
            async with session.get(relay.url) as response:
                assert response.status == 403
                assert "169.254" not in await response.text()
        assert requests == ["/redirect"]
    finally:
        await asyncio.to_thread(relay.close)
    assert not relay._thread.is_alive()


async def test_decoder_does_not_follow_remote_playlist_segments(attack_server, monkeypatch):
    root, requests = attack_server
    allow_test_loopback(monkeypatch)
    source = NativeSource(Stream(root + "/playlist"), volume=100)
    try:
        with pytest.raises(DecoderError, match="unsupported protocol"):
            await asyncio.to_thread(source.read)
        assert requests and "/private-segment" not in requests
        assert source._audio._process.args[0] != root
        assert source._relay.url != root + "/playlist"
    finally:
        await asyncio.to_thread(source.cleanup)
    assert not source._relay._thread.is_alive()


async def test_guarded_child_rejects_socket_connect_connect_ex_and_numeric_aliases(tmp_path):
    worker = Path("audioplus/media_network.py").resolve()
    program = f"""import runpy,socket,json
m=runpy.run_path({str(worker)!r})
m['guard_python_sockets']()
result=[]
for host in ('127.0.0.1','127.1','0x7f000001','2130706433','10.10.1.200'):
    with socket.socket() as s:
        try: s.connect((host,1))
        except m['NetworkPolicyError']: result.append(True)
        else: result.append(False)
    with socket.socket() as s: result.append(s.connect_ex((host,1))==13)
print(json.dumps(result))
"""
    env = {key: value for key, value in os.environ.items() if key not in media_network.PROXY_NAMES}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
    assert process.returncode == 0, stderr.decode()
    import json

    assert all(json.loads(stdout))


def test_proxy_configuration_is_explicitly_refused(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://trusted-admin-private-proxy:3128")
    with pytest.raises(MediaError, match="proxy environment"):
        NativeSource(Stream("https://example.org/audio.mp3"), volume=100)


async def test_pending_lookup_admission_is_bounded_and_cancelled_waiters_release(monkeypatch):
    resolver = MediaResolver()
    resolver._slots = asyncio.Semaphore(0)
    tasks = [asyncio.create_task(resolver._extract("query", flat=True)) for _ in range(16)]
    await asyncio.sleep(0)
    assert resolver._pending == 16
    with pytest.raises(MediaError, match="pending"):
        await resolver._extract("query", flat=True)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert resolver._pending == 0
    await resolver.close()


def test_guarded_helpers_remain_independently_installable_and_identical():
    for name in ("media_network.py", "media_worker.py", "source.py"):
        assert Path("audioplus", name).read_bytes() == Path("introplus", name).read_bytes()
    command = MediaResolver()._command("video", flat=False)
    assert Path(command[1]).name == "media_worker.py"
    assert "--proxy" in command and command[command.index("--proxy") + 1] == ""
    assert "m3u8" not in command[command.index("--format") + 1]


async def test_worker_checks_the_redirect_target_at_its_actual_socket_dial(
    attack_server, monkeypatch
):
    root, requests = attack_server
    allow_test_loopback(monkeypatch)
    resolver = MediaResolver(timeout=10)
    allow_test_worker_loopback(monkeypatch, resolver)
    try:
        with pytest.raises(MediaError):
            await resolver.search(root + "/redirect")
        assert requests == ["/redirect"]
    finally:
        await resolver.close()


async def test_repeated_cancellation_still_closes_a_relay_started_in_a_worker(monkeypatch):
    import threading

    began, finish = threading.Event(), threading.Event()
    closed = []

    class StartingRelay:
        def __init__(self, *args):
            began.set()
            assert finish.wait(5)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(media_network, "MediaRelay", StartingRelay)
    task = asyncio.create_task(media_network.create_relay("https://example.org/audio"))
    assert await asyncio.to_thread(began.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


@pytest.mark.skipif(os.name != "posix", reason="Process groups are a POSIX transport boundary")
async def test_lookup_timeout_terminates_its_javascript_solver_process_group(tmp_path, monkeypatch):
    child_pid = tmp_path / "solver.pid"
    program = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    resolver = MediaResolver(timeout=1)
    monkeypatch.setattr(
        resolver, "_command", lambda *args, **kwargs: [sys.executable, "-c", program]
    )
    with pytest.raises(MediaError, match="timed out"):
        await resolver._extract("query", flat=True)
    assert child_pid.exists()
    pid = int(child_pid.read_text())
    # A killed child can remain a zombie until this container's PID 1 reaps it.
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        assert stat.read_text().split()[2] == "Z"
    assert not resolver._processes and resolver._pending == 0
    await resolver.close()


def test_decoder_pipe_start_failure_still_closes_its_owned_relay(monkeypatch):
    import audioplus.source as decoder

    relays = []

    class StartedRelay:
        url = "http://127.0.0.1/owned-capability"

        def __init__(self, *args):
            self.closed = False
            relays.append(self)

        def close(self):
            self.closed = True

    def failed_pipe():
        raise OSError("too many open files")

    monkeypatch.setattr(decoder, "MediaRelay", StartedRelay)
    monkeypatch.setattr(decoder, "_DecoderStderr", failed_pipe)
    with pytest.raises(OSError):
        decoder.NativeSource(Stream("https://example.org/audio.mp3"), volume=100)
    assert len(relays) == 1 and relays[0].closed
