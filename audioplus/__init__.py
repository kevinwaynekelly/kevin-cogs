# path: cogs/audioplus/__init__.py
from __future__ import annotations

import asyncio
import json
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

def _scheme(https: bool) -> str: return "https" if https else "http"
def _uri(host: str, port: int, https: bool) -> str: return f"{_scheme(https)}://{host}:{port}"

# Require Wavelink 3.x
try:
    import wavelink  # type: ignore
except Exception as e:
    raise ImportError("AudioPlus needs Wavelink 3.x. pip install -U 'wavelink>=3,<4'") from e


class LoopMode: OFF="off"; ONE="one"; ALL="all"


class MusicPlayer(wavelink.Player):
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
        if not ch: return
        try: await ch.send(content)
        except discord.Forbidden: pass


class AudioPlus(redcommands.Cog):
    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0xA71D01, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._wl_api: str = "unset"
        self._last_error: Optional[str] = None
        self._autoconnect_task: Optional[asyncio.Task] = None

    async def _debug(self, guild: discord.Guild, msg: str):
        if not await self.config.guild(guild).debug(): return
        ch_id = await self.config.guild(guild).bind_channel()
        ch = guild.get_channel(ch_id) if ch_id else getattr(guild, "system_channel", None)
        print(f"[AudioPlus] {guild.name}: {msg}")
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try: await ch.send(box(msg, lang="ini"))
            except Exception: pass

    async def cog_load(self) -> None:
        async def _maybe():
            await self.bot.wait_until_ready()
            for g in self.bot.guilds:
                conf = await self.config.guild(g).all()
                if conf["node"]["autoconnect"]:
                    try: await self._connect_node(g)
                    except Exception as e: self._last_error = f"autoconnect-error: {type(e).__name__}: {e}"
        self._autoconnect_task = asyncio.create_task(_maybe())

    async def cog_unload(self) -> None:
        if self._autoconnect_task: self._autoconnect_task.cancel()
        try:
            for vc in list(self.bot.voice_clients):
                if isinstance(vc, MusicPlayer):
                    await vc.disconnect(force=True)
        except Exception: pass

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

    def _iter_nodes(self) -> Iterable["wavelink.Node"]:
        ap = self._apis()
        if ap["has_Pool"] and ap["has_Pool_nodes"]:
            try:
                nodes = getattr(wavelink.Pool, "nodes")  # type: ignore[attr-defined]
                if isinstance(nodes, dict): yield from nodes.values()
                elif isinstance(nodes, (list, tuple, set)): yield from nodes
            except Exception: pass
        if ap["has_NodePool"] and ap["has_NodePool_nodes"]:
            try:
                nodes = getattr(wavelink.NodePool, "nodes")  # type: ignore[attr-defined]
                if isinstance(nodes, dict): yield from nodes.values()
                elif isinstance(nodes, (list, tuple, set)): yield from nodes
            except Exception: pass

    def _get_connected_node(self) -> Optional["wavelink.Node"]:
        for n in self._iter_nodes():
            try:
                if getattr(n, "is_connected", False): return n
            except Exception: continue
        return None

    async def _connect_node(self, guild: discord.Guild) -> str:
        if self._get_connected_node():
            self._wl_api = self._wl_api or "already-connected"; self._last_error = None; return self._wl_api
        g = await self.config.guild(guild).all()
        host, port, pw, https = g["node"]["host"], g["node"]["port"], g["node"]["password"], g["node"]["https"]
        if not host: raise RuntimeError("Node host is empty. Run [p]music node set first.")
        uri = _uri(host, port, https)
        await self._debug(guild, f"node.connect wl={getattr(wavelink,'__version__','?')} apis={self._apis()} uri={uri}")

        ap = self._apis()
        if ap["has_Pool"] and ap["has_Pool_connect"]:
            try:
                Node = getattr(wavelink, "Node")
                node = Node(uri=uri, password=pw)
                if "secure" in node.__init__.__code__.co_varnames:  # type: ignore[attr-defined]
                    node = Node(uri=uri, password=pw, secure=https)
                await wavelink.Pool.connect(client=self.bot, nodes=[node])  # type: ignore[attr-defined]
                self._wl_api = "Pool.connect(Node)"; self._last_error = None
                return self._wl_api
            except Exception as e:
                self._last_error = f"Pool.connect failed: {type(e).__name__}: {e}"
                await self._debug(guild, self._last_error)
        if ap["has_NodePool"] and ap["has_NodePool_create_node"]:
            try:
                await wavelink.NodePool.create_node(bot=self.bot, host=host, port=port, password=pw, https=https)  # type: ignore[attr-defined]
                self._wl_api = "NodePool.create_node"; self._last_error = None
                return self._wl_api
            except Exception as e:
                self._last_error = f"NodePool.create_node failed: {type(e).__name__}: {e}"
                await self._debug(guild, self._last_error)
        self._last_error = self._last_error or "Incompatible Wavelink APIs; need WL 3.x."
        raise RuntimeError(self._last_error)

    async def _wavelink_status(self) -> str:
        parts = [f"wavelink_version={getattr(wavelink,'__version__','unknown')}"]
        try:
            nodes = list(self._iter_nodes())
            parts.append(f"nodes_total={len(nodes)}")
            parts.append(f"nodes_connected={sum(1 for n in nodes if getattr(n,'is_connected',False))}")
        except Exception: parts.append("nodes_introspection=error")
        if self._wl_api != "unset": parts.append(f"api={self._wl_api}")
        if self._last_error: parts.append(f"last_error={self._last_error}")
        return " ".join(parts)

    async def _ping_http(self, host: str, port: int, https: bool, password: Optional[str] = None) -> dict:
        base = _uri(host, port, https)
        out = {"version": None, "info": None, "stats": None, "errors": []}
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            try:
                async with sess.get(f"{base}/version") as resp:
                    out["version"] = {"status": resp.status, "body": (await resp.text())[:200]}
            except Exception as e: out["errors"].append(f"/version {type(e).__name__}: {e}")
            headers = {"Authorization": password} if password else None
            for path in ("/v4/info", "/v4/stats"):
                try:
                    async with sess.get(f"{base}{path}", headers=headers) as resp:
                        out["info" if path.endswith("info") else "stats"] = {
                            "status": resp.status, "body": (await resp.text())[:200]
                        }
                except Exception as e:
                    out["errors"].append(f"{path} {type(e).__name__}: {e}")
        return out

    # ---------- search ----------
    async def _search_best(self, guild: discord.Guild, query: str) -> Tuple[Optional[wavelink.Playable], str]:
        prefer_lyrics = await self.config.guild(guild).prefer_lyrics()
        dbg = [f"search prefer_lyrics={prefer_lyrics} raw='{query}'"]
        if YOUTUBE_URL_RE.search(query):
            try:
                res = await wavelink.Playable.search(query)
                return (res[0] if res else None), "\n".join(dbg+[f"url results={len(res) if res else 0}"])
            except Exception as e:
                return None, "\n".join(dbg+[f"url ERROR {type(e).__name__}"])
        queries = [f"{query} lyrics", f"{query} lyric video", query] if prefer_lyrics else [query]
        for q in queries:
            try:
                res = await wavelink.Playable.search(f"ytsearch:{q}")
                n = len(res) if res else 0
                dbg.append(f"q='{q}' -> {n}")
                if n:
                    items = list(res)
                    if prefer_lyrics:
                        def score(t: wavelink.Playable) -> Tuple[int,int]:
                            title = (getattr(t,"title","") or "").lower()
                            return (0 if ("lyric" in title or "lyrics" in title) else 1, len(title))
                        items.sort(key=score)
                    return items[0], "\n".join(dbg)
            except Exception as e:
                dbg.append(f"q='{q}' ERROR {type(e).__name__}")
        return None, "\n".join(dbg+["NO_RESULTS"])

    # ---------- voice readiness ----------
    async def _await_voice_ready(self, player: MusicPlayer, timeout: float = 6.0) -> bool:
        step = 0.25
        for _ in range(int(timeout/step)):
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
        p: MusicPlayer = payload.player  # type: ignore[assignment]
        if not isinstance(p, MusicPlayer): return
        try:
            if p.loop == LoopMode.ONE and payload.reason == "finished":
                await p.play(payload.track); return
            if p.loop == LoopMode.ALL and payload.reason == "finished":
                try: p.queue.put(payload.track)
                except Exception: pass
            if not p.queue.is_empty:
                nxt = p.queue.get()
                await p.play(nxt)
                await p.announce(f"▶️ Now playing: **{getattr(nxt,'title','Unknown')}**")
            else:
                await p.stop()
        except Exception:
            await p.announce("❌ Playback error; stopped.")

    # ---------- commands ----------
    @redcommands.group(name="music", invoke_without_command=True)
    @redcommands.guild_only()
    async def music(self, ctx: redcommands.Context):
        p = ctx.clean_prefix
        desc = (
            f"**AudioPlus (Lavalink v4 / Wavelink 3.x)**\n\n"
            f"**Node**  `{p}music node set <host> <port> <password> <https>` • `node connect` • `node status` • `node ping`\n"
            f"          `node autoconnect [true|false]`\n"
            f"**Prefs** `music preferlyrics [true|false]` • `music defaultvolume <0-150>` • `music debug [true|false]`\n"
            f"**Play**  `play <query|url>` (`p`) • `search <query>` • `pause` • `resume` • `skip` • `stop` • `seek <mm:ss|sec|+/-sec>`\n"
            f"**Queue** `queue` • `remove <index>` • `clear` • `shuffle` • `repeat <off|one|all>` • `now`\n"
        )
        try:
            await ctx.send(embed=discord.Embed(title="AudioPlus — Help", description=desc, color=discord.Color.blurple()))
        except discord.Forbidden:
            await ctx.send(box(desc, lang="ini"))

    @music.group(name="node")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def nodegrp(self, ctx: redcommands.Context): ...

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
        nodes = list(self._iter_nodes())
        connected = [n for n in nodes if getattr(n, "is_connected", False)]
        players = [vc for vc in self.bot.voice_clients if isinstance(vc, MusicPlayer)]
        lines = [
            f"configured_uri={_uri(conf['host'], conf['port'], conf['https']) if conf['host'] else 'not set'}",
            f"connected={bool(connected)}",
            f"wavelink_version={getattr(wavelink,'__version__','unknown')}",
            f"nodes_total={len(nodes)} nodes_connected={len(connected)}",
            f"players={len(players)}",
            f"api={self._wl_api} last_error={self._last_error or 'none'}",
        ]
        for i,n in enumerate(nodes,1):
            lines.append(f"node[{i}]: connected={getattr(n,'is_connected',False)} uri={getattr(n,'uri','?')}")
        await ctx.send(box("\n".join(lines), lang="ini"))

    @nodegrp.command(name="ping")
    async def node_ping(self, ctx: redcommands.Context):
        g = await self.config.guild(ctx.guild).all()
        res = await self._ping_http(g["node"]["host"], g["node"]["port"], g["node"]["https"], password=g["node"]["password"])
        await ctx.send(box(json.dumps(res, indent=2), lang="json"))

    @nodegrp.command(name="autoconnect")
    async def node_autoc(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None: enabled = not (await self.config.guild(ctx.guild).node.autoconnect())
        await self.config.guild(ctx.guild).node.autoconnect.set(bool(enabled)); await ctx.tick()

    @music.command(name="preferlyrics")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def prefer_lyrics(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None: enabled = not (await self.config.guild(ctx.guild).prefer_lyrics())
        await self.config.guild(ctx.guild).prefer_lyrics.set(bool(enabled)); await ctx.tick()

    @music.command(name="defaultvolume", aliases=["defvol","voldef"])
    @redcommands.admin_or_permissions(manage_guild=True)
    async def default_volume(self, ctx: redcommands.Context, value: int):
        value = int(max(0, min(150, value)))
        await self.config.guild(ctx.guild).default_volume.set(value)
        p = ctx.voice_client
        if isinstance(p, MusicPlayer):
            try: await p.set_volume(value)
            except Exception: pass
        await ctx.send(f"default_volume = **{value}%**")

    async def _get_player(self, ctx: redcommands.Context, *, connect: bool = True) -> MusicPlayer:
        p = ctx.voice_client
        if isinstance(p, MusicPlayer): return p
        if not connect: raise redcommands.UserFeedbackCheckFailure("Not connected.")
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise redcommands.UserFeedbackCheckFailure("Join a voice channel first.")
        await self._connect_node(ctx.guild)
        ch = ctx.author.voice.channel
        try:
            p = await ch.connect(cls=MusicPlayer)  # type: ignore[arg-type]
        except discord.Forbidden:
            raise redcommands.UserFeedbackCheckFailure("Missing Connect permission.")
        bind_id = await self.config.guild(ctx.guild).bind_channel()
        p.text_channel_id = bind_id or ctx.channel.id
        vol = await self.config.guild(ctx.guild).default_volume()
        try: await p.set_volume(int(max(0, min(150, vol))))
        except Exception: pass
        return p

    def _ensure_same_vc(self, ctx: redcommands.Context, p: MusicPlayer):
        if not ctx.author.voice or not ctx.author.voice.channel or ctx.author.voice.channel != p.channel:
            raise redcommands.UserFeedbackCheckFailure("You must be in my voice channel.")

    # ---------- playback ----------
    @music.command(aliases=["p"])
    async def play(self, ctx: redcommands.Context, *, query: str):
        try:
            p = await self._get_player(ctx, connect=True)
        except redcommands.UserFeedbackCheckFailure as e:
            return await ctx.send(f"Setup failed: {e}")

        ready = await self._await_voice_ready(p, timeout=6.0)
        if not ready:
            return await ctx.send(box("Voice not ready (handshake not finished). Try again in a second.", lang="ini"))

        track, dbg = await self._search_best(ctx.guild, query)
        await self._debug(ctx.guild, dbg)
        if not track: return await ctx.send(box("Play failed: no results for query.", lang="ini"))

        async def _try_play():
            await p.play(track)
            await p.announce(f"▶️ Now playing: **{getattr(track,'title','Unknown')}**")

        try:
            if not p.playing and not p.paused:
                try:
                    await _try_play()
                except Exception:
                    await asyncio.sleep(1.0)  # why: race with voice handshake; retry once
                    await _try_play()
            else:
                p.queue.put(track)
                await ctx.send(f"➕ Queued: **{getattr(track,'title','Unknown')}**")
        except Exception as e:
            gv = getattr(ctx.guild.me, "voice", None)
            node = self._get_connected_node()
            diag = [
                f"Play failed: {type(e).__name__}",
                f"state=playing:{p.playing} paused:{p.paused}",
                f"vc_bot={'yes' if (gv and gv.channel) else 'no'} vc_id={getattr(gv.channel,'id',None)}",
                f"player_channel_id={getattr(p.channel,'id',None)}",
                f"node_connected={getattr(node,'is_connected',False)} api={self._wl_api}",
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
                if res: results = list(res); break
        except Exception: pass
        if not results: return await ctx.send("No YouTube results.")
        def dur(ms: int) -> str:
            s=int((ms or 0)/1000); m,s=divmod(s,60); h,m=divmod(m,60); return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        if prefer_lyrics:
            def score(t: wavelink.Playable)->Tuple[int,int]:
                title=(getattr(t,'title','') or '').lower()
                return (0 if ('lyric' in title or 'lyrics' in title) else 1, len(title))
            results.sort(key=score)
        top=results[:5]
        lines=[f"{i}. {getattr(t,'title','Unknown')} [{dur(getattr(t,'length',0))}]" for i,t in enumerate(top,1)]
        lines+=["", f"Tip: `{ctx.clean_prefix}play {query}` to enqueue the best match."]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @music.command() async def pause(self, ctx): p=await self._get_player(ctx,connect=False); self._ensure_same_vc(ctx,p); await p.pause(True); await ctx.tick()
    @music.command() async def resume(self, ctx): p=await self._get_player(ctx,connect=False); self._ensure_same_vc(ctx,p); await p.pause(False); await ctx.tick()
    @music.command() async def stop(self, ctx): p=await self._get_player(ctx,connect=False); self._ensure_same_vc(ctx,p); p.queue.reset(); await p.stop(); await ctx.tick()
    @music.command() async def skip(self, ctx): p=await self._get_player(ctx,connect=False); self._ensure_same_vc(ctx,p); await p.stop(); await ctx.tick()

    @music.command()
    async def seek(self, ctx: redcommands.Context, position: str):
        p=await self._get_player(ctx,connect=False); self._ensure_same_vc(ctx,p)
        def parse(s:str)->Optional[int]:
            s=s.strip()
            if s.startswith(("+","-")):
                try: return max(0,(p.position or 0)+int(s)*1000)
                except: return None
            if ":" in s:
                try:
                    parts=[int(x) for x in s.split(":")]
                    while len(parts)<2: parts.insert(0,0)
                    h,m,s=(0,parts[-2],parts[-1]) if len(parts)==2 else (parts[-3],parts[-2],parts[-1])
                    return max(0,(h*3600+m*60+s)*1000)
                except: return None
            try: return max(0,int(float(s))*1000)
            except: return None
        ms=parse(position)
        if ms is None: return await ctx.send("Use `mm:ss`, `hh:mm:ss`, or seconds (supports +/-).")
        try: await p.seek(ms); await ctx.tick()
        except: await ctx.send("Seek failed.")

    @music.command() async def volume(self, ctx, value:int): p=await self._get_player(ctx,connect=False); self._ensure_same_vc(ctx,p); value=int(max(0,min(150,value))); await p.set_volume(value); await ctx.send(f"Volume **{value}%**")
    @music.command() async def now(self, ctx): p=await self._get_player(ctx,connect=False); t=p.current; 
        # compact for brevity
        if not t: return await ctx.send("Nothing playing.")
        pos=int((p.position or 0)/1000); dur=int((getattr(t,"length",0) or 0)/1000)
        fmt=lambda s: (lambda m,s:(lambda h,m:(f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"))(*divmod(m,60)))(*divmod(max(0,s),60))
        await ctx.send(f"🎵 **{getattr(t,'title','Unknown')}** [{fmt(pos)}/{fmt(dur)}]")

    @music.command()
    async def queue(self, ctx: redcommands.Context):
        p=await self._get_player(ctx,connect=False)
        if p.queue.is_empty: return await ctx.send("Queue is empty.")
        items=list(p.queue)[:15]; lines=[f"{i:>2}. {getattr(t,'title','Unknown')}" for i,t in enumerate(items,1)]
        if len(p.queue)>15: lines.append(f"... and {len(p.queue)-15} more")
        await ctx.send(box("\n".join(lines), lang="ini"))

    @music.command()
    async def remove(self, ctx: redcommands.Context, index: int):
        p=await self._get_player(ctx,connect=False); self._ensure_same_vc(ctx,p)
        if index<1 or index>len(p.queue): return await ctx.send("Index out of range.")
        items=list(p.queue); removed=items.pop(index-1); p.queue.clear(); [p.queue.put(t) for t in items]
        await ctx.send(f"Removed **{getattr(removed,'title','Unknown')}**")

    @music.command()
    async def clear(self, ctx: redcommands.Context):
        p=await self._get_player(ctx,connect=False); self._ensure_same_vc(ctx,p); p.queue.reset(); await p.stop(); await ctx.tick()


async def setup(bot: Red) -> None:
    await bot.add_cog(AudioPlus(bot))
