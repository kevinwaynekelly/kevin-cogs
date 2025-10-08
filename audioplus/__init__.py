# path: cogs/audioplus/__init__.py
from __future__ import annotations

import asyncio
import json
import re
from typing import Optional, List, Tuple

import aiohttp
import discord
from discord.ext import commands
from redbot.core import Config, commands as redcommands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box

# YouTube URL quick check
YOUTUBE_URL_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE)

DEFAULTS_GUILD = {
    "node": {"host": "", "port": 2333, "password": "youshallnotpass", "https": False, "autoconnect": True},
    "bind_channel": None,
    "default_volume": 60,
    "prefer_lyrics": True,
    "debug": True,
}

def _scheme(https: bool) -> str:
    return "https" if https else "http"

def _uri(host: str, port: int, https: bool) -> str:
    return f"{_scheme(https)}://{host}:{port}"

class LoopMode:
    OFF = "off"
    ONE = "one"
    ALL = "all"

# --- Wavelink optional import (we handle API variants at runtime) ---
try:
    import wavelink  # Expect WL 3.x for Lavalink v4
except Exception as e:
    raise ImportError(
        "audioplus requires Wavelink 3.x (for Lavalink v4).\n"
        "Install: pip install -U 'wavelink>=3,<4'  (or [p]pipinstall wavelink==3.*)"
    ) from e

