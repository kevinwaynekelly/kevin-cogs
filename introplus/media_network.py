"""Public-only media transport, with DNS answers pinned at the actual socket dial."""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import os
import re
import secrets
import socket
import threading
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractResolver

PROXY_NAMES = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
FORMATS = "aac,ac3,eac3,flac,matroska,webm,mov,mp3,ogg,wav,aiff,ape,amr,au"
REMOTE_OPTIONS = "-http_proxy '' -protocol_whitelist http,tcp,pipe -format_whitelist " + FORMATS


class NetworkPolicyError(ValueError):
    """A fixed, public error without the rejected URL, address, or credentials."""


def require_no_proxy():
    if any(os.environ.get(name) for name in PROXY_NAMES):
        raise NetworkPolicyError(
            "Audio's public-network guard does not support HTTP proxy environment settings. "
            "Remove the media proxy environment settings and restart Red."
        )


def public_address(value):
    try:
        address = ipaddress.ip_address(value)
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped:
            address = mapped
        if (
            not address.is_global
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            or address.is_loopback
            or address.is_link_local
            or str(address) == "168.63.129.16"
            or (
                isinstance(address, ipaddress.IPv6Address)
                and (
                    address.sixtofour is not None
                    or address.teredo is not None
                    or address in ipaddress.ip_network("64:ff9b::/96")
                    or address in ipaddress.ip_network("64:ff9b:1::/48")
                )
            )
        ):
            raise ValueError
    except (ValueError, TypeError) as error:
        raise NetworkPolicyError("Media can only connect to public Internet addresses.") from error
    return str(address)


def public_url(value):
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "%" in parts.hostname
        ):
            raise ValueError
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        hostname = parts.hostname.rstrip(".").lower()
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(
            (".localhost", ".local", ".internal", ".lan", ".home")
        ):
            raise NetworkPolicyError("Media can only connect to public Internet hosts.")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            # Alternate numerical IP forms are checked after resolution and again at dial.
            pass
        else:
            public_address(hostname)
    except NetworkPolicyError:
        raise
    except (ValueError, TypeError) as error:
        raise NetworkPolicyError(
            "Use an HTTP or HTTPS media URL without embedded credentials."
        ) from error
    return value


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, family=family
        )
        resolved = []
        for answer_family, _, proto, _, address in results:
            public_address(address[0])
            resolved.append(
                {
                    "hostname": host,
                    "host": address[0],
                    "port": address[1],
                    "family": answer_family,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                }
            )
        if not resolved:
            raise NetworkPolicyError("The public media host could not be resolved.")
        return resolved

    async def close(self):
        pass


class PublicConnector(aiohttp.TCPConnector):
    async def _wrap_create_connection(self, *args, **kwargs):
        # aiohttp skips the resolver for IP-looking hosts. This check also rejects
        # legacy numeric spellings, and prevents a second DNS lookup at connect.
        if len(args) < 3:
            raise NetworkPolicyError("The media connection could not be safely opened.")
        public_address(args[1])
        return await super()._wrap_create_connection(*args, **kwargs)


