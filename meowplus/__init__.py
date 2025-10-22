# path: cogs/meowplus/__init__.py
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
    "1-in-N owo probability, cooldown seconds, per-user overrides, exemption lists, intensity, and owner bypass). "
    "It does not store message contents."
)

DEFAULTS_GUILD = {
    "enabled": False,
    "channels": [],
    "one_in": 1000,
    "cooldown_seconds": 5,
    "user_probs": {},
    "exempt_roles": [],
    "exempt_users": [],
    "intensity": 1,        # 1..5
    "owner_bypass": True,  # when True, bot owner is never processed
}

NOW_WORD = re.compile(r"\b(now)\b", re.IGNORECASE)
MEOW_WORD = re.compile(r"\b(meow)\b", re.IGNORECASE)
CODE_SPLIT = re.compile(r"(```[\s\S]*?```|`[^`]*?`)", re.MULTILINE)

OWO_FACES = ["uwu", "owo", ">w<", "^w^", "x3", "~", "nya~", "(⁄˘⁄⁄ ω⁄ ⁄˘⁄)♡"]
EXTRA_FACES = ["rawr x3", "owo~", "uwu~", "^^", "(>w<)"]

EMO = {"ok": "✅", "bad": "⚠️", "core": "🛠️", "channels": "🧵", "msg": "💬", "prob": "🎲", "ex": "🚫", "diag": "🧪", "spark": "✨"}

def _embed(title: str, *, color: int | discord.Color = discord.Color.blurple(), desc: Optional[str] = None) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color)

def _fmt_channels(g: discord.Guild, ids: List[int]) -> str:
    return "**all**" if not ids else ", ".join(f"<#{c}>" for c in ids)

def _bool_emoji(v: bool) -> str:
    return "🟢" if v else "🔴"


