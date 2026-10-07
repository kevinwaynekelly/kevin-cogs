"""Bounded signed GitHub ingress and one coalescing, owned update worker."""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web

from .trigger_page import HEADERS, PAGE, trigger_token

log = logging.getLogger("downloaderplus.webhook")
MAX_BODY = 1024 * 1024
MAX_REQUESTS = 8
MAX_DELIVERIES = 256
COALESCE_SECONDS = 3
MIN_INTERVAL = 30
DELIVERY_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
FULL_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def listener(bind, port):
    try:
        ipaddress.ip_address(bind)
    except ValueError as exc:
        raise ValueError("Use a literal bind address, such as 127.0.0.1 or 0.0.0.0.") from exc
    if not 1024 <= port <= 65535:
        raise ValueError("Use a port between 1024 and 65535.")


def repository_name(url):
    """Canonicalize only installed GitHub remotes, without contacting them."""
    if not isinstance(url, str):
        return None
    if url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:")
    else:
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"https", "ssh"} or parsed.hostname != "github.com":
                return None
            if parsed.query or parsed.fragment or parsed.password:
                return None
            path = parsed.path.lstrip("/")
        except ValueError:
            return None
    path = path.removesuffix("/").removesuffix(".git")
    return path.casefold() if FULL_NAME.fullmatch(path) else None


def matches_push(payload, repos):
    """Only an existing repository's tracked branch can trigger updates."""
    if not isinstance(payload, dict) or payload.get("deleted"):
        return False
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return False
    name = repository.get("full_name")
    if not isinstance(name, str) or not FULL_NAME.fullmatch(name):
        return False
    for repo in repos:
        if repository_name(getattr(repo, "url", None)) == name.casefold():
            branch = getattr(repo, "branch", None)
            if isinstance(branch, str) and payload.get("ref") == "refs/heads/" + branch:
                return True
    return False


