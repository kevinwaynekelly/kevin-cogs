# repo: audioplus/
# ├─ info.json
# └─ __init__.py

# ---------- info.json ----------
# {
#   "author": ["Code Copilot"],
#   "install_msg": "Installed audioplus. Configure your Lavalink node with `[p]audio setnode <host> <port> <password> <secure>`.\nJoin a VC with `[p]audio join` then play with `[p]audio play <query|url>`.",
#   "name": "audioplus",
#   "short": "Single-file Lavalink v4 music cog using Wavelink 3.x.",
#   "description": "Red cog (single-file __init__.py) for Lavalink v4 via Wavelink 3.x. Commands: audio join/leave/play/skip/pause/resume/stop/volume/queue/np/shuffle; owner: audio setnode/shownode.",
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
Commands: [p]audio ...  (join/leave/play/skip/stop/pause/resume/volume/queue/np/shuffle)
Owner: [p]audio setnode <host> <port> <password> <secure>
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple, Iterable

import discord
from discord.ext import commands
import wavelink

from redbot.core import Config, checks
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
        self.config: Config = Config.get_conf(self, identifier=0xAUDIOFAB, force_registration=True)
        self.config.register_global(**self.default_global)

    # ---- lifecycle ----

    async def cog_load(self) -> None:
        node_cfg = await self._get_node_config()
        await self._ensure_nodes(node_cfg)

    async def cog_unload(self) -> None:
        try:
            await wavelink.Pool.close()
        except Exception:
            pass

    # ---- helpers ----

    async def _get_node_config(self) -> NodeConfig:
        data = await self.config.all()
        return NodeConfig.from_parts(
            host=data["host"],
            port=int(data["port"]),
            password=data["password"],
            secure=bool(data["secure"]),
        )

    async def _ensure_nodes(self, node_cfg: NodeConfig) -> None:
        # why: avoid duplicate node connections on reload
        try:
            existing = wavelink.Pool.get_node(node_cfg.identifier)
            if existing:
                return
        except Exception:
            pass

        node = wavelink.Node(
            identifier=node_cfg.identifier,
            uri=node_cfg.uri,
            password=node_cfg.password,
            resume_timeout=int(await self.config.resume_timeout()),
        )
        await wavelink.Pool.connect(nodes=[node], client=self.bot)

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

        player: wavelink.Player = await channel.connect(cls=wavelink.Player)
        return player, channel

    async def _maybe_start_queue(self, player: wavelink.Player) -> None:
        # why: auto-advance on idle
        if not player.playing and not player.paused and player.queue and not player.queue.is_empty:
            try:
                next_track = player.queue.get()
            except Exception:
                return
            await player.play(next_track)

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

    # ---- events ----

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        guilds = len(self.bot.guilds)
        print(f"[audioplus] Node {payload.node.identifier} ready (resumed={payload.resumed}) for {guilds} guilds.")

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        player = payload.player
        if not player or not player.channel:
            return
        await self._maybe_start_queue(player)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        player = payload.player
        if player and player.channel:
            try:
                msg = getattr(payload.exception, "message", None) or "unknown"
                await player.channel.send(f"Track error: {msg}")
            except Exception:
                pass
            await self._maybe_start_queue(player)

    # ---- commands ----

    @commands.group(name="audio", invoke_without_command=True)
    @GUILD_ONLY
    async def audio(self, ctx: commands.Context) -> None:
        """AudioPlus (Lavalink v4) commands."""
        await ctx.send_help()

    @audio.command(name="setnode")
    @checks.is_owner()
    async def audio_setnode(self, ctx: commands.Context, host: str, port: int, password: str, secure: Optional[bool] = False) -> None:
        """Owner: Configure Lavalink node. Example: `[p]audio setnode 127.0.0.1 2333 youshallnotpass false`"""
        await self.config.host.set(host)
        await self.config.port.set(int(port))
        await self.config.password.set(password)
        await self.config.secure.set(bool(secure))
        await self._ensure_nodes(await self._get_node_config())
        await ctx.send("Lavalink node saved & (re)connected.")

    @audio.command(name="shownode")
    @checks.is_owner()
    async def audio_shownode(self, ctx: commands.Context) -> None:
        """Owner: Show current node settings."""
        data = await self.config.all()
        secure = "yes" if data["secure"] else "no"
        await ctx.send(f"Node: {data['host']}:{data['port']} (secure: {secure})")

    @audio.command(name="join", aliases=["connect", "summon"])
    @GUILD_ONLY
    async def audio_join(self, ctx: commands.Context) -> None:
        """Join your voice channel."""
        player, channel = await self._fetch_or_connect_player(ctx)
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
        if hasattr(results, "__len__") and len(results) > 1 and ("list=" in query or "playlist" in str(type(results)).lower()):
            queued = self._queue_put_many(player.queue, results)
        else:
            player.queue.put(first)
            queued = 1

        if not player.playing and not player.paused:
            await self._maybe_start_queue(player)
            await ctx.send(f"Now playing: `{player.current.title if player.current else first.title}`")
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
        skipped = await vc.skip(force=True)
        if skipped:
            await ctx.send(f"Skipped: `{skipped.title}`")
        await self._maybe_start_queue(vc)

    @audio.command(name="stop")
    @GUILD_ONLY
    async def audio_stop(self, ctx: commands.Context) -> None:
        """Stop playback and clear queue."""
        vc = ctx.voice_client
        if vc and isinstance(vc, wavelink.Player):
            vc.queue.clear()
            await vc.stop(force=True)
            await ctx.send("Stopped and cleared the queue.")
        else:
            await ctx.send("Not connected.")

    @audio.command(name="pause")
    @GUILD_ONLY
    async def audio_pause(self, ctx: commands.Context) -> None:
        """Pause the player."""
        vc = ctx.voice_client
        if vc and isinstance(vc, wavelink.Player):
            await vc.pause(True)  # why: Wavelink pause expects bool
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
        if not vc or not isinstance(vc, wavelink.Player) or not vc.current:
            await ctx.send("Nothing is playing.")
            return
        t = vc.current
        await ctx.send(f"Now playing: `{t.title}` by `{getattr(t, 'author', 'Unknown')}`")

    @audio.command(name="queue", aliases=["q"])
    @GUILD_ONLY
    async def audio_queue(self, ctx: commands.Context) -> None:
        """Show the queue."""
        vc = ctx.voice_client
        if not vc or not isinstance(vc, wavelink.Player):
            await ctx.send("Not connected.")
            return
        if vc.queue.is_empty:
            await ctx.send("Queue is empty.")
            return
        lines = []
        for i, tr in enumerate(list(vc.queue)[:10], start=1):
            title = getattr(tr, "title", None) or "Unknown"
            lines.append(f"{i}. {title}")
        extra = f"… and {vc.queue.count - 10} more." if vc.queue.count > 10 else ""
        await ctx.send("\n".join(lines) + (f"\n{extra}" if extra else ""))

    @audio.command(name="shuffle")
    @GUILD_ONLY
    async def audio_shuffle(self, ctx: commands.Context) -> None:
        """Shuffle the queue."""
        vc = ctx.voice_client
        if not vc or not isinstance(vc, wavelink.Player):
            await ctx.send("Not connected.")
            return
        vc.queue.shuffle()
        await ctx.send("Queue shuffled.")


async def setup(bot: Red) -> None:
    await bot.add_cog(AudioPlus(bot))