class MeowPlus(redcommands.Cog):
    """
    Webhook-only meow/owo replacer with adjustable intensity and owner-bypass toggle.
    """

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0x5E0F1A, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._cooldown: Dict[int, float] = {}
        self._wh_cache: Dict[int, discord.Webhook] = {}

    # ---------- transforms ----------
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
            s = MeowPlus._case_like(m.group(1), "meow")
            return f"*{s}*" if italic else s
        return NOW_WORD.sub(repl, text)

    @staticmethod
    def _ensure_meow_italic(text: str) -> str:
        parts: List[str] = []
        i = 0
        for m in MEOW_WORD.finditer(text):
            start, end = m.span()
            parts.append(text[i:start])
            s_start, s_end = m.span(1)
            before = text[s_start - 1] if s_start - 1 >= 0 else ""
            after = text[s_end] if s_end < len(text) else ""
            if before == "*" and after == "*":
                parts.append(m.group(0))
            else:
                parts.append(f"*{MeowPlus._case_like(m.group(1), 'meow')}*")
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
    def _stutter(word: str, prob: float) -> str:
        if len(word) > 2 and word[0].isalpha() and random.random() < prob:
            return f"{word[0]}-{word}"
        return word

    @staticmethod
    def _elongate_vowels(word: str, prob: float) -> str:
        if random.random() >= prob:
            return word
        return re.sub(r"([aeiouAEIOU])(?=[a-zA-Z])", r"\1\1", word, count=1)

    @staticmethod
    def _owoify_plain(text: str, intensity: int) -> str:
        intensity = max(1, min(5, int(intensity)))
        stutter_prob = [0.10, 0.15, 0.20, 0.28, 0.35][intensity - 1]
        elong_prob = [0.00, 0.08, 0.12, 0.18, 0.25][intensity - 1]
        tilde_prob = [0.00, 0.10, 0.18, 0.25, 0.33][intensity - 1]
        extra_face_prob = [0.00, 0.08, 0.12, 0.18, 0.25][intensity - 1]

        def transliterate(s: str) -> str:
            s = re.sub(r"[rl]", "w", s)
            s = re.sub(r"[RL]", "W", s)
            s = re.sub(r"(?i)n([aeiou])", r"ny\1", s)
            s = re.sub(r"(?i)ove", "uv", s)
            if intensity >= 2:
                s = re.sub(r"(?i)th", lambda m: "d" if m.group(0).islower() else "D", s)
            if intensity >= 3:
                s = re.sub(r"(?i)tt", lambda m: "dd" if m.group(0).islower() else "DD", s)

            def tweak_word(w: str) -> str:
                if w.isalpha():
                    w = MeowPlus._stutter(w, stutter_prob)
                    w = MeowPlus._elongate_vowels(w, elong_prob)
                return w

            words = re.split(r"(\s+)", s)
            words = [tweak_word(w) if (i % 2 == 0) else w for i, w in enumerate(words)]
            s = "".join(words)

            def punct(m: re.Match) -> str:
                p = m.group(1)
                out = p
                out += " " + random.choice(OWO_FACES)
                if random.random() < extra_face_prob:
                    out += " " + random.choice(EXTRA_FACES)
                if random.random() < tilde_prob:
                    out += "~"
                return out

            s = re.sub(r"([.!?])", punct, s)
            s = re.sub(r"!+", lambda m: m.group(0) + "~", s)
            try:
                if len(s) > 1:
                    s = s[0].lower() + s[1:]
            except Exception:
                pass
            return s

        return "".join(seg if is_code else transliterate(seg) for seg, is_code in MeowPlus._split_code_segments(text))

    @staticmethod
    def _italicize_changes(original: str, transformed: str) -> str:
        out: List[str] = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, original, transformed).get_opcodes():
            if tag == "equal":
                out.append(transformed[j1:j2])
            else:
                seg = transformed[j1:j2]
                if seg:
                    out.append(f"*{seg}*")
        return "".join(out)

    def _render_message(self, raw: str, apply_owo: bool, *, intensity: int) -> str:
        result: List[str] = []
        for seg, is_code in self._split_code_segments(raw):
            if is_code:
                result.append(seg)
                continue
            if not apply_owo:
                result.append(self._replace_now(seg, italic=True))
                continue
            meow_plain = self._replace_now(seg, italic=False)
            owo = self._owoify_plain(meow_plain, intensity=intensity)
            marked = self._italicize_changes(meow_plain, owo)
            marked = self._ensure_meow_italic(marked)
            result.append(marked)
        return "".join(result)

    # ---------- gating ----------
    @staticmethod
    def _starts_with_prefixes(text: str, prefixes: List[str]) -> bool:
        return any(p and text.startswith(p) for p in prefixes)

    async def _should_process(self, message: discord.Message) -> bool:
        if not message.guild or message.author.bot or message.webhook_id:
            return False
        conf = await self.config.guild(message.guild).all()
        # Only bypass owner if enabled
        try:
            if conf.get("owner_bypass", True) and await self.bot.is_owner(message.author):
                return False
        except Exception:
            pass
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
            hook = hooks[0] if hooks else await base_ch.create_webhook(name="MeowPlus", reason="MeowPlus")
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
            "username": author.display_name[:80],
            "avatar_url": author.display_avatar.url,
            "allowed_mentions": discord.AllowedMentions.none(),
            "wait": wait,
        }
        if content:
            kwargs["content"] = content
        if isinstance(channel, discord.Thread):
            kwargs["thread"] = channel
        if files:
            kwargs["files"] = files
        return await hook.send(**kwargs)

    # ---------- pretty status ----------
    async def _status_embed(self, g: discord.Guild) -> discord.Embed:
        cfg = await self.config.guild(g).all()
        e = _embed(
            f"MeowPlus — Status {_bool_emoji(cfg['enabled'])}",
            desc="now → *meow* always • owo intensity 1–5 • random owo with probability 1/N.",
        )
        e.add_field(
            name=f"{EMO['core']} Core",
            value=box(
                f"enabled = {cfg['enabled']}\n"
                f"one_in  = 1/{cfg['one_in']}\n"
                f"cooldown= {cfg['cooldown_seconds']}s\n"
                f"intensity= {cfg['intensity']}\n"
                f"owner_bypass= {cfg['owner_bypass']}",
                lang="ini",
            ),
            inline=False,
        )
        e.add_field(name=f"{EMO['channels']} Channels", value=_fmt_channels(g, cfg["channels"]), inline=False)
        e.add_field(
            name=f"{EMO['ex']} Exemptions",
            value=(f"users={len(cfg['exempt_users'])} • roles={len(cfg['exempt_roles'])} • user_overrides={len(cfg['user_probs'])}"),
            inline=False,
        )
        e.set_footer(text="Use `[p]meowplus help` for commands.")
        return e

    # ---------- commands ----------
    @redcommands.group(name="meowplus", invoke_without_command=True)
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def meowplus(self, ctx: redcommands.Context) -> None:
        e = await self._status_embed(ctx.guild)
        await ctx.send(embed=e)

    @meowplus.command(name="help")
    async def meowplus_help(self, ctx: redcommands.Context) -> None:
        p = ctx.clean_prefix
        e = _embed("MeowPlus — Commands", desc=f"{EMO['spark']} Examples use `{p}` as prefix.")
        e.add_field(
            name=f"{EMO['core']} Core",
            value=(
                f"• `{p}meowplus` • `{p}meowplus help` • `{p}meowplus diag`\n"
                f"• `{p}meowplus enable` [#channel] • `{p}meowplus disable` [#channel]\n"
                f"• `{p}meowplus test` • `{p}meowplus preview <text>`"
            ),
            inline=False,
        )
        e.add_field(
            name=f"{EMO['prob']} Probability & Cooldown",
            value=(
                f"• `{p}meowplus onein <N>` (default 1000)\n"
                f"• `{p}meowplus cooldown <sec>`\n"
                f"• `{p}meowplus prob add @user <N>` • `remove @user` • `list`"
            ),
            inline=False,
        )
        e.add_field(
            name="OWO Style",
            value=(f"• `{p}meowplus intensity <1..5>` — more OWO at higher levels (default 1)"),
            inline=False,
        )
        e.add_field(
            name="Owner Bypass",
            value=(f"• `{p}meowplus ownerbypass <on|off>` — when on, Red owner is never processed (default on)"),
            inline=False,
        )
        e.add_field(
            name=f"{EMO['ex']} Exemptions",
            value=(f"• `{p}meowplus exempt user add|remove @user` • `list`\n" f"• `{p}meowplus exempt role add|remove @role` • `list`"),
            inline=False,
        )
        await ctx.send(embed=e)

    @meowplus.command(name="ownerbypass")
    async def meowplus_ownerbypass(self, ctx: redcommands.Context, state: Optional[str] = None) -> None:
        """Toggle whether the Red owner is bypassed (on/off)."""
        if state is None:
            cur = await self.config.guild(ctx.guild).owner_bypass()
            return await ctx.send(embed=_embed(f"Owner bypass is **{'on' if cur else 'off'}**"))
        val = state.lower() in {"on", "true", "yes", "1"}
        await self.config.guild(ctx.guild).owner_bypass.set(val)
        await ctx.tick()

    @meowplus.command(name="intensity")
    async def meowplus_intensity(self, ctx: redcommands.Context, level: Optional[int] = None) -> None:
        if level is None:
            cur = await self.config.guild(ctx.guild).intensity()
            return await ctx.send(embed=_embed(f"Current intensity: **{cur}**"))
        if level < 1 or level > 5:
            return await ctx.send(embed=_embed("Use 1..5.", color=discord.Color.orange()))
        await self.config.guild(ctx.guild).intensity.set(int(level))
        await ctx.tick()

    @meowplus.group(name="channels")
    async def meowplus_channels(self, ctx: redcommands.Context) -> None:
        pass

    @meowplus_channels.command(name="add")
    async def meowplus_channels_add(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        ch = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        if not isinstance(ch, discord.TextChannel):
            return await ctx.send(embed=_embed("Pick a text channel.", color=discord.Color.orange()))
        data = await self.config.guild(ctx.guild).channels()
        if ch.id in data:
            return await ctx.send(embed=_embed(f"{EMO['bad']} {ch.mention} already in the list.", color=discord.Color.orange()))
        data.append(ch.id)
        await self.config.guild(ctx.guild).channels.set(data)
        await ctx.send(embed=_embed(f"{EMO['ok']} Added {ch.mention}.", color=discord.Color.green()))

    @meowplus_channels.command(name="remove")
    async def meowplus_channels_remove(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        ch = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        if not isinstance(ch, discord.TextChannel):
            return await ctx.send(embed=_embed("Pick a text channel.", color=discord.Color.orange()))
        data = await self.config.guild(ctx.guild).channels()
        if ch.id not in data:
            return await ctx.send(embed=_embed(f"{EMO['bad']} {ch.mention} not in the list.", color=discord.Color.orange()))
        data = [c for c in data if c != ch.id]
        await self.config.guild(ctx.guild).channels.set(data)
        await ctx.send(embed=_embed(f"{EMO['ok']} Removed {ch.mention}.", color=discord.Color.green()))

    @meowplus_channels.command(name="clear")
    async def meowplus_channels_clear(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).channels.set([])
        await ctx.send(embed=_embed(f"{EMO['ok']} Cleared — active in **all channels**.", color=discord.Color.green()))

    @meowplus_channels.command(name="list")
    async def meowplus_channels_list(self, ctx: redcommands.Context) -> None:
        data = await self.config.guild(ctx.guild).channels()
        e = _embed("MeowPlus — Channels")
        e.add_field(name="Scope", value=_fmt_channels(ctx.guild, data), inline=False)
        await ctx.send(embed=e)

    @meowplus.command(name="enable")
    async def meowplus_enable(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        if channel is None:
            await self.config.guild(ctx.guild).enabled.set(True)
            await ctx.send(embed=_embed(f"{EMO['ok']} MeowPlus enabled (guild-wide).", color=discord.Color.green()))
        else:
            data = await self.config.guild(ctx.guild).channels()
            if channel.id not in data:
                data.append(channel.id)
                await self.config.guild(ctx.guild).channels.set(data)
            await self.config.guild(ctx.guild).enabled.set(True)
            await ctx.send(embed=_embed(f"{EMO['ok']} Enabled for {channel.mention}.", color=discord.Color.green()))

    @meowplus.command(name="disable")
    async def meowplus_disable(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        if channel is None:
            await self.config.guild(ctx.guild).enabled.set(False)
            await ctx.send(embed=_embed(f"{EMO['ok']} MeowPlus disabled (guild-wide).", color=discord.Color.green()))
        else:
            data = await self.config.guild(ctx.guild).channels()
            data = [c for c in data if c != channel.id]
            await self.config.guild(ctx.guild).channels.set(data)
            await ctx.send(embed=_embed(f"{EMO['ok']} Disabled for {channel.mention}.", color=discord.Color.green()))

    @meowplus.command(name="onein")
    async def meowplus_onein(self, ctx: redcommands.Context, n: int) -> None:
        if n < 1 or n > 1_000_000:
            return await ctx.send(embed=_embed("Use 1..1,000,000 (probability = 1/N).", color=discord.Color.orange()))
        await self.config.guild(ctx.guild).one_in.set(int(n))
        await ctx.tick()

    @meowplus.command(name="cooldown")
    async def meowplus_cooldown(self, ctx: redcommands.Context, seconds: int) -> None:
        if seconds < 0 or seconds > 3600:
            return await ctx.send(embed=_embed("Cooldown must be 0–3600s.", color=discord.Color.orange()))
        await self.config.guild(ctx.guild).cooldown_seconds.set(int(seconds))
        await ctx.tick()

    @meowplus.group(name="prob")
    async def meowplus_prob(self, ctx: redcommands.Context) -> None:
        pass

    @meowplus_prob.command(name="add")
    async def meowplus_prob_add(self, ctx: redcommands.Context, member: discord.Member, n: int) -> None:
        if n < 1 or n > 1_000_000:
            return await ctx.send(embed=_embed("Use 1..1,000,000 (probability = 1/N).", color=discord.Color.orange()))
        data = await self.config.guild(ctx.guild).user_probs()
        data[str(member.id)] = int(n)
        await self.config.guild(ctx.guild).user_probs.set(data)
        await ctx.send(embed=_embed(f"{EMO['ok']} Set {member.mention} to 1/{n}.", color=discord.Color.green()))

    @meowplus_prob.command(name="remove")
    async def meowplus_prob_remove(self, ctx: redcommands.Context, member: discord.Member) -> None:
        data = await self.config.guild(ctx.guild).user_probs()
        removed = data.pop(str(member.id), None) is not None
        await self.config.guild(ctx.guild).user_probs.set(data)
        msg = "Removed." if removed else "No override was set."
        await ctx.send(embed=_embed(msg, color=discord.Color.green() if removed else discord.Color.orange()))

    @meowplus_prob.command(name="list")
    async def meowplus_prob_list(self, ctx: redcommands.Context) -> None:
        data = await self.config.guild(ctx.guild).user_probs()
        if not data:
            return await ctx.send(embed=_embed("No overrides.", color=discord.Color.orange()))
        out = []
        for uid, n in data.items():
            m = ctx.guild.get_member(int(uid))
            out.append(f"- {(m.mention if m else uid)}: 1/{n}")
        await ctx.send(embed=_embed("Probability Overrides", desc=box("\n".join(out), lang="ini")))

    @meowplus.group(name="exempt")
    async def meowplus_exempt(self, ctx: redcommands.Context) -> None:
        pass

    @meowplus_exempt.group(name="role")
    async def meowplus_exempt_role(self, ctx: redcommands.Context) -> None:
        pass

    @meowplus_exempt_role.command(name="add")
    async def meowplus_exempt_role_add(self, ctx: redcommands.Context, role: discord.Role) -> None:
        data = await self.config.guild(ctx.guild).exempt_roles()
        if role.id in data:
            return await ctx.send(embed=_embed("Role already exempt.", color=discord.Color.orange()))
        data.append(role.id)
        await self.config.guild(ctx.guild).exempt_roles.set(data)
        await ctx.tick()

    @meowplus_exempt_role.command(name="remove")
    async def meowplus_exempt_role_remove(self, ctx: redcommands.Context, role: discord.Role) -> None:
        data = await self.config.guild(ctx.guild).exempt_roles()
        if role.id not in data:
            return await ctx.send(embed=_embed("Role not exempt.", color=discord.Color.orange()))
        data = [r for r in data if r != role.id]
        await self.config.guild(ctx.guild).exempt_roles.set(data)
        await ctx.tick()

    @meowplus_exempt_role.command(name="list")
    async def meowplus_exempt_role_list(self, ctx: redcommands.Context) -> None:
        ids = await self.config.guild(ctx.guild).exempt_roles()
        roles = [ctx.guild.get_role(r).mention for r in ids if ctx.guild.get_role(r)] or ["none"]
        await ctx.send(embed=_embed("Exempt Roles", desc=", ".join(roles)))

    @meowplus_exempt.group(name="user")
    async def meowplus_exempt_user(self, ctx: redcommands.Context) -> None:
        pass

    @meowplus_exempt_user.command(name="add")
    async def meowplus_exempt_user_add(self, ctx: redcommands.Context, member: discord.Member) -> None:
        data = await self.config.guild(ctx.guild).exempt_users()
        if member.id in data:
            return await ctx.send(embed=_embed("User already exempt.", color=discord.Color.orange()))
        data.append(member.id)
        await self.config.guild(ctx.guild).exempt_users.set(data)
        await ctx.tick()

    @meowplus_exempt_user.command(name="remove")
    async def meowplus_exempt_user_remove(self, ctx: redcommands.Context, member: discord.Member) -> None:
        data = await self.config.guild(ctx.guild).exempt_users()
        if member.id not in data:
            return await ctx.send(embed=_embed("User not exempt.", color=discord.Color.orange()))
        data = [u for u in data if u != member.id]
        await self.config.guild(ctx.guild).exempt_users.set(data)
        await ctx.tick()

    @meowplus_exempt_user.command(name="list")
    async def meowplus_exempt_user_list(self, ctx: redcommands.Context) -> None:
        ids = await self.config.guild(ctx.guild).exempt_users()
        names = [ctx.guild.get_member(uid).mention if ctx.guild.get_member(uid) else f"`{uid}`" for uid in ids]
        await ctx.send(embed=_embed("Exempt Users", desc=("none" if not names else ", ".join(names))))

    @meowplus.command(name="preview")
    async def meowplus_preview(self, ctx: redcommands.Context, *, text: str) -> None:
        g = await self.config.guild(ctx.guild).all()
        meow_only = self._render_message(text, apply_owo=False, intensity=g["intensity"])
        meow_owo = self._render_message(text, apply_owo=True, intensity=g["intensity"])
        e = _embed("MeowPlus — Preview")
        e.add_field(name="MEOW", value=box(meow_only, lang="ini"), inline=False)
        e.add_field(name="OWO", value=box(meow_owo, lang="ini"), inline=False)
        await ctx.send(embed=e)

    @meowplus.command(name="diag")
    async def meowplus_diag(self, ctx: redcommands.Context) -> None:
        g = await self.config.guild(ctx.guild).all()
        perms = ctx.channel.permissions_for(ctx.guild.me) if isinstance(ctx.channel, (discord.TextChannel, discord.Thread)) else None  # type: ignore
        payload = "\n".join(
            [
                f"enabled={g['enabled']} one_in=1/{g['one_in']} cooldown={g['cooldown_seconds']}s intensity={g['intensity']} owner_bypass={g['owner_bypass']}",
                f"channels={ 'all' if not g['channels'] else ', '.join(f'<#{c}>' for c in g['channels']) }",
                f"here perms: view={getattr(perms,'view_channel',None)} send={getattr(perms,'send_messages',None)} manage_messages={getattr(perms,'manage_messages',None)} manage_webhooks={getattr(perms,'manage_webhooks',None)}",
                f"overrides={len(g['user_probs'])} exempts: users={len(g['exempt_users'])} roles={len(g['exempt_roles'])}",
            ]
        )
        await ctx.send(embed=_embed("MeowPlus — Diag", desc=box(payload, lang="ini")))

    @meowplus.command(name="test")
    async def meowplus_test(self, ctx: redcommands.Context) -> None:
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
            return await ctx.send(embed=_embed("MeowPlus — Test", desc=box("\n".join(lines), lang="ini")))

        if not (last.content and last.content.strip()):
            lines.append("skip: last message has no text (attachments-only)")
            return await ctx.send(embed=_embed("MeowPlus — Test", desc=box("\n").join(lines), lang="ini"))

        hook = await self._ensure_webhook(ch)
        if not hook:
            lines.append("hook: none (missing Manage Webhooks?)")
            return await ctx.send(embed=_embed("MeowPlus — Test", desc=box("\n".join(lines), lang="ini")))
        lines.append(f"hook: {hook.id}:{hook.name}")

        g = await self.config.guild(ctx.guild).all()
        content = self._render_message(last.content or "", apply_owo=True, intensity=g["intensity"])

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
            return await ctx.send(embed=_embed("MeowPlus — Test", desc=box("\n".join(lines), lang="ini")))

        try:
            await last.delete()
            lines.append("delete: OK")
        except discord.Forbidden:
            lines.append("delete: Forbidden (need Manage Messages)")
        except Exception as e:
            lines.append(f"delete: FAIL {type(e).__name__}:{e}")

        await ctx.send(embed=_embed("MeowPlus — Test", desc=box("\n".join(lines), lang="ini")))

    # ---------- listener ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not await self._should_process(message):
            return
        conf = await self.config.guild(message.guild).all()
        cd = max(0, int(conf.get("cooldown_seconds", 0)))
        if cd:
            self._cooldown[message.author.id] = time.time()

        original = (message.content or "").strip()
        if not original:
            return

        n = self._one_in(message.author, conf)
        apply_owo = (n <= 1) or (random.randrange(n) == 0)
        content = self._render_message(original, apply_owo=apply_owo, intensity=conf["intensity"]).strip()

        if content == original:
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
    await bot.add_cog(MeowPlus(bot))
