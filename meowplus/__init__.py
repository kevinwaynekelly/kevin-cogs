# path: cogs/meowifier/__init__.py
from __future__ import annotations

import random
import re
import time
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config
from redbot.core.utils.chat_formatting import box

__red_end_user_data_statement__ = (
    "This cog stores per-guild preferences for webhook-based message transformation, including enable flags, "
    "channel selection, a 1-in-N probability, cooldown seconds, per-user overrides, and exemption lists, plus "
    "a mapping of channel->webhook id. It does not store message contents."
)

DEFAULTS_GUILD = {
    "enabled": False,
    "channels": [],            # empty => all text channels
    "one_in": 1000,            # probability is 1 in N (e.g., 1000, 10000)
    "always_meow": True,       # whole-word now->meow
    "cooldown_seconds": 5,     # per-user cooldown
    "user_probs": {},          # {user_id(str): one_in(int)}
    "exempt_roles": [],
    "exempt_users": [],
    "webhooks": {},            # {channel_id(str): webhook_id(int)}
}

OWO_FACES = ["uwu", "owo", ">w<", "^w^", "x3", "~", "nya~", "(⁄˘⁄⁄ ω⁄ ⁄˘⁄)♡"]

class Meowifier(redcommands.Cog):
    """
    Webhook-only meow/owo replacer:
      • Deletes the original message.
      • Re-posts via webhook with the sender’s display name + avatar.
      • Chance = 1 in N (default 1000). Per-user overrides supported.
    """

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0x5E0F1A, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._cooldown: Dict[int, float] = {}          # user_id -> last_ts (why: anti-spam)
        self._wh_cache: Dict[int, discord.Webhook] = {}  # channel_id -> webhook

    # ---------- transforms ----------
    @staticmethod
    def _case_like(src: str, repl: str) -> str:
        if src.isupper(): return repl.upper()
        if src[0].isupper(): return repl.capitalize()
        return repl

    @staticmethod
    def _meow_replace(text: str) -> str:
        pat = re.compile(r"\b(now)\b", re.IGNORECASE)
        return pat.sub(lambda m: Meowifier._case_like(m.group(1), "meow"), text)

    @staticmethod
    def _split_code_segments(text: str) -> List[Tuple[str, bool]]:
        segs: List[Tuple[str, bool]] = []
        pat = re.compile(r"(```[\s\S]*?```|`[^`]*?`)", re.MULTILINE)
        i = 0
        for m in pat.finditer(text):
            if m.start() > i: segs.append((text[i:m.start()], False))
            segs.append((m.group(0), True))
            i = m.end()
        if i < len(text): segs.append((text[i:], False))
        return segs

    @staticmethod
    def _stutter(word: str) -> str:
        return f"{word[0]}-{word}" if len(word) > 2 and word[0].isalpha() else word

    @staticmethod
    def _owoify(text: str) -> str:
        def tr(s: str) -> str:
            s = re.sub(r"[rl]", "w", s)
            s = re.sub(r"[RL]", "W", s)
            s = re.sub(r"n([aeiou])", r"ny\1", s, flags=re.IGNORECASE)
            s = re.sub(r"ove", "uv", s, flags=re.IGNORECASE)
            words = s.split()
            words = [Meowifier._stutter(w) if (w.isalpha() and random.random() < 0.10) else w for w in words]
            s = " ".join(words)
            s = re.sub(r"([.!?])", lambda m: f"{m.group(1)} {random.choice(OWO_FACES)}", s)
            s = re.sub(r"!+", lambda m: m.group(0) + "~", s)
            try:
                if len(s) > 1: s = s[0].lower() + s[1:]
            except Exception:
                pass
            return s

        parts = [seg if is_code else tr(seg) for seg, is_code in Meowifier._split_code_segments(text)]
        return "".join(parts)

    # ---------- gating ----------
    @staticmethod
    def _starts_with_prefixes(text: str, prefixes: List[str]) -> bool:
        for p in prefixes:
            if p and text.startswith(p): return True
        return False

    async def _should_process(self, message: discord.Message) -> bool:
        if not message.guild: return False
        if message.author.bot: return False
        if message.webhook_id: return False   # why: avoid loops on our webhook posts
        g = message.guild
        conf = await self.config.guild(g).all()
        if not conf["enabled"]: return False
        chs: List[int] = conf["channels"]
        if chs and message.channel.id not in chs: return False
        if message.author.id in set(conf["exempt_users"]): return False
        user_roles = {r.id for r in getattr(message.author, "roles", [])}
        if user_roles.intersection(set(conf["exempt_roles"])): return False
        try:
            prefixes = await self.bot.get_valid_prefixes(g)
        except Exception:
            prefixes = []
        if self._starts_with_prefixes(message.content or "", prefixes): return False
        # cooldown
        cd = max(0, int(conf.get("cooldown_seconds", 0)))
        if cd:
            last = self._cooldown.get(message.author.id, 0.0)
            if (time.time() - last) < cd: return False
        return True

    def _one_in(self, member: discord.Member, conf: dict) -> int:
        pmap: dict = conf.get("user_probs", {}) or {}
        val = pmap.get(str(member.id))
        try:
            n = int(val) if val is not None else int(conf["one_in"])
            return max(1, min(n, 1_000_000))
        except Exception:
            return max(1, int(conf["one_in"]))

    # ---------- webhook helpers ----------
    async def _ensure_webhook(self, channel: discord.abc.Messageable) -> Optional[discord.Webhook]:
        t: Optional[discord.Thread] = None
        base_ch: Optional[discord.TextChannel] = None
        if isinstance(channel, discord.Thread):
            t = channel
            base_ch = channel.parent if isinstance(channel.parent, discord.TextChannel) else None
        elif isinstance(channel, discord.TextChannel):
            base_ch = channel
        else:
            return None

        if not base_ch: return None
        if base_ch.id in self._wh_cache:
            return self._wh_cache[base_ch.id]

        conf = await self.config.guild(base_ch.guild).all()
        wh_map: dict = conf.get("webhooks", {}) or {}
        bound_id = int(wh_map.get(str(base_ch.id), 0) or 0)

        webhook: Optional[discord.Webhook] = None
        try:
            wlist = await base_ch.webhooks()
            if bound_id:
                webhook = discord.utils.get(wlist, id=bound_id)
            if webhook is None:
                webhook = discord.utils.find(lambda w: (w.user and w.user.id == base_ch.guild.me.id and w.name == "Meowifier"), wlist)  # type: ignore
            if webhook is None:
                webhook = await base_ch.create_webhook(name="Meowifier", reason="Meowifier webhook mode")
                wh_map[str(base_ch.id)] = int(webhook.id)
                await self.config.guild(base_ch.guild).webhooks.set(wh_map)
        except discord.Forbidden:
            return None

        self._wh_cache[base_ch.id] = webhook
        return webhook

    # ---------- commands ----------
    @redcommands.group(name="meow", invoke_without_command=True)
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def meow(self, ctx: redcommands.Context) -> None:
        g = await self.config.guild(ctx.guild).all()
        chs = g["channels"]
        lines = [
            f"enabled={g['enabled']} one_in=1/{g['one_in']} always_meow={g['always_meow']} cooldown={g['cooldown_seconds']}s",
            f"channels={'all' if not chs else ', '.join(f'<#{c}>' for c in chs)}",
            f"exempt_users={len(g['exempt_users'])} exempt_roles={len(g['exempt_roles'])} user_overrides={len(g['user_probs'])}",
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @meow.command(name="enable")
    async def meow_enable(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        if channel is None:
            await self.config.guild(ctx.guild).enabled.set(True)
            await ctx.send("Meowifier: **enabled** guild-wide.")
        else:
            data = await self.config.guild(ctx.guild).channels()
            if channel.id not in data:
                data.append(channel.id)
                await self.config.guild(ctx.guild).channels.set(data)
            await self.config.guild(ctx.guild).enabled.set(True)
            await ctx.send(f"Meowifier: **enabled** for {channel.mention}.")

    @meow.command(name="disable")
    async def meow_disable(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        if channel is None:
            await self.config.guild(ctx.guild).enabled.set(False)
            await ctx.send("Meowifier: **disabled** guild-wide.")
        else:
            data = await self.config.guild(ctx.guild).channels()
            data = [c for c in data if c != channel.id]
            await self.config.guild(ctx.guild).channels.set(data)
            await ctx.send(f"Meowifier: **disabled** for {channel.mention}.")

    @meow.command(name="onein")
    async def meow_onein(self, ctx: redcommands.Context, n: int) -> None:
        if n < 1 or n > 1_000_000:
            return await ctx.send("Provide N in 1..1,000,000 (probability = 1/N).")
        await self.config.guild(ctx.guild).one_in.set(int(n)); await ctx.tick()

    @meow.command(name="cooldown")
    async def meow_cooldown(self, ctx: redcommands.Context, seconds: int) -> None:
        if seconds < 0 or seconds > 3600:
            return await ctx.send("Cooldown must be 0–3600s.")
        await self.config.guild(ctx.guild).cooldown_seconds.set(int(seconds)); await ctx.tick()

    @meow.command(name="alwaysmeow")
    async def meow_alwaysmeow(self, ctx: redcommands.Context, value: bool) -> None:
        await self.config.guild(ctx.guild).always_meow.set(bool(value)); await ctx.tick()

    @meow.group(name="prob")
    async def meow_prob(self, ctx: redcommands.Context) -> None:
        """Per-user 1-in-N overrides."""
        pass

    @meow_prob.command(name="add")
    async def meow_prob_add(self, ctx: redcommands.Context, member: discord.Member, n: int) -> None:
        if n < 1 or n > 1_000_000:
            return await ctx.send("Provide N in 1..1,000,000 (probability = 1/N).")
        data = await self.config.guild(ctx.guild).user_probs()
        data[str(member.id)] = int(n)
        await self.config.guild(ctx.guild).user_probs.set(data); await ctx.tick()

    @meow_prob.command(name="remove")
    async def meow_prob_remove(self, ctx: redcommands.Context, member: discord.Member) -> None:
        data = await self.config.guild(ctx.guild).user_probs()
        removed = data.pop(str(member.id), None) is not None
        await self.config.guild(ctx.guild).user_probs.set(data)
        await ctx.send("Removed." if removed else "No override was set.")

    @meow_prob.command(name="list")
    async def meow_prob_list(self, ctx: redcommands.Context) -> None:
        data = await self.config.guild(ctx.guild).user_probs()
        if not data: return await ctx.send("No overrides.")
        parts = []
        for uid, n in data.items():
            m = ctx.guild.get_member(int(uid))
            parts.append(f"- {(m.mention if m else uid)}: 1/{n}")
        await ctx.send(box("\n".join(parts), lang="ini"))

    @meow.group(name="exempt")
    async def meow_exempt(self, ctx: redcommands.Context) -> None:
        pass

    @meow_exempt.group(name="role")
    async def meow_exempt_role(self, ctx: redcommands.Context) -> None:
        pass

    @meow_exempt_role.command(name="add")
    async def meow_exempt_role_add(self, ctx: redcommands.Context, role: discord.Role) -> None:
        data = await self.config.guild(ctx.guild).exempt_roles()
        if role.id in data: return await ctx.send("Role already exempt.")
        data.append(role.id); await self.config.guild(ctx.guild).exempt_roles.set(data); await ctx.tick()

    @meow_exempt_role.command(name="remove")
    async def meow_exempt_role_remove(self, ctx: redcommands.Context, role: discord.Role) -> None:
        data = await self.config.guild(ctx.guild).exempt_roles()
        if role.id not in data: return await ctx.send("Role not exempt.")
        data = [r for r in data if r != role.id]; await self.config.guild(ctx.guild).exempt_roles.set(data); await ctx.tick()

    @meow_exempt_role.command(name="list")
    async def meow_exempt_role_list(self, ctx: redcommands.Context) -> None:
        ids = await self.config.guild(ctx.guild).exempt_roles()
        roles = [ctx.guild.get_role(r).mention for r in ids if ctx.guild.get_role(r)] or ["none"]
        await ctx.send("Exempt roles: " + ", ".join(roles))

    @meow_exempt.group(name="user")
    async def meow_exempt_user(self, ctx: redcommands.Context) -> None:
        pass

    @meow_exempt_user.command(name="add")
    async def meow_exempt_user_add(self, ctx: redcommands.Context, member: discord.Member) -> None:
        data = await self.config.guild(ctx.guild).exempt_users()
        if member.id in data: return await ctx.send("User already exempt.")
        data.append(member.id); await self.config.guild(ctx.guild).exempt_users.set(data); await ctx.tick()

    @meow_exempt_user.command(name="remove")
    async def meow_exempt_user_remove(self, ctx: redcommands.Context, member: discord.Member) -> None:
        data = await self.config.guild(ctx.guild).exempt_users()
        if member.id not in data: return await ctx.send("User not exempt.")
        data = [u for u in data if u != member.id]; await self.config.guild(ctx.guild).exempt_users.set(data); await ctx.tick()

    @meow_exempt_user.command(name="list")
    async def meow_exempt_user_list(self, ctx: redcommands.Context) -> None:
        ids = await self.config.guild(ctx.guild).exempt_users()
        names = [ctx.guild.get_member(uid).mention if ctx.guild.get_member(uid) else f"`{uid}`" for uid in ids]
        await ctx.send("Exempt users: " + (", ".join(names) if names else "none"))

    @meow.command(name="preview")
    async def meow_preview(self, ctx: redcommands.Context, *, text: str) -> None:
        g = await self.config.guild(ctx.guild).all()
        s = text
        if g["always_meow"]: s = self._meow_replace(s)
        s2 = self._owoify(s)
        await ctx.send(box(f"MEOW: {s}\nOWO:  {s2}", lang="ini"))

    @meow.command(name="diag")
    async def meow_diag(self, ctx: redcommands.Context) -> None:
        g = await self.config.guild(ctx.guild).all()
        perms = ctx.guild.me.guild_permissions  # type: ignore
        ok_del = perms.manage_messages
        ok_wh = perms.manage_webhooks
        chs = g["channels"]
        lines = [
            f"enabled={g['enabled']} one_in=1/{g['one_in']} always_meow={g['always_meow']} cooldown={g['cooldown_seconds']}s",
            f"channels={'all' if not chs else ', '.join(f'<#{c}>' for c in chs)}",
            f"perm.manage_messages={'OK' if ok_del else 'MISSING'}  perm.manage_webhooks={'OK' if ok_wh else 'MISSING'}",
            f"user overrides={len(g['user_probs'])}  exempts: users={len(g['exempt_users'])} roles={len(g['exempt_roles'])}",
        ]
        # channel-by-channel webhook check hints
        if chs:
            hints: List[str] = []
            for cid in chs:
                ch = ctx.guild.get_channel(cid)
                if isinstance(ch, discord.TextChannel):
                    p = ch.permissions_for(ctx.guild.me)  # type: ignore
                    if not p.manage_webhooks:
                        hints.append(f"- Missing Manage Webhooks in {ch.mention}")
                    if not p.manage_messages:
                        hints.append(f"- Missing Manage Messages in {ch.mention}")
            if hints:
                lines.append("hints:\n" + "\n".join(hints))
        await ctx.send(box("\n".join(lines), lang="ini"))

    # ---------- listener ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not await self._should_process(message): return

        conf = await self.config.guild(message.guild).all()
        # cooldown start
        cd = max(0, int(conf.get("cooldown_seconds", 0)))
        if cd: self._cooldown[message.author.id] = time.time()

        content = message.content or ""
        if conf["always_meow"]:
            content = self._meow_replace(content)

        n = self._one_in(message.author, conf)
        if n <= 1 or random.randrange(n) == 0:
            content = self._owoify(content)

        if (content.strip() == (message.content or "").strip()) and not message.attachments:
            return  # no change and no files to preserve

        webhook = await self._ensure_webhook(message.channel)
        if not webhook:
            return  # why: no webhook perms; silent (diag will reveal)

        # collect attachments (cap)
        files: List[discord.File] = []
        for a in message.attachments[:5]:
            try: files.append(await a.to_file())
            except Exception: pass

        # delete original first to emulate replacement
        try:
            await message.delete()
        except discord.Forbidden:
            return  # why: cannot delete

        username = message.author.display_name[:80]
        avatar_url = message.author.display_avatar.url if message.author.display_avatar else None
        allowed = discord.AllowedMentions.none()

        try:
            if isinstance(message.channel, discord.Thread):
                await webhook.send(
                    content or "(meow)",
                    username=username,
                    avatar_url=avatar_url,
                    thread=message.channel,
                    files=files or None,
                    allowed_mentions=allowed,
                    wait=False,
                )
            else:
                await webhook.send(
                    content or "(meow)",
                    username=username,
                    avatar_url=avatar_url,
                    files=files or None,
                    allowed_mentions=allowed,
                    wait=False,
                )
        except Exception:
            pass


async def setup(bot: Red) -> None:
    await bot.add_cog(Meowifier(bot))
