from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections import defaultdict
from functools import partial
from typing import Optional

import aiohttp
import discord
import wavelink
from redbot.core import Config, checks, commands
from redbot.core.bot import Red
from wavelink.exceptions import ChannelTimeoutException

from .constants import NodeConfig
from .presentation import Presentation

log = logging.getLogger(__name__)

GUILD_ONLY = commands.guild_only()


class AudioPlus(commands.Cog):
    """Music playback, queue controls, and Lavalink voice diagnostics."""

    default_global = {
        "host": "127.0.0.1",
        "port": 2333,
        "password": "youshallnotpass",
        "secure": False,
        "resume_timeout": 60,
    }

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self._presentation = Presentation("AudioPlus", "audio")
        self.config: Config = Config.get_conf(self, identifier=0xA10DEFAB, force_registration=True)
        self.config.register_global(**self.default_global)
        self._node = None
        self._http = None
        self._connect_lock = asyncio.Lock()
        self._player_locks = defaultdict(asyncio.Lock)
        self._playback_contexts = {}
        self._last_playback_errors = {}

    # ---- helpers ----

    @staticmethod
    def _is_node_connected(node: wavelink.Node) -> bool:
        status = getattr(node, "status", None)
        name = getattr(status, "name", None) or (str(status) if status is not None else "")
        return str(name).upper() == "CONNECTED"

    @staticmethod
    def _fmt_version(ver: object) -> str:
        if isinstance(ver, dict) and "semver" in ver:
            return str(ver.get("semver"))
        return str(ver)

    async def _get_node_config(self):
        data = await self.config.all()
        cfg = NodeConfig.from_parts(
            host=data["host"],
            port=int(data["port"]),
            password=data["password"],
            secure=bool(data["secure"]),
        )
        cfg.resume_timeout = max(0, int(data["resume_timeout"]))
        return cfg

    def _connected_node(self):
        return (
            self._node if self._node is not None and self._is_node_connected(self._node) else None
        )

    def _new_identifier(self) -> str:
        short = secrets.token_hex(3)
        return f"AP-{int(time.time())}-{short}"

    async def _reconnect_fresh(self, cfg, *, force=True):
        async with self._connect_lock:
            if not force and self._connected_node() is not None:
                return
            if self._node is not None:
                await self._node.close(eject=True)
            self._playback_contexts.clear()
            self._last_playback_errors.clear()
            node = wavelink.Node(
                identifier=self._new_identifier(),
                uri=cfg.uri,
                password=cfg.password,
                session=self._http,
                resume_timeout=cfg.resume_timeout,
                retries=2,
            )
            self._node = node
            try:
                await asyncio.wait_for(
                    wavelink.Pool.connect(nodes=[node], client=self.bot), timeout=20
                )
                if not self._is_node_connected(node):
                    raise commands.CommandError(
                        "Lavalink is unavailable. Check host, port, password, TLS, and node logs."
                    )
            except BaseException as exc:
                try:
                    await node.close(eject=True)
                finally:
                    self._node = None
                if isinstance(
                    exc, (wavelink.WavelinkException, aiohttp.ClientError, asyncio.TimeoutError)
                ):
                    raise commands.CommandError(
                        "Lavalink connection failed. Check audio pingnode and the node's logs."
                    ) from exc
                raise

    async def _ensure_nodes(self, node_cfg):
        await self._reconnect_fresh(node_cfg, force=False)

    async def _ensure_pool_available(self):
        await self._ensure_nodes(await self._get_node_config())
        if self._connected_node() is None:
            raise commands.CommandError("No connected AudioPlus node. Use audio connectnode.")

    async def _stage_unsuppress_if_needed(
        self, guild: discord.Guild, channel: discord.abc.GuildChannel
    ) -> None:
        if not isinstance(channel, discord.StageChannel):
            return
        me = guild.me
        vs = getattr(me, "voice", None)
        if not vs:
            return
        if getattr(vs, "suppress", False):
            try:
                await me.edit(suppress=False, reason="AudioPlus unsuppress for playback")
                log.debug("[audioplus] Unsuppressed on Stage channel.")
            except discord.Forbidden:
                try:
                    await me.request_to_speak()
                    log.debug("[audioplus] Requested to speak on Stage channel.")
                except Exception:
                    pass

    async def _force_undeafen(self, guild, channel):
        try:
            await guild.change_voice_state(channel=channel, self_deaf=False, self_mute=False)
            return True
        except discord.HTTPException:
            log.debug("Could not clear voice flags", exc_info=True)
            return False

    async def _fetch_player_state(self, guild_id):
        node = self._connected_node()
        if node is None or not node.session_id:
            return None
        return await self._request_json(node, f"/v4/sessions/{node.session_id}/players/{guild_id}")

    async def _rebind_voice(self, guild):
        async with self._player_locks[guild.id]:
            old = self._get_player(guild)
            if not isinstance(old, wavelink.Player) or not old.channel:
                return False
            channel, track, queued = old.channel, old.current, list(old.queue)
            position, volume, paused = old.position, old.volume, old.paused
            try:
                await old.disconnect()
                player = await channel.connect(
                    cls=partial(wavelink.Player, nodes=[self._node]),
                    timeout=30,
                    self_deaf=False,
                    self_mute=False,
                )
                player.autoplay = wavelink.AutoPlayMode.partial
                request = self._playback_contexts.get(guild.id)
                if request is not None and request[0] is old:
                    self._playback_contexts[guild.id] = (player, request[1])
                self._queue_put_many(player.queue, queued)
                await player.set_volume(volume)
                if track:
                    await player.play(track, start=position, paused=paused)
                await self._stage_unsuppress_if_needed(guild, channel)
                return True
            except (discord.HTTPException, wavelink.WavelinkException):
                log.warning("Voice rejoin failed for guild %s", guild.id, exc_info=True)
                return False

    # ---- lifecycle ----

    async def cog_load(self):
        # Loading must succeed before an owner can configure a non-default node.
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=7))

    async def cog_unload(self):
        try:
            if self._node is not None:
                await self._node.close(eject=True)
        finally:
            self._node = None
            if self._http is not None:
                await self._http.close()
            self._player_locks.clear()
            self._playback_contexts.clear()
            self._last_playback_errors.clear()

    # ---- events / logs ----

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload):
        if payload.node is self._node:
            log.info("AudioPlus node connected (resumed=%s)", payload.resumed)

    @commands.Cog.listener()
    async def on_wavelink_node_closed(self, node, disconnected):
        if node is self._node:
            log.info("AudioPlus node closed; disconnected players=%s", len(disconnected))

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        if payload.player is None or payload.player.node is not self._node:
            return
        self._last_playback_errors.pop(payload.player.guild.id, None)
        try:
            guild = payload.player.guild
            ch = payload.player.channel
            await self._stage_unsuppress_if_needed(guild, ch)
            vs = getattr(guild.me, "voice", None)
            if vs and vs.self_deaf:
                await self._force_undeafen(guild, ch)
        except (discord.HTTPException, AttributeError):
            log.debug("Track-start voice flags could not be updated", exc_info=True)
        t = getattr(payload, "track", None)
        title = getattr(t, "title", None) or "Unknown"
        log.debug(f"[audioplus] Track started: {title}")

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        if payload.player is None or payload.player.node is not self._node:
            return
        log.debug(f"[audioplus] Track ended: reason={getattr(payload, 'reason', 'unknown')}")

    @commands.Cog.listener()
    async def on_wavelink_track_exception(
        self, payload: wavelink.TrackExceptionEventPayload
    ) -> None:
        if payload.player is None or payload.player.node is not self._node:
            return
        ex = getattr(payload, "exception", None)
        cause = ex.get("cause") or ex.get("message", "") if isinstance(ex, dict) else str(ex or "")
        await self._report_playback_failure(payload.player, payload.track, cause)

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload) -> None:
        if payload.player is None or payload.player.node is not self._node:
            return
        player = payload.player
        # A delayed event for an old track must not skip a newly started track.
        async with self._player_locks[player.guild.id]:
            if player.current != payload.track:
                return
            try:
                await player.skip(force=True)
            except (wavelink.WavelinkException, aiohttp.ClientError, asyncio.TimeoutError):
                log.warning(
                    "Could not skip a stuck track in guild %s", player.guild.id, exc_info=True
                )
        await self._report_playback_failure(
            player, payload.track, "Playback stalled. A skip was requested to recover the queue."
        )

    @staticmethod
    def _playback_failure_hint(cause, source=""):
        text = str(cause).lower()
        if text.startswith("playback stalled."):
            return str(cause)
        if "no supported audio streams" in text:
            return (
                "Lavalink found the track, but its source returned no playable audio stream. "
                "For YouTube, check the Lavalink youtube-source plugin and its client configuration."
            )
        if source == "youtube" and any(
            word in text for word in ("sign in", "login", "bot", "cipher", "sig function")
        ):
            return (
                "YouTube rejected the stream or its signature could not be decoded. "
                "Check the node's youtube-source plugin, clients, and authentication in Lavalink's logs."
            )
        return "Lavalink could not play this track. Check the node's source configuration and logs."

    async def _report_playback_failure(self, player, track, cause):
        request = self._playback_contexts.get(player.guild.id)
        if request is not None and request[0] is not player:
            return
        title = discord.utils.escape_markdown(str(getattr(track, "title", "Unknown track")))
        hint = self._playback_failure_hint(cause, getattr(track, "source", ""))
        message = f"**{title}**\n{hint}"
        self._last_playback_errors[player.guild.id] = message
        log.warning("Playback failed in guild %s: %s", player.guild.id, cause)
        if request is None:
            return
        ctx = request[1]
        prefix = ctx.clean_prefix
        message += (
            f"\n\nRun `{prefix}audio pingnode` for sources and plugin versions. "
            f"Try `{prefix}audio tone` to test a direct MP3. "
            f"If SoundCloud is enabled, use `{prefix}audio play scsearch:<search terms>`."
        )
        try:
            await self._reply(ctx, message, title="Playback failed", tone="error")
        except discord.HTTPException:
            log.warning("Could not send playback failure to guild %s", player.guild.id)

    # ---- VC / playback helpers ----

    async def _fetch_or_connect_player(self, ctx):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        voice = getattr(ctx.author, "voice", None)
        if voice is None or voice.channel is None:
            raise commands.UserInputError("Join a voice channel first.")
        channel = voice.channel
        await self._ensure_pool_available()
        async with self._player_locks[ctx.guild.id]:
            player = ctx.guild.voice_client
            if player is not None and not isinstance(player, wavelink.Player):
                raise commands.CommandError(
                    "Another voice client is connected. Disconnect it before using AudioPlus."
                )
            if player is None:
                permissions = channel.permissions_for(ctx.guild.me)
                missing = [name for name in ("connect", "speak") if not getattr(permissions, name)]
                if missing:
                    raise commands.BotMissingPermissions(missing)
                try:
                    player = await channel.connect(
                        cls=partial(wavelink.Player, nodes=[self._node]),
                        timeout=30,
                        self_deaf=False,
                        self_mute=False,
                    )
                except ChannelTimeoutException as exc:
                    raise commands.CommandError(
                        "Voice connection timed out. Check permissions and Lavalink connectivity."
                    ) from exc
            elif player.node is not self._node:
                raise commands.CommandError(
                    "Another cog owns this Wavelink player. Disconnect it before using AudioPlus."
                )
            elif player.channel != channel:
                await player.move_to(channel, self_deaf=False, self_mute=False)
            player.autoplay = wavelink.AutoPlayMode.partial
            await self._stage_unsuppress_if_needed(ctx.guild, channel)
            self._playback_contexts[ctx.guild.id] = (player, ctx)
            return player, channel

    async def _maybe_start_queue(self, player):
        async with self._player_locks[player.guild.id]:
            # Wavelink's playing property is also true for a paused, loaded track.
            # An idle player can retain its old pause flag after stop/end.
            if not player.playing and player.queue:
                track = player.queue.get()
                try:
                    await player.play(track, paused=False)
                except BaseException:
                    player.queue.put_at(0, track)
                    raise

    @staticmethod
    def _normalize_query(query):
        query = query.strip()
        if not query:
            raise commands.BadArgument("Provide search terms, a source search, or a track URL.")
        if query.lower().startswith(("http://", "https://")) or re.match(
            r"^[a-z][a-z0-9_-]*search:", query, re.IGNORECASE
        ):
            return query
        return f"ytsearch:{query}"

    async def _load_tracks(self, query):
        try:
            return await wavelink.Pool.fetch_tracks(query, node=self._node)
        except wavelink.LavalinkLoadException as exc:
            source = "youtube" if query.startswith(("ytsearch:", "ytmsearch:")) else ""
            raise commands.CommandError(self._playback_failure_hint(exc.cause, source)) from exc
        except (wavelink.WavelinkException, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise commands.CommandError(
                "Lavalink could not load that query. Check audio pingnode and the node's source logs."
            ) from exc

    async def _start_queued_tracks(self, player):
        try:
            await self._maybe_start_queue(player)
        except (wavelink.WavelinkException, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise commands.CommandError(
                "Lavalink did not accept the playback request. The track is still queued. "
                "Check audio pingnode and the node's logs before retrying."
            ) from exc

    @staticmethod
    def _queue_put_many(queue, items):
        tracks = list(items)
        return queue.put(tracks) if tracks else 0

    @staticmethod
    def _fmt_bytes_mib(value: Optional[int]) -> Optional[int]:
        return None if value is None else max(0, int(value / (1024 * 1024)))

    async def _fetch_lavalink_info(self, node_like, timeout=7.0):
        started = time.monotonic()
        data = await self._request_json(node_like, "/v4/info", timeout)
        if data is not None:
            data["_rtt_ms"] = round((time.monotonic() - started) * 1000)
        return data

    # ---- commands ----

    @commands.group(name="audio", invoke_without_command=True)
    @GUILD_ONLY
    async def audio(self, ctx: commands.Context) -> None:
        """Play music and manage the queue and voice connection.

        Run this command alone for playback controls, voice tools, and diagnostics.
        """
        p = ctx.clean_prefix
        embed = self._presentation.embed("Commands", "Music, playback controls, and voice tools.")
        sections = {
            "Playback": f"`{p}audio play <query>`\n`{p}audio np` · `{p}audio queue`\n`{p}audio skip` · `{p}audio stop`",
            "Controls": f"`{p}audio pause` · `{p}audio resume`\n`{p}audio volume [0..1000]` · `{p}audio shuffle`",
            "Voice": f"`{p}audio join` · `{p}audio leave`\n`{p}audio speak` · `{p}audio undeafen`\n`{p}audio fixvoice` · `{p}audio rejoin`",
            "Diagnostics": f"`{p}audio pingnode` · `{p}audio playerstate`\n`{p}audio debugvc` · `{p}audio tone`",
        }
        if await self.bot.is_owner(ctx.author):
            sections["Node settings"] = (
                f"`{p}audio setnode <host> <port> <password> [secure]`\n`{p}audio shownode` · `{p}audio connectnode`"
            )
        for name, value in sections.items():
            embed.add_field(name=name, value=value, inline=False)
        await self._reply(ctx, embed=embed)

    @audio.command(name="setnode")
    @checks.is_owner()
    async def audio_setnode(
        self,
        ctx: commands.Context,
        host: str,
        port: int,
        password: str,
        secure: Optional[bool] = False,
    ):
        """Save Lavalink node settings and reconnect.

        Bot owner only. Saves the host, port, password, and optional TLS setting, then
        reconnects. A fresh node connection disconnects existing AudioPlus players. Use a
        private server channel because the command includes the password.
        """
        if (
            not host.strip()
            or "://" in host
            or "/" in host
            or not 1 <= port <= 65535
            or not password
        ):
            raise commands.BadArgument(
                "Use a hostname without a scheme/path, a port from 1 to 65535, and a nonempty password."
            )
        async with self.config.all() as config:
            config.update(host=host.strip(), port=port, password=password, secure=bool(secure))
        try:
            await self._reconnect_fresh(await self._get_node_config())
        except (commands.CommandError, asyncio.TimeoutError):
            return await self._reply(
                ctx,
                "Node settings saved, but the connection failed. Check the node and run audio connectnode.",
                tone="warning",
            )
        await self._reply(ctx, "Node settings saved and connected.", tone="success")

    @audio.command(name="shownode")
    @checks.is_owner()
    async def audio_shownode(self, ctx: commands.Context) -> None:
        """Show node settings without revealing the password."""
        data = await self.config.all()
        await self._reply(
            ctx,
            f"Node: {data['host']}:{data['port']} (secure: {'yes' if data['secure'] else 'no'})",
        )

    @audio.command(name="connectnode")
    @checks.is_owner()
    async def audio_connectnode(self, ctx: commands.Context) -> None:
        """Reconnect to Lavalink using the saved node settings."""
        cfg = await self._get_node_config()
        await self._reconnect_fresh(cfg)
        node = self._connected_node()
        connected = node is not None
        info = await self._fetch_lavalink_info(node or cfg, timeout=7.0)
        ver = self._fmt_version(info.get("version")) if info else "unknown"
        uri = getattr(node, "uri", None) or cfg.uri
        await self._reply(
            ctx,
            f"**Connection** · {'Ready' if connected else 'Failed'}\n**Version** · {ver}\n**Node** · `{uri}`",
            title="Node connection",
            tone="success" if connected else "error",
        )

    @audio.command(name="pingnode")
    @GUILD_ONLY
    async def audio_pingnode(self, ctx: commands.Context) -> None:
        """Check Lavalink connectivity, version, and statistics."""
        node = self._connected_node()
        cfg = await self._get_node_config() if node is None else None
        info = await self._fetch_lavalink_info(node or cfg, timeout=7.0) if (node or cfg) else None
        stats = getattr(node, "stats", None) if node else None

        connected = node is not None
        ident = getattr(node, "identifier", "none") if node else "none"
        uri = getattr(node, "uri", None) or (cfg.uri if cfg else "unknown")

        lines = [
            "**Lavalink Node**",
            f"Connected: {'yes' if connected else 'no'}",
            f"Identifier: `{ident}`",
            f"URI: `{uri}`",
        ]

        if info:
            ver = self._fmt_version(info.get("version", "unknown"))
            rtt = info.get("_rtt_ms", None)
            build = info.get("buildTime", None)
            lines.append(
                f"Version: `{ver}`" + (f" | HTTP RTT: `{rtt} ms`" if rtt is not None else "")
            )
            if build:
                lines.append(f"Build time: `{build}`")
            lines.append(f"Lavaplayer: `{info.get('lavaplayer', 'unknown')}`")
            sources = info.get("sourceManagers")
            lines.append(
                "Sources: "
                + (
                    ", ".join(str(source) for source in sources) or "None enabled"
                    if isinstance(sources, list)
                    else "Not reported"
                )
            )
            plugins = info.get("plugins")
            lines.append(
                "Plugins: "
                + (
                    ", ".join(
                        f"{plugin.get('name', 'unknown')} {plugin.get('version', 'unknown')}"
                        for plugin in plugins
                        if isinstance(plugin, dict)
                    )
                    or "None installed"
                    if isinstance(plugins, list)
                    else "Not reported"
                )
            )
        else:
            lines.append("Version: `unknown` (info endpoint not reachable)")

        if stats and connected:
            players = getattr(stats, "players", None) or getattr(stats, "player_count", None) or 0
            playing = (
                getattr(stats, "playing_players", None) or getattr(stats, "playing", None) or 0
            )
            uptime = getattr(stats, "uptime", None)
            lines.append(
                f"Players: `{players}` | Playing: `{playing}`"
                + (f" | Uptime: `{uptime} ms`" if uptime is not None else "")
            )

        if ctx.guild.id in self._last_playback_errors:
            lines.append("\n**Last playback failure**\n" + self._last_playback_errors[ctx.guild.id])
        await self._reply(ctx, "\n".join(lines))

    @audio.command(name="playerstate")
    @GUILD_ONLY
    async def audio_playerstate(self, ctx: commands.Context) -> None:
        """Inspect playback and voice state reported by Lavalink."""
        data = await self._fetch_player_state(ctx.guild.id)
        if not data:
            await self._reply(
                ctx,
                "No REST player state found (not connected, wrong session, or Lavalink denied).",
                tone="warning",
            )
            return

        state = data.get("state") or {}
        voice = data.get("voice") or {}
        filters = data.get("filters") or {}
        paused = bool(data.get("paused", False))
        vol = data.get("volume", "n/a")
        pos = state.get("position", 0)
        connected = bool(state.get("connected", False))
        playing = connected and bool(data.get("track")) and not paused

        tinfo = {}
        track_container = data.get("track")
        if isinstance(track_container, dict):
            tinfo = track_container.get("info") or {}
        title = tinfo.get("title", "unknown")
        author = tinfo.get("author", "unknown")
        length = tinfo.get("length", "unknown")

        lines = [
            "**Lavalink Player State**",
            f"Connected: `{connected}`  Playing: `{playing}`  Paused: `{paused}`  Volume: `{vol}`  Position(ms): `{pos}`",
            f"Track: `{title}` by `{author}` (len={length})",
            f"Voice: endpoint=`{voice.get('endpoint', 'n/a')}` sessionId=`{voice.get('sessionId', 'n/a')}`",
            f"Filters: keys={list(filters.keys()) if isinstance(filters, dict) else 'n/a'}",
        ]
        await self._reply(ctx, "\n".join(lines))

    @audio.command(name="speak")
    @GUILD_ONLY
    async def audio_speak(self, ctx: commands.Context) -> None:
        """Request speaking access in the current Stage channel."""
        vc = getattr(ctx.guild.me, "voice", None)
        if not vc or not vc.channel:
            await self._reply(ctx, "I'm not connected to a voice channel.", tone="warning")
            return
        await self._stage_unsuppress_if_needed(ctx.guild, vc.channel)
        await self._reply(ctx, "Tried to unsuppress/request-to-speak (if applicable).")

    @audio.command(name="undeafen")
    @GUILD_ONLY
    async def audio_undeafen(self, ctx: commands.Context) -> None:
        """Try to clear the bot's self-deafen voice flag."""
        me_vs = getattr(ctx.guild.me, "voice", None)
        if not me_vs or not me_vs.channel:
            await self._reply(ctx, "I'm not connected to voice.", tone="warning")
            return
        ok = await self._force_undeafen(ctx.guild, me_vs.channel)
        await self._reply(
            ctx,
            "Undeafen attempt: " + ("**OK**" if ok else "**failed**"),
            tone="success" if ok else "warning",
        )

    @audio.command(name="fixvoice")
    @GUILD_ONLY
    async def audio_fixvoice(self, ctx: commands.Context) -> None:
        """Try to recover Stage speaking and self-deafen state."""
        me_vs = getattr(ctx.guild.me, "voice", None)
        if not me_vs or not me_vs.channel:
            await self._reply(ctx, "I'm not connected to voice.", tone="warning")
            return
        ch = me_vs.channel
        await self._stage_unsuppress_if_needed(ctx.guild, ch)
        ok = await self._force_undeafen(ctx.guild, ch)
        await self._reply(
            ctx,
            "Voice fix attempted: " + ("**OK**" if ok else "**partial**"),
            tone="success" if ok else "warning",
        )

    @audio.command(name="debugvc")
    @GUILD_ONLY
    async def audio_debugvc(self, ctx: commands.Context) -> None:
        """Show Discord voice flags and playback diagnostics."""
        me_vs = getattr(ctx.guild.me, "voice", None)
        vc = self._get_player(ctx.guild)
        if not me_vs or not me_vs.channel:
            await self._reply(ctx, "I'm not connected to voice.", tone="warning")
            return
        ch = me_vs.channel
        lines = [
            f"Channel: **{ch}** ({ch.__class__.__name__})",
            f"ServerMuted: `{me_vs.mute}`  ServerDeaf: `{me_vs.deaf}`",
            f"SelfMute: `{me_vs.self_mute}`  SelfDeaf: `{me_vs.self_deaf}`  Suppressed(Stage): `{getattr(me_vs, 'suppress', False)}`",
            f"VC connected: `{bool(vc)}`  Player type ok: `{isinstance(vc, wavelink.Player)}`",
        ]
        if vc and isinstance(vc, wavelink.Player):
            lines.append(
                f"Playing: `{bool(vc.playing)}`  Paused: `{bool(vc.paused)}`  Volume: `{getattr(vc, 'volume', 'n/a')}`"
            )
            if vc.current:
                lines.append(f"Track: `{getattr(vc.current, 'title', 'Unknown')}`")
        state = await self._fetch_player_state(ctx.guild.id)
        if state:
            s = state.get("state", {}) or {}
            playing = (
                bool(s.get("connected"))
                and bool(state.get("track"))
                and not state.get("paused", False)
            )
            lines.append(
                f"Lavalink: connected=`{bool(s.get('connected'))}` playing=`{playing}` pos=`{s.get('position', 0)}` vol=`{state.get('volume')}`"
            )
        await self._reply(ctx, "\n".join(lines))

    @audio.command(name="rejoin")
    @GUILD_ONLY
    async def audio_rejoin(self, ctx: commands.Context) -> None:
        """Reconnect to voice and restore playback and the queue.

        Restores the current track, playback position, volume, pause state, and queued tracks
        when reconnection succeeds.
        """
        ok = await self._rebind_voice(ctx.guild)
        await self._reply(
            ctx, "Rejoin: " + ("**OK**" if ok else "**failed**"), tone="success" if ok else "error"
        )

    @audio.command(name="tone")
    @GUILD_ONLY
    async def audio_tone(self, ctx: commands.Context):
        """Queue a direct MP3 track to test audio playback."""
        player, _ = await self._fetch_or_connect_player(ctx)
        tracks = await self._load_tracks(
            "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
        )
        if not tracks:
            return await self._reply(ctx, "The test source is unavailable.", tone="warning")
        player.queue.put(tracks[0])
        await self._start_queued_tracks(player)
        await self._reply(
            ctx,
            "Queued the direct MP3 test track. Use audio playerstate or audio debugvc to inspect playback.",
            tone="success",
        )

    @audio.command(name="join", aliases=["connect", "summon"])
    @GUILD_ONLY
    async def audio_join(self, ctx: commands.Context) -> None:
        """Join or move to your current voice channel."""
        _, channel = await self._fetch_or_connect_player(ctx)
        await self._reply(ctx, f"Connected to **{channel}**.", tone="success")

    @audio.command(name="leave", aliases=["dc", "disconnect"])
    @GUILD_ONLY
    async def audio_leave(self, ctx: commands.Context) -> None:
        """Disconnect the bot from voice."""
        vc = self._get_player(ctx.guild)
        if vc and isinstance(vc, wavelink.Player):
            await vc.disconnect()
            self._playback_contexts.pop(ctx.guild.id, None)
            self._last_playback_errors.pop(ctx.guild.id, None)
            await self._reply(ctx, "Disconnected.", tone="success")
        else:
            await self._reply(ctx, "Not connected.", tone="warning")

    @audio.command(name="play", aliases=["p"])
    @GUILD_ONLY
    async def audio_play(self, ctx: commands.Context, *, query: str):
        """Search for music or queue tracks from a URL.

        Plain search terms use YouTube. Use scsearch: for SoundCloud or another source plugin's
        search prefix to select a source explicitly. Support depends on the Lavalink node.
        """
        query = self._normalize_query(query)
        player, _ = await self._fetch_or_connect_player(ctx)
        results = await self._load_tracks(query)
        if not results:
            return await self._reply(ctx, "No results.", tone="warning")
        tracks = list(results.tracks) if isinstance(results, wavelink.Playlist) else [results[0]]
        self._queue_put_many(player.queue, tracks)
        await self._start_queued_tracks(player)
        await self._reply(ctx, f"Queued {len(tracks)} track(s).", tone="success")

    @audio.command(name="skip", aliases=["next", "s"])
    @GUILD_ONLY
    async def audio_skip(self, ctx):
        """Skip the current track and advance the queue."""
        player = self._get_player(ctx.guild)
        if not isinstance(player, wavelink.Player):
            return await self._reply(ctx, "Not connected.", tone="warning")
        track = player.current
        await player.skip(force=True)
        if track:
            await self._reply(ctx, f"Skipped: {track.title}", tone="success")

    @audio.command(name="stop")
    @GUILD_ONLY
    async def audio_stop(self, ctx: commands.Context) -> None:
        """Stop playback and clear the queue."""
        vc = self._get_player(ctx.guild)
        if vc and isinstance(vc, wavelink.Player):
            try:
                if hasattr(vc, "queue"):
                    vc.queue.clear()
                await vc.stop(force=True)
            finally:
                await self._reply(ctx, "Stopped and cleared the queue.", tone="success")
        else:
            await self._reply(ctx, "Not connected.", tone="warning")

    @audio.command(name="pause")
    @GUILD_ONLY
    async def audio_pause(self, ctx: commands.Context) -> None:
        """Pause the current track."""
        vc = self._get_player(ctx.guild)
        if vc and isinstance(vc, wavelink.Player):
            await vc.pause(True)
            await self._reply(ctx, "Paused.", tone="success")
        else:
            await self._reply(ctx, "Not connected.", tone="warning")

    @audio.command(name="resume")
    @GUILD_ONLY
    async def audio_resume(self, ctx: commands.Context) -> None:
        """Resume paused playback."""
        vc = self._get_player(ctx.guild)
        if vc and isinstance(vc, wavelink.Player):
            await vc.pause(False)
            await self._reply(ctx, "Resumed.", tone="success")
        else:
            await self._reply(ctx, "Not connected.", tone="warning")

    @audio.command(name="volume", aliases=["vol"])
    @GUILD_ONLY
    async def audio_volume(self, ctx: commands.Context, value: Optional[int] = None) -> None:
        """Show or set the playback volume.

        Omit the value to show the current volume. Values are clamped to 0 through 1000.
        """
        vc = self._get_player(ctx.guild)
        if not vc or not isinstance(vc, wavelink.Player):
            await self._reply(ctx, "Not connected.", tone="warning")
            return
        if value is None:
            await self._reply(ctx, f"Volume: {vc.volume}%")
            return
        value = max(0, min(1000, int(value)))
        await vc.set_volume(value)
        await self._reply(ctx, f"Volume set to {value}%.", tone="success")

    @audio.command(name="np", aliases=["nowplaying"])
    @GUILD_ONLY
    async def audio_nowplaying(self, ctx: commands.Context) -> None:
        """Show the current track and playback progress."""
        vc = self._get_player(ctx.guild)
        if not vc or not isinstance(vc, wavelink.Player) or not getattr(vc, "current", None):
            await self._reply(ctx, "Nothing is playing.", tone="warning")
            return
        t = vc.current
        author = getattr(t, "author", None) or "Unknown"
        e = self._presentation.embed(
            "Now playing",
            f"**{discord.utils.escape_markdown(t.title)}**\n{discord.utils.escape_markdown(author)}",
        )
        length = getattr(t, "length", 0)
        position = getattr(vc, "position", 0)
        if isinstance(length, (int, float)) and length > 0:

            def duration(milliseconds):
                seconds = max(0, int(milliseconds // 1000))
                minutes, seconds = divmod(seconds, 60)
                hours, minutes = divmod(minutes, 60)
                return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"

            e.add_field(name="Progress", value=f"{duration(position)} / {duration(length)}")
        e.add_field(name="Volume", value=f"{vc.volume}%")
        e.add_field(name="Playback", value="Paused" if vc.paused else "Playing")
        await self._reply(ctx, embed=e)

    @audio.command(name="queue", aliases=["q"])
    @GUILD_ONLY
    async def audio_queue(self, ctx: commands.Context) -> None:
        """Show the next ten queued tracks."""
        vc = self._get_player(ctx.guild)
        if not vc or not isinstance(vc, wavelink.Player):
            await self._reply(ctx, "Not connected.", tone="warning")
            return
        if len(vc.queue) == 0:
            await self._reply(ctx, "Queue is empty.", tone="warning")
            return
        items = list(vc.queue)
        lines = []
        for i, tr in enumerate(items[:10], start=1):
            title = getattr(tr, "title", None) or "Unknown"
            lines.append(f"{i}. {title}")
        extra_count = max(0, len(items) - 10)
        extra = f"\n… and {extra_count} more." if extra_count else ""
        await self._reply(ctx, "\n".join(lines) + extra)

    @audio.command(name="shuffle")
    @GUILD_ONLY
    async def audio_shuffle(self, ctx: commands.Context) -> None:
        """Shuffle the queued tracks."""
        vc = self._get_player(ctx.guild)
        if not vc or not isinstance(vc, wavelink.Player):
            await self._reply(ctx, "Not connected.", tone="warning")
            return
        try:
            vc.queue.shuffle()
            await self._reply(ctx, "Queue shuffled.", tone="success")
        except Exception:
            await self._reply(ctx, "Unable to shuffle right now.", tone="warning")

    async def _request_json(self, node, path, timeout=7.0):
        if self._http is None or self._http.closed:
            return None
        try:
            async with self._http.get(
                f"{node.uri}{path}",
                headers={"Authorization": str(node.password)},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)
                return data if isinstance(data, dict) else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.debug("Lavalink diagnostic request failed", exc_info=True)
            return None

    async def red_delete_data_for_user(self, *, requester, user_id):
        return

    async def red_get_data_for_user(self, *, user_id):
        return {}

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        self._player_locks.pop(guild.id, None)
        self._playback_contexts.pop(guild.id, None)
        self._last_playback_errors.pop(guild.id, None)

    def _get_player(self, guild):
        player = guild.voice_client
        return (
            player
            if self._node is not None
            and isinstance(player, wavelink.Player)
            and player.node is self._node
            else None
        )
