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
    "channel selection, a 1-in-N probability, cooldown seconds, per-user overrides, and exemption lists. "
    "It does not store message contents."
)

DEFAULTS_GUILD = {
    "enabled": False,
    "channels": [],            # empty => all text channels
    "one_in": 1000,            # probability is 1 in N (e.g., 1000, 10000)
    "cooldown_seconds": 5,     # per-user cooldown
    "user_probs": {},          # {user_id(str): one_in(int)}
    "exempt_roles": [],
    "exempt_users": [],
}

OWO_FACES = ["uwu", "owo", ">w<", "^w^", "x3", "~", "nya~", "(⁄˘⁄⁄ ω⁄ ⁄˘⁄)♡"]

class Meowifier(redcommands.Cog):
    """
    Webhook-only meow/owo replacer:
      • Always replace whole-word “now”→“meow”.
      • With probability 1/N (default 1/1000), rewrite to egirl owo.
      • Sends via webhook (mimic user), then deletes original.
    """

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0x5E0F1A, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._cooldown: Dict[int, float] = {}            # user_id -> last_ts (why: anti-spam)
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
        return "".join(seg if is_code else tr(seg) for seg, is_code in Meowifier._split_code_segments(text))

    # ---------- gating ----------
    @staticmethod
    def _starts_with_prefixes(text: str, prefixes: List[str]) -> bool:
        for p in prefixes:
            if p and text.startswith(p): return True
        return False

    async def _should_process(self, message: discord.Message) -> bool:
        if not message.guild or message.author.bot or message.webhook_id: return False
        conf = await self.config.guild(message.guild).all()
        if not conf["enabled"]: return False
        chs: List[int] = conf["channels"]
        if chs and message.channel.id not in chs: return False
        if message.author.id in set(conf["exempt_users"]): return False
        user_roles = {r.id for r in getattr(message.author, "roles", [])}
        if user_roles.intersection(set(conf["exempt_roles"])): return False
        try:
            prefixes = await self.bot.get_valid_prefixes(message.guild)
        except Exception:
            prefixes = []
        if self._starts_with_prefixes(message.content or "", prefixes): return False
        cd = max(0, int(conf.get("cooldown_seconds", 0)))
        if cd and (time.time() - self._cooldown.get(message.author.id, 0.0)) < cd: return False
        return True

    def _one_in(self, member: discord.Member, conf: dict) -> int:
        pmap: dict = conf.get("user_probs", {}) or {}
        try:
            n = int(pmap.get(str(member.id))) if str(member.id) in pmap else int(conf["one_in"])
            return max(1, min(n, 1_000_000))
        except Exception:
            return max(1, int(conf["one_in"]))

    # ---------- webhooks ----------
    async def _ensure_webhook(self, channel: discord.abc.Messageable) -> Optional[discord.Webhook]:
        base_ch: Optional[discord.TextChannel] = None
        if isinstance(channel, discord.Thread):
            base_ch = channel.parent if isinstance(channel.parent, discord.TextChannel) else None
        elif isinstance(channel, discord.TextChannel):
            base_ch = channel
        else:
            return None
        if not base_ch: return None
        hook = self._wh_cache.get(base_ch.id)
        if hook: return hook
        try:
            hooks = await base_ch.webhooks()
            hook = discord.utils.find(
                lambda w: (w.user and w.user.id == base_ch.guild.me.id) or w.name == "Meowifier",  # type: ignore
                hooks,
            )
            if hook is None:
                hook = await base_ch.create_webhook(name="Meowifier", reason="Meowifier webhook mode")
        except discord.Forbidden:
            return None
        except Exception:
            return None
        self._wh_cache[base_ch.id] = hook
        return hook

    # ---------- commands ----------
    @redcommands.group(name="meow", invoke_without_command=True)
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def meow(self, ctx: redcommands.Context) -> None:
        g = await self.config.guild(ctx.guild).all()
        chs = g["channels"]
        lines = [
            f"enabled={g['enabled']} one_in=1/{g['one_in']} cooldown={g['cooldown_seconds']}s",
            f"channels={'all' if not chs else ', '.join(f'<#{c}>' for c in chs)}",
            f"exempt_users={len(g['exempt_users'])} exempt_roles={len(g['exempt_roles'])} user_overrides={len(g['user_probs'])}",
            "mode=webhook-only • always-meow=True",
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @meow.command(name="help")
    async def meow_help(self, ctx: redcommands.Context, section: Optional[str] = None) -> None:
        p = ctx.clean_prefix
        sections = {
            "quickstart": (
                f"1) Grant **Manage Messages** + **Manage Webhooks**.\n"
                f"2) `{p}meow enable` (or `{p}meow enable #channel`).\n"
                f"3) `{p}meow onein 1000` (rarity). `now`→`meow` is always on.\n"
                f"4) Type a normal message, then `{p}meow test`."
            ),
            "channels": (
                f"`{p}meow channels add [#channel]`\n"
                f"`{p}meow channels remove [#channel]`\n"
                f"`{p}meow channels list`\n"
                f"`{p}meow channels clear`  (empty list = all)"
            ),
            "config": (
                f"`{p}meow` • `{p}meow enable/disable [#channel]`\n"
                f"`{p}meow onein <N>` • `{p}meow cooldown <seconds>`\n"
                f"`{p}meow prob add @user <N>` • `remove` • `list`\n"
                f"`{p}meow exempt role|user add/remove/list`\n"
                f"`{p}meow preview <text>` • `{p}meow diag` • `{p}meow test`"
            ),
            "notes": (
                "• Webhook send first; delete original only after success.\n"
                "• Skips bots, webhooks, commands; respects cooldown/exempts.\n"
                "• Owoify ignores code blocks/backticks."
            ),
        }
        if section and section.lower() in sections:
            e = discord.Embed(title=f"Meowifier — {section.title()}", description=sections[section.lower()], color=discord.Color.blurple())
            return await ctx.send(embed=e)
        e = discord.Embed(title="Meowifier — Help", color=discord.Color.blurple())
        e.add_field(name="Quickstart", value=sections["quickstart"], inline=False)
        e.add_field(name="Channels", value=sections["channels"], inline=False)
        e.add_field(name="Config", value=sections["config"], inline=False)
        e.add_field(name="Notes", value=sections["notes"], inline=False)
        await ctx.send(embed=e)

    @meow.group(name="channels")
    async def meow_channels(self, ctx: redcommands.Context) -> None:
        pass

    @meow_channels.command(name="add")
    async def meow_channels_add(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        ch = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        if not isinstance(ch, discord.TextChannel): return await ctx.send("Pick a text channel.")
        data = await self.config.guild(ctx.guild).channels()
        if ch.id in data: return await ctx.send(f"{ch.mention} already in the list.")
        data.append(ch.id); await self.config.guild(ctx.guild).channels.set(data)
        await ctx.send(f"Added {ch.mention}. Target list now: {', '.join(f'<#{c}>' for c in data)}")

    @meow_channels.command(name="remove")
    async def meow_channels_remove(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        ch = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        if not isinstance(ch, discord.TextChannel): return await ctx.send("Pick a text channel.")
        data = await self.config.guild(ctx.guild).channels()
        if ch.id not in data: return await ctx.send(f"{ch.mention} was not in the list.")
        data = [c for c in data if c != ch.id]; await self.config.guild(ctx.guild).channels.set(data)
        await ctx.send(f"Removed {ch.mention}. Target list now: {('all channels' if not data else ', '.join(f'<#{c}>' for c in data))}")

    @meow_channels.command(name="clear")
    async def meow_channels_clear(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).channels.set([])
        await ctx.send("Cleared channel list — now active in **all channels**.")

    @meow_channels.command(name="list")
    async def meow_channels_list(self, ctx: redcommands.Context) -> None:
        data = await self.config.guild(ctx.guild).channels()
        if not data: return await ctx.send("Channels: **all**")
        await ctx.send("Channels: " + ", ".join(f"<#{c}>" for c in data))

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
        if n < 1 or n > 1_000_000: return await ctx.send("Provide N in 1..1,000,000 (probability = 1/N).")
        await self.config.guild(ctx.guild).one_in.set(int(n)); await ctx.tick()

    @meow.command(name="cooldown")
    async def meow_cooldown(self, ctx: redcommands.Context, seconds: int) -> None:
        if seconds < 0 or seconds > 3600: return await ctx.send("Cooldown must be 0–3600s.")
        await self.config.guild(ctx.guild).cooldown_seconds.set(int(seconds)); await ctx.tick()

    @meow.group(name="prob")
    async def meow_prob(self, ctx: redcommands.Context) -> None:
        """Per-user 1-in-N overrides."""
        pass

    @meow_prob.command(name="add")
    async def meow_prob_add(self, ctx: redcommands.Context, member: discord.Member, n: int) -> None:
        if n < 1 or n > 1_000_000: return await ctx.send("Provide N in 1..1,000,000 (probability = 1/N).")
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
        s = self._meow_replace(text)            # always meow
        s2 = self._owoify(s)                     # sample owo
        await ctx.send(box(f"MEOW: {s}\nOWO:  {s2}", lang="ini"))

    @meow.command(name="diag")
    async def meow_diag(self, ctx: redcommands.Context) -> None:
        g = await self.config.guild(ctx.guild).all()
        perms = ctx.guild.me.guild_permissions  # type: ignore
        chs = g["channels"]
        lines = [
            f"enabled={g['enabled']} one_in=1/{g['one_in']} cooldown={g['cooldown_seconds']}s",
            f"channels={'all' if not chs else ', '.join(f'<#{c}>' for c in chs)}",
            f"perm.manage_messages={'OK' if perms.manage_messages else 'MISSING'}  perm.manage_webhooks={'OK' if perms.manage_webhooks else 'MISSING'}",
            f"user overrides={len(g['user_probs'])}  exempts: users={len(g['exempt_users'])} roles={len(g['exempt_roles'])}",
            "note: always-meow=True; owo is chanced.",
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @meow.command(name="test")
    async def meow_test(self, ctx: redcommands.Context) -> None:
        """Force-owoify your last message here via webhook (sanity check)."""
        try:
            prefixes = await self.bot.get_valid_prefixes(ctx.guild)
        except Exception:
            prefixes = []
        last: Optional[discord.Message] = None
        async for m in ctx.channel.history(limit=50, before=ctx.message.created_at):  # type: ignore
            if m.author.id == ctx.author.id and not m.author.bot and not m.webhook_id and not self._starts_with_prefixes(m.content or "", prefixes):
                last = m
                break
        if not last: return await ctx.send("No recent message of yours found here to test.")
        hook = await self._ensure_webhook(ctx.channel)
        if not hook: return await ctx.send("Missing webhook permissions in this channel.")
        content = self._owoify(self._meow_replace(last.content or ""))  # always meow, force owo
        files: List[discord.File] = []
        for a in last.attachments[:5]:
            try: files.append(await a.to_file())
            except Exception: pass
        allowed = discord.AllowedMentions.none()
        try:
            if isinstance(ctx.channel, discord.Thread):
                await hook.send(content or "(meow)", username=last.author.display_name[:80], avatar_url=last.author.display_avatar.url, thread=ctx.channel, files=files or None, allowed_mentions=allowed, wait=False)
            else:
                await hook.send(content or "(meow)", username=last.author.display_name[:80], avatar_url=last.author.display_avatar.url, files=files or None, allowed_mentions=allowed, wait=False)
        except Exception:
            return await ctx.send("Webhook send failed. Check permissions.")
        try:
            await last.delete()
        except discord.Forbidden:
            return await ctx.send("Sent via webhook, but couldn't delete your original message (need Manage Messages).")
        await ctx.tick()

    # ---------- listener ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not await self._should_process(message): return
        conf = await self.config.guild(message.guild).all()
        cd = max(0, int(conf.get("cooldown_seconds", 0)))
        if cd: self._cooldown[message.author.id] = time.time()

        # Always meow; owo is chanced
        content = self._meow_replace(message.content or "")
        n = self._one_in(message.author, conf)
        if n <= 1 or random.randrange(n) == 0:
            content = self._owoify(content)

        if (content.strip() == (message.content or "").strip()) and not message.attachments: return

        hook = await self._ensure_webhook(message.channel)
        if not hook: return

        files: List[discord.File] = []
        for a in message.attachments[:5]:
            try: files.append(await a.to_file())
            except Exception: pass

        allowed = discord.AllowedMentions.none()
        try:
            if isinstance(message.channel, discord.Thread):
                await hook.send(content or "(meow)", username=message.author.display_name[:80], avatar_url=message.author.display_avatar.url, thread=message.channel, files=files or None, allowed_mentions=allowed, wait=False)
            else:
                await hook.send(content or "(meow)", username=message.author.display_name[:80], avatar_url=message.author.display_avatar.url, files=files or None, allowed_mentions=allowed, wait=False)
        except Exception:
            self._wh_cache.pop(getattr(message.channel, "id", 0), None)  # refresh next time
            return
        try:
            await message.delete()
        except discord.Forbidden:
            pass


async def setup(bot: Red) -> None:
    await bot.add_cog(Meowifier(bot))