def guard_python_sockets():
    """Guard every stdlib HTTP redirect/subrequest at connect, inside the child only."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_resolve = socket.getaddrinfo

    def target(sock, address):
        if sock.family not in {socket.AF_INET, socket.AF_INET6}:
            return address
        host, port = address[:2]
        try:
            public_address(host)
        except NetworkPolicyError:
            answers = original_resolve(host, port, family=sock.family, type=sock.type)
            if not answers:
                raise NetworkPolicyError("The public media host could not be resolved.")
            # Reject mixed public/private answers; connect to the literal we checked.
            for answer in answers:
                public_address(answer[4][0])
            return answers[0][4]
        return address

    def connect(sock, address):
        return original_connect(sock, target(sock, address))

    def connect_ex(sock, address):
        try:
            return original_connect_ex(sock, target(sock, address))
        except NetworkPolicyError:
            return errno.EACCES

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex


class MediaRelay:
    """An owned capability-only loopback relay; FFmpeg never sees the remote URL."""

    def __init__(self, url, headers=None):
        self.source = public_url(url)
        require_no_proxy()
        self.headers = {
            str(key): value
            for key, value in (headers or {}).items()
            if re.fullmatch(r"[A-Za-z0-9-]+", str(key))
            and isinstance(value, str)
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
            and str(key).lower() not in {"host", "connection", "proxy-authorization"}
        }
        self._ready = threading.Event()
        self._closing = threading.Event()
        self._thread = threading.Thread(target=self._run, name="MediaPublicRelay", daemon=True)
        self._loop = None
        self._stopped = None
        self._failure = None
        self.url = None
        self._token = secrets.token_urlsafe(32)
        self._thread.start()
        if not self._ready.wait(5) or self._failure:
            self.close()
            raise NetworkPolicyError("The guarded media relay could not start.")

    def _run(self):
        try:
            asyncio.run(self._serve())
        except Exception:
            self._failure = True
        finally:
            self._ready.set()

    async def _serve(self):
        self._loop = asyncio.get_running_loop()
        self._stopped = asyncio.Event()
        connector = PublicConnector(
            resolver=PublicResolver(), use_dns_cache=False, force_close=True, limit=4
        )
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=20)
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
        ) as self._session:
            app = web.Application(client_max_size=1024)
            app.router.add_route("GET", "/{token}", self._request)
            app.router.add_route("HEAD", "/{token}", self._request)
            runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
            await runner.setup()
            try:
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                port = site._server.sockets[0].getsockname()[1]
                self.url = f"http://127.0.0.1:{port}/{self._token}"
                self._ready.set()
                if not self._closing.is_set():
                    await self._stopped.wait()
            finally:
                await runner.cleanup()

    async def _request(self, request):
        if not secrets.compare_digest(request.match_info["token"], self._token):
            raise web.HTTPNotFound()
        headers = dict(self.headers)
        for name in ("Range", "If-Range"):
            if name in request.headers:
                headers[name] = request.headers[name]
        url = self.source
        try:
            for _ in range(6):
                public_url(url)
                async with self._session.request(
                    request.method, url, headers=headers, allow_redirects=False
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        destination = urljoin(url, response.headers.get("Location", ""))
                        public_url(destination)
                        previous, following = urlsplit(url), urlsplit(destination)
                        if (previous.scheme, previous.hostname, previous.port) != (
                            following.scheme,
                            following.hostname,
                            following.port,
                        ):
                            headers = {
                                key: value
                                for key, value in headers.items()
                                if key.lower() not in {"authorization", "cookie"}
                            }
                        url = destination
                        continue
                    outgoing = web.StreamResponse(
                        status=response.status,
                        headers={
                            name: response.headers[name]
                            for name in (
                                "Content-Type",
                                "Content-Length",
                                "Content-Range",
                                "Accept-Ranges",
                                "Last-Modified",
                                "Content-Encoding",
                            )
                            if name in response.headers
                        },
                    )
                    await outgoing.prepare(request)
                    if request.method != "HEAD":
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            await outgoing.write(chunk)
                    await outgoing.write_eof()
                    return outgoing
            raise NetworkPolicyError("The media source redirected too many times.")
        except NetworkPolicyError:
            raise web.HTTPForbidden(text="The media destination is not public.") from None
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            raise web.HTTPBadGateway(text="The public media source could not be loaded.") from None

    def close(self):
        self._closing.set()
        if self._loop and self._stopped and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stopped.set)
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self.headers.clear()


async def create_relay(url, headers=None):
    """Retain ownership if cancellation arrives while its thread is starting."""
    creating = asyncio.create_task(asyncio.to_thread(MediaRelay, url, headers))
    try:
        return await asyncio.shield(creating)
    except asyncio.CancelledError:
        try:
            relay = await settle_owned(creating)
        except Exception:
            pass
        else:
            await settle_owned(asyncio.create_task(asyncio.to_thread(relay.close)))
        raise


async def settle_owned(task):
    """Finish owned cleanup even if cancellation is requested more than once."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