class MusicPlayer(wavelink.Player):
    """Minimal queue/loop TextChannel-aware player."""
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
    """Lavalink v4 (YouTube) player with autoconnect, rich help, search preview, and loud debug."""

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        # FIX: valid integer identifier (hex or decimal). The previous 0xAUDIOP10 was invalid.
        self.config: Config = Config.get_conf(self, identifier=0xA71D01, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._wl_api: str = "unset"
        self._last_error: Optional[str] = None
        self._autoconnect_task: Optional[asyncio.Task] = None

    # ---------- debug ----------
    async def _debug(self, guild: discord.Guild, msg: str):
        if not await self.config.guild(guild).debug():
            return
        ch_id = await self.config.guild(guild).bind_channel()
        ch = guild.get_channel(ch_id) if ch_id else getattr(guild, "system_channel", None)
        print(f"[AudioPlus] {guild.name}: {msg}")
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try:
                await ch.send(box(msg, lang="ini"))
            except Exception:
                pass

    # ---------- lifecycle ----------
    async def cog_load(self) -> None:
        async def _maybe():
            await self.bot.wait_until_ready()
            for guild in self.bot.guilds:
                g = await self.config.guild(guild).all()
                if g["node"]["autoconnect"]:
                    try:
                        await self._connect_node(guild)
                    except Exception as e:
                        self._last_error = f"autoconnect-error: {type(e).__name__}: {e}"
        self._autoconnect_task = asyncio.create_task(_maybe())

    async def cog_unload(self) -> None:
        if self._autoconnect_task:
            self._autoconnect_task.cancel()
        # best-effort cleanup to avoid aiohttp session leaks
        try:
            for vc in list(self.bot.voice_clients):
                if isinstance(vc, MusicPlayer):
                    try:
                        await vc.disconnect(force=True)
                    except Exception:
                        pass
            NP = getattr(wavelink, "NodePool", None)
            if NP and hasattr(NP, "nodes"):
                for node in list(NP.nodes.values()):  # type: ignore[attr-defined]
                    try:
                        await node.disconnect()
                    except Exception:
                        pass
            PL = getattr(wavelink, "Pool", None)
            if PL and hasattr(PL, "nodes"):
                for node in list(PL.nodes.values()):  # type: ignore[attr-defined]
                    try:
                        await node.disconnect()
                    except Exception:
                        pass
        except Exception:
            pass

    # ---------- wavelink API probing ----------
    def _apis(self):
        NP = getattr(wavelink, "NodePool", None)
        PL = getattr(wavelink, "Pool", None)
        return {
            "has_NodePool": bool(NP),
            "has_NodePool_create_node": bool(getattr(NP, "create_node", None)),
            "has_NodePool_get_node": bool(getattr(NP, "get_node", None)),
            "has_Pool": bool(PL),
            "has_Pool_connect": bool(getattr(PL, "connect", None)),
            "has_Pool_get_node": bool(getattr(PL, "get_node", None)),
        }

    def _get_connected_node(self) -> Optional["wavelink.Node"]:
        ap = self._apis()
        try:
            if ap["has_NodePool"] and ap["has_NodePool_get_node"]:
                node = wavelink.NodePool.get_node()
                if getattr(node, "is_connected", False):
                    return node
        except Exception:
            pass
        try:
            if ap["has_Pool"] and ap["has_Pool_get_node"]:
                node = wavelink.Pool.get_node()  # type: ignore[attr-defined]
                if getattr(node, "is_connected", False):
                    return node
        except Exception:
            pass
        return None

    async def _connect_node(self, guild: discord.Guild) -> str:
        node = self._get_connected_node()
        if node:
            return self._wl_api or "already-connected"

        g = await self.config.guild(guild).all()
        host, port, pw, https = g["node"]["host"], g["node"]["port"], g["node"]["password"], g["node"]["https"]
        if not host:
            raise RuntimeError("Node host is empty. Run [p]music node set first.")

        uri = _uri(host, port, https)
        ver = getattr(wavelink, "__version__", "unknown")
        ap = self._apis()
        await self._debug(guild, f"node.connect: wl={ver} apis={ap} uri={uri}")

        # Prefer Pool.connect (WL 3.x)
        if ap["has_Pool"] and ap["has_Pool_connect"]:
            try:
                Node = getattr(wavelink, "Node")
                node = Node(uri=uri, password=pw)
                # Some builds accept secure kw; guard usage
                if "secure" in node.__init__.__code__.co_varnames:  # type: ignore[attr-defined]
                    node = Node(uri=uri, password=pw, secure=https)
                await wavelink.Pool.connect(client=self.bot, nodes=[node])  # type: ignore[attr-defined]
                self._wl_api = "Pool.connect(Node)"
                return self._wl_api
            except Exception as e:
                self._last_error = f"Pool.connect failed: {type(e).__name__}: {e}"
                await self._debug(guild, self._last_error)

        # Fallback: NodePool.create_node
        if ap["has_NodePool"] and ap["has_NodePool_create_node"]:
            try:
                await wavelink.NodePool.create_node(bot=self.bot, host=host, port=port, password=pw, https=https)
                self._wl_api = "NodePool.create_node"
                return self._wl_api
            except Exception as e:
                self._last_error = f"NodePool.create_node failed: {type(e).__name__}: {e}"
                await self._debug(guild, self._last_error)

        self._last_error = self._last_error or f"Incompatible Wavelink APIs; need WL 3.x. apis={ap}"
        raise RuntimeError(self._last_error)

    async def _wavelink_status(self) -> str:
        parts: List[str] = []
        ver = getattr(wavelink, "__version__", "unknown")
        parts.append(f"wavelink_version={ver}")
        try:
            Pool = getattr(wavelink, "Pool", None)
            npool = getattr(wavelink, "NodePool", None)
            if Pool and hasattr(Pool, "nodes"):
                nodes = getattr(Pool, "nodes")
                parts.append(f"pool_nodes={len(nodes) if hasattr(nodes, '__len__') else 'unknown'}")
            elif npool and hasattr(npool, "nodes"):
                nodes = getattr(npool, "nodes")
                parts.append(f"nodepool_nodes={len(nodes) if hasattr(nodes, '__len__') else 'unknown'}")
        except Exception:
            parts.append("pool_introspection=error")
        if self._wl_api != "unset":
            parts.append(f"api={self._wl_api}")
        if self._last_error:
            parts.append(f"last_error={self._last_error}")
        return " ".join(parts)

    async def _ping_http(self, host: str, port: int, https: bool) -> dict:
        uri_v = f"{_uri(host, port, https)}/version"
        uri_info = f"{_uri(host, port, https)}/v4/info"
        out = {"version": None, "info": None, "errors": []}
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            try:
                async with sess.get(uri_v) as resp:
                    out["version"] = {"status": resp.status, "body": (await resp.text())[:200]}
            except Exception as e:
                out["errors"].append(f"/version {type(e).__name__}: {e}")
            try:
                async with sess.get(uri_info) as resp:
                    out["info"] = {"status": resp.status, "body": (await resp.text())[:200]}
            except Exception as e:
                out["errors"].append(f"/v4/info {type(e).__name__}: {e}")
        return out

    # ---------- search helper ----------
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
                t = res[0] if res else None
                debug_lines.append(f"search.url: results={len(res) if res else 0}")
                return t, "\n".join(debug_lines)
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
                        return (0 if ("lyric" in title or "lyrics" in title) else 1, len(title))
                    items.sort(key=score)
                t = items[0]
                debug_lines.append(f"search.pick: title='{getattr(t, 'title', 'Unknown')}' length={getattr(t, 'length', 0)}")
                return t, "\n".join(debug_lines)

        debug_lines.append("search: NO_RESULTS (Check Lavalink v4 + YouTube cipher plugin)")
        return None, "\n".join(debug_lines)

    # ---------- events ----------
    @commands.Cog.listener()
    async def on_ready(self):
        # autoconnect executed in cog_load, keep as safety if bot reconnects without reload
        try:
            for g in self.bot.guilds:
                conf = await self.config.guild(g).all()
                if conf["node"]["autoconnect"]:
                    await self._connect_node(g)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload):
        node = payload.node
        for g in self.bot.guilds:
            await self._debug(g, f"node.ready: connected uri={getattr(node, 'uri', 'unknown')} session={getattr(payload, 'session_id', '?')}")
        self._wl_api = self._wl_api or "ready"

    @commands.Cog.listener()
    async def on_wavelink_node_connection_closed(self, payload):
        node = payload.node
        self._last_error = f"closed: code={getattr(payload,'code','?')} reason={getattr(payload,'reason','?')}"
        for g in self.bot.guilds:
            await self._debug(g, f"node.closed: uri={getattr(node,'uri','unknown')} code={getattr(payload,'code','?')} reason={getattr(payload,'reason','?')}")
            conf = await self.config.guild(g).all()
            if conf["node"]["autoconnect"]:
                async def _recon():
                    await asyncio.sleep(5)
                    try:
                        await self._connect_node(g)
                    except Exception:
                        pass
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
        await self._help_full(ctx)

    async def _help_full(self, ctx: redcommands.Context):
        p = ctx.clean_prefix
        desc = (
            f"**AudioPlus (Lavalink v4 / Wavelink 3.x / YouTube)**\n\n"
            f"**Setup**\n"
            f"• `{p}music node set <host> <port> <password> <https>`\n"
            f"• `{p}music node connect` • `{p}music node status` • `{p}music node autoconnect [true|false]`\n"
            f"• `{p}music node ping` • `{p}music debug [true|false]` • `{p}music bind [#channel]`\n"
            f"• `{p}music preferlyrics [true|false]` • `{p}music defaultvolume <0-150>`\n\n"
            f"**Playback**\n"
            f"• `{p}play <query|youtube-url>` (`{p}p`) — lyric-first\n"
            f"• `{p}search <query>` — preview top 5\n"
            f"• `pause` • `resume` • `skip` • `stop` • `seek <mm:ss|hh:mm:ss|sec|+/-sec>` • `volume <0-150>` • `now`\n\n"
            f"**Queue**\n"
            f"• `queue` • `remove <index>` • `clear` • `shuffle` • `repeat <off|one|all>`\n"
        )
        try:
            await ctx.send(embed=discord.Embed(title="AudioPlus — Full Help", description=desc, color=discord.Color.blurple()))
        except discord.Forbidden:
            await ctx.send(box(desc, lang="ini"))

    @music.command(name="debug")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def debug_cmd(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).debug())
        await self.config.guild(ctx.guild).debug.set(bool(enabled))
        await ctx.send(f"debug = **{bool(enabled)}**")

    @music.group(name="node")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def nodegrp(self, ctx: redcommands.Context):
        pass

    @nodegrp.command(name="autoconnect")
    async def node_autoconnect(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).node.autoconnect())
        await self.config.guild(ctx.guild).node.autoconnect.set(bool(enabled))
        await ctx.send(f"autoconnect = **{bool(enabled)}**")

    @nodegrp.command(name="set")
    async def node_set(self, ctx: redcommands.Context, host: str, port: int, password: str, https: bool):
        await self.config.guild(ctx.guild).node.set({"host": host, "port": int(port), "password": password, "https": bool(https), "autoconnect": True})
        await ctx.send(box(f"Saved.\nuri={_uri(host, port, https)}\nautoconnect=True", lang="ini"))

    @nodegrp.command(name="connect")
    async def node_connect(self, ctx: redcommands.Context):
        try:
            api = await self._connect_node(ctx.guild)
            await ctx.send(box(f"Connected via {api}", lang="ini"))
        except Exception as e:
            await ctx.send(box(f"Connect failed: {type(e).__name__}: {e}", lang="ini"))

    @nodegrp.command(name="status")
    async def node_status(self, ctx: redcommands.Context):
        conf = await self.config.guild(ctx.guild).node()
        node = self._get_connected_node()
        ver = getattr(wavelink, "__version__", "unknown")
        ap = self._apis()
        uri = _uri(conf["host"], conf["port"], conf["https"]) if conf["host"] else "not set"
        players = getattr(getattr(node, "stats", None), "players", None) if node else None
        lines = [
            f"configured_uri={uri}",
            f"connected={bool(node)}",
            f"wavelink_version={ver}",
            f"apis={ap}",
            f"players={players if players is not None else 'n/a'}",
            f"last_error={self._last_error or 'none'}",
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @nodegrp.command(name="show")
    async def node_show(self, ctx: redcommands.Context):
        g = await self.config.guild(ctx.guild).all()
        uri = _uri(g["node"]["host"], g["node"]["port"], g["node"]["https"]) if g["node"]["host"] else "not set"
        status = await self._wavelink_status()
        await ctx.send(box(f"uri={uri}\nautoconnect={g['node']['autoconnect']}\n{status}", lang="ini"))

    @nodegrp.command(name="ping")
    async def node_ping(self, ctx: redcommands.Context):
        g = await self.config.guild(ctx.guild).all()
        res = await self._ping_http(g["node"]["host"], g["node"]["port"], g["node"]["https"])
        await ctx.send(box(json.dumps(res, indent=2), lang="json"))

    # ----- prefs -----
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
            try: await player.set_volume(value)
            except Exception: pass
        await ctx.send(f"default_volume = **{value}%**")

    # ----- voice/connect -----
    async def _get_player(self, ctx: redcommands.Context, *, connect: bool = True) -> MusicPlayer:
        player = ctx.voice_client
        if isinstance(player, MusicPlayer):
            return player
        if not connect:
            raise redcommands.UserFeedbackCheckFailure("Not connected.")
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise redcommands.UserFeedbackCheckFailure("Join a voice channel first.")
        await self._connect_node(ctx.guild)
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
        try: await player.set_volume(int(max(0, min(150, vol))))
        except Exception: pass
        await self._debug(ctx.guild, f"player.init: text_channel_id={player.text_channel_id} volume={vol}")
        return player

    def _ensure_same_vc(self, ctx: redcommands.Context, player: MusicPlayer):
        if not ctx.author.voice or not ctx.author.voice.channel or ctx.author.voice.channel != player.channel:
            raise redcommands.UserFeedbackCheckFailure("You must be in my voice channel.")

    # ----- playback -----
    @music.command(aliases=["p"])
    async def play(self, ctx: redcommands.Context, *, query: str):
        try:
            player = await self._get_player(ctx, connect=True)
        except redcommands.UserFeedbackCheckFailure as e:
            return await ctx.send(f"Setup failed: {e}")

        track, dbg = await self._search_youtube(ctx.guild, query)
        await self._debug(ctx.guild, dbg)
        if not track:
            hint = "Ensure Lavalink v4 is up, port open, password correct, HTTPS flag matches, and the YouTube cipher plugin is enabled."
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
        prefer_lyrics = await self.config.guild(ctx.guild).prefer_lyrics()
        qlist = [f"{query} lyrics", f"{query} lyric video", query] if prefer_lyrics and not YOUTUBE_URL_RE.search(query) else [query]
        results: List[wavelink.Playable] = []
        dbg: List[str] = [f"search.preview prefer_lyrics={prefer_lyrics}"]
        try:
            for q in qlist:
                res = await wavelink.Playable.search(f"ytsearch:{q}")
                dbg.append(f"q='{q}' -> {len(res) if res else 0}")
                if res:
                    results = list(res)
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
        for t in items: player.queue.put(t)
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
    await bot.add_cog(AudioPlus(bot))
