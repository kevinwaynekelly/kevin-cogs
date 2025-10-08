# path: cogs/meowifier/__init__.py
from __future__ import annotations

import difflib
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
    "This cog stores per-guild preferences for webhook-based message transformation (enable flags, channel scope, "
    "1-in-N owo probability, cooldown seconds, per-user overrides, and exemption lists). It does not store message contents."
)

DEFAULTS_GUILD = {
    "enabled": False,
    "channels": [],            # empty => all text channels
    "one_in": 1000,            # owo chance = 1 / N
    "cooldown_seconds": 5,     # per-user cooldown
    "user_probs": {},          # {user_id(str): one_in(int)}
    "exempt_roles": [],
    "exempt_users": [],
}

NOW_WORD = re.compile(r"\b(now)\b", re.IGNORECASE)
MEOW_WORD = re.compile(r"\b(meow)\b", re.IGNORECASE)
CODE_SPLIT = re.compile(r"(```[\s\S]*?```|`[^`]*?`)", re.MULTILINE)

OWO_FACES = ["uwu", "owo", ">w<", "^w^", "x3", "~", "nya~", "(⁄˘⁄⁄ ω⁄ ⁄˘⁄)♡"]

class Meowifier(redcommands.Cog):
    """
    Webhook-only meow/owo replacer:
      • Always replace whole-word “now” → *meow* (case-preserving).
      • With probability 1/N (default 1/1000), owo-ify; only changed parts are italicized.
      • Send via webhook (mimic user), then delete the original on success.
    """

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0x5E0F1A, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._cooldown: Dict[int, float] = {}            # user_id -> last_ts
        self._wh_cache: Dict[int, discord.Webhook] = {}  # base_channel_id -> webhook

    # ---------- transforms & marking ----------
    @staticmethod
    def _case_like(src: str, repl: str) -> str:
        if src.isupper():
            return repl.upper()
        if src[0].isupper():
            return repl.capitalize()
        return repl

    @staticmethod
    def _replace_now(text: str, italic: bool) -> str:
        def repl(m: re.Match) -> str:
            s = Meowifier._case_like(m.group(1), "meow")
            return f"*{s}*" if italic else s
        return NOW_WORD.sub(repl, text)

    @staticmethod
    def _ensure_meow_italic(text: str) -> str:
        # Only wrap bare 'meow' not already surrounded by *
        def repl(m: re.Match) -> str:
            start, end = m.span(1)
            before = text[start - 1] if start - 1 >= 0 else ""
            after = text[end] if end < len(text) else ""
            if before == "*" and after == "*":
                return m.group(0)  # already italic
            return f"*{Meowifier._case_like(m.group(1), 'meow')}*"
        # Run using a callable that peeks; re.sub can't change the same string in-place reliably,
        # so we build incrementally with SequenceMatcher to be safe.
        parts: List[str] = []
        i = 0
        for m in MEOW_WORD.finditer(text):
            start, end = m.span()
            parts.append(text[i:start])
            # same logic as above:
            s_start, s_end = m.span(1)
            before = text[s_start - 1] if s_start - 1 >= 0 else ""
            after = text[s_end] if s_end < len(text) else ""
            if before == "*" and after == "*":
                parts.append(m.group(0))
            else:
                parts.append(f"*{Meowifier._case_like(m.group(1), 'meow')}*")
            i = end
        parts.append(text[i:])
        return "".join(parts)

    @staticmethod
    def _split_code_segments(text: str) -> List[Tuple[str, bool]]:
        segs: List[Tuple[str, bool]] = []
        i = 0
        for m in CODE_SPLIT.finditer(text):
            if m.start() > i:
                segs.append((text[i:m.start()], False))
            segs.append((m.group(0), True))
            i = m.end()
        if i < len(text):
            segs.append((text[i:], False))
        return segs

    @staticmethod
    def _stutter(word: str) -> str:
        return f"{word[0]}-{word}" if len(word) > 2 and word[0].isalpha() else word

    @staticmethod
    def _owoify_plain(text: str) -> str:
        # identical to before, but without italics logic
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
                if len(s) > 1:
                    s = s[0].lower() + s[1:]
            except Exception:
                pass
            return s
        return "".join(seg if is_code else tr(seg) for seg, is_code in Meowifier._split_code_segments(text))

    @staticmethod
    def _italicize_changes(original: str, transformed: str) -> str:
        """Mark only changed spans with *…* using a char-level diff (outside code blocks)."""
        out: List[str] = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, original, transformed).get_opcodes():
            if tag == "equal":
                out.append(transformed[j1:j2])
            else:  # replace/insert/delete
                # Only transformed text is visible; delete shows as nothing on output
                seg = transformed[j1:j2]
                if seg:
                    out.append(f"*{seg}*")
        return "".join(out)

    def _render_message(self, raw: str, apply_owo: bool) -> str:
        """Process text with per-segment handling and italics on changes."""
        result: List[str] = []
        for seg, is_code in self._split_code_segments(raw):
            if is_code:
                result.append(seg)
                continue
            if not apply_owo:
                # just now->*meow*
                result.append(self._replace_now(seg, italic=True))
                continue
            # owo path: compute meow-plain, then diff italics, then ensure meow italic
            meow_plain = self._replace_now(seg, italic=False)
            owo = self._owoify_plain(meow_plain)
            marked = self._italicize_changes(meow_plain, owo)
            marked = self._ensure_meow_italic(marked)
            result.append(marked)
        return "".join(result)

    # ---------- gating ----------
    @staticmethod
    def _starts_with_prefixes(text: str, prefixes: List[str]) -> bool:
        for p in prefixes:
            if p and text.startswith(p):
                return True
        return False

    async def _should_process(self, message: discord.Message) -> bool:
        if not message.guild or message.author.bot or message.webhook_id:
            return False
        conf = await self.config.guild(message.guild).all()
        if not conf["enabled"]:
            return False
        chs: List[int] = conf["channels"]
        if chs and message.channel.id not in chs:
            return False
        if message.author.id in set(conf["exempt_users"]):
            return False
        if {r.id for r in getattr(message.author, "roles", [])}.intersection(set(conf["exempt_roles"])):
            return False
        try:
            prefixes = await self.bot.get_valid_prefixes(message.guild)
        except Exception:
            prefixes = []
        if self._starts_with_prefixes(message.content or "", prefixes):
            return False
        cd = max(0, int(conf.get("cooldown_seconds", 0)))
        if cd and (time.time() - self._cooldown.get(message.author.id, 0.0)) < cd:
            return False
        return True

    def _one_in(self, member: discord.Member, conf: dict) -> int:
        pmap: dict = conf.get("user_probs", {}) or {}
        try:
            n = int(pmap.get(str(member.id))) if str(member.id) in pmap else int(conf["one_in"])
            return max(1, min(n, 1_000_000))
        except Exception:
            return max(1, int(conf["one_in"]))

    # ---------- webhook helpers ----------
    async def _ensure_webhook(self, channel: discord.abc.Messageable) -> Optional[discord.Webhook]:
        base_ch: Optional[discord.TextChannel] = None
        if isinstance(channel, discord.Thread):
            base_ch = channel.parent if isinstance(channel.parent, discord.TextChannel) else None
        elif isinstance(channel, discord.TextChannel):
            base_ch = channel
        else:
            return None
        if not base_ch:
            return None

        if base_ch.id in self._wh_cache:
            return self._wh_cache[base_ch.id]

        perms = base_ch.permissions_for(base_ch.guild.me)  # type: ignore
        if not perms.manage_webhooks:
            return None

        try:
            hooks = await base_ch.webhooks()
            hook = hooks[0] if hooks else await base_ch.create_webhook(name="Meowifier", reason="Meowifier")
        except discord.Forbidden:
            return None
        except Exception:
            return None

        self._wh_cache[base_ch.id] = hook
        return hook

    async def _send_via_webhook(
        self,
        hook: discord.Webhook,
        *,
        channel: discord.abc.Messageable,
        author: discord.abc.User,
        content: str,
        files: List[discord.File],
        wait: bool,
    ):
        kwargs = {
            "content": content or "(meow)",
            "username": author.display_name[:80],
            "avatar_url": author.display_avatar.url,
            "allowed_mentions": discord.AllowedMentions.none(),
            "wait": wait,
        }
        if isinstance(channel, discord.Thread):
            kwargs["thread"] = channel
        if files:  # crucial: don't pass None
            kwargs["files"] = files
        return await hook.send(**kwargs)

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
            "mode=webhook-only • now→*meow* always • owo=chanced",
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @meow.command(name="help")
    async def meow_help(self, ctx: redcommands.Context) -> None:
        p = ctx.clean_prefix
        e = discord.Embed(title="Meowifier — Help", color=discord.Color.blurple())
        e.add_field(
            name="Quickstart",
            value=(
                f"1) Needs **Manage Messages** + **Manage Webhooks**.\n"
                f"2) `{p}meow enable` (or `{p}meow enable #channel`).\n"
                f"3) `{p}meow onein 1000` (owo rarity). `now`→`*meow*` is always on.\n"
                f"4) Type a message, then `{p}meow test`."
            ),
            inline=False,
        )
        e.add_field(
            name="Channels",
            value=f"`{p}meow channels add/remove/list/clear` (empty list = all)",
            inline=False,
        )
        e.add_field(
            name="Config",
            value=f"`{p}meow onein <N>` • `{p}meow cooldown <sec>` • `prob add/remove/list` • `exempt role|user add/remove/list`",
            inline=False,
        )
        await ctx.send(embed=e)

    @meow.group(name="channels")
    async def meow_channels(self, ctx: redcommands.Context) -> None: pass

    @meow_channels.command(name="add")
    async def meow_channels_add(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        ch = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        if not isinstance(ch, discord.TextChannel): return await ctx.send("Pick a text channel.")
        data = await self.config.guild(ctx.guild).channels()
        if ch.id in data: return await ctx.send(f"{ch.mention} already in the list.")
        data.append(ch.id); await self.config.guild(ctx.guild).channels.set(data)
        await ctx.send(f"Added {ch.mention}.")

    @meow_channels.command(name="remove")
    async def meow_channels_remove(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        ch = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        if not isinstance(ch, discord.TextChannel): return await ctx.send("Pick a text channel.")
        data = await self.config.guild(ctx.guild).channels()
        if ch.id not in data: return await ctx.send(f"{ch.mention} not in the list.")
        data = [c for c in data if c != ch.id]; await self.config.guild(ctx.guild).channels.set(data)
        await ctx.send(f"Removed {ch.mention}.")

    @meow_channels.command(name="clear")
    async def meow_channels_clear(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).channels.set([])
        await ctx.send("Cleared list — active in **all channels**.")

    @meow_channels.command(name="list")
    async def meow_channels_list(self, ctx: redcommands.Context) -> None:
        data = await self.config.guild(ctx.guild).channels()
        await ctx.send("Channels: " + ("**all**" if not data else ", ".join(f"<#{c}>" for c in data)))

    @meow.command(name="enable")
    async def meow_enable(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        if channel is None:
            await self.config.guild(ctx.guild).enabled.set(True)
            await ctx.send("Meowifier: **enabled** (guild-wide).")
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
            await ctx.send("Meowifier: **disabled** (guild-wide).")
        else:
            data = await self.config.guild(ctx.guild).channels()
            data = [c for c in data if c != channel.id]
            await self.config.guild(ctx.guild).channels.set(data)
            await ctx.send(f"Meowifier: **disabled** for {channel.mention}.")

    @meow.command(name="onein")
    async def meow_onein(self, ctx: redcommands.Context, n: int) -> None:
        if n < 1 or n > 1_000_000: return await ctx.send("Use 1..1,000,000 (probability = 1/N).")
        await self.config.guild(ctx.guild).one_in.set(int(n)); await ctx.tick()

    @meow.command(name="cooldown")
    async def meow_cooldown(self, ctx: redcommands.Context, seconds: int) -> None:
        if seconds < 0 or seconds > 3600: return await ctx.send("Cooldown must be 0–3600s.")
        await self.config.guild(ctx.guild).cooldown_seconds.set(int(seconds)); await ctx.tick()

    @meow.group(name="prob")
    async def meow_prob(self, ctx: redcommands.Context) -> None: pass

    @meow_prob.command(name="add")
    async def meow_prob_add(self, ctx: redcommands.Context, member: discord.Member, n: int) -> None:
        if n < 1 or n > 1_000_000: return await ctx.send("Use 1..1,000,000 (probability = 1/N).")
        data = await self.config.guild(ctx.guild).user_probs(); data[str(member.id)] = int(n)
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
        out = []
        for uid, n in data.items():
            m = ctx.guild.get_member(int(uid))
            out.append(f"- {(m.mention if m else uid)}: 1/{n}")
        await ctx.send(box("\n".join(out), lang="ini"))

    @meow.group(name="exempt")
    async def meow_exempt(self, ctx: redcommands.Context) -> None: pass

    @meow_exempt.group(name="role")
    async def meow_exempt_role(self, ctx: redcommands.Context) -> None: pass

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
    async def meow_exempt_user(self, ctx: redcommands.Context) -> None: pass

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
        meow_only = self._render_message(text, apply_owo=False)
        meow_owo = self._render_message(text, apply_owo=True)
        await ctx.send(box(f"MEOW: {meow_only}\nOWO:  {meow_owo}", lang="ini"))

    @meow.command(name="diag")
    async def meow_diag(self, ctx: redcommands.Context) -> None:
        g = await self.config.guild(ctx.guild).all()
        perms = ctx.channel.permissions_for(ctx.guild.me) if isinstance(ctx.channel, (discord.TextChannel, discord.Thread)) else None  # type: ignore
        lines = [
            f"enabled={g['enabled']} one_in=1/{g['one_in']} cooldown={g['cooldown_seconds']}s",
            f"channels={'all' if not g['channels'] else ', '.join(f'<#{c}>' for c in g['channels'])}",
            f"here perms: view={getattr(perms,'view_channel',None)} send={getattr(perms,'send_messages',None)} manage_messages={getattr(perms,'manage_messages',None)} manage_webhooks={getattr(perms,'manage_webhooks',None)}",
            f"overrides={len(g['user_probs'])} exempts: users={len(g['exempt_users'])} roles={len(g['exempt_roles'])}",
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @meow.command(name="test")
    async def meow_test(self, ctx: redcommands.Context) -> None:
        """Force-owoify your last message here via webhook; changed parts are italicized."""
        try:
            prefixes = await self.bot.get_valid_prefixes(ctx.guild)
        except Exception:
            prefixes = []
        last: Optional[discord.Message] = None
        async for m in ctx.channel.history(limit=50, before=ctx.message.created_at):  # type: ignore
            if m.author.id == ctx.author.id and not m.author.bot and not m.webhook_id and not self._starts_with_prefixes(m.content or "", prefixes):
                last = m
                break

        ch = ctx.channel
        perms = ch.permissions_for(ctx.guild.me) if isinstance(ch, (discord.TextChannel, discord.Thread)) else None  # type: ignore
        lines = [
            f"channel={getattr(ch, 'id', None)} type={ch.__class__.__name__}",
            f"perms: send={getattr(perms,'send_messages',None)} manage_messages={getattr(perms,'manage_messages',None)} manage_webhooks={getattr(perms,'manage_webhooks',None)}",
            f"last_msg={'found' if last else 'not found'}",
        ]
        if not last:
            return await ctx.send(box("\n".join(lines), lang="ini"))

        hook = await self._ensure_webhook(ch)
        if not hook:
            lines.append("hook: none (missing Manage Webhooks?)")
            return await ctx.send(box("\n".join(lines), lang="ini"))
        lines.append(f"hook: {hook.id}:{hook.name}")

        # Always meow; force owo in test to visualize italics for changes
        content = self._render_message(last.content or "", apply_owo=True)

        files: List[discord.File] = []
        for a in last.attachments[:5]:
            try:
                files.append(await a.to_file())
            except Exception as e:
                lines.append(f"attach_fail:{a.id}:{type(e).__name__}")

        try:
            await self._send_via_webhook(hook, channel=ch, author=last.author, content=content, files=files, wait=True)
            lines.append("send: OK")
        except Exception as e:
            lines.append(f"send: FAIL {type(e).__name__}: {e}")
            return await ctx.send(box("\n".join(lines), lang="ini"))

        try:
            await last.delete()
            lines.append("delete: OK")
        except discord.Forbidden:
            lines.append("delete: Forbidden (need Manage Messages)")
        except Exception as e:
            lines.append(f"delete: FAIL {type(e).__name__}:{e}")

        await ctx.send(box("\n".join(lines), lang="ini"))

    # ---------- listener ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not await self._should_process(message):
            return
        conf = await self.config.guild(message.guild).all()
        cd = max(0, int(conf.get("cooldown_seconds", 0)))
        if cd:
            self._cooldown[message.author.id] = time.time()

        # Always meow; owo probabilistic, with italics on changed parts
        n = self._one_in(message.author, conf)
        apply_owo = (n <= 1) or (random.randrange(n) == 0)
        content = self._render_message(message.content or "", apply_owo=apply_owo)

        if (content.strip() == (message.content or "").strip()) and not message.attachments:
            return

        hook = await self._ensure_webhook(message.channel)
        if not hook:
            return

        files: List[discord.File] = []
        for a in message.attachments[:5]:
            try:
                files.append(await a.to_file())
            except Exception:
                pass

        try:
            await self._send_via_webhook(hook, channel=message.channel, author=message.author, content=content, files=files, wait=False)
        except Exception:
            self._wh_cache.pop(getattr(message.channel, "id", 0), None)
            return

        try:
            await message.delete()
        except discord.Forbidden:
            pass


async def setup(bot: Red) -> None:
    await bot.add_cog(Meowifier(bot))
