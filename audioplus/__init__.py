# repo: audioplus/
# ├─ info.json
# └─ __init__.py

# ---------- info.json ----------
# {
#   "author": ["Code Copilot"],
#   "install_msg": "Installed audioplus. Configure with `[p]audio setnode <host> <port> <password> <secure>`.\nUse `[p]audio pingnode` to check health or `[p]audio connectnode` to force reconnect.\nJoin a VC with `[p]audio join` then play with `[p]audio play <query|url>`. If using Stage, try `[p]audio speak`.",
#   "name": "audioplus",
#   "short": "Single-file Lavalink v4 music cog using Wavelink 3.x.",
#   "description": "Red cog (single-file __init__.py) for Lavalink v4 via Wavelink 3.x. Commands: audio join/leave/play/skip/pause/resume/stop/volume/queue/np/shuffle/pingnode/connectnode/speak; owner: audio setnode/shownode.",
#   "end_user_data_statement": "Stores Lavalink node details (host, port, password, secure) in Red's Config.",
#   "requirements": ["wavelink>=3.4.1,<4.0.0", "aiohttp>=3.8"],
#   "tags": ["music", "lavalink", "wavelink", "audio"],
#   "min_bot_version": "3.5.0",
#   "hidden": false,
#   "disabled": false
# }

