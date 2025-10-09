# repo: audioplus/
# ├─ info.json
# └─ __init__.py

# ---------- info.json ----------
# {
#   "author": ["Code Copilot"],
#   "install_msg": "Installed audioplus. Configure your Lavalink node with `[p]audio setnode <host> <port> <password> <secure>`.\nUse `[p]audio pingnode` to check health or `[p]audio connectnode` to force reconnect.\nJoin a VC with `[p]audio join` then play with `[p]audio play <query|url>`.",
#   "name": "audioplus",
#   "short": "Single-file Lavalink v4 music cog using Wavelink 3.x.",
#   "description": "Red cog (single-file __init__.py) for Lavalink v4 via Wavelink 3.x. Commands: audio join/leave/play/skip/pause/resume/stop/volume/queue/np/shuffle/pingnode/connectnode; owner: audio setnode/shownode.",
#   "end_user_data_statement": "Stores Lavalink node details (host, port, password, secure) in Red's Config.",
#   "requirements": ["wavelink>=3.4.1,<4.0.0"],
#   "tags": ["music", "lavalink", "wavelink", "audio"],
#   "min_bot_version": "3.5.0",
#   "hidden": false,
#   "disabled": false
# }

# ---------- __init__.py ----------
"""
AudioPlus: Single-file Red cog for Lavalink v4 using Wavelink 3.x.
Commands: [p]audio ...  (join/leave/play/skip/stop/pause/resume/volume/queue/np/shuffle/pingnode/connectnode)
Owner: [p]audio setnode <host> <port> <password> <secure>
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

import aiohttp  # why: REST call to /v4/info for pingnode
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
        # why: gate voice connects until node reports ready at least once
        self._node_ready: asyncio.Event = asyncio.Event()

    # ---- helpers ----

    @staticmethod
    def _is_node_connected(node: wavelink.Node) -> bool:
        status = getattr(node, "status", None)
        name = getattr(status, "name", None) or (str(status) if status is not None else "")
        return str(name).upper() == "CONNECTED"

    async def _get_node_config(self) -> NodeConfig:
        data = await self.config.all()
        return NodeConfig.from_parts(
            host=data["host"],
            port=int(data["port"]),
            password=data["password"],
            secure=bool(data["secure"]),
        )

    async def _ensure_nodes(self, node_cfg: NodeConfig) -> None:
        # ensure there is a CONNECTED node; (re)connect if needed
        existing = None
        try:
            existing = wavelink.Pool.get_node(node_cfg.identifier)
        except Exception:
            existing = None

        if existing:
            if not self._is_node_connected(existing):
                print("[audioplus] Node present but not CONNECTED; reconnecting…")
                await wavelink.Pool.connect(nodes=[existing], client=self.bot)
            return

        node = wavelink.Node(
            identifier=node_cfg.identifier,
            uri=node_cfg.uri,
            password=node_cfg.password,
            resume_timeout=int(await self.config.resume_timeout()),
        )
        print(f"[audioplus] Connecting new node {node.identifier} at {node.uri} …")
        await wavelink.Pool.connect(nodes=[node], client=self.bot)

    async def _ensure_pool_available(self, wait_timeout: float = 20.0) -> None:
        """Ensure at least one CONNECTED node exists; reconnect if needed."""
        needs_connect = False
        try:
            node = wavelink.Pool.get_node()
            if not self._is_node_connected(node):
                needs_connect = True
        except Exception:
            needs_connect = True
        if needs_connect:
            cfg = await self._get_node_config()
            await self._ensure_nodes(cfg)
        await self._wait_node_ready(timeout=wait_timeout)

    async def _wait_node_ready(self, timeout: float = 20.0) -> None:
        try:
            if self._node_ready.is_set():
                return
            await asyncio.wait_for(self._node_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # node may still be usable; continue
            pass

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
            return vc, channel

        # ensure node is available/connected
        await self._ensure_pool_available(wait_timeout=30.0)

        # perms guard
        me = ctx.guild.me
        perms = channel.permissions_for(me)
        if not perms.connect:
            raise commands.CheckFailure("I need the **Connect** permission for that voice channel.")
        if isinstance(channel, discord.StageChannel) and not perms.speak:
            raise commands.CheckFailure("On a Stage channel I also need **Speak** permission.")

        # connect with extended timeout; one retry on timeout
        try:
            player: wavelink.Player = await channel.connect(cls=wavelink.Player, timeout=60.0, self_deaf=True)
        except ChannelTimeoutException:
            try:
                player = await channel.connect(
                    cls=wavelink.Player, timeout=90.0, self_deaf=True, reconnect=True
                )
            except ChannelTimeoutException as e:
                raise commands.CommandError(
                    "Voice connect timed out. Check my **Connect/Speak** perms and try again."
                ) from e
        return player, channel

    async def _maybe_start_queue(self, player: wavelink.Player) -> None:
        # why: auto-advance on idle
        try:
            if not player.playing and not player.paused and len(player.queue) > 0:
                next_track = player.queue.get()
                await player.play(next_track)
        except Exception:
            pass

    @staticmethod
    def _queue_put_many(queue: "wavelink.Queue", items: Iterable) -> int:
        # why: Queue.put expects single item; add many safely
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

    async def _fetch_lavalink_info(self, node: wavelink.Node, timeout: float = 7.0) -> Optional[dict]:
        """GET /v4/info with Authorization header; include RTT (ms)."""
        url = f"{getattr(node, 'uri', '')}/v4/info"
        password = getattr(node, "password", None)
        if not url or not password:
            return None
        headers = {"Authorization": str(password)}
        t0 = asyncio.get_running_loop().time()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    data = await resp.json(content_type=None)
        except Exception:
            return None
        data["_rtt_ms"] = int((asyncio.get_running_loop().time() - t0) * 1000)
        return data

    # ---- lifecycle ----

    async def cog_load(self) -> None:
        node_cfg = await self._get_node_config()
        await self._ensure_nodes(node_cfg)

    async def cog_unload(self) -> None:
        try:
            await wavelink.Pool.close()
        except Exception:
            pass

    # ---- events (logs) ----

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        guilds = len(self.bot.guilds)
        print(f"[audioplus] Node {payload.node.identifier} **CONNECTED** (resumed={payload.resumed}) | guilds={guilds}")
        try:
            self._node_ready.set()
        except Exception:
            pass

    # Some Wavelink versions emit this on close; safe to implement even if not fired.
    @commands.Cog.listener()
    async def on_wavelink_node_closed(self, payload) -> None:
        code = getattr(payload, "code", "unknown")
        reason = getattr(payload, "reason", "")
        ident = getattr(getattr(payload, "node", None), "identifier", "MAIN")
        print(f"[audioplus] Node {ident} **CLOSED** code={code} reason={reason}")
        try:
            self._node_ready.clear()
        except Exception:
            pass

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
        """Owner: Configure Lavalink node. Example: `[p]audio setnode 127.0.0.1 2333 youshallnotpass false`"""
        await self.config.host.set(host)
        await self.config.port.set(int(port))
        await self.config.password.set(password)
        await self.config.secure.set(bool(secure))
        # reset ready gate since we will reconnect
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
        """Owner: Show current node settings."""
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
        # hard reset connections
        try:
            await wavelink.Pool.close()
        except Exception:
            pass
        await self._ensure_nodes(cfg)
        await self._wait_node_ready(timeout=15.0)
        # status
        node = None
        try:
            node = wavelink.Pool.get_node(cfg.identifier)
        except Exception:
            node = None
        connected = self._is_node_connected(node) if node else False
        info = await self._fetch_lavalink_info(node, timeout=7.0) if node else None
        ver = info.get("version") if info else "unknown"
        await ctx.send(
            f"Reconnect {'**successful**' if connected else '**failed**'} — Version: `{ver}` | URI: `{cfg.uri}`."
        )

    @audio.command(name="pingnode")
    @GUILD_ONLY
    async def audio_pingnode(self, ctx: commands.Context) -> None:
        """Show Lavalink node version/state."""
        node = None
        try:
            await self._ensure_pool_available(wait_timeout=10.0)
            node = wavelink.Pool.get_node()
        except Exception:
            # try REST probe from config even if Pool has no node
            cfg = await self._get_node_config()
            node = wavelink.Node(identifier=cfg.identifier, uri=cfg.uri, password=cfg.password)

        info = await self._fetch_lavalink_info(node, timeout=7.0) if node else None
        stats = getattr(node, "stats", None) if node else None

        connected = False
        if node:
            try:
                connected = self._is_node_connected(wavelink.Pool.get_node(getattr(node, "identifier", "MAIN")))
            except Exception:
                connected = False

        ident = getattr(node, "identifier", "MAIN") if node else "MAIN"
        uri = getattr(node, "uri", "unknown") if node else "unknown"

        lines = [f"**Lavalink Node — {ident}**", f"URI: `{uri}`", f"Connected: {'yes' if connected else 'no'}"]

        if info:
            ver = info.get("version", "unknown")
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

    @audio.command(name="join", aliases=["connect", "summon"])
    @GUILD_ONLY
    async def audio_join(self, ctx: commands.Context) -> None:
        """Join your voice channel."""
        _, channel = await self._fetch_or_connect_player(ctx)
        await ctx.send(f"Connected to **{channel}**.")

    @audio.command(name="leave", aliases=["dc", "disconnect"])
    @GUILD_ONLY
    async def audio_leave(self, ctx: commands.Context) -> None:
        """Leave voice channel and cleanup."""
        vc = ctx.voice_client
        if vc and isinstance(vc, wavelink.Player):
            await vc.disconnect()
            await ctx.send("Disconnected.")
        else:
            await ctx.send("Not connected.")

    @audio.command(name="play", aliases=["p"])
    @GUILD_ONLY
    async def audio_play(self, ctx: commands.Context, *, query: str) -> None:
        """Play a URL or search (YouTube, etc.). Example: `[p]audio play never gonna give you up`"""
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
        """Skip the current track."""
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
        """Stop playback and clear queue."""
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
        """Pause the player."""
        vc = ctx.voice_client
        if vc and isinstance(vc, wavelink.Player):
            await vc.pause(True)
            await ctx.send("Paused.")
        else:
            await ctx.send("Not connected.")

    @audio.command(name="resume")
    @GUILD_ONLY
    async def audio_resume(self, ctx: commands.Context) -> None:
        """Resume the player."""
        vc = ctx.voice_client
        if vc and isinstance(vc, wavelink.Player):
            await vc.pause(False)
            await ctx.send("Resumed.")
        else:
            await ctx.send("Not connected.")

    @audio.command(name="volume", aliases=["vol"])
    @GUILD_ONLY
    async def audio_volume(self, ctx: commands.Context, value: Optional[int] = None) -> None:
        """Get/set volume (0-1000)."""
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
        """Show the current track."""
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
        """Show the queue."""
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
        """Shuffle the queue."""
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
