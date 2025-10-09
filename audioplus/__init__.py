# path: cogs/audioplus/__init__.py
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Optional, List, Tuple, Iterable

import aiohttp
import discord
from discord.ext import commands
from redbot.core import Config, commands as redcommands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box

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


try:
    import wavelink  # 3.x required
except Exception as e:
    raise ImportError("AudioPlus requires Wavelink 3.x. Install: pip install -U 'wavelink>=3,<4'") from e


class LoopMode:
    OFF = "off"
    ONE = "one"
    ALL = "all"


class MusicPlayer(wavelink.Player):
    """Simple queueing player with loop modes."""
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
    """Lavalink v4 (YouTube) music player for Red — WL 3.x client."""

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
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
            # WHY: autoconnect once; with fixed connection check we won't spawn duplicates.
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
        try:
            for vc in list(self.bot.voice_clients):
                if isinstance(vc, MusicPlayer):
                    await vc.disconnect(force=True)
        except Exception:
            pass

    # ---------- WL API helpers ----------
    def _apis(self):
        NP = getattr(wavelink, "NodePool", None)
        PL = getattr(wavelink, "Pool", None)
        return {
            "has_NodePool": bool(NP),
            "has_NodePool_create_node": bool(getattr(NP, "create_node", None)),
            "has_NodePool_nodes": hasattr(NP, "nodes"),
            "has_Pool": bool(PL),
            "has_Pool_connect": bool(getattr(PL, "connect", None)),
            "has_Pool_nodes": hasattr(PL, "nodes"),
        }

    @staticmethod
    def _node_connected(node: "wavelink.Node") -> bool:
        # WHY: Wavelink 3.4.x exposes `connected`; earlier code looked at `is_connected`.
        return bool(getattr(node, "connected", False) or getattr(node, "is_connected", False))

    def _iter_nodes(self) -> Iterable["wavelink.Node"]:
        ap = self._apis()
        if ap["has_Pool"] and ap["has_Pool_nodes"]:
            try:
                nodes = getattr(wavelink.Pool, "nodes")  # type: ignore[attr-defined]
                if isinstance(nodes, dict):
                    yield from nodes.values()
                elif isinstance(nodes, (list, tuple, set)):
                    yield from nodes
            except Exception:
                pass
        if ap["has_NodePool"] and ap["has_NodePool_nodes"]:
            try:
                nodes = getattr(wavelink.NodePool, "nodes")  # type: ignore[attr-defined]
                if isinstance(nodes, dict):
                    yield from nodes.values()
                elif isinstance(nodes, (list, tuple, set)):
                    yield from nodes
            except Exception:
                pass

    def _get_connected_node(self) -> Optional["wavelink.Node"]:
        for n in self._iter_nodes():
            try:
                if self._node_connected(n):
                    return n
            except Exception:
                continue
        return None

    async def _connect_node(self, guild: discord.Guild) -> str:
        # If we already have a connected node, don't create more.
        n_connected = self._get_connected_node()
        if n_connected:
            self._wl_api = self._wl_api or "already-connected"
            self._last_error = None
            return self._wl_api

        # If nodes exist but aren't connected, don't keep piling up duplicates.
        existing_nodes = list(self._iter_nodes())
        if existing_nodes:
            await self._debug(guild, f"node.exists count={len(existing_nodes)} connected=False; skipping duplicate connect")
            self._wl_api = self._wl_api or "existing-nodes"
            return self._wl_api

        g = await self.config.guild(guild).all()
        host, port, pw, https = g["node"]["host"], g["node"]["port"], g["node"]["password"], g["node"]["https"]
        if not host:
            raise RuntimeError("Node host is empty. Run [p]music node set first.")
        uri = _uri(host, port, https)
        await self._debug(guild, f"node.connect wl={getattr(wavelink,'__version__','?')} apis={self._apis()} uri={uri}")

        ap = self._apis()
        if ap["has_Pool"] and ap["has_Pool_connect"]:
            try:
                Node = getattr(wavelink, "Node")
                # WHY: Some builds accept secure=..., others infer from URI.
                try:
                    node = Node(uri=uri, password=pw, secure=https)  # type: ignore[call-arg]
                except TypeError:
                    node = Node(uri=uri, password=pw)  # type: ignore[call-arg]
                await wavelink.Pool.connect(client=self.bot, nodes=[node])  # type: ignore[attr-defined]
                self._wl_api = "Pool.connect(Node)"
                self._last_error = None
                return self._wl_api
            except Exception as e:
                self._last_error = f"Pool.connect failed: {type(e).__name__}: {e}"
                await self._debug(guild, self._last_error)

        if ap["has_NodePool"] and ap["has_NodePool_create_node"]:
            try:
                await wavelink.NodePool.create_node(  # type: ignore[attr-defined]
                    bot=self.bot, host=host, port=port, password=pw, https=https
                )
                self._wl_api = "NodePool.create_node"
                self._last_error = None
                return self._wl_api
            except Exception as e:
                self._last_error = f"NodePool.create_node failed: {type(e).__name__}: {e}"
                await self._debug(guild, self._last_error)

        self._last_error = self._last_error or "Incompatible Wavelink APIs; need WL 3.x."
        raise RuntimeError(self._last_error)

    async def _wavelink_status(self) -> str:
        parts: List[str] = [f"wavelink_version={getattr(wavelink,'__version__','unknown')}"]
        try:
            nodes = list(self._iter_nodes())
            parts.append(f"nodes_total={len(nodes)}")
            parts.append(f"nodes_connected={sum(1 for n in nodes if self._node_connected(n))}")
        except Exception:
            parts.append("nodes_introspection=error")
        if self._wl_api != "unset":
            parts.append(f"api={self._wl_api}")
        if self._last_error:
            parts.append(f"last_error={self._last_error}")
        return " ".join(parts)

    async def _ping_http(self, host: str, port: int, https: bool, password: Optional[str] = None) -> dict:
        base = _uri(host, port, https)
        out = {"version": None, "info": None, "stats": None, "errors": []}
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            try:
                async with sess.get(f"{base}/version") as resp:
                    out["version"] = {"status": resp.status, "body": (await resp.text())[:200]}
            except Exception as e:
                out["errors"].append(f"/version {type(e).__name__}: {e}")
            headers = {"Authorization": password} if password else None
            for path in ("/v4/info", "/v4/stats"):
                try:
                    async with sess.get(f"{base}{path}", headers=headers) as resp:
                        out["info" if path.endswith("info") else "stats"] = {
                            "status": resp.status,
                            "body": (await resp.text())[:200],
                        }
                except Exception as e:
                    out["errors"].append(f"{path} {type(e).__name__}: {e}")
        return out

    # ---------- search ----------
    async def _search_best(self, guild: discord.Guild, query: str) -> Tuple[Optional[wavelink.Playable], str]:
        prefer_lyrics = await self.config.guild(guild).prefer_lyrics()
        dbg: List[str] = [f"search prefer_lyrics={prefer_lyrics} raw='{query}'"]

        if YOUTUBE_URL_RE.search(query):
            try:
                res = await wavelink.Playable.search(query)
            except Exception as e:
                dbg.append(f"url ERROR {type(e).__name__}")
                return None, "\n".join(dbg)
            return (res[0] if res else None), "\n".join(dbg + [f"url results={len(res) if res else 0}"])

        queries = [f"{query} lyrics", f"{query} lyric video", query] if prefer_lyrics else [query]
        for q in queries:
            try:
                res = await wavelink.Playable.search(f"ytsearch:{q}")
            except Exception as e:
                dbg.append(f"q='{q}' ERROR {type(e).__name__}")
                continue
            n = len(res) if res else 0
            dbg.append(f"q='{q}' -> {n}")
            if n:
                items = list(res)
                if prefer_lyrics:
                    def score(t: wavelink.Playable) -> Tuple[int, int]:
                        title = (getattr(t, "title", "") or "").lower()
                        return (0 if ("lyric" in title or "lyrics" in title) else 1, len(title))
                    items.sort(key=score)
                return items[0], "\n".join(dbg)
        dbg.append("NO_RESULTS")
        return None, "\n".join(dbg)

    # ---------- voice readiness ----------
    async def _await_voice_ready(self, player: MusicPlayer, timeout: float = 6.0) -> bool:
        step = 0.25
        for _ in range(int(timeout / step)):
            me_vc = getattr(player.guild.me, "voice", None)
            ok = bool(me_vc and me_vc.channel and player.channel and me_vc.channel.id == player.channel.id)
            if ok and getattr(player, "connected", True):
                return True
            await asyncio.sleep(step)
        return False

    # ---------- events ----------
    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload):
        self._last_error = None
        for g in self.bot.guilds:
            await self._debug(g, f"node.ready session={getattr(payload,'session_id','?')} api={self._wl_api}")

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player: MusicPlayer = payload.player  # type: ignore[assignment]
        if not isinstance(player, MusicPlayer):
            return
        try:
            if player.loop == LoopMode.ONE and payload.reason == "finished":
                await player.play(payload.track)
                return
            if player.loop == LoopMode.ALL and payload.reason == "finished":
                try:
                    player.queue.put(payload.track)
                except Exception:
                    pass
            if not player.queue.is_empty:
                nxt = player.queue.get()
                await player.play(nxt)
                await player.announce(f"▶️ Now playing: **{getattr(nxt, 'title', 'Unknown')}**")
            else:
                await player.stop()
        except Exception:
            await player.announce("❌ Playback error; stopped.")

    # ---------- commands: help ----------
    @redcommands.group(name="music", invoke_without_command=True)
    @redcommands.guild_only()
    async def music(self, ctx: redcommands.Context):
        p = ctx.clean_prefix
        desc = (
            f"**AudioPlus (Lavalink v4 / Wavelink 3.x / YouTube)**\n\n"
            f"**Node**  `{p}music node set <host> <port> <password> <https>` • `node connect` • `node status` • `node ping`\n"
            f"          `node autoconnect [true|false]`\n"
            f"**Prefs** `music preferlyrics [true|false]` • `music defaultvolume <0-150>` • `music debug [true|false]` • "
            f"`music bind [#channel]`\n"
            f"**Play**  `play <query|url>` (`p`) • `search <query>` • `pause` • `resume` • `skip` • `stop` • `seek <mm:ss|sec|+/-sec>`\n"
            f"**Queue** `queue` • `remove <index>` • `clear` • `shuffle` • `repeat <off|one|all>` • `now`\n"
        )
        try:
            await ctx.send(embed=discord.Embed(title="AudioPlus — Help", description=desc, color=discord.Color.blurple()))
        except discord.Forbidden:
            await ctx.send(box(desc, lang="ini"))

    # ---------- prefs ----------
    @music.command()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def bind(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        await self.config.guild(ctx.guild).bind_channel.set(channel.id)
        await ctx.send(f"Bound music messages to {channel.mention}")

    @music.command(name="preferlyrics")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def prefer_lyrics(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).prefer_lyrics())
        await self.config.guild(ctx.guild).prefer_lyrics.set(bool(enabled))
        await ctx.tick()

    @music.command(name="defaultvolume", aliases=["defvol", "voldef"])
    @redcommands.admin_or_permissions(manage_guild=True)
    async def default_volume(self, ctx: redcommands.Context, value: int):
        value = int(max(0, min(150, value)))
        await self.config.guild(ctx.guild).default_volume.set(value)
        player = ctx.voice_client
        if isinstance(player, MusicPlayer):
            try:
                await player.set_volume(value)
            except Exception:
                pass
        await ctx.send(f"default_volume = **{value}%**")

    @music.command()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def debug(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).debug())
        await self.config.guild(ctx.guild).debug.set(bool(enabled))
        await ctx.tick()

    # ---------- node ----------
    @music.group(name="node")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def nodegrp(self, ctx: redcommands.Context):
        ...

    @nodegrp.command(name="set")
    async def node_set(self, ctx: redcommands.Context, host: str, port: int, password: str, https: bool):
        await self.config.guild(ctx.guild).node.set(
            {"host": host, "port": int(port), "password": password, "https": bool(https), "autoconnect": True}
        )
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
        nodes = list(self._iter_nodes())
        connected = [n for n in nodes if self._node_connected(n)]
        players = [vc for vc in self.bot.voice_clients if isinstance(vc, MusicPlayer)]
        lines = [
            f"configured_uri={_uri(conf['host'], conf['port'], conf['https']) if conf['host'] else 'not set'}",
            f"connected={bool(connected)}",
            f"wavelink_version={getattr(wavelink,'__version__','unknown')}",
            f"nodes_total={len(nodes)} nodes_connected={len(connected)}",
            f"players={len(players)}",
            f"api={self._wl_api} last_error={self._last_error or 'none'}",
        ]
        for i, n in enumerate(nodes, 1):
            uri = getattr(n, "uri", None) or getattr(n, "rest_uri", "?")
            lines.append(f"node[{i}]: connected={self._node_connected(n)} uri={uri}")
        await ctx.send(box("\n".join(lines), lang="ini"))

    @nodegrp.command(name="ping")
    async def node_ping(self, ctx: redcommands.Context):
        g = await self.config.guild(ctx.guild).all()
        res = await self._ping_http(g["node"]["host"], g["node"]["port"], g["node"]["https"], password=g["node"]["password"])
        await ctx.send(box(json.dumps(res, indent=2), lang="json"))

    @nodegrp.command(name="autoconnect")
    async def node_autoc(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).node.autoconnect())
        await self.config.guild(ctx.guild).node.autoconnect.set(bool(enabled))
        await ctx.tick()

    # ---------- helpers ----------
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
        bind_id = await self.config.guild(ctx.guild).bind_channel()
        player.text_channel_id = bind_id or ctx.channel.id
        vol = await self.config.guild(ctx.guild).default_volume()
        try:
            await player.set_volume(int(max(0, min(150, vol))))
        except Exception:
            pass
        return player

    def _ensure_same_vc(self, ctx: redcommands.Context, player: MusicPlayer):
        if not ctx.author.voice or not ctx.author.voice.channel or ctx.author.voice.channel != player.channel:
            raise redcommands.UserFeedbackCheckFailure("You must be in my voice channel.")

    # ---------- playback ----------
    @music.command(aliases=["p"])
    async def play(self, ctx: redcommands.Context, *, query: str):
        try:
            player = await self._get_player(ctx, connect=True)
        except redcommands.UserFeedbackCheckFailure as e:
            return await ctx.send(f"Setup failed: {e}")

        ready = await self._await_voice_ready(player, timeout=6.0)
        if not ready:
            return await ctx.send(box("Voice not ready (handshake not finished). Try again in a second.", lang="ini"))

        track, dbg = await self._search_best(ctx.guild, query)
        await self._debug(ctx.guild, dbg)
        if not track:
            return await ctx.send(box("Play failed: no results for query.", lang="ini"))

        async def _try_play():
            await player.play(track)
            await player.announce(f"▶️ Now playing: **{getattr(track, 'title', 'Unknown')}**")

        try:
            if not player.playing and not player.paused:
                try:
                    await _try_play()
                except Exception:
                    await asyncio.sleep(1.0)
                    await _try_play()
            else:
                player.queue.put(track)
                await ctx.send(f"➕ Queued: **{getattr(track, 'title', 'Unknown')}**")
        except Exception as e:
            gv = getattr(ctx.guild.me, "voice", None)
            node = self._get_connected_node()
            diag = [
                f"Play failed: {type(e).__name__}",
                f"state=playing:{player.playing} paused:{player.paused}",
                f"vc_bot={'yes' if (gv and gv.channel) else 'no'} vc_id={getattr(getattr(gv,'channel',None),'id',None)}",
                f"player_channel_id={getattr(getattr(player,'channel',None),'id',None)}",
                f"node_connected={bool(node and self._node_connected(node))} api={self._wl_api}",
                "hint=Check Connect/Speak perms, Lavalink logs, and YouTube cipher plugin.",
            ]
            await ctx.send(box("\n".join(diag), lang="ini"))

    @music.command()
    async def search(self, ctx: redcommands.Context, *, query: str):
        prefer_lyrics = await self.config.guild(ctx.guild).prefer_lyrics()
        qlist = [f"{query} lyrics", f"{query} lyric video", query] if prefer_lyrics and not YOUTUBE_URL_RE.search(query) else [query]
        results: List[wavelink.Playable] = []
        try:
            for q in qlist:
                res = await wavelink.Playable.search(f"ytsearch:{q}")
                if res:
                    results = list(res)
                    break
        except Exception:
            pass
        if not results:
            return await ctx.send("No YouTube results.")

        def dur(ms: int) -> str:
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
        lines = [f"{i}. {getattr(t,'title','Unknown')} [{dur(getattr(t,'length',0))}]" for i, t in enumerate(top, 1)]
        lines += ["", f"Tip: `{ctx.clean_prefix}play {query}` to enqueue the best match."]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @music.command()
    async def pause(self, ctx: redcommands.Context):
        p = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, p)
        await p.pause(True)
        await ctx.tick()

    @music.command()
    async def resume(self, ctx: redcommands.Context):
        p = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, p)
        await p.pause(False)
        await ctx.tick()

    @music.command()
    async def stop(self, ctx: redcommands.Context):
        p = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, p)
        p.queue.reset()
        await p.stop()
        await ctx.tick()

    @music.command()
    async def skip(self, ctx: redcommands.Context):
        p = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, p)
        await p.stop()
        await ctx.tick()

    @music.command()
    async def seek(self, ctx: redcommands.Context, position: str):
        p = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, p)

        def parse(s: str) -> Optional[int]:
            s = s.strip()
            if s.startswith(("+", "-")):
                try:
                    return max(0, (p.position or 0) + int(s) * 1000)
                except Exception:
                    return None
            if ":" in s:
                try:
                    parts = [int(x) for x in s.split(":")]
                    while len(parts) < 2:
                        parts.insert(0, 0)
                    h, m, sec = (0, parts[-2], parts[-1]) if len(parts) == 2 else (parts[-3], parts[-2], parts[-1])
                    return max(0, (h * 3600 + m * 60 + sec) * 1000)
                except Exception:
                    return None
            try:
                return max(0, int(float(s)) * 1000)
            except Exception:
                return None

        ms = parse(position)
        if ms is None:
            return await ctx.send("Use `mm:ss`, `hh:mm:ss`, or seconds (supports +/-).")
        try:
            await p.seek(ms)
            await ctx.tick()
        except Exception:
            await ctx.send("Seek failed.")

    @music.command(aliases=["vol"])
    async def volume(self, ctx: redcommands.Context, value: int):
        p = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, p)
        value = int(max(0, min(150, value)))
        await p.set_volume(value)
        await ctx.send(f"Volume **{value}%**")

    @music.command()
    async def now(self, ctx: redcommands.Context):
        p = await self._get_player(ctx, connect=False)
        t = p.current
        if not t:
            return await ctx.send("Nothing playing.")

        def fmt(seconds: int) -> str:
            seconds = max(0, seconds)
            m, s = divmod(seconds, 60)
            h, m = divmod(m, 60)
            return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

        pos = int((p.position or 0) / 1000)
        dur = int((getattr(t, "length", 0) or 0) / 1000)
        await ctx.send(f"🎵 **{getattr(t,'title','Unknown')}** [{fmt(pos)}/{fmt(dur)}]")

    @music.command()
    async def queue(self, ctx: redcommands.Context):
        p = await self._get_player(ctx, connect=False)
        if p.queue.is_empty:
            return await ctx.send("Queue is empty.")
        items = list(p.queue)[:15]
        lines = [f"{i:>2}. {getattr(t,'title','Unknown')}" for i, t in enumerate(items, 1)]
        if len(p.queue) > 15:
            lines.append(f"... and {len(p.queue) - 15} more")
        await ctx.send(box("\n".join(lines), lang="ini"))

    @music.command()
    async def remove(self, ctx: redcommands.Context, index: int):
        p = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, p)
        if index < 1 or index > len(p.queue):
            return await ctx.send("Index out of range.")
        items = list(p.queue)
        removed = items.pop(index - 1)
        p.queue.clear()
        for it in items:
            p.queue.put(it)
        await ctx.send(f"Removed **{getattr(removed,'title','Unknown')}**")

    @music.command()
    async def clear(self, ctx: redcommands.Context):
        p = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, p)
        p.queue.reset()
        await p.stop()
        await ctx.tick()

    @music.command()
    async def shuffle(self, ctx: redcommands.Context):
        p = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, p)
        items = list(p.queue)
        if not items:
            return await ctx.send("Queue is empty.")
        random.shuffle(items)
        p.queue.clear()
        for it in items:
            p.queue.put(it)
        await ctx.send("Shuffled queue.")

    @music.command()
    async def repeat(self, ctx: redcommands.Context, mode: str):
        p = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, p)
        mode = mode.lower()
        if mode not in {LoopMode.OFF, LoopMode.ONE, LoopMode.ALL}:
            return await ctx.send("Use: `repeat off|one|all`")
        p.loop = mode
        await ctx.send(f"Repeat mode: **{mode}**")


async def setup(bot: Red) -> None:
    await bot.add_cog(AudioPlus(bot))
