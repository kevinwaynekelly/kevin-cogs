from __future__ import annotations

import asyncio
import difflib
import io
import json
import logging
import random
import re
import time
from collections import OrderedDict, defaultdict
from functools import lru_cache
from typing import Callable, List, Optional, Tuple
from weakref import WeakValueDictionary

import discord
from discord.ext import commands
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config

from .command_support import prepare_hybrid
from .constants import (
    CODE_SPLIT,
    DEFAULTS_GUILD,
    HAIKU_SUFFIX,
    KEY_MAP,
    KEY_RX,
    OWO_FACES,
    TARGETS,
)
from .events import guild_enabled
from .haiku import (
    Haiku,
    HaikuMeter,
    _count_syllables,
    _detect_haiku_breaks,
    _normalize_for_haiku,
    _reflow_text_as_haiku,
)
from .presentation import Presentation, settings

log = logging.getLogger(__name__)


def _embed(
    title: str,
    *,
    color: int | discord.Color = discord.Color.blurple(),
    desc: Optional[str] = None,
) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color)


class OwoPlus(redcommands.Cog):
    """Webhook message transformations and automatic haiku formatting."""

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self._presentation = Presentation("OwoPlus", "owo")
        self.config: Config = Config.get_conf(self, identifier=0x5E0F1A, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._wh_cache = OrderedDict()
        self._webhook_locks = WeakValueDictionary()
        self._settings_cache = {}
        self._settings_locks = defaultdict(asyncio.Lock)
        self._render_gate = asyncio.Semaphore(4)

    # ---------- transforms ----------
    @staticmethod
    def _case_like(src: str, repl: str) -> str:
        if src.isupper():
            return repl.upper()
        if src and src[0].isupper():
            return repl.capitalize()
        return repl

    @staticmethod
    def _split_code_segments(text: str) -> List[Tuple[str, bool]]:
        segs: List[Tuple[str, bool]] = []
        i = 0
        for m in CODE_SPLIT.finditer(text):
            if m.start() > i:
                segs.append((text[i : m.start()], False))
            segs.append((m.group(0), True))
            i = m.end()
        if i < len(text):
            segs.append((text[i:], False))
        return segs

    @staticmethod
    def _apply_key_map(text: str) -> str:
        def repl(m: re.Match) -> str:
            src = m.group(1)
            tgt = KEY_MAP[src.lower()]
            return OwoPlus._case_like(src, tgt)

        return KEY_RX.sub(repl, text)

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
    def _sub_prob(
        s: str,
        pattern: str,
        repl: str | Callable[[re.Match], str],
        prob: float,
        flags: int = 0,
    ) -> str:
        if prob <= 0.0:
            return s
        rx = re.compile(pattern, flags)

        def _choose(m: re.Match) -> str:
            if random.random() < prob:
                return m.expand(repl) if isinstance(repl, str) else repl(m)
            return m.group(0)

        return rx.sub(_choose, s)

    @staticmethod
    def _owoify_plain(text: str, intensity: int) -> str:
        prof = {
            1: dict(
                rl=0.35,
                ny=0.85,
                uv=0.85,
                th=0.30,
                tt=0.15,
                lc_first=False,
                stutter=0.08,
                elong=0.06,
                face=0.18,
                tilde=0.08,
            ),
            2: dict(
                rl=0.55,
                ny=0.95,
                uv=0.95,
                th=0.40,
                tt=0.25,
                lc_first=False,
                stutter=0.10,
                elong=0.10,
                face=0.20,
                tilde=0.10,
            ),
            3: dict(
                rl=0.75,
                ny=1.00,
                uv=1.00,
                th=0.60,
                tt=0.45,
                lc_first=True,
                stutter=0.14,
                elong=0.14,
                face=0.22,
                tilde=0.12,
            ),
            4: dict(
                rl=0.90,
                ny=1.00,
                uv=1.00,
                th=0.80,
                tt=0.65,
                lc_first=True,
                stutter=0.18,
                elong=0.18,
                face=0.25,
                tilde=0.14,
            ),
            5: dict(
                rl=1.00,
                ny=1.00,
                uv=1.00,
                th=1.00,
                tt=1.00,
                lc_first=True,
                stutter=0.22,
                elong=0.24,
                face=0.28,
                tilde=0.16,
            ),
        }[max(1, min(5, int(intensity)))]

        def transliterate(s: str) -> str:
            s = OwoPlus._sub_prob(s, r"[rl]", "w", prof["rl"])
            s = OwoPlus._sub_prob(s, r"[RL]", "W", prof["rl"])
            s = OwoPlus._sub_prob(s, r"(?i)n([aeiou])", r"ny\1", prof["ny"])
            s = OwoPlus._sub_prob(s, r"(?i)ove", "uv", prof["uv"])
            s = OwoPlus._sub_prob(
                s, r"(?i)th", lambda m: "d" if m.group(0).islower() else "D", prof["th"]
            )
            s = OwoPlus._sub_prob(
                s,
                r"(?i)tt",
                lambda m: "dd" if m.group(0).islower() else "DD",
                prof["tt"],
            )

            def tweak_word(w: str) -> str:
                if w.isalpha():
                    w = OwoPlus._stutter(w, prof["stutter"])
                    w = OwoPlus._elongate_vowels(w, prof["elong"])
                return w

            words = re.split(r"(\s+)", s)
            words = [tweak_word(w) if (i % 2 == 0) else w for i, w in enumerate(words)]
            s = "".join(words)

            def punct(m: re.Match) -> str:
                p = m.group(1)
                out = p
                if random.random() < prof["face"]:
                    out += " " + random.choice(OWO_FACES)
                if random.random() < prof["tilde"]:
                    out += "~"
                return out

            s = re.sub(r"([.!?]+)", punct, s)
            s = re.sub(r"~{2,}", "~", s)
            if prof["lc_first"] and len(s) > 1:
                s = s[0].lower() + s[1:]
            return s

        return "".join(
            seg if is_code else transliterate(seg)
            for seg, is_code in OwoPlus._split_code_segments(text)
        )

    @staticmethod
    def _italicize_changes(original: str, transformed: str) -> str:
        out: List[str] = []
        wordish = re.compile(r"[A-Za-z0-9]")
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, original, transformed
        ).get_opcodes():
            if tag == "equal":
                out.append(transformed[j1:j2])
                continue
            if tag == "insert":
                out.append(transformed[j1:j2])
                continue
            seg = transformed[j1:j2]
            if not seg or not wordish.search(seg):
                out.append(seg)
                continue
            leading = len(seg) - len(seg.lstrip())
            trailing = len(seg) - len(seg.rstrip())
            left = seg[:leading]
            core = seg[leading : len(seg) - trailing] if trailing else seg[leading:]
            right = seg[len(seg) - trailing :] if trailing else ""
            out.append(f"{left}*{core}*{right}")
        return "".join(out)

    # ---------- italics helpers (for haiku only) ----------
    @staticmethod
    def _sanitize_italics_and_ticks(text: str) -> str:
        text = text.replace("*`", "* `").replace("`*", "` *")
        text = text.replace("_`", "_ `").replace("`_", "` _")
        return text

    @staticmethod
    def _wrap_all_italics(text: str) -> str:
        parts: List[str] = []
        for seg, is_code in OwoPlus._split_code_segments(text):
            if is_code:
                parts.append(seg)
            else:
                if seg:
                    parts.append(f"_{seg}_")
        return OwoPlus._sanitize_italics_and_ticks("".join(parts))

    @staticmethod
    def _add_haiku_suffix(text: str) -> str:
        lines = text.split("\n")
        if not lines:
            return text + HAIKU_SUFFIX
        lines[-1] = lines[-1].rstrip() + HAIKU_SUFFIX
        return "\n".join(lines)

    # ---------- italics support for key targets ----------
    @staticmethod
    @lru_cache(maxsize=16)
    def _build_var_regex(token: str) -> re.Pattern:
        m = re.search(r"[aeiouAEIOU]", token)
        first = re.escape(token[0])
        if not m:
            core = re.escape(token)
            return re.compile(rf"\b(?:{first}-)?{core}\b", re.IGNORECASE)
        i = m.start()
        pre = re.escape(token[:i])
        vow = re.escape(token[i])
        post = re.escape(token[i + 1 :])
        return re.compile(rf"\b(?:{first}-)?{pre}{vow}{{1,2}}{post}\b", re.IGNORECASE)

    @staticmethod
    def _inside_italics(s: str, start: int, end: int) -> bool:
        left = s.rfind("*", 0, start)
        right = s.find("*", end)
        return left != -1 and right != -1 and left < start < right

    @staticmethod
    def _ensure_targets_italic(text: str) -> str:
        patterns = [OwoPlus._build_var_regex(t) for t in TARGETS]

        def apply(seg: str) -> str:
            for pat in patterns:
                out: List[str] = []
                i = 0
                for m in pat.finditer(seg):
                    start, end = m.span()
                    out.append(seg[i:start])
                    out.append(
                        m.group(0)
                        if OwoPlus._inside_italics(seg, start, end)
                        else f"*{m.group(0)}*"
                    )
                    i = end
                out.append(seg[i:])
                seg = "".join(out)
            return seg

        parts: List[str] = []
        for seg, is_code in OwoPlus._split_code_segments(text):
            parts.append(seg if is_code else apply(seg))
        return "".join(parts)

    # ---------- auto intensity 1..5 ----------
    @staticmethod
    def _auto_intensity(nchars: int) -> int:
        if nchars <= 80:
            return 5
        if nchars <= 160:
            return 4
        if nchars <= 400:
            return 3
        if nchars <= 1200:
            return 2
        return 1

    # ---------- haiku wrappers ----------
    @staticmethod
    def _normalize_for_haiku(s: str) -> str:
        return _normalize_for_haiku(s)

    @staticmethod
    def _count_syllables(word: str) -> int:
        return _count_syllables(word)

    @staticmethod
    def _detect_haiku_breaks(text: str) -> Optional[Tuple[int, int]]:
        return _detect_haiku_breaks(text)

    @staticmethod
    def _reflow_text_as_haiku(rendered: str, cuts: Tuple[int, int]) -> str:
        return _reflow_text_as_haiku(rendered, cuts)

    # NEW: final per-line cleanup
    @staticmethod
    def _format_haiku_lines(text: str) -> str:
        return Haiku.clean_lines(text)

    @staticmethod
    def _plain_text_if_no_code(raw: str) -> Optional[str]:
        segs = OwoPlus._split_code_segments(raw)
        if any(is_code for _, is_code in segs):
            return None
        return "".join(s for s, _ in segs)

    @staticmethod
    def _has_key_trigger(text: str) -> bool:
        return any(
            KEY_RX.search(seg) for seg, is_code in OwoPlus._split_code_segments(text) if not is_code
        )

    # ---------- render modes ----------
    def _render_message_mode(self, raw: str, mode: str, *, use_haiku: bool) -> str:
        """
        mode: 'full' | 'keys' | 'none'
        Haiku (when enabled) returns reflowed original (no OWO), fully italicized, and ends with 🌸.
        """
        if use_haiku:
            plain = self._plain_text_if_no_code(raw)
            if plain:
                cuts = self._detect_haiku_breaks(plain)
                if cuts:
                    haiku = self._reflow_text_as_haiku(plain, cuts)
                    haiku = self._format_haiku_lines(haiku)  # strip leading spaces
                    haiku = self._add_haiku_suffix(haiku)
                    haiku = self._format_haiku_lines(haiku)  # final pass
                    return self._wrap_all_italics(haiku)

        result: List[str] = []
        intensity = self._auto_intensity(len(raw or ""))
        for seg, is_code in self._split_code_segments(raw):
            if is_code or mode == "none":
                result.append(seg)
                continue
            if mode == "keys":
                mapped = self._apply_key_map(seg)
                mapped = self._ensure_targets_italic(mapped)
                result.append(mapped)
            else:
                seed = self._apply_key_map(seg)
                owo = self._owoify_plain(seed, intensity=intensity)
                marked = self._italicize_changes(seed, owo)
                marked = self._ensure_targets_italic(marked)
                result.append(marked)
        final = "".join(result)
        return self._sanitize_italics_and_ticks(final)

    # ---------- chunking ----------
    @staticmethod
    def _find_breakpoint(window: str) -> int:
        candidates: List[int] = []
        for m in re.finditer(r"[.!?](?:\s|$)", window):
            candidates.append(m.end())
        nl = window.rfind("\n")
        if nl != -1:
            candidates.append(nl + 1)
        sp = window.rfind(" ")
        if sp != -1:
            candidates.append(sp + 1)
        return max(candidates) if candidates else len(window)

    def _chunk_message(self, text, limit=2000):
        if limit < 2:
            raise ValueError("Chunk limit must be at least two UTF-16 code units.")
        chunks = []
        start = 0
        while start < len(text):
            end, units = start, 0
            while end < len(text):
                size = 2 if ord(text[end]) > 0xFFFF else 1
                if units + size > limit:
                    break
                units += size
                end += 1
            if end < len(text):
                boundary = self._find_breakpoint(text[start:end])
                if boundary > 0:
                    end = start + boundary
            chunks.append(text[start:end])
            start = end
        return chunks

    # ---------- gating ----------
    @staticmethod
    def _starts_with_prefixes(text: str, prefixes: List[str]) -> bool:
        return any(p and (text.startswith(p) or text.startswith(p + " ")) for p in prefixes)

    async def _should_process(self, message, conf=None):
        if (
            not message.guild
            or message.author.bot
            or message.webhook_id
            or not isinstance(message.channel, (discord.TextChannel, discord.Thread))
        ):
            return False
        conf = conf if conf is not None else await self._settings(message.guild)
        if (
            not conf["enabled"]
            or not message.content
            or message.embeds
            or len(message.attachments) > 10
        ):
            return False
        if conf.get("owner_bypass", True) and await self.bot.is_owner(message.author):
            return False
        return not self._starts_with_prefixes(
            message.content, await self.bot.get_valid_prefixes(message.guild)
        )

    def _one_in(self, member: discord.Member, conf: dict) -> int:
        pmap: dict = conf.get("user_probs", {}) or {}
        try:
            n = int(pmap.get(str(member.id))) if str(member.id) in pmap else int(conf["one_in"])
            return max(1, min(n, 1_000_000))
        except Exception:
            return max(1, int(conf["one_in"]))

    # ---------- webhook helpers ----------
    async def _ensure_webhook(self, channel):
        base = channel.parent if isinstance(channel, discord.Thread) else channel
        if not isinstance(base, (discord.TextChannel, discord.ForumChannel)) or not base.guild.me:
            return None
        if not base.permissions_for(base.guild.me).manage_webhooks:
            return None
        lock = self._webhook_locks.setdefault(base.id, asyncio.Lock())
        async with lock:
            if base.id in self._wh_cache:
                self._wh_cache.move_to_end(base.id)
                return self._wh_cache[base.id]
            try:
                hooks = await base.webhooks()
                hook = next(
                    (
                        h
                        for h in hooks
                        if h.name == "OwoPlus"
                        and h.user
                        and h.user.id == self.bot.user.id
                        and h.token
                    ),
                    None,
                )
                if hook is None:
                    hook = await base.create_webhook(name="OwoPlus", reason="OwoPlus")
            except discord.HTTPException:
                return None
            self._wh_cache[base.id] = hook
            while len(self._wh_cache) > 256:
                evicted, _ = self._wh_cache.popitem(last=False)
                old_lock = self._webhook_locks.get(evicted)
                if old_lock and not old_lock.locked():
                    self._webhook_locks.pop(evicted, None)
            return hook

    async def _send_via_webhook(
        self,
        hook: discord.Webhook,
        *,
        channel: discord.abc.Messageable,
        author: discord.abc.User,
        content: str,
        files: Optional[List[discord.File]],
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
        e = self._presentation.embed(
            "Status", "Message transformations and automatic haiku formatting."
        )
        e.add_field(
            name="Message transformations",
            value=f"**Status** · {'Enabled' if cfg['enabled'] else 'Disabled'}\n"
            f"**Full transformation chance** · 1 in {cfg['one_in']:,}\n"
            f"**Bot owner bypass** · {'Enabled' if cfg['owner_bypass'] else 'Disabled'}",
            inline=False,
        )
        e.add_field(
            name="Haiku",
            value=f"**Automatic formatting** · {'Enabled' if cfg['haiku_enabled'] else 'Disabled'}\n"
            "Detected haiku becomes three italic lines ending with 🌸.",
            inline=False,
        )
        e.add_field(
            name="Member overrides",
            value=f"{len(cfg['user_probs']):,} custom probabilities",
            inline=True,
        )
        e.add_field(name="Key substitutions", value="meow · bwo · duwde · bwud", inline=True)
        return e

    # ---------- commands ----------
    @redcommands.hybrid_group(name="owo", invoke_without_command=True, fallback="status")
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def owoplus(self, ctx: redcommands.Context) -> None:
        """Configure message transformations and haiku formatting.

        Run this command alone to see settings. Transformations start disabled and must be
        enabled for this server.
        """
        e = await self._status_embed(ctx.guild)
        await self._reply(ctx, embed=e)

    @owoplus.command(name="help")
    async def owoplus_help(self, ctx: redcommands.Context) -> None:
        """Show the transformation and haiku command overview."""
        p = ctx.clean_prefix
        e = _embed("OwoPlus - Commands", desc=f"Commands and examples use `{p}` as prefix.")
        e.add_field(
            name="Core",
            value=(
                f"• `{p}owo` • `{p}owo help` • `{p}owo diag`\n"
                f"• `{p}owo enable` • `{p}owo disable`\n"
                f"• `{p}owo test` • `{p}owo preview <text>`"
            ),
            inline=False,
        )
        e.add_field(
            name="Probability",
            value=(
                f"• `{p}owo onein <N>` (default 1000)\n"
                f"• `{p}owo prob add @user <N>` • `remove @user` • `list`"
            ),
            inline=False,
        )
        e.add_field(
            name="Toggles & Tools",
            value=(
                f"• `{p}owo ownerbypass <on|off>`\n"
                f"• `{p}owo poem on|off`  - toggle haiku reflow\n"
                f"• `{p}owo poem diag <text>` - syllables & breaks"
            ),
            inline=False,
        )
        e.add_field(
            name="Slash commands",
            value="Use `/owo status`, `/owo preview`, `/owo onein`, or `/owo poem diag`. Settings keep the same administrator permissions.",
            inline=False,
        )
        e.add_field(
            name="Behavior",
            value="Haiku detected ⇒ three italic lines with a blossom; otherwise RNG full vs keys-only.",
            inline=False,
        )
        await self._reply(ctx, embed=e)

    @owoplus.command(name="ownerbypass")
    async def owoplus_ownerbypass(
        self, ctx: redcommands.Context, state: Optional[str] = None
    ) -> None:
        """Show or set the bot owner's transformation bypass."""
        if state is None:
            cur = await self.config.guild(ctx.guild).owner_bypass()
            return await self._reply(
                ctx, f"Bot owner bypass is **{'enabled' if cur else 'disabled'}**."
            )
        if state.lower() not in {"on", "true", "yes", "1", "off", "false", "no", "0"}:
            return await self._reply(ctx, "Use on or off.", tone="warning")
        val = state.lower() in {"on", "true", "yes", "1"}
        await self.config.guild(ctx.guild).owner_bypass.set(val)
        await self._presentation.confirm(ctx)

    # ------- poem group (haiku tools) -------
    @owoplus.group(name="poem", invoke_without_command=True)
    async def owoplus_poem(self, ctx: redcommands.Context) -> None:
        """Show and configure automatic haiku formatting."""
        cur = await self.config.guild(ctx.guild).haiku_enabled()
        await self._reply(
            ctx,
            f"Automatic haiku formatting is **{'enabled' if cur else 'disabled'}**.\n"
            f"`{ctx.clean_prefix}owo poem on` · `{ctx.clean_prefix}owo poem off`\n"
            f"`{ctx.clean_prefix}owo poem diag <text>`",
            title="Haiku",
        )

    @owoplus_poem.command(name="on")
    async def owoplus_poem_on(self, ctx: redcommands.Context) -> None:
        """Enable automatic haiku formatting."""
        await self.config.guild(ctx.guild).haiku_enabled.set(True)
        await self._presentation.confirm(ctx)

    @owoplus_poem.command(name="off")
    async def owoplus_poem_off(self, ctx: redcommands.Context) -> None:
        """Disable automatic haiku formatting."""
        await self.config.guild(ctx.guild).haiku_enabled.set(False)
        await self._presentation.confirm(ctx)

    @owoplus_poem.command(name="diag")
    async def owoplus_poem_diag(self, ctx: redcommands.Context, *, text: str) -> None:
        """Inspect syllable counts and possible haiku breaks."""
        norm = self._normalize_for_haiku(text)
        words = [w for w in re.findall(r"[A-Za-z']+", norm)]
        syl = [self._count_syllables(w) for w in words]
        cum = []
        c = 0
        for s in syl:
            c += s
            cum.append(c)
        cuts = self._detect_haiku_breaks(norm)
        cut1, cut2 = cuts if cuts else (-1, -1)
        preview_tokens = []
        for i, w in enumerate(words, 1):
            token = w
            if i == cut1 or i == cut2:
                token += " |"
            preview_tokens.append(token)
        preview = " ".join(preview_tokens)
        lines = [
            f"words={len(words)} syllables_total={sum(syl)}",
            f"word_list={words}",
            f"syllables={syl}",
            f"cumulative={cum}",
            f"breaks=(cut1={cut1}, cut2={cut2})",
            f"preview={preview}",
        ]
        out = text
        if cuts:
            out = self._reflow_text_as_haiku(norm, cuts)
            out = self._format_haiku_lines(out)
            out = self._add_haiku_suffix(out)
            out = self._format_haiku_lines(out)
        e = _embed("OwoPlus - Haiku Diag", desc=settings("\n".join(lines)))
        e.add_field(
            name="Haiku Render", value=discord.utils.escape_markdown(out) or "(empty)", inline=False
        )
        await self._reply(ctx, embed=e)

    # ---------------------------------------

    @owoplus.command(name="enable")
    async def owoplus_enable(self, ctx: redcommands.Context) -> None:
        """Enable automatic message transformations."""
        await self.config.guild(ctx.guild).enabled.set(True)
        await self._reply(ctx, "Message transformations enabled for this server.", tone="success")

    @owoplus.command(name="disable")
    async def owoplus_disable(self, ctx: redcommands.Context) -> None:
        """Disable automatic message transformations."""
        await self.config.guild(ctx.guild).enabled.set(False)
        await self._reply(ctx, "Message transformations disabled for this server.", tone="success")

    @owoplus.command(name="onein")
    async def owoplus_onein(self, ctx: redcommands.Context, n: int) -> None:
        """Set the full transformation chance to one in N.

        N must be from 1 to 1,000,000. One means every eligible message gets a full
        transformation.
        """
        if n < 1 or n > 1_000_000:
            return await self._reply(
                ctx,
                "Enter a number from 1 to 1,000,000. The full transformation chance is 1 in that number.",
                tone="warning",
            )
        await self.config.guild(ctx.guild).one_in.set(int(n))
        await self._presentation.confirm(ctx)

    @owoplus.group(name="prob", autohelp=False)
    async def owoplus_prob(self, ctx: redcommands.Context) -> None:
        """Manage transformation probabilities for members."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @owoplus_prob.command(name="add")
    async def owoplus_prob_add(self, ctx: redcommands.Context, member: discord.Member, n: int):
        """Set a member's full transformation chance to one in N.

        N must be from 1 to 1,000,000. Overrides the server probability for this member.
        """
        if not 1 <= n <= 1_000_000:
            raise redcommands.BadArgument("Probability denominator must be from 1 to 1,000,000.")
        async with self.config.guild(ctx.guild).user_probs() as probabilities:
            probabilities[str(member.id)] = n
        await self._presentation.confirm(ctx)

    @owoplus_prob.command(name="remove")
    async def owoplus_prob_remove(self, ctx: redcommands.Context, member: discord.Member):
        """Remove a member's custom transformation probability."""
        async with self.config.guild(ctx.guild).user_probs() as probabilities:
            removed = probabilities.pop(str(member.id), None)
        await self._reply(
            ctx,
            "Override removed." if removed is not None else "No override was set.",
            tone="success" if removed is not None else "info",
        )

    @owoplus_prob.command(name="list")
    async def owoplus_prob_list(self, ctx: redcommands.Context) -> None:
        """List custom member transformation probabilities."""
        data = await self.config.guild(ctx.guild).user_probs()
        if not data:
            return await self._reply(ctx, "No member probability overrides configured.")
        out = []
        for uid, n in data.items():
            m = ctx.guild.get_member(int(uid))
            out.append(f"- {(m.mention if m else uid)}: 1/{n}")
        await self._reply(ctx, embed=_embed("Probability Overrides", desc=("\n".join(out))))

    @owoplus.command(name="preview")
    async def owoplus_preview(self, ctx: redcommands.Context, *, text: str) -> None:
        """Preview a transformation without replacing a message.

        Uses the server probability and does not post a webhook replacement or delete the
        original message.
        """
        conf = await self.config.guild(ctx.guild).all()
        n = conf["one_in"]
        forced = self._has_key_trigger(text)
        roll = 0 if n <= 1 else random.randrange(n)
        full = (n <= 1) or (roll == 0)
        mode = "full" if full else ("keys" if forced else "none")
        out = self._render_message_mode(
            text, mode=mode, use_haiku=bool(conf.get("haiku_enabled", True))
        )
        e = _embed(
            "OwoPlus - Preview",
            desc="A preview of this message's transformation.",
        )
        e.add_field(
            name="Mode",
            value={"full": "Full transformation", "keys": "Key substitutions", "none": "Unchanged"}[
                mode
            ],
        )
        e.add_field(name="Full transformation chance", value=f"1 in {n:,}")
        e.add_field(
            name="Haiku", value="Enabled" if conf.get("haiku_enabled", True) else "Disabled"
        )
        e.add_field(
            name="Output", value=discord.utils.escape_markdown(out) or "(empty)", inline=False
        )
        await self._reply(ctx, embed=e)

    @owoplus.command(name="diag")
    async def owoplus_diag(self, ctx: redcommands.Context) -> None:
        """Check transformation settings and channel permissions."""
        g = await self.config.guild(ctx.guild).all()
        perms = (
            ctx.channel.permissions_for(ctx.guild.me)
            if isinstance(ctx.channel, (discord.TextChannel, discord.Thread))
            else None
        )  # type: ignore
        payload = "\n".join(
            [
                f"enabled={g['enabled']} one_in=1/{g['one_in']} owner_bypass={g['owner_bypass']} haiku_enabled={g.get('haiku_enabled', True)}",
                f"here perms: view={getattr(perms, 'view_channel', None)} send={getattr(perms, 'send_messages', None)} manage_messages={getattr(perms, 'manage_messages', None)} manage_webhooks={getattr(perms, 'manage_webhooks', None)}",
                f"overrides={len(g['user_probs'])}",
            ]
        )
        await self._reply(ctx, embed=_embed("OwoPlus - Diag", desc=settings(payload)))

    @owoplus.command(name="test")
    async def owoplus_test(self, ctx):
        """Repost your recent message through the webhook.

        Performs a real repost and attempts to delete the original, even if automatic
        transformations are disabled or owner bypass is enabled. The original is retained if
        replacement fails. Use preview for a read-only sample.
        """
        prefixes = await self.bot.get_valid_prefixes(ctx.guild)
        last = None
        async for message in ctx.channel.history(limit=50, before=ctx.message.created_at):
            if (
                message.author.id == ctx.author.id
                and not message.webhook_id
                and message.content
                and not self._starts_with_prefixes(message.content, prefixes)
            ):
                last = message
                break
        if last is None:
            return await self._reply(
                ctx, embed=_embed("OwoPlus test", desc="No eligible recent message found.")
            )
        conf = await self._settings(ctx.guild)
        mode = self._choose_mode(last.author, last.content, conf)
        content = await self._render_async(
            last.content, mode, bool(conf.get("haiku_enabled", True))
        )
        success = await self._repost(last, content)
        await self._reply(
            ctx,
            embed=_embed(
                "OwoPlus test",
                desc="Reposted successfully."
                if success
                else "Original retained. Check permissions, rich content, and attachment limits.",
            ),
            tone="success" if success else "warning",
        )

    # ---------- listener ----------
    @commands.Cog.listener()
    @guild_enabled
    async def on_message(self, message):
        if not message.guild or message.author.bot or message.webhook_id:
            return
        conf = await self._settings(message.guild)
        if not await self._should_process(message, conf):
            return
        original = message.content
        mode = self._choose_mode(message.author, original, conf)
        if mode == "none" and not conf.get("haiku_enabled", True):
            return
        output = await self._render_async(original, mode, bool(conf.get("haiku_enabled", True)))
        if output != original:
            await self._repost(message, output)

    async def cog_load(self):
        await asyncio.to_thread(HaikuMeter.initialize)

    def cog_unload(self):
        self._wh_cache.clear()
        self._settings_cache.clear()
        self._webhook_locks.clear()

    async def cog_before_invoke(self, ctx):
        await prepare_hybrid(ctx)
        self._settings_cache.pop(ctx.guild.id, None)

    async def cog_after_invoke(self, ctx):
        self._settings_cache.pop(ctx.guild.id, None)

    async def _settings(self, guild):
        now = time.monotonic()
        cached = self._settings_cache.get(guild.id)
        if cached is not None and now - cached[0] < 5:
            return cached[1]
        async with self._settings_locks[guild.id]:
            now = time.monotonic()
            cached = self._settings_cache.get(guild.id)
            if cached is not None and now - cached[0] < 5:
                return cached[1]
            settings = await self.config.guild(guild).all()
            self._settings_cache[guild.id] = (now, settings)
            return settings

    def _choose_mode(self, member, text, conf):
        n = self._one_in(member, conf)
        if n == 1 or random.randrange(n) == 0:
            return "full"
        return "keys" if self._has_key_trigger(text) else "none"

    async def _render_async(self, raw, mode, use_haiku):
        async with self._render_gate:
            return await asyncio.to_thread(
                self._render_message_mode, raw, mode, use_haiku=use_haiku
            )

    async def _repost(self, message, content):
        # Preserve rich content that cannot be faithfully copied by this transformer.
        if not content or message.embeds or len(message.attachments) > 10:
            return False
        files, sent = [], []
        channel = message.channel
        base = channel.parent if isinstance(channel, discord.Thread) else channel
        permissions = channel.permissions_for(message.guild.me)
        if not permissions.manage_messages:
            return False
        try:
            for attachment in message.attachments:
                files.append(await attachment.to_file())
            hook = await self._ensure_webhook(channel)
            if hook is None:
                return False
            for index, part in enumerate(self._chunk_message(content)):
                posted = await self._send_via_webhook(
                    hook,
                    channel=channel,
                    author=message.author,
                    content=part,
                    files=files if index == 0 else None,
                    wait=True,
                )
                sent.append(posted)
            try:
                await message.delete()
            except discord.NotFound:
                pass
            return True
        except (
            discord.HTTPException,
            OSError,
            ValueError,
            asyncio.CancelledError,
        ) as error:
            self._wh_cache.pop(getattr(base, "id", 0), None)
            for posted in sent:
                try:
                    await posted.delete()
                except discord.HTTPException:
                    log.warning("Could not roll back a partial webhook repost", exc_info=True)
            log.debug("Repost failed; original message retained", exc_info=True)
            if isinstance(error, asyncio.CancelledError):
                raise
            return False
        finally:
            for file in files:
                file.close()
                file.fp.close()

    async def red_delete_data_for_user(self, *, requester, user_id):
        for guild_id in await self.config.all_guilds():
            group = self.config.guild_from_id(guild_id)
            async with group.user_probs() as probabilities:
                probabilities.pop(str(user_id), None)
            self._settings_cache.pop(guild_id, None)

    async def red_get_data_for_user(self, *, user_id):
        data = {
            str(gid): conf["user_probs"][str(user_id)]
            for gid, conf in (await self.config.all_guilds()).items()
            if str(user_id) in conf.get("user_probs", {})
        }
        return {"owoplus.json": io.BytesIO(json.dumps(data, indent=2).encode())} if data else {}

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        self._settings_cache.pop(guild.id, None)
        self._settings_locks.pop(guild.id, None)
        for channel_id, webhook in list(self._wh_cache.items()):
            if webhook.guild_id == guild.id:
                self._wh_cache.pop(channel_id, None)
                self._webhook_locks.pop(channel_id, None)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        self._wh_cache.pop(channel.id, None)
        self._webhook_locks.pop(channel.id, None)

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        self._wh_cache.pop(channel.id, None)
