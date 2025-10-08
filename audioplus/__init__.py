# path: cogs/audioplus/__init__.py
from __future__ import annotations

import re
from typing import Optional, List, Tuple

import discord
from discord.ext import commands
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config
from redbot.core.utils.chat_formatting import box

try:
    import wavelink  # Requires Wavelink 3.x (compatible with Lavalink v4)
except Exception as e:
    raise ImportError(
        "audioplus requires Wavelink 3.x (Lavalink v4).\n"
        "Install with your Red venv: pip install -U 'wavelink>=3,<4'\n"
        "or via Red: [p]pipinstall wavelink==3.*"
    ) from e

YOUTUBE_URL_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE)

DEFAULTS_GUILD = {
    "node": {"host": "", "port": 2333, "password": "youshallnotpass", "https": False, "resume_key": None},
    "bind_channel": None,
    "default_volume": 60,
    "prefer_lyrics": True,
}

class LoopMode:
    OFF = "off"
    ONE = "one"
    ALL = "all"

class MusicPlayer(wavelink.Player):
    """Guild-scoped player with queue/loop state."""
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
    """Lavalink v4 YouTube-only player (lyric-first) with queue/seek/repeat, help/diag, and probe."""

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        # FIX: use a valid hex literal (A–F, 0–9). 0xA71D01 is arbitrary but stable.
        self.config: Config = Config.get_conf(self, identifier=0xA71D01, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._node_ready: bool = False

    # ---------- node mgmt ----------
    async def _connect_node(self, guild: Optional[discord.Guild] = None) -> Optional[wavelink.Node]:
        """Return a connected node if available; otherwise try to create one from guild config."""
        try:
            if wavelink.NodePool.nodes:
                node = wavelink.NodePool.get_node()
                if node and node.is_connected:
                    self._node_ready = True
                    return node
        except Exception:
            pass

        targets = [guild] if guild else list(self.bot.guilds)
        for g in targets:
            conf = await self.config.guild(g).node()
            if not conf["host"]:
                continue
            try:
                node = await wavelink.NodePool.create_node(
                    bot=self.bot,
                    host=conf["host"],
                    port=int(conf["port"]),
                    password=conf["password"],
                    https=bool(conf["https"]),
                    resume_key=conf.get("resume_key"),
                )
                self._node_ready = True
                return node
            except Exception:
                continue
        return None

    # ---------- helpers ----------
    async def _get_player(self, ctx: redcommands.Context, *, connect: bool = True) -> MusicPlayer:
        player = ctx.voice_client
        if isinstance(player, MusicPlayer):
            return player
        if not connect:
            raise redcommands.UserFeedbackCheckFailure("Not connected.")
        if not ctx.author.voice or not ctx.author.voice.channel:
            raise redcommands.UserFeedbackCheckFailure("Join a voice channel first.")
        node = await self._connect_node(ctx.guild)
        if not node:
            raise redcommands.UserFeedbackCheckFailure("Node not configured/connected. Run `music node set` then `music diag`.")
        channel = ctx.author.voice.channel
        player = await channel.connect(cls=MusicPlayer)  # type: ignore[arg-type]
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

    async def _search_youtube(self, guild: discord.Guild, query: str) -> Optional[wavelink.Playable]:
        """YouTube-only search; prefer lyric videos by title."""
        prefer_lyrics = await self.config.guild(guild).prefer_lyrics()

        async def _search(q: str) -> List[wavelink.Playable]:
            try:
                res = await wavelink.Playable.search(f"ytsearch:{q}")
                return list(res) if res else []
            except Exception:
                return []

        # URL path: only accept YouTube URLs
        if YOUTUBE_URL_RE.search(query):
            try:
                res = await wavelink.Playable.search(query)
                return res[0] if res else None
            except Exception:
                return None

        # Query path: try lyric-first, then plain
        queries = [f"{query} lyrics", f"{query} lyric video", query] if prefer_lyrics else [query]
        candidates: List[wavelink.Playable] = []
        for q in queries:
            items = await _search(q)
            if items:
                candidates.extend(items)
                break
        if not candidates:
            return None

        if prefer_lyrics:
            def score(t: wavelink.Playable) -> Tuple[int, int]:
                title = (getattr(t, "title", "") or "").lower()
                has = 0 if ("lyric" in title or "lyrics" in title) else 1  # why: prefer lyric videos
                return (has, len(title))
            candidates.sort(key=score)

        return candidates[0]

    # ---------- wavelink events ----------
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
            pass

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
            f"• `{p}music node show` • `{p}music diag` • `{p}music probe [voice-channel]`\n"
            f"• `{p}music bind [#channel]` • `{p}music preferlyrics [true|false]` • `{p}music defaultvolume <0-150>`\n\n"
            f"**Voice**\n"
            f"• `{p}join [voice-channel]` • `{p}leave`\n\n"
            f"**Playback**\n"
            f"• `{p}play <query|youtube-url>` (alias: `{p}p`) — prefers lyric videos\n"
            f"• `{p}pause` • `{p}resume` • `{p}skip` • `{p}stop`\n"
            f"• `{p}seek <mm:ss|hh:mm:ss|seconds|+/-seconds>` • `{p}volume <0-150>` • `{p}now`\n\n"
            f"**Queue**\n"
            f"• `{p}queue` • `{p}remove <index>` • `{p}clear` • `{p}shuffle` • `{p}repeat <off|one|all>`\n\n"
            f"*Node must be Lavalink **v4** with a YouTube cipher plugin (server-side).*"
        )
        try:
            await ctx.send(embed=discord.Embed(title="AudioPlus — Full Help", description=desc, color=discord.Color.blurple()))
        except discord.Forbidden:
            await ctx.send(box(desc, lang="ini"))

    @music.command()
    async def diag(self, ctx: redcommands.Context):
        """Run a non-intrusive system check."""
        g = ctx.guild
        me = g.me
        node = await self._connect_node(g)
        node_ok = bool(node and node.is_connected)

        players = getattr(getattr(node, "stats", None), "players", None) if node_ok else None
        mem = getattr(getattr(getattr(node, "stats", None), "memory", None), "used", None) if node_ok else None
        cpu = getattr(getattr(getattr(node, "stats", None), "cpu", None), "system_load", None) if node_ok else None
        ver = None
        for attr in ("version", "client_version"):
            ver = ver or getattr(node, attr, None)

        ch = ctx.channel
        tperm = ch.permissions_for(me) if isinstance(ch, (discord.TextChannel, discord.Thread)) else None
        author_vc = getattr(ctx.author.voice, "channel", None)
        vc_perms = author_vc.permissions_for(me) if author_vc else None

        search_probe = "skip"
        if node_ok:
            try:
                res = await wavelink.Playable.search("ytsearch:lofi hip hop lyrics")
                search_probe = "OK" if res else "EMPTY"
            except Exception as e:
                search_probe = f"FAIL:{type(e).__name__}"

        hints = []
        if not node_ok:
            hints.append("Node not connected: run `music node set` then retry.")
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            if not tperm.send_messages:
                hints.append("Missing Send Messages here.")
            if not tperm.embed_links:
                hints.append("Missing Embed Links here.")
        if author_vc and vc_perms:
            if not vc_perms.connect:
                hints.append(f"Missing Connect in {author_vc.name}.")
            if not vc_perms.speak:
                hints.append(f"Missing Speak in {author_vc.name}.")
        if not author_vc:
            hints.append("Join a voice channel to fully validate VC perms.")

        lines = [
            f"node_connected={node_ok}  version={ver or 'unknown'}",
            f"stats(players={players if players is not None else 'n/a'}, mem_used={mem if mem is not None else 'n/a'}, cpu_load={cpu if cpu is not None else 'n/a'})",
            f"text_perms(send={getattr(tperm,'send_messages',None)}, embed={getattr(tperm,'embed_links',None)})",
            f"author_vc={getattr(author_vc,'name',None)}  vc_perms(connect={getattr(vc_perms,'connect',None)}, speak={getattr(vc_perms,'speak',None)})",
            f"search_probe={search_probe}",
        ]
        if hints:
            lines.append("hints=" + " | ".join(hints))
        await ctx.send(box("\n".join(lines), lang="ini"))

    @music.command()
    async def probe(self, ctx: redcommands.Context, channel: Optional[discord.VoiceChannel] = None):
        """Silent VC connect → validate perms → disconnect. No audio is played."""
        node = await self._connect_node(ctx.guild)
        if not node:
            return await ctx.send("Node not configured/connected. Run `music node set` then `music diag`.")
        target = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if not target:
            return await ctx.send("Join a voice channel or specify one.")

        me = ctx.guild.me
        vperm = target.permissions_for(me)
        pre_connected = isinstance(ctx.voice_client, MusicPlayer)
        created = False

        if not pre_connected:
            try:
                player: MusicPlayer = await target.connect(cls=MusicPlayer)  # type: ignore[arg-type]
                bind_id = await self.config.guild(ctx.guild).bind_channel()
                player.text_channel_id = bind_id or ctx.channel.id
                vol = await self.config.guild(ctx.guild).default_volume()
                try:
                    await player.set_volume(int(max(0, min(150, vol))))
                except Exception:
                    pass
                created = True
            except Exception as e:
                return await ctx.send(f"Connect failed: `{type(e).__name__}`")

        lines = [
            f"target={target.name}",
            f"bot_perms(connect={vperm.connect}, speak={vperm.speak}, move_members={vperm.move_members})",
            f"connected={'yes' if (created or pre_connected) else 'no'}",
        ]
        if created:
            try:
                await ctx.voice_client.disconnect()  # type: ignore[union-attr]
            except Exception:
                pass
        await ctx.send(box("\n".join(lines), lang="ini"))

    # ---------- node config & binding ----------
    @music.group(name="node")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def nodegrp(self, ctx: redcommands.Context): ...

    @nodegrp.command(name="set")
    async def node_set(self, ctx: redcommands.Context, host: str, port: int, password: str, https: Optional[bool] = False):
        await self.config.guild(ctx.guild).node.set({
            "host": host, "port": int(port), "password": password, "https": bool(https), "resume_key": None
        })
        self._node_ready = False
        await ctx.tick()

    @nodegrp.command(name="show")
    async def node_show(self, ctx: redcommands.Context):
        node = await self.config.guild(ctx.guild).node()
        await ctx.send(box(f"host={node['host'] or 'not set'}\nport={node['port']}\nhttps={node['https']}", lang="ini"))

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

    # ---------- voice ----------
    @music.command()
    async def join(self, ctx: redcommands.Context, channel: Optional[discord.VoiceChannel] = None):
        node = await self._connect_node(ctx.guild)
        if not node:
            return await ctx.send("Node not configured/connected. Run `music node set` then `music diag`.")
        target = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if not target:
            return await ctx.send("Join a voice channel or specify one.")
        if ctx.voice_client and isinstance(ctx.voice_client, MusicPlayer):
            if ctx.voice_client.channel == target:
                return await ctx.tick()
            await ctx.voice_client.move_to(target)
            return await ctx.tick()
        player: MusicPlayer = await target.connect(cls=MusicPlayer)  # type: ignore[arg-type]
        bind_id = await self.config.guild(ctx.guild).bind_channel()
        player.text_channel_id = bind_id or ctx.channel.id
        vol = await self.config.guild(ctx.guild).default_volume()
        try:
            await player.set_volume(int(max(0, min(150, vol))))
        except Exception:
            pass
        await ctx.tick()

    @music.command()
    async def leave(self, ctx: redcommands.Context):
        player = ctx.voice_client
        if not isinstance(player, MusicPlayer):
            return await ctx.send("Not connected.")
        self._ensure_same_vc(ctx, player)
        await player.disconnect()
        await ctx.tick()

    # ---------- playback ----------
    @music.command(aliases=["p"])
    async def play(self, ctx: redcommands.Context, *, query: str):
        player = await self._get_player(ctx, connect=True)
        # Only YouTube is allowed; free-form queries are searched on YT
        track = await self._search_youtube(ctx.guild, query)
        if not track:
            return await ctx.send("No YouTube results.")
        player.requester_id = ctx.author.id

        if not player.playing and not player.paused:
            await player.play(track)
            await player.announce(f"▶️ Now playing: **{getattr(track, 'title', 'Unknown')}**")
        else:
            player.queue.put(track)
            await ctx.send(f"➕ Queued: **{getattr(track, 'title', 'Unknown')}**")

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

    # ---------- queue ----------
    @music.command()
    async def queue(self, ctx: redcommands.Context):
        player = await self._get_player(ctx, connect=False)
        if player.queue.is_empty:
            return await ctx.send("Queue is empty.")
        lines = []
        for i, t in enumerate(list(player.queue)[:15], start=1):
            lines.append(f"{i:>2}. {getattr(t, 'title', 'Unknown')}")
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
    await bot.add_cog(AudioPlus(bot))
