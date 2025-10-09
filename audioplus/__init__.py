# path: cogs/audioplus/__init__.py
from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Optional, List, Tuple, Iterable, Any, Dict

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
    # Optional: store cipher probe for doctor
    "cipher_url": None,
    "cipher_password": None,
}

def _scheme(https: bool) -> str:
    return "https" if https else "http"

def _uri(host: str, port: int, https: bool) -> str:
    return f"{_scheme(https)}://{host}:{port}"

try:
    import wavelink  # 3.x required
except Exception as e:
    raise ImportError("AudioPlus requires Wavelink 3.x. Install with: pip install -U 'wavelink>=3,<4'") from e


class LoopMode:
    OFF = "off"
    ONE = "one"
    ALL = "all"


class MusicPlayer(wavelink.Player):
    """Per-guild music player with a simple queue and loop modes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue: wavelink.Queue[wavelink.Playable] = wavelink.Queue()
        self.loop: str = LoopMode.OFF
        self.text_channel_id: Optional[int] = None
        self.requester_id: Optional[int] = None  # why: for future features (vote skip, etc.)

    @property
    def text_channel(self) -> Optional[discord.abc.Messageable]:
        if self.text_channel_id and self.guild:
            ch = self.guild.get_channel(self.text_channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                return ch
        return getattr(self.guild, "system_channel", None)

    async def announce(self, content: str) -> None:
        ch = self.text_channel
        if not ch:
            return
        try:
            await ch.send(content)
        except discord.Forbidden:
            pass


class AudioPlus(redcommands.Cog):
    """Lavalink v4 (YouTube) music player for Red — Wavelink 3.x client."""

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0xA71D01, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._wl_api: str = "unset"
        self._last_error: Optional[str] = None
        self._autoconnect_task: Optional[asyncio.Task] = None
        self._did_global_connect: bool = False  # single node connect per process

    # ---------- debug ----------
    async def _debug(self, guild: discord.Guild, msg: str) -> None:
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
            if self._did_global_connect:
                return
            for guild in self.bot.guilds:
                g = await self.config.guild(guild).all()
                if g["node"]["autoconnect"] and g["node"]["host"]:
                    try:
                        await self._connect_node(guild)
                    except Exception as e:
                        self._last_error = f"autoconnect-error: {type(e).__name__}: {e}"
                    finally:
                        self._did_global_connect = True
                        break
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
        try:
            await self._close_all_nodes()
        except Exception:
            pass

    # ---------- WL API helpers ----------
    def _apis(self) -> dict:
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
        return bool(getattr(node, "connected", False) or getattr(node, "is_connected", False))

    def _iter_nodes(self) -> Iterable["wavelink.Node"]:
        seen: set[Any] = set()
        ap = self._apis()

        def _yield(nodes_obj):
            if isinstance(nodes_obj, dict):
                it = nodes_obj.values()
            elif isinstance(nodes_obj, (list, tuple, set)):
                it = nodes_obj
            else:
                return
            for n in it:
                key = getattr(n, "uri", None) or getattr(n, "rest_uri", None) or id(n)
                if key in seen:
                    continue
                seen.add(key)
                yield n

        if ap["has_Pool"] and ap["has_Pool_nodes"]:
            try:
                yield from _yield(getattr(wavelink.Pool, "nodes"))  # type: ignore[attr-defined]
            except Exception:
                pass
        if ap["has_NodePool"] and ap["has_NodePool_nodes"]:
            try:
                yield from _yield(getattr(wavelink.NodePool, "nodes"))  # type: ignore[attr-defined]
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

    async def _remove_node_from_registries(self, n: "wavelink.Node") -> None:
        try:
            close = getattr(n, "close", None) or getattr(n, "disconnect", None) or getattr(n, "destroy", None)
            if asyncio.iscoroutinefunction(close):
                await close()  # type: ignore[misc]
            elif callable(close):
                close()  # type: ignore[misc]
        except Exception:
            pass
        ident = getattr(n, "identifier", None)
        for obj_name in ("Pool", "NodePool"):
            try:
                cls = getattr(wavelink, obj_name, None)
                store = getattr(cls, "nodes", None)
                if isinstance(store, dict):
                    if ident:
                        store.pop(ident, None)
                    for k, v in list(store.items()):
                        if v is n:
                            store.pop(k, None)
                elif isinstance(store, (list, set)):
                    if n in store:
                        store.remove(n)
            except Exception:
                pass

    async def _close_all_nodes(self) -> int:
        cnt = 0
        for n in list(self._iter_nodes()):
            await self._remove_node_from_registries(n)
            cnt += 1
        return cnt

    async def _reconnect_node(self, guild: discord.Guild, n: "wavelink.Node") -> bool:
        try:
            await self._debug(guild, f"node.reconnect identifier={getattr(n,'identifier','?')}")
            try:
                await n.connect(client=self.bot)  # WL 3.4.x+
            except TypeError:
                await n.connect(self.bot)
            return True
        except Exception as e:
            await self._debug(guild, f"node.reconnect failed: {type(e).__name__}: {e}")
            return False

    async def _connect_node(self, guild: discord.Guild) -> str:
        nodes_existing = list(self._iter_nodes())
        if nodes_existing and any(self._node_connected(n) for n in nodes_existing):
            self._wl_api = self._wl_api or "already-connected"
            self._last_error = None
            return self._wl_api

        if nodes_existing:
            ok = False
            for n in nodes_existing:
                if not self._node_connected(n):
                    ok = await self._reconnect_node(guild, n)
                    if ok:
                        break
            if ok:
                self._wl_api = "Node.connect(reuse)"
                self._last_error = None
                return self._wl_api
            for n in nodes_existing:
                await self._remove_node_from_registries(n)

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

    # ---------- HTTP helpers ----------
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

    async def _fetch_json(self, url: str, *, password: Optional[str] = None, timeout_s: int = 6) -> Tuple[Optional[dict], Optional[str]]:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        headers = {"Authorization": password} if password else None
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url, headers=headers) as resp:
                    if resp.status >= 400:
                        return None, f"HTTP {resp.status}"
                    ct = resp.headers.get("content-type", "")
                    text = await resp.text()
                    if "json" not in ct and not text.strip().startswith("{"):
                        # why: lavalink may not set CT
                        try:
                            data = json.loads(text)
                        except Exception:
                            return None, "Non-JSON body"
                        return data, None
                    return await resp.json(content_type=None), None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    # ---------- search ----------
    async def _search_best(self, guild: discord.Guild, query: str) -> Tuple[Optional[wavelink.Playable], str]:
        prefer_lyrics = await self.config.guild(guild).prefer_lyrics()
        dbg: List[str] = [f"search prefer_lyrics={prefer_lyrics} raw='{query}'"]

        if YOUTUBE_URL_RE.search(query):
            try:
                res = await wavelink.Playable.search(query)
                n = len(res) if res else 0
                dbg.append(f"url results={n}")
            except Exception as e:
                dbg.append(f"url ERROR {type(e).__name__}: {e}")
                return None, "\n".join(dbg)
            return (res[0] if res else None), "\n".join(dbg)

        qlist = [f"{query} lyrics", f"{query} lyric video", query] if prefer_lyrics else [query]
        for q in qlist:
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
        reason = str(getattr(payload, "reason", "") or "").lower()
        try:
            if player.loop == LoopMode.ONE and reason == "finished":
                await player.play(payload.track)
                return
            if player.loop == LoopMode.ALL and reason == "finished":
                try:
                    if payload.track:
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

    @commands.Cog.listener())
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        player: MusicPlayer = payload.player  # type: ignore[assignment]
        if not isinstance(player, MusicPlayer):
            return
        await player.announce("⚠️ Track errored; skipping.")
        try:
            if not player.queue.is_empty:
                nxt = player.queue.get()
                await player.play(nxt)
                await player.announce(f"▶️ Now playing: **{getattr(nxt, 'title', 'Unknown')}**")
            else:
                await player.stop()
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload):
        player: MusicPlayer = payload.player  # type: ignore[assignment]
        if not isinstance(player, MusicPlayer):
            return
        await player.announce("⚠️ Track stuck; skipping.")
        try:
            if not player.queue.is_empty:
                nxt = player.queue.get()
                await player.play(nxt)
                await player.announce(f"▶️ Now playing: **{getattr(nxt, 'title', 'Unknown')}**")
            else:
                await player.stop()
        except Exception:
            pass

    # ---------- commands: help ----------
    @redcommands.group(name="music", invoke_without_command=True)
    @redcommands.guild_only()
    async def music(self, ctx: redcommands.Context):
        p = ctx.clean_prefix
        desc = (
            f"**AudioPlus (Lavalink v4 / Wavelink 3.x / YouTube)**\n\n"
            f"**Node**  `{p}music node set <host> <port> <password> <https>` • `node connect` • `node reset` • `node status` • `node ping`\n"
            f"          `node autoconnect [true|false]`\n"
            f"**Prefs** `music preferlyrics [true|false]` • `music defaultvolume <0-150>` • `music debug [true|false]` • "
            f"`music bind [#channel]`\n"
            f"**Play**  `play <query|url>` (`p`) • `search <query>` • `pause` • `resume` • `skip` • `stop` • `seek <mm:ss|sec|+/-sec>`\n"
            f"**Queue** `queue` • `remove <index>` • `clear` • `shuffle` • `repeat <off|one|all>` • `now`\n"
            f"**Dev**   `music doctor [cipher_url] [cipher_password]` • `music selftest [playback:false] [query:\"test audio\"]` • `music tests [verbose:false]`\n"
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

    @nodegrp.command(name="reset")
    async def node_reset(self, ctx: redcommands.Context, reconnect: Optional[bool] = True):
        n = await self._close_all_nodes()
        self._wl_api = "unset"
        self._last_error = None
        msg = [f"Cleared {n} nodes."]
        if reconnect:
            try:
                api = await self._connect_node(ctx.guild)
                msg.append(f"Reconnected via {api}.")
            except Exception as e:
                msg.append(f"Reconnect failed: {type(e).__name__}: {e}")
        await ctx.send(box(" ".join(msg), lang="ini"))

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
    def _ensure_same_vc(self, ctx: redcommands.Context, player: MusicPlayer) -> None:
        if not ctx.author.voice or not ctx.author.voice.channel or ctx.author.voice.channel != player.channel:
            raise redcommands.UserFeedbackCheckFailure("You must be in my voice channel.")

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

    # ---------- seek helpers ----------
    @staticmethod
    def _parse_seek_string(current_ms: int, s: str) -> Optional[int]:
        s = (s or "").strip()
        if not s:
            return None
        if s.startswith(("+", "-")):
            try:
                return max(0, current_ms + int(s) * 1000)
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

    @music.command()
    async def seek(self, ctx: redcommands.Context, position: str):
        p = await self._get_player(ctx, connect=False)
        self._ensure_same_vc(ctx, p)

        ms = self._parse_seek_string(p.position or 0, position)
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

    # ---------- DEV: doctor ----------
    @music.command(name="doctor", aliases=["preflight"])
    @redcommands.guild_only()
    async def music_doctor(self, ctx: redcommands.Context, cipher_url: Optional[str] = None, cipher_password: Optional[str] = None):
        g = await self.config.guild(ctx.guild).all()
        node = g["node"]
        if not node["host"]:
            return await ctx.send(box("No node configured. Run: [p]music node set <host> <port> <password> <https>", lang="ini"))

        base = _uri(node["host"], node["port"], node["https"])
        info_url = f"{base}/v4/info"
        stats_url = f"{base}/v4/stats"
        version_url = f"{base}/version"
        pw = node["password"]

        lines: List[str] = []
        # basic pings
        ping = await self._ping_http(node["host"], node["port"], node["https"], password=pw)
        lines.append(f"HTTP /version = {ping['version'] and ping['version']['status']}")
        lines.append(f"HTTP /v4/info = {ping['info'] and ping['info']['status']}")
        lines.append(f"HTTP /v4/stats = {ping['stats'] and ping['stats']['status']}")

        # parse JSON
        info, e1 = await self._fetch_json(info_url, password=pw)
        stats, e2 = await self._fetch_json(stats_url, password=pw)
        if e1:
            lines.append(f"info.json error: {e1}")
        if e2:
            lines.append(f"stats.json error: {e2}")

        # plugins / youtube plugin presence
        plugin_names: List[str] = []
        if isinstance(info, dict):
            # try both shapes
            plugins = info.get("plugins") or []
            if isinstance(plugins, list):
                for p in plugins:
                    name = p.get("name") or p.get("artifact") or p.get("id")
                    if name:
                        plugin_names.append(str(name))
        yt_present = any("youtube" in s.lower() for s in plugin_names)
        lines.append(f"plugins: {', '.join(plugin_names) if plugin_names else 'unknown'}")
        lines.append(f"youtube_plugin_present={yt_present}")

        # wavelink connectivity + search test
        try:
            await self._connect_node(ctx.guild)
            wl_ok = True
        except Exception as e:
            wl_ok = False
            lines.append(f"wavelink_connect_error={type(e).__name__}: {e}")

        search_ok = False
        if wl_ok:
            try:
                res = await wavelink.Playable.search("ytsearch:test audio")
                search_ok = bool(res and len(res) > 0)
            except Exception as e:
                lines.append(f"search_error={type(e).__name__}: {e}")
        lines.append(f"wavelink_search_ok={search_ok}")

        # cipher probe (optional)
        if not cipher_url:
            cipher_url = g.get("cipher_url")
        if not cipher_password:
            cipher_password = g.get("cipher_password")

        if cipher_url:
            try:
                # try GET /health first, else GET /
                health_url = cipher_url.rstrip("/") + "/health"
                data, err = await self._fetch_json(health_url, password=cipher_password)
                if err:
                    # fallback to plain GET root
                    data2, err2 = await self._fetch_json(cipher_url, password=cipher_password)
                    if err2:
                        lines.append(f"remote_cipher_unreachable: {err2}")
                    else:
                        lines.append(f"remote_cipher_ok (root)")
                else:
                    lines.append("remote_cipher_ok (/health)")
            except Exception as e:
                lines.append(f"remote_cipher_error={type(e).__name__}: {e}")
        else:
            lines.append("remote_cipher_skipped (no url provided)")

        # stats summary
        if isinstance(stats, dict):
            cpu = stats.get("cpu", {})
            mem = stats.get("memory", {})
            pl = stats.get("players", 0)
            pl_act = stats.get("playingPlayers", 0) or stats.get("playing_players", 0)
            lines.append(f"stats players={pl} playing={pl_act} cpu_cores={cpu.get('cores')} mem_used={mem.get('used')}/{mem.get('allocated')}")

        await ctx.send(box("\n".join(lines), lang="ini"))

    # ---------- DEV: selftest ----------
    @music.command(name="selftest")
    @redcommands.guild_only()
    async def music_selftest(self, ctx: redcommands.Context, playback: Optional[bool] = False, *, query: str = "test audio"):
        out: List[str] = []
        # node + search
        try:
            await self._connect_node(ctx.guild)
            out.append("node_connect=ok")
        except Exception as e:
            out.append(f"node_connect=ERROR {type(e).__name__}: {e}")
            return await ctx.send(box("\n".join(out), lang="ini"))
        try:
            res = await wavelink.Playable.search(f"ytsearch:{query}")
            out.append(f"search_ok={bool(res)} count={len(res) if res else 0}")
        except Exception as e:
            out.append(f"search_ok=ERROR {type(e).__name__}: {e}")
            return await ctx.send(box("\n".join(out), lang="ini"))

        # optional playback
        if playback:
            try:
                player = await self._get_player(ctx, connect=True)
            except redcommands.UserFeedbackCheckFailure as e:
                out.append(f"playback_setup=ERROR {e}")
                return await ctx.send(box("\n".join(out), lang="ini"))
            if not res:
                out.append("playback_skipped=no search results")
                return await ctx.send(box("\n".join(out), lang="ini"))
            t = res[0]
            ready = await self._await_voice_ready(player)
            if not ready:
                out.append("playback_error=voice handshake not ready")
                return await ctx.send(box("\n".join(out), lang="ini"))
            try:
                await player.play(t)
                await asyncio.sleep(2.0)  # why: verify play starts
                await player.stop()
                out.append(f"playback=ok track='{getattr(t,'title','Unknown')}'")
            except Exception as e:
                out.append(f"playback=ERROR {type(e).__name__}: {e}")

        await ctx.send(box("\n".join(out), lang="ini"))

    # ---------- DEV: tests (unit-like) ----------
    @music.command(name="tests")
    @redcommands.guild_only()
    async def music_tests(self, ctx: redcommands.Context, verbose: Optional[bool] = False):
        """Runs in-process unit-like checks without touching Lavalink."""
        fails: List[str] = []
        notes: List[str] = []

        # seek parsing
        def chk_seek(cur, s, exp):
            got = self._parse_seek_string(cur, s)
            if got != exp:
                fails.append(f"seek '{s}' cur={cur} -> {got}, expected {exp}")

        chk_seek(0, "90", 90000)
        chk_seek(0, "1:30", 90000)
        chk_seek(0, "01:02:03", (1*3600+2*60+3)*1000)
        chk_seek(30_000, "+10", 40_000)
        chk_seek(5_000, "-10", 0)

        # queue + loop behavior (mock)
        class MockTrack:
            def __init__(self, title): self.title = title
        class MockPlayer:
            def __init__(self):
                self.queue = wavelink.Queue()
                self.loop = LoopMode.OFF
                self.played: List[str] = []
                self._current: Optional[MockTrack] = None
                self.channel = object()
                self.guild = type("G", (), {"me": type("M", (), {"voice": type("V", (), {"channel": object()})()})()})()
            @property
            def current(self): return self._current
            async def play(self, t): 
                self._current = t
                self.played.append(t.title)
            async def stop(self): self._current = None
            async def announce(self, _): pass

        mp = MockPlayer()
        t1,t2,t3 = MockTrack("A"), MockTrack("B"), MockTrack("C")
        mp.queue.put(t2); mp.queue.put(t3)
        # simulate end with loop off -> plays next
        payload = type("P", (), {"player": mp, "track": t1, "reason": "finished"})()
        try:
            await self.on_wavelink_track_end(payload)  # type: ignore[misc]
            if mp.played != ["B"]:
                fails.append(f"loop_off expected ['B'] got {mp.played}")
        except Exception as e:
            fails.append(f"event_loop_off raised {type(e).__name__}: {e}")

        # loop all -> requeue finished
        mp2 = MockPlayer(); mp2.loop = LoopMode.ALL
        mp2.queue.put(t2)
        payload2 = type("P", (), {"player": mp2, "track": t1, "reason": "finished"})()
        try:
            await self.on_wavelink_track_end(payload2)  # type: ignore[misc]
            # B should play, and A requeued for later
            if "B" not in mp2.played:
                fails.append("loop_all did not play next")
            if len(mp2.queue) == 0:
                fails.append("loop_all did not requeue finished track")
        except Exception as e:
            fails.append(f"event_loop_all raised {type(e).__name__}: {e}")

        # search scoring (pure)
        def score_title(title: str) -> Tuple[int, int]:
            t = title.lower()
            return (0 if ("lyric" in t or "lyrics" in t) else 1, len(t))
        if score_title("Song (Lyrics)") >= score_title("Song"):
            pass
        else:
            fails.append("lyrics scoring order incorrect")

        status = "ok" if not fails else "FAILED"
        lines = [f"tests={status}", f"fail_count={len(fails)}"]
        if verbose or fails:
            lines += (["--- FAILS ---"] + fails) if fails else (["--- NOTES ---"] + notes)
        await ctx.send(box("\n".join(lines), lang="ini"))


async def setup(bot: Red) -> None:
    await bot.add_cog(AudioPlus(bot))