# ---------- __init__.py ----------
"""
AudioPlus: Single-file Red cog for Lavalink v4 using Wavelink 3.x.
Commands: [p]audio ...  (join/leave/play/skip/stop/pause/resume/volume/queue/np/shuffle/pingnode/connectnode/speak)
Owner: [p]audio setnode <host> <port> <password> <secure>
"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

import aiohttp
import discord
import wavelink
from wavelink.exceptions import ChannelTimeoutException, InvalidNodeException

from redbot.core import Config, checks, commands
from redbot.core.bot import Red

GUILD_ONLY = commands.guild_only()


@dataclass
class NodeConfig:
    uri: str
    password: str
    resume_timeout: int = 60
    secure: bool = False
    identifier: str = "MAIN"

    @classmethod
    def from_parts(cls, host: str, port: int, password: str, secure: bool) -> "NodeConfig":
        scheme = "https" if secure else "http"
        return cls(uri=f"{scheme}://{host}:{port}", password=password, secure=secure)


class AudioPlus(commands.Cog):
    """Lavalink v4 music using Wavelink. Set node: `[p]audio setnode`."""

    default_global = {
        "host": "127.0.0.1",
        "port": 2333,
        "password": "youshallnotpass",
        "secure": False,
        "resume_timeout": 60,
    }

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0xA10DEFAB, force_registration=True)
        self.config.register_global(**self.default_global)
        self._node_ready: asyncio.Event = asyncio.Event()

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

    async def _get_node_config(self) -> NodeConfig:
        data = await self.config.all()
        return NodeConfig.from_parts(
            host=data["host"],
            port=int(data["port"]),
            password=data["password"],
            secure=bool(data["secure"]),
        )

    def _connected_node(self) -> Optional[wavelink.Node]:
        try:
            n = wavelink.Pool.get_node()
            return n if self._is_node_connected(n) else None
        except Exception:
            return None

    def _new_identifier(self) -> str:
        short = secrets.token_hex(3)
        return f"AP-{int(time.time())}-{short}"

    async def _reconnect_fresh(self, cfg: NodeConfig) -> None:
        # why: use a fresh identifier to avoid collisions
        node = wavelink.Node(
            identifier=self._new_identifier(),
            uri=cfg.uri,
            password=cfg.password,
            resume_timeout=int(await self.config.resume_timeout()),
        )
        print(f"[audioplus] Connecting node {node.identifier} at {node.uri} …")
        await wavelink.Pool.connect(nodes=[node], client=self.bot)

    async def _ensure_nodes(self, node_cfg: NodeConfig) -> None:
        if self._connected_node():
            return
        print("[audioplus] No CONNECTED node — connecting fresh…")
        await self._reconnect_fresh(node_cfg)

    async def _ensure_pool_available(self, wait_timeout: float = 20.0) -> None:
        if not self._connected_node():
            await self._ensure_nodes(await self._get_node_config())
        await self._wait_node_ready(timeout=wait_timeout)

    async def _wait_node_ready(self, timeout: float = 20.0) -> None:
        try:
            if self._node_ready.is_set():
                return
            await asyncio.wait_for(self._node_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def _stage_unsuppress_if_needed(self, guild: discord.Guild, channel: discord.abc.GuildChannel) -> None:
        # why: Stage channels suppress speakers by default; request/unsuppress so audio is audible
        if not isinstance(channel, discord.StageChannel):
            return
        me = guild.me
        vs = getattr(me, "voice", None)
        if not vs:
            return
        if getattr(vs, "suppress", False):
            try:
                await me.edit(suppress=False, reason="AudioPlus unsuppress for playback")
                print("[audioplus] Unsuppressed on Stage channel.")
            except discord.Forbidden:
                # fallback: request to speak; a mod must approve if required
                try:
                    await me.request_to_speak()
                    print("[audioplus] Requested to speak on Stage channel.")
                except Exception:
                    pass
        # also ensure not server-muted
        if getattr(vs, "mute", False):
            print("[audioplus] Warning: I am server-muted; mods must unmute me to hear audio.")

    async def _fetch_or_connect_player(
        self, ctx: commands.Context
    ) -> Tuple[wavelink.Player, discord.VoiceChannel | discord.StageChannel]:
        if not ctx.guild:
            raise commands.NoPrivateMessage()

        voice = getattr(ctx.author, "voice", None)
        if not voice or not voice.channel:
            raise commands.UserInputError("Join a voice channel first.")

        channel = voice.channel

        vc = ctx.voice_client
        if vc and isinstance(vc, wavelink.Player):
            if vc.channel and vc.channel.id != channel.id:
                await vc.move_to(channel)
                await self._stage_unsuppress_if_needed(ctx.guild, channel)
            return vc, channel

        await self._ensure_pool_available(wait_timeout=30.0)

        me = ctx.guild.me
        perms = channel.permissions_for(me)
        if not perms.connect:
            raise commands.CheckFailure("I need the **Connect** permission for that voice channel.")
        if isinstance(channel, discord.StageChannel) and not perms.speak:
            raise commands.CheckFailure("On a Stage channel I also need **Speak** permission.")

        try:
            player: wavelink.Player = await channel.connect(
                cls=wavelink.Player, timeout=60.0, self_deaf=True, self_mute=False
            )
        except ChannelTimeoutException:
            try:
                player = await channel.connect(
                    cls=wavelink.Player, timeout=90.0, self_deaf=True, self_mute=False, reconnect=True
                )
            except ChannelTimeoutException as e:
                raise commands.CommandError(
                    "Voice connect timed out. Check my **Connect/Speak** perms and try again."
                ) from e

        await self._stage_unsuppress_if_needed(ctx.guild, channel)
        return player, channel

    async def _maybe_start_queue(self, player: wavelink.Player) -> None:
        try:
            if not player.playing and not player.paused and len(player.queue) > 0:
                next_track = player.queue.get()
                await player.play(next_track)
        except Exception:
            pass

    @staticmethod
    def _queue_put_many(queue: "wavelink.Queue", items: Iterable) -> int:
        count = 0
        for it in items:
            try:
                queue.put(it)
                count += 1
            except Exception:
                continue
        return count

    @staticmethod
    def _fmt_bytes_mib(value: Optional[int]) -> Optional[int]:
        return None if value is None else max(0, int(value / (1024 * 1024)))

    async def _fetch_lavalink_info(self, node_like, timeout: float = 7.0) -> Optional[dict]:
        url = f"{getattr(node_like, 'uri', '')}/v4/info"
        password = getattr(node_like, "password", None)
        if not url or not password:
            return None
        headers = {"Authorization": str(password)}
        t0 = asyncio.get_running_loop().time()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(url, headers=headers) as resp:
                    data = await resp.json(content_type=None)
        except Exception:
            return None
        data["_rtt_ms"] = int((asyncio.get_running_loop().time() - t0) * 1000)
        return data

    # ---- lifecycle ----

    async def cog_load(self) -> None:
        cfg = await self._get_node_config()
        await self._ensure_nodes(cfg)

    async def cog_unload(self) -> None:
        try:
            await wavelink.Pool.close()
        except Exception:
            pass

    # ---- events / logs ----

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        guilds = len(self.bot.guilds)
        print(f"[audioplus] Node {payload.node.identifier} **CONNECTED** (resumed={payload.resumed}) | guilds={guilds}")
        try:
            self._node_ready.set()
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_wavelink_node_closed(self, *args, **kwargs) -> None:
        node = None
        payload = None
        if args:
            if len(args) == 1:
                payload = args[0]
                node = getattr(payload, "node", None)
            else:
                node = args[0]
                payload = args[1]
        ident = getattr(node, "identifier", "UNKNOWN") if node else "UNKNOWN"
        code = getattr(payload, "code", "unknown")
        reason = getattr(payload, "reason", "")
        print(f"[audioplus] Node {ident} **CLOSED** code={code} reason={reason}")
        try:
            self._node_ready.clear()
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        t = getattr(payload, "track", None)
        title = getattr(t, "title", None) or "Unknown"
        print(f"[audioplus] Track started: {title}")

    # ---- commands ----

    @commands.group(name="audio", invoke_without_command=True)
    @GUILD_ONLY
    async def audio(self, ctx: commands.Context) -> None:
        """AudioPlus (Lavalink v4) commands."""
        await ctx.send_help()

    @audio.command(name="setnode")
    @checks.is_owner()
    async def audio_setnode(
        self, ctx: commands.Context, host: str, port: int, password: str, secure: Optional[bool] = False
    ) -> None:
        await self.config.host.set(host)
        await self.config.port.set(int(port))
        await self.config.password.set(password)
        await self.config.secure.set(bool(secure))
        try:
            self._node_ready.clear()
        except Exception:
            pass
        await self._ensure_nodes(await self._get_node_config())
        await self._wait_node_ready(timeout=10.0)
        await ctx.send("Lavalink node saved & (re)connected.")

    @audio.command(name="shownode")
    @checks.is_owner()
    async def audio_shownode(self, ctx: commands.Context) -> None:
        data = await self.config.all()
        secure = "yes" if data["secure"] else "no"
        await ctx.send(f"Node: {data['host']}:{data['port']} (secure: {secure})")

    @audio.command(name="connectnode")
    @checks.is_owner()
    async def audio_connectnode(self, ctx: commands.Context) -> None:
        """Force (re)connect to the Lavalink node and print status."""
        cfg = await self._get_node_config()
        try:
            self._node_ready.clear()
        except Exception:
            pass
        await self._reconnect_fresh(cfg)
        await self._wait_node_ready(timeout=15.0)
        node = self._connected_node()
        connected = node is not None
        info = await self._fetch_lavalink_info(node or cfg, timeout=7.0)
        ver = self._fmt_version(info.get("version")) if info else "unknown"
        uri = getattr(node, "uri", None) or cfg.uri
        await ctx.send(
            f"Reconnect {'**successful**' if connected else '**failed**'} — Version: `{ver}` | URI: `{uri}`."
        )

    @audio.command(name="pingnode")
    @GUILD_ONLY
    async def audio_pingnode(self, ctx: commands.Context) -> None:
        """Show Lavalink node version/state."""
        node = self._connected_node()
        cfg = await self._get_node_config() if node is None else None
        info = await self._fetch_lavalink_info(node or cfg, timeout=7.0) if (node or cfg) else None
        stats = getattr(node, "stats", None) if node else None

        connected = node is not None
        ident = getattr(node, "identifier", "none") if node else "none"
        uri = getattr(node, "uri", None) or (cfg.uri if cfg else "unknown")

        lines = [f"**Lavalink Node**", f"Connected: {'yes' if connected else 'no'}", f"Identifier: `{ident}`", f"URI: `{uri}`"]

        if info:
            ver = self._fmt_version(info.get("version", "unknown"))
            rtt = info.get("_rtt_ms", None)
            build = info.get("buildTime", None)
            lines.append(f"Version: `{ver}`" + (f" | HTTP RTT: `{rtt} ms`" if rtt is not None else ""))
            if build:
                lines.append(f"Build time: `{build}`")
        else:
            lines.append("Version: `unknown` (info endpoint not reachable)")

        if stats and connected:
            players = getattr(stats, "players", None) or getattr(stats, "player_count", None) or 0
            playing = getattr(stats, "playing_players", None) or getattr(stats, "playing", None) or 0
            uptime = getattr(stats, "uptime", None)
            mem = getattr(stats, "memory", None)
            used_mib = None
            reservable_mib = None
            if isinstance(mem, dict):
                used_mib = self._fmt_bytes_mib(mem.get("used"))
                reservable_mib = self._fmt_bytes_mib(mem.get("reservable") or mem.get("allocated"))
            else:
                used_mib = self._fmt_bytes_mib(getattr(mem, "used", None))
                reservable_mib = self._fmt_bytes_mib(getattr(mem, "reservable", None) or getattr(mem, "allocated", None))
            lines.append(f"Players: `{players}` | Playing: `{playing}`" + (f" | Uptime: `{uptime} ms`" if uptime is not None else ""))
            if used_mib is not None:
                lines.append(f"Memory used: `{used_mib} MiB`" + (f" / `{reservable_mib} MiB` reservable" if reservable_mib is not None else ""))

        await ctx.send("\n".join(lines))

    @audio.command(name="speak")
    @GUILD_ONLY
    async def audio_speak(self, ctx: commands.Context) -> None:
        """Unsuppress/request-to-speak in a Stage channel."""
        vc = getattr(ctx.guild.me, "voice", None)
        if not vc or not vc.channel:
            await ctx.send("I'm not connected to a voice channel.")
            return
        await self._stage_unsuppress_if_needed(ctx.guild, vc.channel)
        await ctx.send("Tried to unsuppress/request-to-speak (if applicable).")

    @audio.command(name="join", aliases=["connect", "summon"])
    @GUILD_ONLY
    async def audio_join(self, ctx: commands.Context) -> None:
        _, channel = await self._fetch_or_connect_player(ctx)
        await ctx.send(f"Connected to **{channel}**.")

    @audio.command(name="leave", aliases=["dc", "disconnect"])
    @GUILD_ONLY
    async def audio_leave(self, ctx: commands.Context) -> None:
        vc = ctx.voice_client
        if vc and isinstance(vc, wavelink.Player):
            await vc.disconnect()
            await ctx.send("Disconnected.")
        else:
            await ctx.send("Not connected.")

    @audio.command(name="play", aliases=["p"])
    @GUILD_ONLY
    async def audio_play(self, ctx: commands.Context, *, query: str) -> None:
        await self._ensure_pool_available(wait_timeout=30.0)
        player, _ = await self._fetch_or_connect_player(ctx)

        if not (query.startswith("http://") or query.startswith("https://")):
            query = f"ytsearch:{query}"

        results = await wavelink.Playable.search(query)
        if not results:
            await ctx.send("No results.")
            return

        try:
            first = results[0]
        except Exception:
            await ctx.send("No playable results.")
            return

        queued = 0
        if hasattr(results, "__len__") and len(results) > 1 and ("list=" in query or "playlist" in query):
            queued = self._queue_put_many(player.queue, results)
        else:
            player.queue.put(first)
            queued = 1

        if not player.playing and not player.paused:
            await self._maybe_start_queue(player)
            title = player.current.title if getattr(player, "current", None) else first.title
            await ctx.send(f"Now playing: `{title}`")
        else:
            await ctx.send(f"Queued {queued} track{'s' if queued != 1 else ''}.")

    @audio.command(name="skip", aliases=["next", "s"])
    @GUILD_ONLY
    async def audio_skip(self, ctx: commands.Context) -> None:
        vc = ctx.voice_client
        if not vc or not isinstance(vc, wavelink.Player):
            await ctx.send("Not connected.")
            return
        prev = getattr(vc, "current", None)
        try:
            await vc.skip(force=True)
        except Exception:
            pass
        if prev and getattr(prev, "title", None):
            await ctx.send(f"Skipped: `{prev.title}`")
        await self._maybe_start_queue(vc)

    @audio.command(name="stop")
    @GUILD_ONLY
    async def audio_stop(self, ctx: commands.Context) -> None:
        vc = ctx.voice_client
        if vc and isinstance(vc, wavelink.Player):
            try:
                if hasattr(vc, "queue"):
                    vc.queue.clear()
                await vc.stop(force=True)
            finally:
                await ctx.send("Stopped and cleared the queue.")
        else:
            await ctx.send("Not connected.")

    @audio.command(name="pause")
    @GUILD_ONLY
    async def audio_pause(self, ctx: commands.Context) -> None:
        vc = ctx.voice_client
        if vc and isinstance(vc, wavelink.Player):
            await vc.pause(True)
            await ctx.send("Paused.")
        else:
            await ctx.send("Not connected.")

    @audio.command(name="resume")
    @GUILD_ONLY
    async def audio_resume(self, ctx: commands.Context) -> None:
        vc = ctx.voice_client
        if vc and isinstance(vc, wavelink.Player):
            await vc.pause(False)
            await ctx.send("Resumed.")
        else:
            await ctx.send("Not connected.")

    @audio.command(name="volume", aliases=["vol"])
    @GUILD_ONLY
    async def audio_volume(self, ctx: commands.Context, value: Optional[int] = None) -> None:
        vc = ctx.voice_client
        if not vc or not isinstance(vc, wavelink.Player):
            await ctx.send("Not connected.")
            return
        if value is None:
            await ctx.send(f"Volume: {vc.volume}%")
            return
        value = max(0, min(1000, int(value)))
        await vc.set_volume(value)
        await ctx.send(f"Volume set to {value}%.")

    @audio.command(name="np", aliases=["nowplaying"])
    @GUILD_ONLY
    async def audio_nowplaying(self, ctx: commands.Context) -> None:
        vc = ctx.voice_client
        if not vc or not isinstance(vc, wavelink.Player) or not getattr(vc, "current", None):
            await ctx.send("Nothing is playing.")
            return
        t = vc.current
        author = getattr(t, "author", None) or "Unknown"
        await ctx.send(f"Now playing: `{t.title}` by `{author}`")

    @audio.command(name="queue", aliases=["q"])
    @GUILD_ONLY
    async def audio_queue(self, ctx: commands.Context) -> None:
        vc = ctx.voice_client
        if not vc or not isinstance(vc, wavelink.Player):
            await ctx.send("Not connected.")
            return
        if len(vc.queue) == 0:
            await ctx.send("Queue is empty.")
            return
        items = list(vc.queue)
        lines = []
        for i, tr in enumerate(items[:10], start=1):
            title = getattr(tr, "title", None) or "Unknown"
            lines.append(f"{i}. {title}")
        extra_count = max(0, len(items) - 10)
        extra = f"\n… and {extra_count} more." if extra_count else ""
        await ctx.send("\n".join(lines) + extra)

    @audio.command(name="shuffle")
    @GUILD_ONLY
    async def audio_shuffle(self, ctx: commands.Context) -> None:
        vc = ctx.voice_client
        if not vc or not isinstance(vc, wavelink.Player):
            await ctx.send("Not connected.")
            return
        try:
            vc.queue.shuffle()
            await ctx.send("Queue shuffled.")
        except Exception:
            await ctx.send("Unable to shuffle right now.")


async def setup(bot: Red) -> None:
    await bot.add_cog(AudioPlus(bot))
