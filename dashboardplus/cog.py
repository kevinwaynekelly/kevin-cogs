"""Authenticated local dashboard hosted by Red's own asyncio process."""

import asyncio
import hmac
import io
import ipaddress
import json
import logging
import math
from pathlib import Path
from urllib.parse import urlsplit

import discord
from aiohttp import web
from redbot.core import Config, commands

from .auth import Auth, digest, hostname, listener
from .command_support import check_command, finish_configuration_audit, prepare_hybrid
from .constants import COOKIE, DEFAULTS, SESSION_SECONDS
from .context import execute, make_context
from .presentation import Presentation
from .schema import EDITORS, MUSIC, SETTINGS, music_arguments, track_card

log = logging.getLogger("red.kevin.dashboardplus")
SECURITY = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' https: http: data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; object-src 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def identifier(value):
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 20
    ):
        raise web.HTTPBadRequest(reason="Use a valid server/channel ID.")
    return int(value)


class DashboardPlus(commands.Cog):
    """Host a private local web dashboard for the bot owner."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=702035014, force_registration=True)
        self.config.register_global(settings=DEFAULTS)
        self._presentation = Presentation("DashboardPlus", "dashboard")
        self._auth = Auth()
        self._runner = None
        self._startup = None
        self._requests = set()
        self._lifecycle = asyncio.Lock()
        self._actions = asyncio.Semaphore(4)
        self._closing = False
        self._listener = dict(DEFAULTS)
        self._error = ""
        assets = Path(__file__).parent / "web"
        self._assets = {
            "index.html": (assets / "index.html").read_bytes(),
            "app.js": (assets / "app.js").read_bytes(),
            "style.css": (assets / "style.css").read_bytes(),
        }

    async def cog_load(self):
        self._listener = await self.config.settings()
        self._startup = asyncio.create_task(self._restore())

    async def _restore(self):
        await self.bot.wait_until_red_ready()
        async with self._lifecycle:
            if not self._closing and (await self.config.settings())["enabled"]:
                try:
                    await self._start_server()
                except (OSError, ValueError) as error:
                    self._error = type(error).__name__
                    log.warning("Dashboard listener could not start (%s)", self._error)

    async def cog_unload(self):
        self._closing = True
        if self._startup:
            self._startup.cancel()
            await asyncio.gather(self._startup, return_exceptions=True)
        async with self._lifecycle:
            await self._stop_server()

    async def cog_before_invoke(self, ctx):
        if ctx.command is self.dashboard_login and ctx.interaction is not None:
            await check_command(ctx, ctx.command)
            if not ctx.interaction.response.is_done():
                await ctx.defer(ephemeral=True)
        await prepare_hybrid(ctx)

    async def cog_after_invoke(self, ctx):
        finish_configuration_audit(ctx)

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    def _url(self):
        bind = self._listener["bind"]
        host = (
            "127.0.0.1"
            if bind == "0.0.0.0"
            else "[::1]"
            if bind == "::"
            else f"[{bind}]"
            if ":" in bind
            else bind
        )
        return f"http://{host}:{self._listener['port']}"

    async def _start_server(self):
        if self._runner is not None:
            return
        policy = await self.config.settings()
        listener(policy["bind"], policy["port"])
        for host in policy["hosts"]:
            hostname(host)
        runner = web.AppRunner(self.application(), access_log=None, shutdown_timeout=5)
        await runner.setup()
        try:
            await web.TCPSite(runner, policy["bind"], policy["port"]).start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner, self._listener, self._error = runner, policy, ""

    async def _stop_server(self):
        self._auth.clear()
        for task in list(self._requests):
            task.cancel()
        if self._requests:
            await asyncio.gather(*list(self._requests), return_exceptions=True)
        runner, self._runner = self._runner, None
        if runner:
            await runner.cleanup()

    def _host_allowed(self, host):
        try:
            name = urlsplit("//" + host).hostname
            if not name:
                return False
            if name == "localhost" or name in self._listener["hosts"]:
                return True
            address = ipaddress.ip_address(name)
            bound = ipaddress.ip_address(self._listener["bind"])
            return (
                bound.is_unspecified
                or address == bound
                or (bound.is_loopback and address.is_loopback)
            )
        except ValueError:
            return False

    @web.middleware
    async def _boundary(self, request, handler):
        task = asyncio.current_task()
        self._requests.add(task)
        try:
            if self._closing:
                raise web.HTTPServiceUnavailable(reason="Dashboard is stopping.")
            if not self._host_allowed(request.host):
                raise web.HTTPForbidden(reason="Hostname is not enabled for this dashboard.")
            if request.method not in {"GET", "HEAD"}:
                try:
                    origin = urlsplit(request.headers.get("Origin", ""))
                    same_origin = (
                        origin.scheme in {"http", "https"}
                        and origin.netloc.lower() == request.host.lower()
                        and not origin.username
                        and not origin.password
                    )
                except ValueError:
                    same_origin = False
                if not same_origin:
                    raise web.HTTPForbidden(reason="Open the dashboard and use its own controls.")
            if request.path.startswith("/api/") and request.path != "/api/login":
                cookie = request.cookies.get(COOKIE, "")
                session = self._auth.get(cookie)
                if session is None or not await self.bot.is_owner(
                    discord.Object(id=session.owner_id)
                ):
                    self._auth.sessions.pop(digest(cookie), None)
                    raise web.HTTPUnauthorized(reason="Sign in with a fresh Discord login code.")
                request["session"] = session
                if request.method not in {"GET", "HEAD"} and not hmac.compare_digest(
                    request.headers.get("X-CSRF-Token", "").encode(), session.csrf.encode()
                ):
                    raise web.HTTPForbidden(reason="Refresh the dashboard before trying again.")
                if not self._auth.allow(("session", digest(cookie)), limit=120):
                    raise web.HTTPTooManyRequests(reason="Too many requests. Try again shortly.")
            response = await handler(request)
        except web.HTTPException as error:
            response = web.json_response({"error": error.reason}, status=error.status)
        except commands.UserInputError as error:
            response = web.json_response({"error": str(error)[:1000]}, status=400)
        except (commands.CheckFailure, commands.DisabledCommand):
            response = web.json_response(
                {"error": "This command is disabled or unavailable to you in that channel."},
                status=403,
            )
        except asyncio.TimeoutError:
            response = web.json_response(
                {"error": "The action timed out. Check the player before retrying."}, status=504
            )
        except Exception as error:
            self._error = type(getattr(error, "original", error)).__name__
            log.warning("Dashboard request failed (%s)", self._error)
            response = web.json_response(
                {"error": f"Action failed ({self._error}). Check Red's logs or /audiostatus."},
                status=500,
            )
        finally:
            self._requests.discard(task)
        response.headers.update(SECURITY)
        return response

    def application(self):
        app = web.Application(middlewares=[self._boundary], client_max_size=16 * 1024)
        app.router.add_get("/", self._asset)
        app.router.add_get("/app.js", self._asset)
        app.router.add_get("/style.css", self._asset)
        app.router.add_post("/api/login", self._login)
        app.router.add_get("/api/session", self._session)
        app.router.add_post("/api/logout", self._logout)
        app.router.add_get("/api/overview", self._overview)
        app.router.add_get("/api/guild/{guild}", self._guild)
        app.router.add_post("/api/action", self._action)
        return app

    async def _asset(self, request):
        name = request.path[1:] or "index.html"
        kind = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[
            name
        ]
        return web.Response(body=self._assets[name], content_type=kind, charset="utf-8")

    async def _json(self, request):
        if request.content_type != "application/json":
            raise web.HTTPBadRequest(reason="Send a JSON object.")
        try:
            value = await request.json(
                loads=lambda raw: json.loads(
                    raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError())
                )
            )
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise web.HTTPBadRequest(reason="Send a valid JSON object.") from None
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(reason="Send a JSON object.")
        return value

    async def _login(self, request):
        if not self._auth.allow(("login", request.remote), limit=10):
            raise web.HTTPTooManyRequests(reason="Too many login attempts. Wait one minute.")
        value = await self._json(request)
        code = value.get("code")
        if set(value) != {"code"} or not isinstance(code, str) or len(code) > 100:
            raise web.HTTPUnauthorized(reason="The login code is invalid or expired.")
        owner_id = self._auth.redeem(code.strip())
        if owner_id is None or not await self.bot.is_owner(discord.Object(id=owner_id)):
            raise web.HTTPUnauthorized(reason="The login code is invalid or expired.")
        cookie, session = self._auth.create(owner_id)
        response = web.json_response({"csrf": session.csrf})
        response.set_cookie(
            COOKIE,
            cookie,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=request.secure or urlsplit(request.headers.get("Origin", "")).scheme == "https",
            samesite="Strict",
            path="/",
        )
        return response

    async def _session(self, request):
        session = request["session"]
        user = self.bot.get_user(session.owner_id)
        return web.json_response(
            {"csrf": session.csrf, "owner": str(user) if user else str(session.owner_id)}
        )

    async def _logout(self, request):
        self._auth.sessions.pop(digest(request.cookies.get(COOKIE, "")), None)
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/")
        return response

    async def _overview(self, request):
        latency = self.bot.latency
        return web.json_response(
            {
                "bot": self.bot.user.display_name,
                "avatar": str(self.bot.user.display_avatar.url),
                "ready": self.bot.is_ready(),
                "latency": round(latency * 1000) if math.isfinite(latency) else None,
                "cogs": sorted(self.bot.cogs),
                "servers": [
                    {
                        "id": str(guild.id),
                        "name": guild.name,
                        "members": guild.member_count,
                        "icon": str(guild.icon.url) if guild.icon else "",
                        "accessible": guild.get_member(request["session"].owner_id) is not None,
                    }
                    for guild in sorted(self.bot.guilds, key=lambda item: item.name.lower())
                ],
            }
        )

    def _channels(self, guild, member):
        return [
            channel
            for channel in guild.text_channels
            if channel.permissions_for(member).view_channel
            and channel.permissions_for(member).send_messages
            and channel.permissions_for(guild.me).view_channel
            and channel.permissions_for(guild.me).send_messages
        ]

    async def _context(self, owner_id, guild_id, channel_id):
        guild = self.bot.get_guild(identifier(guild_id))
        if guild is None or guild.me is None:
            raise web.HTTPNotFound(reason="That server is unavailable.")
        member = guild.get_member(owner_id)
        if member is None:
            raise web.HTTPForbidden(reason="Join this server with your owner account to manage it.")
        channels = self._channels(guild, member)
        channel = (
            next((item for item in channels if str(item.id) == channel_id), None)
            if channel_id
            else next(iter(channels), None)
        )
        if channel is None:
            raise web.HTTPForbidden(
                reason="Select a text channel you and the bot can read and send in."
            )
        ctx = make_context(self.bot, member, channel, self.dashboard)
        await check_command(ctx, self.dashboard)
        return ctx, channels

    def _command(self, path, name):
        source = self.bot.get_cog(name)
        command = self.bot.get_command(path)
        if (
            source is None
            or getattr(source, "_closing", False)
            or command is None
            or command.cog is not source
        ):
            raise web.HTTPNotFound(reason=f"Load {name} to use this feature.")
        return command

    async def _editors(self, ctx):
        result = []
        for spec in EDITORS:
            try:
                command = self._command(spec.path, spec.cog)
                await check_command(ctx, command)
                if spec.off:
                    await check_command(ctx, self._command(spec.off, spec.cog))
            except (web.HTTPException, commands.CommandError):
                continue
            source = command.cog
            group = source.config if spec.global_scope else source.config.guild(ctx.guild)
            entry = group
            for key in spec.key.split("."):
                entry = entry.get_attr(key)
            value = await entry()
            if spec.kind == "limits":
                value = {key: value[key] for key in ("max_seconds", "per_member")}
            elif spec.kind == "channel":
                value = str(value or 0)
            result.append(
                {
                    "id": spec.id,
                    "cog": spec.cog,
                    "label": spec.label,
                    "kind": spec.kind,
                    "value": value,
                    "min": spec.minimum,
                    "max": spec.maximum,
                    "choices": spec.choices,
                    "global": spec.global_scope,
                }
            )
        return result

    async def _music(self, ctx):
        try:
            await check_command(ctx, self._command("np", "AudioPlus"))
            await check_command(ctx, self._command("audio np", "AudioPlus"))
        except (web.HTTPException, commands.CommandError):
            return {"available": False, "connected": False, "queue": [], "current": None}
        source = self.bot.get_cog("AudioPlus")
        player = source._get_player(ctx.guild)
        state = {"available": True, "connected": False, "queue": [], "current": None}
        if player is None:
            return state
        async with player.lock:
            state.update(
                {
                    "connected": player.voice.is_connected(),
                    "current": track_card(player.current),
                    "position": player.position,
                    "paused": player.paused,
                    "preparing": player.preparing,
                    "volume": player.volume,
                    "repeat": player.repeat,
                    "channel": player.voice.channel.name,
                    "listeners": sum(not member.bot for member in player.voice.channel.members),
                    "queue": [track_card(track) for track in list(player.queue)[:100]],
                }
            )
        return state

    async def _guild(self, request):
        ctx, channels = await self._context(
            request["session"].owner_id,
            request.match_info["guild"],
            request.query.get("channel", ""),
        )
        cogs = []
        for name, cog in sorted(self.bot.cogs.items()):
            cogs.append(
                {"name": name, "enabled": not await self.bot.cog_disabled_in_guild(cog, ctx.guild)}
            )
        theme = getattr(self.bot, "_kevin_cogs_themes", {}).get(ctx.guild.id, {})
        colors = {
            key: f"#{value:06x}"
            for key, value in theme.get("colors", {}).items()
            if type(value) is int and 0 <= value <= 0xFFFFFF
        }
        return web.json_response(
            {
                "id": str(ctx.guild.id),
                "name": ctx.guild.name,
                "channel": str(ctx.channel.id),
                "channels": [{"id": str(channel.id), "name": channel.name} for channel in channels],
                "cogs": cogs,
                "settings": await self._editors(ctx),
                "music": await self._music(ctx),
                "colors": colors,
            }
        )

    async def _action(self, request):
        value = await self._json(request)
        if set(value) - {"guild", "channel", "kind", "action", "value"} or not {
            "guild",
            "channel",
            "kind",
            "action",
        } <= set(value):
            raise web.HTTPBadRequest(reason="Choose one dashboard action.")
        ctx, _ = await self._context(request["session"].owner_id, value["guild"], value["channel"])
        action, incoming = value["action"], value.get("value")
        if not isinstance(action, str):
            raise web.HTTPBadRequest(reason="Choose a valid action.")
        if value["kind"] == "music" and action in MUSIC:
            command = self._command(MUSIC[action][0], "AudioPlus")
            args, rest = music_arguments(action, incoming), action == "play"
        elif value["kind"] == "setting" and action in SETTINGS:
            spec = SETTINGS[action]
            incoming = spec.validate(incoming)
            command = self._command(spec.command(incoming), spec.cog)
            args, rest = spec.arguments(incoming), spec.kind == "text"
        else:
            raise web.HTTPBadRequest(reason="This action is not supported.")
        if self._actions.locked():
            raise web.HTTPTooManyRequests(reason="Several actions are running. Try again shortly.")
        ctx = make_context(self.bot, ctx.author, ctx.channel, command, args, rest=rest)
        async with self._actions:
            if value["kind"] == "music":
                source = self.bot.get_command(f"audio {command.name}")
                if source is not None and source.cog is command.cog:
                    await check_command(ctx, source)
            result = await asyncio.wait_for(execute(ctx, command), timeout=60)
        return web.json_response(result)

    @commands.hybrid_group(name="dashboard", invoke_without_command=True, fallback="status")
    @commands.is_owner()
    async def dashboard(self, ctx):
        """Show the private local dashboard listener and login status."""
        policy = await self.config.settings()
        embed = self._presentation.embed(
            "Local dashboard", "Manage Scarlet from a private browser dashboard."
        )
        embed.add_field(name="Server", value="Running" if self._runner else "Stopped")
        embed.add_field(name="Start after reboot", value="Yes" if policy["enabled"] else "No")
        embed.add_field(name="Listen address", value=f"`{policy['bind']}:{policy['port']}`")
        embed.add_field(name="Open", value=self._url(), inline=False)
        embed.add_field(
            name="Sign in",
            value="Run `/dashboard login` or `dashboard login` for a private five-minute code.",
            inline=False,
        )
        if self._error:
            embed.add_field(name="Latest error", value=self._error, inline=False)
        await self._presentation.send(ctx, embed=embed)

    @dashboard.command(name="help")
    async def dashboard_help(self, ctx):
        """Show local dashboard setup and owner controls."""
        await self._presentation.help(ctx, self.dashboard)

    @dashboard.command(name="start")
    async def dashboard_start(self, ctx):
        """Start the dashboard and enable starting it again after reboot."""
        async with self._lifecycle:
            try:
                await self._start_server()
            except (ValueError, OSError) as error:
                self._error = type(error).__name__
                raise commands.BadArgument(
                    "The listener could not start. Check its bind address/port and Red logs."
                ) from error
            async with self.config.settings() as policy:
                policy["enabled"] = True
        await self._presentation.send(
            ctx,
            f"Dashboard started: {self._url()}\nUse dashboard login to sign in.",
            tone="success",
        )

    @dashboard.command(name="stop")
    async def dashboard_stop(self, ctx):
        """Stop the web server, revoke logins and disable automatic startup."""
        async with self._lifecycle:
            async with self.config.settings() as policy:
                policy["enabled"] = False
            await self._stop_server()
        await self._presentation.send(
            ctx, "Dashboard stopped. All login codes and sessions were revoked.", tone="success"
        )

    @dashboard.command(name="bind")
    async def dashboard_bind(self, ctx, address: str = "127.0.0.1", port: int = 8765):
        """Set a stopped listener's IP and port; use 0.0.0.0 for container access."""
        try:
            address, port = listener(address, port)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from None
        async with self._lifecycle:
            if self._runner:
                raise commands.BadArgument("Stop the dashboard before changing its listen address.")
            async with self.config.settings() as policy:
                policy.update(bind=address, port=port)
                self._listener = dict(policy)
        await self._presentation.send(
            ctx, f"Listen address saved: `{address}:{port}`. Run dashboard start.", tone="success"
        )

    @dashboard.command(name="host")
    async def dashboard_host(self, ctx, name: str):
        """Allow a private DNS/proxy hostname without a scheme, port or path."""
        try:
            name = hostname(name)
        except ValueError as error:
            raise commands.BadArgument(str(error)) from None
        async with self.config.settings() as policy:
            if name not in policy["hosts"]:
                if len(policy["hosts"]) >= 10:
                    raise commands.BadArgument("Allow at most ten hostnames.")
                policy["hosts"].append(name)
            self._listener["hosts"] = list(policy["hosts"])
        await self._presentation.send(ctx, f"Allowed dashboard hostname: `{name}`.", tone="success")

    @dashboard.command(name="unhost")
    async def dashboard_unhost(self, ctx, name: str):
        """Remove a previously allowed private hostname."""
        async with self.config.settings() as policy:
            policy["hosts"] = [item for item in policy["hosts"] if item != name.lower().rstrip(".")]
            self._listener["hosts"] = list(policy["hosts"])
        await self._presentation.send(ctx, "Hostname removed.", tone="success")

    @dashboard.command(name="login")
    async def dashboard_login(self, ctx):
        """Privately issue a single-use owner login code valid for five minutes."""
        if self._runner is None:
            raise commands.BadArgument("Run dashboard start first.")
        code = self._auth.issue(ctx.author.id)
        embed = self._presentation.embed(
            "Private login",
            f"Open {self._url()} and enter this code:\n\n`{code}`\n\nSingle use. Expires in five minutes.",
        )
        if ctx.interaction is not None:
            await ctx.send(embed=embed, ephemeral=True)
        else:
            try:
                await ctx.author.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                self._auth.codes.pop(digest(code), None)
                raise commands.BadArgument(
                    "Allow bot DMs, or use /dashboard login for a private reply."
                ) from None
            await self._presentation.send(
                ctx, "Your dashboard login code was sent by DM.", tone="success"
            )

    @dashboard.command(name="logout")
    async def dashboard_logout(self, ctx):
        """Revoke all of your dashboard sessions and unused login codes."""
        self._auth.revoke(ctx.author.id)
        await self._presentation.send(ctx, "Your dashboard logins were revoked.", tone="success")

    async def red_get_data_for_user(self, *, user_id):
        self._auth.prune()
        sessions = [
            session for session in self._auth.sessions.values() if session.owner_id == user_id
        ]
        count = sum(owner == user_id for owner, _ in self._auth.codes.values())
        if not sessions and not count:
            return {}
        record = {
            "owner_id": user_id,
            "unused_login_codes": count,
            "active_sessions": len(sessions),
        }
        return {"dashboardplus.json": io.BytesIO(json.dumps(record).encode())}

    async def red_delete_data_for_user(self, *, requester, user_id):
        self._auth.revoke(user_id)