class GitHubWebhook:
    def __init__(self, config, repos, update, *, coalesce=COALESCE_SECONDS, interval=MIN_INTERVAL):
        self.config, self.repos, self.update = config, repos, update
        self.coalesce, self.interval = coalesce, interval
        self.runner = None
        self.worker = None
        self.closed = True
        self.policy = {}
        self.error = ""
        self._event = asyncio.Event()
        self._requests = set()
        self._accept_lock = asyncio.Lock()
        self._run_token = ""

    async def start(self):
        self.policy = await self.config.webhook()
        if not self.policy["enabled"] or not self.policy["secret"]:
            return
        listener(self.policy["bind"], self.policy["port"])
        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_post("/github", self.receive)
        app.router.add_get("/update", self.page)
        app.router.add_post("/update", self.trigger)
        app.router.add_get("/update/status", self.status)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=2, auto_decompress=False)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.policy["bind"], self.policy["port"]).start()
        except BaseException:
            await runner.cleanup()
            raise
        self.runner, self.closed = runner, False
        self.worker = asyncio.create_task(self._run(), name="DownloaderPlusWebhook")
        if self.policy["pending"] or self.policy["last_result"].get("status") == "running":
            self._event.set()

    async def close(self):
        self.closed = True
        current = asyncio.current_task()
        tasks = [task for task in [self.worker, *self._requests] if task and task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.worker = None
        runner, self.runner = self.runner, None
        if runner:
            await runner.cleanup()

    async def receive(self, request):
        return await self._guard(request, self._receive)

    async def page(self, request):
        return web.Response(text=PAGE, content_type="text/html", headers=HEADERS)

    async def trigger(self, request):
        return await self._guard(request, self._trigger)

    async def status(self, request):
        return await self._guard(request, self._status)

    def _authenticate_trigger(self, request):
        value = request.headers.get("Authorization", "")
        expected = "Bearer " + trigger_token(self.policy["secret"])
        if not re.fullmatch(r"Bearer [0-9a-f]{64}", value) or not hmac.compare_digest(
            expected, value
        ):
            raise web.HTTPForbidden(text="Invalid update token.", headers=HEADERS)

    def _active(self, policy):
        if self.closed or not policy["enabled"] or policy["secret"] != self.policy["secret"]:
            raise web.HTTPServiceUnavailable(headers=HEADERS)

    async def _trigger(self, request):
        self._authenticate_trigger(request)
        if request.query_string or request.can_read_body:
            raise web.HTTPBadRequest(
                text="Send an empty POST with a Bearer token.", headers=HEADERS
            )
        async with self._accept_lock, self.config.webhook() as policy:
            self._active(policy)
            policy["pending"] = True
        self._event.set()
        return web.json_response({"status": "queued"}, status=202, headers=HEADERS)

    async def _status(self, request):
        self._authenticate_trigger(request)
        policy = await self.config.webhook()
        self._active(policy)
        result = policy["last_result"]
        return web.json_response(
            {
                "pending": policy["pending"],
                "result": {key: result[key] for key in ("status", "detail", "at") if key in result},
            },
            headers=HEADERS,
        )

    async def _guard(self, request, handler):
        if self.closed:
            raise web.HTTPServiceUnavailable()
        if len(self._requests) >= MAX_REQUESTS:
            raise web.HTTPServiceUnavailable(text="Webhook ingress is busy.")
        task = asyncio.current_task()
        self._requests.add(task)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as error:
            log.error(
                "Update webhook could not handle an authenticated request.",
                extra={
                    "notification_error": type(error).__name__,
                    "notification_stage": "Webhook receipt",
                },
            )
            raise web.HTTPInternalServerError(text="Could not queue the update.") from None
        finally:
            self._requests.discard(task)

    async def _receive(self, request):
        if request.headers.get("Content-Encoding", "identity").casefold() != "identity":
            raise web.HTTPUnsupportedMediaType(text="Use an uncompressed JSON payload.")
        try:
            body = await asyncio.wait_for(request.read(), timeout=10)
        except asyncio.TimeoutError:
            raise web.HTTPRequestTimeout() from None
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = (
            "sha256=" + hmac.new(self.policy["secret"].encode(), body, hashlib.sha256).hexdigest()
        )
        if not re.fullmatch(r"sha256=[0-9a-f]{64}", signature) or not hmac.compare_digest(
            expected, signature
        ):
            raise web.HTTPForbidden(text="Invalid signature.")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        if not DELIVERY_ID.fullmatch(delivery):
            raise web.HTTPBadRequest(text="A delivery ID is required.")
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            raise web.HTTPBadRequest(text="Invalid JSON.") from None
        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping" and isinstance(payload, dict):
            return web.json_response({"status": "ready"})
        if event != "push" or not matches_push(payload, self.repos()):
            return web.json_response({"status": "ignored"}, status=202)
        digest = hashlib.sha256(body).hexdigest()
        async with self._accept_lock, self.config.webhook() as policy:
            if self.closed or not policy["enabled"] or policy["secret"] != self.policy["secret"]:
                raise web.HTTPServiceUnavailable()
            # GitHub headers are not signed; the body digest also blocks a replay
            # with a changed delivery ID. Only bounded digests, never payloads, persist.
            if any(
                item.get("id") == delivery or item.get("digest") == digest
                for item in policy["deliveries"]
            ):
                return web.json_response({"status": "duplicate"}, status=202)
            policy["deliveries"] = [*policy["deliveries"], {"id": delivery, "digest": digest}][
                -MAX_DELIVERIES:
            ]
            policy["pending"] = True
        self._event.set()
        return web.json_response({"status": "queued"}, status=202)

    async def _result(self, status, detail, *, after_reload=False):
        async with self.config.webhook() as policy:
            if (
                (self.closed and not after_reload)
                or not policy["enabled"]
                or policy["secret"] != self.policy["secret"]
            ):
                return False
            if status != "running" and policy["last_result"].get("token") != self._run_token:
                return False
            policy["last_result"] = {
                "status": status,
                "detail": detail,
                "at": int(time.time()),
                "token": self._run_token,
            }
            if status == "running":
                policy["pending"] = False
        return True

    async def _run(self):
        # Respect the burst cooldown after a process/cog restart as well.
        previous_at = self.policy["last_result"].get("at", 0)
        elapsed = (
            max(0, time.time() - previous_at)
            if isinstance(previous_at, (int, float))
            else self.interval
        )
        last_start = time.monotonic() - min(self.interval, elapsed)
        while not self.closed:
            await self._event.wait()
            await asyncio.sleep(max(self.coalesce, last_start + self.interval - time.monotonic()))
            self._event.clear()
            self._run_token = secrets.token_hex(8)
            if not await self._result("running", "Refreshing all repositories and unpinned cogs."):
                return
            last_start = time.monotonic()
            reloading = False
            try:
                status, detail, reload = await self.update(self.policy)
                if not await self._result("reloading" if reload else status, detail):
                    return
                # Save completion before a native reload unloads this cog. close()
                # leaves its own current worker alive to finish that reload safely.
                if reload is not None:
                    reloading = True
                    await reload()
                    await self._result(status, detail, after_reload=True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error(
                    "Automatic repository/cog update failed.",
                    extra={
                        "notification_error": type(error).__name__,
                        "notification_stage": "Automatic reload"
                        if reloading
                        else "Repository update",
                    },
                )
                await self._result(
                    "failed",
                    "Update failed; check Red logs and the result channel.",
                    after_reload=reloading,
                )
