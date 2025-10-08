# path: cogs/audioplus/__init__.py
from __future__ import annotations
import asyncio
import json
from typing import Optional, List

import discord
from discord.ext import commands

import aiohttp
from redbot.core import Config, commands as redcommands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box

# ---- Defaults
DEFAULTS_GUILD = {
    "node": {
        "host": "",
        "port": 2333,
        "password": "youshallnotpass",
        "https": False,
        "autoconnect": True,
    },
    "prefer_lyrics": True,
    "default_volume": 80,
}

def _scheme(https: bool) -> str:
    return "https" if https else "http"

def _uri(host: str, port: int, https: bool) -> str:
    # Lavalink wants an explicit scheme in Wavelink 3.x
    return f"{_scheme(https)}://{host}:{port}"

class AudioPlus(redcommands.Cog):
    """
    AudioPlus (Lavalink v4 + Wavelink 3.x, YouTube-only)
    Admin:
      • [p]music node set <host> <port> <password> <https>
      • [p]music node show | connect | ping | autoconnect [true|false]
      • [p]music preferlyrics [true|false] | defaultvolume <0-150>
      • [p]music diag  — deep probe & hints
    User:
      • [p]music search <query>  — show top 5 (no enqueue)
    """

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0xAUDIOP10, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._wl_api: str = "unset"
        self._last_error: Optional[str] = None
        self._autoconnect_task: Optional[asyncio.Task] = None

    # ------------- Lifecyle -------------
    async def cog_load(self) -> None:
        # Optional autoconnect on load
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

    # ------------- Internals -------------
    async def _ping_http(self, host: str, port: int, https: bool) -> dict:
        uri_v = f"{_uri(host, port, https)}/version"
        uri_info = f"{_uri(host, port, https)}/v4/info"
        out = {"version": None, "info": None, "errors": []}
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            # /version (no auth)
            try:
                async with sess.get(uri_v) as resp:
                    out["version"] = {"status": resp.status, "body": (await resp.text())[:200]}
            except Exception as e:
                out["errors"].append(f"/version {type(e).__name__}: {e}")
            # /v4/info (may or may not require auth depending on LL; try without)
            try:
                async with sess.get(uri_info) as resp:
                    snippet = (await resp.text())[:200]
                    out["info"] = {"status": resp.status, "body": snippet}
            except Exception as e:
                out["errors"].append(f"/v4/info {type(e).__name__}: {e}")
        return out

    async def _connect_node(self, guild: discord.Guild) -> str:
        # Prefer Wavelink 3.x Pool API; fallback to NodePool if present
        try:
            import wavelink  # type: ignore
        except Exception as e:
            self._last_error = f"import wavelink failed: {type(e).__name__}: {e}"
            raise

        g = await self.config.guild(guild).all()
        host, port, pw, https = g["node"]["host"], g["node"]["port"], g["node"]["password"], g["node"]["https"]
        if not host:
            raise RuntimeError("Node host is empty. Run [p]music node set first.")

        uri = _uri(host, port, https)

        # Try Pool.connect (WL 2/3 style)
        try:
            Node = getattr(wavelink, "Node")
            Pool = getattr(wavelink, "Pool")
            node = Node(uri=uri, password=pw)
            # Some WL builds need secure kw; if present, pass it
            if "secure" in node.__init__.__code__.co_varnames:  # type: ignore[attr-defined]
                node = Node(uri=uri, password=pw, secure=https)
            await Pool.connect(client=self.bot, nodes=[node])
            self._wl_api = "Pool.connect(Node)"
            return self._wl_api
        except AttributeError:
            pass  # Fallthrough to NodePool
        except Exception as e:
            self._last_error = f"Pool.connect failed: {type(e).__name__}: {e}"
            raise

        # Fallback: NodePool.create_node (older WL)
        try:
            NodePool = getattr(wavelink, "NodePool")
            await NodePool.create_node(bot=self.bot, host=host, port=port, password=pw, https=https)
            self._wl_api = "NodePool.create_node"
            return self._wl_api
        except AttributeError as e:
            self._last_error = "Unsupported wavelink APIs (no Pool/NodePool). Install Wavelink 3.x."
            raise RuntimeError(self._last_error) from e
        except Exception as e:
            self._last_error = f"NodePool.create_node failed: {type(e).__name__}: {e}"
            raise

    async def _wavelink_status(self) -> str:
        try:
            import wavelink  # type: ignore
        except Exception as e:
            return f"import-error: {type(e).__name__}: {e}"

        parts = []
        ver = getattr(wavelink, "__version__", "unknown")
        parts.append(f"wavelink_version={ver}")
        # Try to introspect Pool/NodePool
        try:
            Pool = getattr(wavelink, "Pool", None)
            npool = getattr(wavelink, "NodePool", None)
            if Pool and hasattr(Pool, "nodes"):
                nodes = getattr(Pool, "nodes")
                parts.append(f"pool_nodes={len(nodes) if isinstance(nodes, (list, tuple, dict, set)) else 'unknown'}")
            elif npool and hasattr(npool, "nodes"):
                nodes = getattr(npool, "nodes")
                parts.append(f"nodepool_nodes={len(nodes) if isinstance(nodes, (list, tuple, dict, set)) else 'unknown'}")
        except Exception:
            parts.append("pool_introspection=error")

        if self._wl_api != "unset":
            parts.append(f"api={self._wl_api}")
        if self._last_error:
            parts.append(f"last_error={self._last_error}")
        return " ".join(parts)

    # ------------- Commands -------------
    @redcommands.group(name="music", invoke_without_command=True)
    @redcommands.guild_only()
    async def music(self, ctx: redcommands.Context):
        """AudioPlus: Lavalink v4 + Wavelink 3.x (YouTube-only)"""
        g = await self.config.guild(ctx.guild).all()
        uri = _uri(g["node"]["host"], g["node"]["port"], g["node"]["https"]) if g["node"]["host"] else "not set"
        lines = [
            f"Node: uri={uri} pw={'set' if g['node']['password'] else 'unset'} autoconnect={g['node']['autoconnect']}",
            f"Prefs: prefer_lyrics={g['prefer_lyrics']} default_volume={g['default_volume']}",
            await self._wavelink_status(),
            "",
            "Help:",
            f"• {ctx.clean_prefix}music node set <host> <port> <password> <https>",
            f"• {ctx.clean_prefix}music node connect | ping | show | autoconnect [true|false]",
            f"• {ctx.clean_prefix}music search <query>",
            f"• {ctx.clean_prefix}music diag",
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @music.group(name="node")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def node_grp(self, ctx: redcommands.Context):
        """Node config & connectivity."""
        ...

    @node_grp.command(name="set")
    async def node_set(self, ctx: redcommands.Context, host: str, port: int, password: str, https: bool):
        await self.config.guild(ctx.guild).node.set(
            {"host": host, "port": int(port), "password": password, "https": bool(https), "autoconnect": True}
        )
        uri = _uri(host, port, https)
        await ctx.send(box(f"Saved.\nuri={uri}\nautoconnect=True", lang="ini"))

    @node_grp.command(name="autoconnect")
    async def node_autoc(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).node.autoconnect())
        await self.config.guild(ctx.guild).node.autoconnect.set(bool(enabled))
        await ctx.tick()

    @node_grp.command(name="connect")
    async def node_connect(self, ctx: redcommands.Context):
        g = await self.config.guild(ctx.guild).all()
        try:
            api = await self._connect_node(ctx.guild)
            await ctx.send(box(f"Connected via {api}\nuri={_uri(g['node']['host'], g['node']['port'], g['node']['https'])}", lang="ini"))
        except Exception as e:
            await ctx.send(box(f"Connect failed: {type(e).__name__}: {e}", lang="ini"))

    @node_grp.command(name="ping")
    async def node_ping(self, ctx: redcommands.Context):
        g = await self.config.guild(ctx.guild).all()
        res = await self._ping_http(g["node"]["host"], g["node"]["port"], g["node"]["https"])
        await ctx.send(box(json.dumps(res, indent=2), lang="json"))

    @node_grp.command(name="show")
    async def node_show(self, ctx: redcommands.Context):
        g = await self.config.guild(ctx.guild).all()
        uri = _uri(g["node"]["host"], g["node"]["port"], g["node"]["https"]) if g["node"]["host"] else "not set"
        status = await self._wavelink_status()
        await ctx.send(box(f"uri={uri}\nautoconnect={g['node']['autoconnect']}\n{status}", lang="ini"))

    @music.command(name="preferlyrics")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def prefer_lyrics(self, ctx: redcommands.Context, enabled: bool):
        await self.config.guild(ctx.guild).prefer_lyrics.set(bool(enabled)); await ctx.tick()

    @music.command(name="defaultvolume")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def default_volume(self, ctx: redcommands.Context, value: int):
        await self.config.guild(ctx.guild).default_volume.set(int(max(0, min(150, value)))); await ctx.tick()

    @music.command(name="search")
    async def music_search(self, ctx: redcommands.Context, *, query: str):
        # Show top 5 candidates (no enqueue)
        try:
            import wavelink  # type: ignore
        except Exception as e:
            return await ctx.send(box(f"wavelink import failed: {type(e).__name__}: {e}", lang="ini"))

        # Hint if no nodes connected
        wl_status = await self._wavelink_status()
        if "pool_nodes=0" in wl_status or "nodepool_nodes=0" in wl_status:
            await ctx.send(box("No connected nodes. Run: [p]music node connect", lang="ini"))
            return

        try:
            # Wavelink 3.x path
            tracks = await wavelink.Playable.search(query)  # YouTube by default
            if not tracks:
                await ctx.send("No results.")
                return
            lines = []
            for idx, t in enumerate(tracks[:5], 1):
                title = getattr(t, "title", "unknown")
                length = getattr(t, "length", None)
                dur = f"{int(length // 60000)}:{int((length % 60000)/1000):02d}" if isinstance(length, (int, float)) else "?"
                lines.append(f"{idx}. {title} [{dur}]")
            await ctx.send(box("\n".join(lines), lang="ini"))
        except Exception as e:
            await ctx.send(box(f"search failed: {type(e).__name__}: {e}", lang="ini"))

    @music.command(name="diag")
    async def music_diag(self, ctx: redcommands.Context):
        """
        Connectivity probe: versions, HTTP ping, Pool/NodePool availability, search sanity.
        """
        g = await self.config.guild(ctx.guild).all()
        lines: List[str] = []
        # import + version
        try:
            import wavelink  # type: ignore
            wl_ver = getattr(wavelink, "__version__", "unknown")
            apis = ["Pool" if hasattr(wavelink, "Pool") else "-",
                    "Node" if hasattr(wavelink, "Node") else "-",
                    "NodePool" if hasattr(wavelink, "NodePool") else "-"]
            lines.append(f"wavelink={wl_ver} apis={','.join(a for a in apis if a!='-')}")
        except Exception as e:
            lines.append(f"wavelink import FAILED: {type(e).__name__}: {e}")
            return await ctx.send(box("\n".join(lines), lang="ini"))

        # HTTP ping
        ping = await self._ping_http(g["node"]["host"], g["node"]["port"], g["node"]["https"])
        ok_http = bool(ping["version"]) and isinstance(ping["version"].get("status"), int)
        lines.append(f"http_ping={'OK' if ok_http else 'FAIL'} {ping['version']}")
        if ping["errors"]:
            lines.append("http_errors=" + "; ".join(ping["errors"]))

        # Try connect (non-fatal if already connected)
        try:
            api = await self._connect_node(ctx.guild)
            lines.append(f"connect={api}")
        except Exception as e:
            lines.append(f"connect FAILED: {type(e).__name__}: {e}")

        # Try a lightweight search probe (no VC)
        try:
            import wavelink  # type: ignore
            _ = await wavelink.Playable.search("lofi beats")  # okay if empty; proves REST/WS ok
            lines.append("search_probe=OK")
        except Exception as e:
            lines.append(f"search_probe FAILED: {type(e).__name__}: {e}")

        # Final status
        lines.append(await self._wavelink_status())
        lines.append("Hints: Ensure Lavalink v4, Java 17+, correct password/port, and URI includes http(s) scheme. See lavalink.dev REST docs (/version, /v4/*).")
        await ctx.send(box("\n".join(lines), lang="ini"))

async def setup(bot: Red) -> None:
    await bot.add_cog(AudioPlus(bot))
