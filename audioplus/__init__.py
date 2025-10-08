# path: cogs/audioplus/__init__.py
from __future__ import annotations

import asyncio
import re
from typing import Optional, List, Tuple

import discord
from discord.ext import commands
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config
from redbot.core.utils.chat_formatting import box

try:
    import wavelink  # Expecting 3.x family; we handle API diffs at runtime
except Exception as e:
    raise ImportError(
        "audioplus requires Wavelink 3.x (Lavalink v4).\n"
        "Install: pip install -U 'wavelink>=3,<4'  (or [p]pipinstall wavelink==3.*)"
    ) from e

YOUTUBE_URL_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE)

DEFAULTS_GUILD = {
    "node": {"host": "", "port": 2333, "password": "youshallnotpass", "https": False, "resume_key": None},
    "bind_channel": None,
    "default_volume": 60,
    "prefer_lyrics": True,
    "debug": True,
    "autoconnect": True,
}

class LoopMode:
    OFF = "off"
    ONE = "one"
    ALL = "all"

class MusicPlayer(wavelink.Player):
    """Guild player with queue + loop."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue: wavelink.Queue[wavelink.Playable] = wavelink.Queue()
        self.loop: str = LoopMode.OFF
        self.text_channel_id: Optional[int] = None
        self.requester_id: Optional[int] = None

    @property
    def text_channel(self) -> Optional[discord.TextChannel]:
        if self.text_channel_id and self.guild:
            ch = self.guild.get_channel(self.text_channel_id)
            return ch if isinstance(ch, discord.TextChannel) else None
        return None

    async def announce(self, content: str):
        ch = self.text_channel or getattr(self.guild, "system_channel", None)
        if not ch:
            return
        try:
            await ch.send(content)
        except discord.Forbidden:
            pass

class AudioPlus(redcommands.Cog):
    """Lavalink v4 YouTube-only player (lyric-first), with queue/seek/repeat, autoconnect, search preview, and loud debug."""

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0xA71D01, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._node_ready: bool = False
        self._last_node_error: Optional[str] = None

    # ---------- util/debug ----------
    async def _debug(self, guild: discord.Guild, msg: str):
        if not await self.config.guild(guild).debug():
            return
        ch_id = await self.config.guild(guild).bind_channel()
        ch = guild.get_channel(ch_id) if ch_id else getattr(guild, "system_channel", None)
        print(f"[AudioPlus] {guild.name}: {msg}")  # console log
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try:
                await ch.send(box(msg, lang="ini"))
            except Exception:
                pass

    # ---------- node mgmt (support multiple WL3 APIs) ----------
    def _get_connected_node(self) -> Optional["wavelink.Node"]:
        try:
            # WL3 classic path
            if hasattr(wavelink, "NodePool") and hasattr(wavelink.NodePool, "get_node"):
                node = wavelink.NodePool.get_node()
                return node if getattr(node, "is_connected", False) else None
        except Exception:
            return None
        return None

    async def _connect_node_any(self, guild: discord.Guild) -> Optional["wavelink.Node"]:
        # Already connected?
        node = self._get_connected_node()
        if node:
            return node

        conf = await self.config.guild(guild).node()
        host = (conf["host"] or "").strip()
        port = int(conf["port"])
        password = conf["password"]
        https = bool(conf["https"])
        ver = getattr(wavelink, "__version__", "unknown")
        apis = {
            "NodePool.create_node": hasattr(getattr(wavelink, "NodePool", None), "create_node"),
            "NodePool.connect": hasattr(getattr(wavelink, "NodePool", None), "connect") and hasattr(wavelink, "Node"),
        }
        if not host:
            await self._debug(guild, f"node.connect: host not set.\nwl_version={ver} apis={apis}")
            return None

        uri = f"http{'s' if https else ''}://{host}:{port}"
        await self._debug(guild, f"node.connect: wl_version={ver} apis={apis}\nuri={uri}")

        # Try WL3 classic: create_node
        if apis["NodePool.create_node"]:
            try:
                node = await wavelink.NodePool.create_node(
                    bot=self.bot,
                    host=host,
                    port=port,
                    password=password,
                    https=https,
                    resume_key=conf.get("resume_key"),
                )
                self._node_ready = True
                await self._debug(guild, "node.connect: SUCCESS via create_node")
                return node
            except Exception as e:
                self._last_node_error = f"create_node {type(e).__name__}: {e!s}"
                await self._debug(guild, f"node.connect: ERROR {self._last_node_error}")

        # Try WL3 alt: NodePool.connect(Node(...))
        if apis["NodePool.connect"]:
            try:
                node_obj = wavelink.Node(uri=uri, password=password)
                await wavelink.NodePool.connect(client=self.bot, nodes=[node_obj])
                node = self._get_connected_node()
                self._node_ready = bool(node)
                await self._debug(guild, f"node.connect: {'SUCCESS' if node else 'FAIL'} via NodePool.connect")
                return node
            except Exception as e:
                self._last_node_error = f"connect {type(e).__name__}: {e!s}"
                await self._debug(guild, f"node.connect: ERROR {self._last_node_error}")

        self._last_node_error = self._last_node_error or "Unsupported wavelink version/APIs; need 3.x"
        return None

    async def _ensure_autoconnect(self):
        await self.bot.wait_until_red_ready()
        for g in list(self.bot.guilds):
            try:
                if await self.config.guild(g).autoconnect():
                    await self._connect_node_any(g)
            except Exception:
                continue

    def cog_unload(self):
        # why: try to avoid unclosed aiohttp sessions by disconnecting gracefully
        self.bot.loop.create_task(self._cleanup())

    async def _cleanup(self):
        try:
            for vc in list(self.bot.voice_clients):
                if isinstance(vc, MusicPlayer):
                    try:
                        await vc.disconnect(force=True)
                    except Exception:
                        pass
            # Best-effort node cleanup (APIs can differ)
            np = getattr(wavelink, "NodePool", None)
            if np and hasattr(np, "nodes"):
                for node in list(np.nodes.values()):  # type: ignore[attr-defined]
                    try:
                        await node.disconnect()
                    except Exception:
                        pass
        except Exception:
            pass

    # ---------- helpers ----------
    async def _get_player(self, ctx: redcommands.Context, *, connect: bool = True) -> MusicPlayer:
        player = ctx.voice_client
        if isinstance(player, MusicPlayer):
            return player
        if not connect:
            raise redcommands.UserFeedbackCheckFailure("Not connected.")
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise redcommands.UserFeedbackCheckFailure("Join a voice channel first.")
        node = await self._connect_node_any(ctx.guild)
        if not node:
            raise redcommands.UserFeedbackCheckFailure("Node not connected. Run `music node connect` (check password/port/https).")
        channel = ctx.author.voice.channel
        try:
            player = await channel.connect(cls=MusicPlayer)  # type: ignore[arg-type]
        except discord.Forbidden:
            raise redcommands.UserFeedbackCheckFailure("Missing Connect permission for that voice channel.")
        except Exception as e:
            raise redcommands.UserFeedbackCheckFailure(f"VC connect failed: {type(e).__name__}")
        bind_id = await self.config.guild(ctx.guild).bind_channel()
        player.text_channel_id = bind_id or ctx.channel.id
        vol = await self.config.guild(ctx.guild).default_volume()
        try:
            await player.set_volume(int(max(0, min(150, vol))))
        except Exception:
            pass
        await self._debug(ctx.guild, f"player.init: text_channel_id={player.text_channel_id} volume={vol}")
        return player

    def _ensure_same_vc(self, ctx: redcommands.Context, player: MusicPlayer):
        if not ctx.author.voice or not ctx.author.voice.channel or ctx.author.voice.channel != player.channel:
            raise redcommands.UserFeedbackCheckFailure("You must be in my voice channel.")

    async def _search_youtube(self, guild: discord.Guild, query: str) -> Tuple[Optional[wavelink.Playable], str]:
        prefer_lyrics = await self.config.guild(guild).prefer_lyrics()
        debug_lines: List[str] = [f"search: prefer_lyrics={prefer_lyrics} raw='{query}'"]

        async def _search(q: str) -> List[wavelink.Playable]:
            try:
                res = await wavelink.Playable.search(f"ytsearch:{q}")
                return list(res) if res else []
            except Exception as e:
                debug_lines.append(f"search: FAIL {type(e).__name__}")
                return []

        if YOUTUBE_URL_RE.search(query):
            try:
                res = await wavelink.Playable.search(query)
                track = res[0] if res else None
                debug_lines.append(f"search.url: results={len(res) if res else 0}")
                return track, "\n".join(debug_lines)
            except Exception as e:
                debug_lines.append(f"search.url: ERROR {type(e).__name__}")
                return None, "\n".join(debug_lines)

        queries = [f"{query} lyrics", f"{query} lyric video", query] if prefer_lyrics else [query]
        for q in queries:
            items = await _search(q)
            debug_lines.append(f"search.q: '{q}' -> {len(items)}")
            if items:
                if prefer_lyrics:
                    def score(t: wavelink.Playable) -> Tuple[int, int]:
                        title = (getattr(t, "title", "") or "").lower()
                        has = 0 if ("lyric" in title or "lyrics" in title) else 1  # why: prefer lyric videos
                        return (has, len(title))
                    items.sort(key=score)
                t = items[0]
                debug_lines.append(f"search.pick: title='{getattr(t, 'title', 'Unknown')}' length={getattr(t, 'length', 0)}")
                return t, "\n".join(debug_lines)

        debug_lines.append("search: NO_RESULTS (Check Lavalink v4 + YouTube cipher plugin)")
        return None, "\n".join(debug_lines)

    # ---------- events (node + tracks) ----------
    @commands.Cog.listener()
    async def on_ready(self):
        # why: autoconnect silently on bot ready
        await self._ensure_autoconnect()

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload):
        node = payload.node
        for g in self.bot.guilds:
            await self._debug(g, f"node.ready: connected uri={getattr(node, 'uri', 'unknown')} session={getattr(payload, 'session_id', '?')}")
        self._node_ready = True

    @commands.Cog.listener()
    async def on_wavelink_node_connection_closed(self, payload):
        node = payload.node
        self._node_ready = False
        self._last_node_error = f"closed: code={getattr(payload,'code','?')} reason={getattr(payload,'reason','?')}"
        for g in self.bot.guilds:
            await self._debug(g, f"node.closed: uri={getattr(node,'uri','unknown')} code={getattr(payload,'code','?')} reason={getattr(payload,'reason','?')}")
            # try reconnect if autoconnect
            if await self.config.guild(g).autoconnect():
                async def _recon():
                    await asyncio.sleep(5)
                    await self._connect_node_any(g)
                self.bot.loop.create_task(_recon())

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player: MusicPlayer = payload.player  # type: ignore[assignment]
        if not isinstance(player, MusicPlayer):
            return
        try:
            if player.loop == LoopMode.ONE and payload.reason == "finished":
                await player.play(payload.track); return
            if player.loop == LoopMode.ALL and payload.reason == "finished":
                try: player.queue.put(payload.track)
                except Exception: pass
            if not player.queue.is_empty:
                nxt = player.queue.get()
                await player.play(nxt)
                await player.announce(f"▶️ Now playing: **{getattr(nxt, 'title', 'Unknown')}**")
            else:
                await player.stop()
        except Exception as e:
            await self._debug(player.guild, f"track_end: ERROR {type(e).__name__}")

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload):
        player: MusicPlayer = payload.player  # type: ignore[assignment]
        if isinstance(player, MusicPlayer) and not player.queue.is_empty:
            nxt = player.queue.get()
            await player.play(nxt)
            await player.announce(f"⚠️ Track stuck. Skipping to **{getattr(nxt, 'title', 'Unknown')}**")

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        player: MusicPlayer = payload.player  # type: ignore[assignment]
        if isinstance(player, MusicPlayer):
            if not player.queue.is_empty:
                nxt = player.queue.get()
                await player.play(nxt)
                await player.announce(f"❌ Track error. Next: **{getattr(nxt, 'title', 'Unknown')}**")
            else:
                await player.announce("❌ Track error.")

    # ---------- commands ----------
    @redcommands.group(name="music", invoke_without_command=True)
    @redcommands.guild_only()
    async def music(self, ctx: redcommands.Context):
        await self._help_full(ctx)

    @music.command()
    async def help(self, ctx: redcommands.Context):
        """Full command list."""
        await self._help_full(ctx)

    async def _help_full(self, ctx: redcommands.Context):
        p = ctx.clean_prefix
        desc = (
            f"**AudioPlus (Lavalink v4 / YouTube-only)**\n\n"
            f"**Setup**\n"
            f"• `{p}music node set <host> <port> <password> [https?]`\n"
            f"• `{p}music node connect` • `{p}music node status` • `{p}music node autoconnect [true|false]`\n"
            f"• `{p}music diag` • `{p}music probe [voice-channel]` • `{p}music debug [true|false]`\n"
            f"• `{p}music bind [#channel]` • `{p}music preferlyrics [true|false]` • `{p}music defaultvolume <0-150>`\n\n"
            f"**Playback**\n"
            f"• `{p}play <query|youtube-url>` (`{p}p`) — lyric-first\n"
            f"• `{p}search <query>` — preview top 5 results\n"
            f"• `pause` • `resume` • `skip` • `stop` • `seek <mm:ss|hh:mm:ss|sec|+/-sec>` • `volume <0-150>` • `now`\n\n"
            f"**Queue**\n"
            f"• `queue` • `remove <index>` • `clear` • `shuffle` • `repeat <off|one|all>`\n\n"
            f"*Requires Lavalink **v4** with YouTube cipher plugin server-side.*"
        )
        try:
            await ctx.send(embed=discord.Embed(title="AudioPlus — Full Help", description=desc, color=discord.Color.blurple()))
        except discord.Forbidden:
            await ctx.send(box(desc, lang="ini"))

    # ----- debug/autoconnect toggles -----
    @music.command(name="debug")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def debug_cmd(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).debug())
        await self.config.guild(ctx.guild).debug.set(bool(enabled))
        await ctx.send(f"debug = **{bool(enabled)}**")

    @music.group(name="node")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def nodegrp(self, ctx: redcommands.Context): ...

    @nodegrp.command(name="autoconnect")
    async def node_autoconnect(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).autoconnect())
        await self.config.guild(ctx.guild).autoconnect.set(bool(enabled))
        await ctx.send(f"autoconnect = **{bool(enabled)}**")

    @nodegrp.command(name="set")
    async def node_set(self, ctx: redcommands.Context, host: str, port: int, password: str, https: Optional[bool] = False):
        await self.config.guild(ctx.guild).node.set({
            "host": host, "port": int(port), "password": password, "https": bool(https), "resume_key": None
        })
        self._node_ready = False
        await ctx.tick()
        await ctx.send(box(f"host={host}\nport={port}\nhttps={bool(https)}", lang="ini"))

    @nodegrp.command(name="connect")
    async def node_connect(self, ctx: redcommands.Context):
        await ctx.send("Attempting node connect…")
        node = await self._connect_node_any(ctx.guild)
        if node:
            await ctx.send(box(f"connected=True uri={getattr(node, 'uri', 'unknown')}", lang="ini"))
        else:
            ver = getattr(wavelink, "__version__", "unknown")
            await ctx.send(box(
                "connected=False\n"
                f"wavelink_version={ver}\n"
                f"hint=Use Wavelink 3.x; check Lavalink v4, password/port, HTTPS flag, and cipher plugin.\n"
                f"last_error={self._last_node_error or 'n/a'}", lang="ini"))

    @nodegrp.command(name="status")
    async def node_status(self, ctx: redcommands.Context):
        conf = await self.config.guild(ctx.guild).node()
        node = self._get_connected_node()
        ver = getattr(wavelink, "__version__", "unknown")
        pool_nodes = len(getattr(getattr(wavelink, "NodePool", None), "nodes", {}) or {}) if hasattr(getattr(wavelink, "NodePool", None), "nodes") else "n/a"
        uri = f"http{'s' if conf['https'] else ''}://{conf['host']}:{conf['port']}" if conf["host"] else "not set"
        players = getattr(getattr(node, "stats", None), "players", None) if node else None
        apis = {
            "NodePool.create_node": hasattr(getattr(wavelink, "NodePool", None), "create_node"),
            "NodePool.connect": hasattr(getattr(wavelink, "NodePool", None), "connect") and hasattr(wavelink, "Node"),
        }
        lines = [
            f"configured_uri={uri}",
            f"connected={bool(node)} pool_nodes={pool_nodes}",
            f"wavelink_version={ver} apis={apis}",
            f"players={players if players is not None else 'n/a'}",
            f"last_error={self._last_node_error or 'none'}",
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @nodegrp.command(name="show")
    async def node_show(self, ctx: redcommands.Context):
        node = await self.config.guild(ctx.guild).node()
        await ctx.send(box(
            f"host={node['host'] or 'not set'}\nport={node['port']}\nhttps={node['https']}\npassword_set={'yes' if node['password'] else 'no'}",
            lang="ini"
        ))

    # ----- binding / prefs -----
    @music.command()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def bind(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None):
        ch = channel or ctx.channel
        await self.config.guild(ctx.guild).bind_channel.set(ch.id)
        player = ctx.voice_client
        if isinstance(player, MusicPlayer):
            player.text_channel_id = ch.id
        await ctx.send(f"Bound player messages to {ch.mention}")

    @music.command(name="preferlyrics")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def preferlyrics_cmd(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            cur = await self.config.guild(ctx.guild).prefer_lyrics()
            enabled = not cur
        await self.config.guild(ctx.guild).prefer_lyrics.set(bool(enabled))
        await ctx.send(f"prefer_lyrics = **{bool(enabled)}**")

    @music.command(name="defaultvolume", aliases=["defvol", "setvolume"])
    @redcommands.admin_or_permissions(manage_guild=True)
    async def defaultvolume_cmd(self, ctx: redcommands.Context, value: int):
        value = int(max(0, min(150, value)))
        await self.config.guild(ctx.guild).default_volume.set(value)
        player = ctx.voice_client
        if isinstance(player, MusicPlayer):
            try:
                await player.set_volume(value)
            except Exception:
                pass
        await ctx.send(f"default_volume = **{value}%**")

    # ----- voice -----
    @music.command()
    async def join(self, ctx: redcommands.Context, channel: Optional[discord.VoiceChannel] = None):
        node = await self._connect_node_any(ctx.guild)
        if not node:
            return await ctx.send("Node not connected. Run `music node connect`.")
        target = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if not target:
            return await ctx.send("Join a voice channel or specify one.")
        if ctx.voice_client and isinstance(ctx.voice_client, MusicPlayer):
            if ctx.voice_client.channel == target:
                return await ctx.tick()
            await ctx.voice_client.move_to(target); return await ctx.tick()
        player: MusicPlayer = await target.connect(cls=MusicPlayer)  # type: ignore[arg-type]
        bind_id = await self.config.guild(ctx.guild).bind_channel()
        player.text_channel_id = bind_id or ctx.channel.id
        vol = await self.config.guild(ctx.guild).default_volume()
        try: await player.set_volume(int(max(0, min(150, vol))))
        except Exception: pass
        await ctx.tick()

    @music.command()
    async def leave(self, ctx: redcommands.Context):
        player = ctx.voice_client
        if not isinstance(player, MusicPlayer):
            return await ctx.send("Not connected.")
        self._ensure_same_vc(ctx, player)
        await player.disconnect(); await ctx.tick()

    # ----- playback / search -----
    @music.command(aliases=["p"])
    async def play(self, ctx: redcommands.Context, *, query: str):
        try:
            player = await self._get_player(ctx, connect=True)
        except redcommands.UserFeedbackCheckFailure as e:
            return await ctx.send(f"Setup failed: {e}")

        track, dbg = await self._search_youtube(ctx.guild, query)
        await self._debug(ctx.guild, dbg)
        if not track:
            hint = "Ensure Lavalink v4 is up, port open, password correct, HTTPS flag matches, and YouTube cipher plugin is enabled."
            return await ctx.send(box(f"Play failed: no results for query.\n{dbg}\nhint={hint}", lang="ini"))

        try:
            player.requester_id = ctx.author.id
            if not player.playing and not player.paused:
                await player.play(track)
                await player.announce(f"▶️ Now playing: **{getattr(track, 'title', 'Unknown')}**")
            else:
                player.queue.put(track)
                await ctx.send(f"➕ Queued: **{getattr(track, 'title', 'Unknown')}**")
        except Exception as e:
            return await ctx.send(box(
                f"Play failed: {type(e).__name__}\nstate=playing:{player.playing} paused:{player.paused}\ntrack='{getattr(track,'title','?')}'",
                lang="ini"
            ))

    @music.command()
    async def search(self, ctx: redcommands.Context, *, query: str):
        """Preview top 5 YouTube results (lyric-first if enabled)."""
        prefer_lyrics = await self.config.guild(ctx.guild).prefer_lyrics()
        qlist = [f"{query} lyrics", f"{query} lyric video", query] if prefer_lyrics and not YOUTUBE_URL_RE.search(query) else [query]
        results: List[wavelink.Playable] = []
        dbg: List[str] = [f"search.preview prefer_lyrics={prefer_lyrics}"]
        try:
            for q in qlist:
                res = await wavelink.Playable.search(f"ytsearch:{q}")
                dbg.append(f"q='{q}' -> {len(res) if res else 0}")
                if res:  # type: ignore[truthy-function]
                    results = list(res)  # pick first non-empty bucket
                    break
        except Exception as e:
            dbg.append(f"ERROR {type(e).__name__}")
        await self._debug(ctx.guild, "\n".join(dbg))

        if not results:
            return await ctx.send("No YouTube results.")

        def dur_fmt(ms: int) -> str:
            s = int((ms or 0) / 1000)
            m, s = divmod(s, 60)
            h, m = divmod(m, 60)
            return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

        # Prefer lyric titles at the top if configured
        if prefer_lyrics:
            def score(t: wavelink.Playable) -> Tuple[int, int]:
                title = (getattr(t, "title", "") or "").lower()
                return (0 if ("lyric" in title or "lyrics" in title) else 1, len(title))
            results.sort(key=score)

        top = results[:5]
        lines = []
        for i, t in enumerate(top, start=1):
            title = getattr(t, "title", "Unknown")
            length = dur_fmt(getattr(t, "length", 0))
            lines.append(f"{i}. {title} [{length}]")
        lines.append("")
        lines.append(f"Tip: `{ctx.clean_prefix}play {query}` to enqueue the best match.")
        await ctx.send(box("\n".join(lines), lang="ini"))

    @music.command()
    async def pause(self, ctx: redcommands.Context):
        player = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, player)
        await player.pause(True); await ctx.tick()

    @music.command()
    async def resume(self, ctx: redcommands.Context):
        player = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, player)
        await player.pause(False); await ctx.tick()

    @music.command()
    async def stop(self, ctx: redcommands.Context):
        player = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, player)
        player.queue.reset()
        await player.stop(); await ctx.tick()

    @music.command()
    async def skip(self, ctx: redcommands.Context):
        player = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, player)
        await player.stop(); await ctx.tick()

    @music.command()
    async def seek(self, ctx: redcommands.Context, position: str):
        player = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, player)

        def parse_pos(s: str) -> Optional[int]:
            s = s.strip()
            if s.startswith(("+", "-")):
                try:
                    delta = int(s)
                    return max(0, (player.position or 0) + (delta * 1000))
                except Exception:
                    return None
            if ":" in s:
                try:
                    parts = [int(p) for p in s.split(":")]
                    while len(parts) < 2: parts.insert(0, 0)
                    h, m, sec = (0, parts[-2], parts[-1]) if len(parts) == 2 else (parts[-3], parts[-2], parts[-1])
                    return max(0, (h * 3600 + m * 60 + sec) * 1000)
                except Exception:
                    return None
            try:
                return max(0, int(float(s)) * 1000)
            except Exception:
                return None

        ms = parse_pos(position)
        if ms is None:
            return await ctx.send("Use `mm:ss`, `hh:mm:ss`, or seconds (supports `+/-` delta).")
        try:
            await player.seek(ms); await ctx.tick()
        except Exception:
            await ctx.send("Seek failed.")

    @music.command(aliases=["vol"])
    async def volume(self, ctx: redcommands.Context, value: int):
        player = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, player)
        value = int(max(0, min(150, value)))
        await player.set_volume(value)
        await ctx.send(f"Volume set to **{value}%**")

    @music.command()
    async def now(self, ctx: redcommands.Context):
        player = await self._get_player(ctx, connect=False)
        track = player.current
        if not track:
            return await ctx.send("Nothing playing.")
        pos = int((player.position or 0) / 1000)
        dur = int((getattr(track, "length", 0) or 0) / 1000)

        def fmt(s: int) -> str:
            m, s = divmod(max(0, s), 60)
            h, m = divmod(m, 60)
            return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        await ctx.send(f"🎵 **{getattr(track, 'title', 'Unknown')}** [{fmt(pos)}/{fmt(dur)}]")

    # ----- queue -----
    @music.command()
    async def queue(self, ctx: redcommands.Context):
        player = await self._get_player(ctx, connect=False)
        if player.queue.is_empty:
            return await ctx.send("Queue is empty.")
        lines = [f"{i:>2}. {getattr(t, 'title', 'Unknown')}" for i, t in enumerate(list(player.queue)[:15], start=1)]
        more = len(player.queue) - 15
        if more > 0:
            lines.append(f"... and {more} more")
        await ctx.send(box("\n".join(lines), lang="ini"))

    @music.command()
    async def remove(self, ctx: redcommands.Context, index: int):
        player = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, player)
        if index < 1 or index > len(player.queue):
            return await ctx.send("Index out of range.")
        items = list(player.queue)
        removed = items.pop(index - 1)
        player.queue.clear()
        for t in items:
            player.queue.put(t)
        await ctx.send(f"Removed **{getattr(removed, 'title', 'Unknown')}**")

    @music.command()
    async def clear(self, ctx: redcommands.Context):
        player = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, player)
        player.queue.reset()
        await ctx.tick()

    @music.command()
    async def shuffle(self, ctx: redcommands.Context):
        player = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, player)
        player.queue.shuffle()
        await ctx.tick()

    @music.command()
    async def repeat(self, ctx: redcommands.Context, mode: str):
        player = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, player)
        mode = mode.lower()
        if mode not in (LoopMode.OFF, LoopMode.ONE, LoopMode.ALL):
            return await ctx.send("Use: `off|one|all`.")
        player.loop = mode
        await ctx.send(f"Repeat mode: **{mode}**")


async def setup(bot: Red) -> None:
    cog = AudioPlus(bot)
    await bot.add_cog(cog)
