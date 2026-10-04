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
from typing import Callable, List, Optional, Tuple, Union
from weakref import WeakValueDictionary

import aiohttp
import discord
from discord.ext import commands
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config

from .command_support import finish_configuration_audit, prepare_hybrid
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
from .features import (
    FEATURE_DEFAULTS,
    STYLE_WORDS,
    channel_allowed,
    channel_features,
    keyword_match,
    replace_keywords,
    transform_style,
    valid_word,
)
from .fun import FUN_DEFAULTS, POETRY_DEFAULTS, FunCommands, style_name
from .haiku import (
    Haiku,
    HaikuMeter,
    _count_syllables,
    _detect_haiku_breaks,
    _normalize_for_haiku,
    _reflow_text_as_haiku,
)
from .interactive import SetupView, close_views
from .presentation import Presentation, settings
from .repost import (
    MAX_MESSAGE_BYTES,
    RepostBudget,
    attachment_size,
    bounded_repost,
    download_attachment,
)

log = logging.getLogger(__name__)


def _embed(
    title: str,
    *,
    color: int | discord.Color = discord.Color.blurple(),
    desc: Optional[str] = None,
) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color)


class OwoPlus(FunCommands, redcommands.Cog):
    """Webhook message transformations and automatic haiku formatting."""

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self._presentation = Presentation("OwoPlus", "owo")
        self.config: Config = Config.get_conf(self, identifier=0x5E0F1A, force_registration=True)
        self.config.register_guild(
            **DEFAULTS_GUILD,
            features=FEATURE_DEFAULTS,
            fun_settings=FUN_DEFAULTS,
            poetry=POETRY_DEFAULTS,
        )
        self._wh_cache = OrderedDict()
        self._webhook_locks = WeakValueDictionary()
        self._settings_cache = {}
        self._settings_locks = defaultdict(asyncio.Lock)
        self._render_gate = asyncio.Semaphore(4)
        self._transform_times = OrderedDict()
        self._views = set()
        self._closing = False
        self._undos = OrderedDict()
        self._fun_task = None
        self._attachment_session = None
        self._repost_budget = RepostBudget()

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
    def _render_message_mode(self, raw: str, mode: str, *, use_haiku: bool, features=None) -> str:
        """
        mode: 'full' | 'keys' | 'none'
        Haiku (when enabled) returns reflowed original (no OWO), fully italicized, and ends with 🌸.
        """
        if use_haiku:
            plain = self._plain_text_if_no_code(raw)
            if plain:
                cuts = (
                    _detect_haiku_breaks(plain, features["syllables"])
                    if features
                    else self._detect_haiku_breaks(plain)
                )
                if cuts:
                    haiku = self._reflow_text_as_haiku(plain, cuts)
                    haiku = self._format_haiku_lines(haiku)  # strip leading spaces
                    haiku = self._add_haiku_suffix(haiku)
                    haiku = self._format_haiku_lines(haiku)  # final pass
                    return self._wrap_all_italics(haiku)

        result: List[str] = []
        intensity = (
            features["intensity"]
            if features and features["intensity"]
            else self._auto_intensity(len(raw or ""))
        )
        for seg, is_code in self._split_code_segments(raw):
            if is_code or mode == "none":
                result.append(seg)
                continue
            if features and features.get("style", "owo") != "owo":
                result.append(transform_style(seg, features, self._case_like, full=mode == "full"))
                continue
            if mode == "keys":
                mapped = (
                    replace_keywords(seg, features, self._case_like)
                    if features
                    else self._apply_key_map(seg)
                )
                mapped = self._ensure_targets_italic(mapped)
                result.append(mapped)
            else:
                seed = (
                    replace_keywords(seg, features, self._case_like)
                    if features
                    else self._apply_key_map(seg)
                )
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
            self._closing
            or not message.guild
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
        features = conf.get("features", FEATURE_DEFAULTS)
        if str(message.author.id) in features["optouts"] or not channel_allowed(
            message.channel, features
        ):
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
        cfg = await self._settings(g)
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
            value=f"{len(cfg['user_probs']):,} custom probabilities\n{len(cfg['features']['optouts']):,} personal opt-outs",
            inline=True,
        )
        features = cfg["features"]
        e.add_field(
            name="Channel scope",
            value=f"**Mode** · {features['channel_mode']}\n"
            f"**Allowed channels** · {len(features['allowed'])}\n**Excluded channels** · {len(features['excluded'])}",
            inline=True,
        )
        e.add_field(
            name="Transform controls",
            value=f"**Keyword triggers** · {'Enabled' if features['keywords'] else 'Disabled'}\n"
            f"**Intensity** · {features['intensity'] or 'Automatic'}\n**Repost cooldown** · {features['cooldown']}s\n"
            f"**Custom words / syllable corrections** · {len(features['words'])} / {len(features['syllables'])}",
            inline=False,
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
        e.add_field(
            name="Channel styles",
            value=f"`{p}owo style set #channel <style> [minutes]` · `{p}owo style clear #channel`\n`{p}stylize <style> <text>` · `{p}customstyle`",
            inline=False,
        )
        e.add_field(
            name="Member commands",
            value=f"`{p}owooptout [enabled]` · `{p}owoify <text>` · `{p}haiku <text>`\n`{p}owoundo` · `{p}haikuhall` · `{p}haikucontest`",
            inline=False,
        )
        e.add_field(
            name="Scopes and custom words",
            value=f"`{p}owo setup` · `{p}owo channels mode <all|allowlist>`\n"
            f"`{p}owo channels allow|exclude|remove <channel>` · `{p}owo intensity <0..5>`\n"
            f"`{p}owo words add <word> <replacement>` · `{p}owo syllables set <word> <count>` · `{p}owo cooldown <seconds>`",
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
        features = await self.config.guild(ctx.guild).features()
        syl = [
            features["syllables"][w.lower()]
            if w.lower() in features["syllables"]
            else self._count_syllables(w)
            for w in words
        ]
        cum = []
        c = 0
        for s in syl:
            c += s
            cum.append(c)
        cuts = _detect_haiku_breaks(norm, features["syllables"])
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
        conf = dict(await self._settings(ctx.guild))
        conf["features"] = channel_features(ctx.channel, conf["features"])
        n = conf["one_in"]
        forced = any(
            keyword_match(seg, conf["features"])
            for seg, code in self._split_code_segments(text)
            if not code
        )
        roll = 0 if n <= 1 else random.randrange(n)
        full = (n <= 1) or (roll == 0)
        mode = "full" if full else ("keys" if forced else "none")
        out = await self._render_async(
            text, mode, bool(conf.get("haiku_enabled", True)), conf["features"]
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
        e.add_field(name="Style", value=conf["features"]["style"].title())
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
        g = await self._settings(ctx.guild)
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
        conf = {**conf, "features": channel_features(ctx.channel, conf["features"])}
        mode = self._choose_mode(last.author, last.content, conf)
        content = await self._render_async(
            last.content, mode, bool(conf.get("haiku_enabled", True)), conf["features"]
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
    @bounded_repost
    @guild_enabled
    async def on_message(self, message):
        if not message.guild or message.author.bot or message.webhook_id:
            return
        conf = await self._settings(message.guild)
        if not await self._should_process(message, conf):
            return
        conf = {**conf, "features": channel_features(message.channel, conf["features"])}
        original = message.content
        mode = self._choose_mode(message.author, original, conf)
        if mode == "none" and not conf.get("haiku_enabled", True):
            return
        output = await self._render_async(
            original, mode, bool(conf.get("haiku_enabled", True)), conf["features"]
        )
        if output != original:
            latest = await self._settings(message.guild)
            if (
                channel_features(message.channel, latest["features"])["style"]
                != conf["features"]["style"]
                or not await self._should_process(message, latest)
                or await self.bot.cog_disabled_in_guild(self, message.guild)
            ):
                return
            key = (message.guild.id, message.author.id)
            now = time.monotonic()
            if now - self._transform_times.get(key, -3601) < conf["features"]["cooldown"]:
                return
            self._transform_times[key] = now
            while len(self._transform_times) > 50000:
                self._transform_times.popitem(last=False)
            if not await self._repost(message, output) and self._transform_times.get(key) == now:
                self._transform_times.pop(key, None)

    async def cog_load(self):
        self._closing = False
        await asyncio.to_thread(HaikuMeter.initialize)
        self._attachment_session = aiohttp.ClientSession()
        self._fun_task = asyncio.create_task(self._fun_loop(), name="owoplus-haiku-activities")

    async def cog_unload(self):
        self._closing = True
        await self._repost_budget.close()
        if self._attachment_session:
            await self._attachment_session.close()
            self._attachment_session = None
        if self._fun_task:
            self._fun_task.cancel()
            await asyncio.gather(self._fun_task, return_exceptions=True)
            self._fun_task = None
        await close_views(self)
        self._wh_cache.clear()
        self._settings_cache.clear()
        self._webhook_locks.clear()

    async def cog_before_invoke(self, ctx):
        await prepare_hybrid(ctx)
        self._settings_cache.pop(ctx.guild.id, None)

    async def cog_after_invoke(self, ctx):
        finish_configuration_audit(ctx)
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
            group = self.config.guild(guild)
            settings = {key: await group.get_attr(key)() for key in (*DEFAULTS_GUILD, "features")}
            self._settings_cache[guild.id] = (now, settings)
            return settings

    def _choose_mode(self, member, text, conf):
        n = self._one_in(member, conf)
        if n == 1 or random.randrange(n) == 0:
            return "full"
        found = any(
            keyword_match(seg, conf.get("features", FEATURE_DEFAULTS))
            for seg, code in self._split_code_segments(text)
            if not code
        )
        return "keys" if found else "none"

    async def _render_async(self, raw, mode, use_haiku, features=None):
        async with self._render_gate:
            if self._closing:
                return raw
            return await asyncio.to_thread(
                self._render_message_mode, raw, mode, use_haiku=use_haiku, features=features
            )

    async def _repost(self, message, content):
        with self._repost_budget.slot(message.guild.id) as admitted:
            if not admitted:
                return False
            return await self._repost_bounded(message, content)

    async def _repost_bounded(self, message, content):
        # Preserve rich content that cannot be faithfully copied by this transformer.
        if self._closing or not content or message.embeds or len(message.attachments) > 10:
            return False
        files, sent = [], []
        original_deleted = False
        channel = message.channel
        base = channel.parent if isinstance(channel, discord.Thread) else channel
        permissions = channel.permissions_for(message.guild.me)
        if not permissions.manage_messages:
            return False
        try:
            if sum(attachment_size(item) for item in message.attachments) > MAX_MESSAGE_BYTES:
                return False
            remaining = MAX_MESSAGE_BYTES
            for attachment in message.attachments:
                file, size = await download_attachment(
                    self._attachment_session, attachment, remaining
                )
                files.append(file)
                remaining -= size
            hook = await self._ensure_webhook(channel)
            if hook is None:
                return False
            for index, part in enumerate(self._chunk_message(content)):
                if self._closing:
                    raise asyncio.CancelledError
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
                if self._closing:
                    raise asyncio.CancelledError
                await message.delete()
            except discord.NotFound:
                pass
            original_deleted = True
            # The original is gone. A control failure must never roll back the repost.
            try:
                await self._attach_undo(message, hook, sent)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Undo control failed; successful repost retained", exc_info=True)
            return True
        except (
            discord.HTTPException,
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError,
            ValueError,
            asyncio.CancelledError,
        ) as error:
            if original_deleted:
                if isinstance(error, asyncio.CancelledError):
                    raise
                return True
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

    async def _set_feature_setting(self, guild, key, value):
        group = self.config.guild(guild).features
        async with group.get_lock():
            await group.get_attr(key).set(value)
        self._settings_cache.pop(guild.id, None)

    @redcommands.hybrid_command(name="owooptout")
    @redcommands.guild_only()
    async def owooptout(self, ctx, enabled: bool = True):
        """Opt out of automatic transformations here."""
        async with self.config.guild(ctx.guild).features() as data:
            if enabled:
                data["optouts"][str(ctx.author.id)] = True
            else:
                data["optouts"].pop(str(ctx.author.id), None)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._reply(
            ctx,
            "Automatic transformations are disabled for you here."
            if enabled
            else "You can receive automatic transformations again.",
            tone="success",
        )

    @redcommands.hybrid_command(name="owoify")
    @redcommands.guild_only()
    async def owoify(self, ctx, *, text: str):
        """Transform supplied text without deleting a message."""
        if len(text) > 2000:
            raise redcommands.BadArgument("Use at most 2000 characters.")
        conf = await self._settings(ctx.guild)
        output = await self._render_async(text, "full", False, conf["features"])
        await self._reply(ctx, output, title="Owoify")

    @redcommands.hybrid_command(name="haiku")
    @redcommands.guild_only()
    async def haiku_command(self, ctx, *, text: str):
        """Format supplied text as a detected 5-7-5 haiku."""
        if len(text) > 300:
            raise redcommands.BadArgument("Use at most 300 characters for haiku detection.")
        conf = await self._settings(ctx.guild)
        output = await self._render_async(text, "none", True, conf["features"])
        if output == text:
            raise redcommands.BadArgument(
                "No English 5-7-5 haiku was detected. Use owo poem diag to inspect syllables."
            )
        await self._reply(ctx, output, title="Haiku")

    @redcommands.hybrid_command(name="stylize")
    @redcommands.guild_only()
    async def stylize(self, ctx, style: str, *, text: str):
        """Preview supplied text in a chosen style."""
        if len(text) > 2000:
            raise redcommands.BadArgument("Use at most 2000 characters.")
        conf = await self._settings(ctx.guild)
        style = style_name(style)
        if style not in STYLE_WORDS and style not in conf["features"]["custom_styles"]:
            raise redcommands.BadArgument("Choose owo, pirate, robot, or a name from customstyle.")
        output = await self._render_async(text, "full", False, {**conf["features"], "style": style})
        await self._reply(ctx, output, title=f"{style.title()} preview")

    @owoplus.group(name="style", autohelp=False, fallback="list")
    async def owo_style(self, ctx):
        """List channel styles and their expiry times."""
        data = await self.config.guild(ctx.guild).features()
        now = time.time()
        lines = []
        for cid, entry in data["channel_styles"].items():
            expiry = entry["expires"]
            state = (
                "Permanent"
                if not expiry
                else (f"Until <t:{int(expiry)}:R>" if expiry > now else "Expired")
            )
            lines.append(f"<#{cid}>: {entry['style'].title()} · {state}")
        await self._reply(
            ctx, "\n".join(lines) or "All channels use Owo. Set a channel style to override it."
        )

    @owo_style.command(name="set")
    async def owo_style_set(
        self,
        ctx,
        channel: Union[discord.TextChannel, discord.ForumChannel, discord.Thread],
        style: str,
        minutes: int = 0,
    ):
        """Set a permanent or temporary channel style."""
        if not 0 <= minutes <= 10080:
            raise redcommands.BadArgument("Use 0 for permanent, or 1 to 10080 minutes.")
        now = time.time()
        style = style_name(style)
        async with self.config.guild(ctx.guild).features() as data:
            if style not in STYLE_WORDS and style not in data["custom_styles"]:
                raise redcommands.BadArgument(
                    "Choose owo, pirate, robot, or a name from customstyle."
                )
            data["channel_styles"] = {
                cid: entry
                for cid, entry in data["channel_styles"].items()
                if not entry["expires"] or entry["expires"] > now
            }
            if str(channel.id) not in data["channel_styles"] and len(data["channel_styles"]) >= 100:
                raise redcommands.BadArgument("Keep at most 100 active channel styles.")
            data["channel_styles"][str(channel.id)] = {
                "style": style,
                "expires": now + minutes * 60 if minutes else 0,
            }
        self._settings_cache.pop(ctx.guild.id, None)
        await self._reply(
            ctx,
            f"{channel.mention} uses {style.title()} "
            + (f"for {minutes} minutes." if minutes else "until cleared."),
            tone="success",
        )

    @owo_style.command(name="clear")
    async def owo_style_clear(
        self, ctx, channel: Union[discord.TextChannel, discord.ForumChannel, discord.Thread]
    ):
        """Remove a channel override and restore inheritance."""
        async with self.config.guild(ctx.guild).features() as data:
            data["channel_styles"].pop(str(channel.id), None)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @owoplus.group(name="channels", autohelp=False)
    async def owo_channels(self, ctx):
        """Choose where automatic transformations run."""
        data = await self.config.guild(ctx.guild).features()
        await self._reply(
            ctx,
            f"Mode: {data['channel_mode']}\nAllowed: {', '.join(f'<#{cid}>' for cid in data['allowed']) or 'None'}\nExcluded: {', '.join(f'<#{cid}>' for cid in data['excluded']) or 'None'}",
        )

    @owo_channels.command(name="mode")
    async def owo_channels_mode(self, ctx, mode: str):
        """Use all accessible channels or only an allowlist."""
        if mode not in {"all", "allowlist"}:
            raise redcommands.BadArgument(
                "Choose all or allowlist. An empty allowlist disables automatic processing everywhere."
            )
        await self._set_feature_setting(ctx.guild, "channel_mode", mode)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    async def _channel_option(self, ctx, channel, field):
        async with self.config.guild(ctx.guild).features() as data:
            if channel.id not in data[field]:
                data[field].append(channel.id)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @owo_channels.command(name="allow")
    async def owo_channels_allow(
        self, ctx, channel: Union[discord.TextChannel, discord.ForumChannel]
    ):
        """Add a text channel and its threads to the allowlist."""
        await self._channel_option(ctx, channel, "allowed")

    @owo_channels.command(name="exclude")
    async def owo_channels_exclude(
        self, ctx, channel: Union[discord.TextChannel, discord.ForumChannel]
    ):
        """Exclude a text channel and its threads."""
        await self._channel_option(ctx, channel, "excluded")

    @owo_channels.command(name="remove")
    async def owo_channels_remove(
        self, ctx, channel: Union[discord.TextChannel, discord.ForumChannel]
    ):
        """Remove a channel from both transformation lists."""
        async with self.config.guild(ctx.guild).features() as data:
            for field in ("allowed", "excluded"):
                data[field] = [cid for cid in data[field] if cid != channel.id]
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @owoplus.command(name="keywords")
    async def owo_keywords(self, ctx, enabled: bool):
        """Enable or disable keyword substitutions."""
        await self._set_feature_setting(ctx.guild, "keywords", enabled)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @owoplus.command(name="intensity")
    async def owo_intensity(self, ctx, value: int):
        """Set transformation intensity, 0 for automatic."""
        if not 0 <= value <= 5:
            raise redcommands.BadArgument(
                "Choose 0 for automatic, or 1 through 5 for fixed intensity."
            )
        await self._set_feature_setting(ctx.guild, "intensity", value)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @owoplus.command(name="cooldown")
    async def owo_cooldown(self, ctx, seconds: int):
        """Set an automatic transformation cooldown per member."""
        if not 0 <= seconds <= 3600:
            raise redcommands.BadArgument("Choose 0 through 3600 seconds; 0 disables the cooldown.")
        await self._set_feature_setting(ctx.guild, "cooldown", seconds)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @owoplus.group(name="words", autohelp=False)
    async def owo_words(self, ctx):
        """Manage custom whole-word replacements."""
        await self.owo_words_list.callback(self, ctx)

    @owo_words.command(name="list")
    async def owo_words_list(self, ctx):
        """List active keyword replacements."""
        data = await self.config.guild(ctx.guild).features()
        replacements = {**KEY_MAP, **data["words"]}
        await self._reply(
            ctx,
            "\n".join(
                f"**{word}** → {discord.utils.escape_markdown(replacement) if replacement is not None else 'Disabled'}"
                for word, replacement in sorted(replacements.items())
            ),
        )

    @owo_words.command(name="add")
    async def owo_words_add(self, ctx, original: str, *, replacement: str):
        """Set a case-preserving whole-word replacement."""
        word = valid_word(original)
        replacement = replacement.strip()
        if not replacement or len(replacement) > 60 or any(ord(char) < 32 for char in replacement):
            raise redcommands.BadArgument(
                "Use a replacement of 1 to 60 characters without line breaks."
            )
        async with self.config.guild(ctx.guild).features() as data:
            if word not in data["words"] and len(data["words"]) >= 100:
                raise redcommands.BadArgument("You can configure 100 custom keyword entries.")
            data["words"][word] = replacement
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @owo_words.command(name="remove")
    async def owo_words_remove(self, ctx, original: str):
        """Disable a built-in keyword or remove a custom one."""
        word = valid_word(original)
        async with self.config.guild(ctx.guild).features() as data:
            if any(word in words for words in STYLE_WORDS.values()):
                if word not in data["words"] and len(data["words"]) >= 100:
                    raise redcommands.BadArgument("You can configure 100 custom keyword entries.")
                data["words"][word] = None
            else:
                data["words"].pop(word, None)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @owo_words.command(name="reset")
    async def owo_words_reset(self, ctx, confirm: str):
        """Restore the original keyword map with confirmation."""
        if confirm != "yes":
            raise redcommands.BadArgument(
                "Use owo words reset yes to erase custom words and restore the four original keywords."
            )
        await self._set_feature_setting(ctx.guild, "words", {})
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @owoplus.group(name="syllables", autohelp=False)
    async def owo_syllables(self, ctx):
        """Correct syllable counts for this server."""
        await self.owo_syllables_list.callback(self, ctx)

    @owo_syllables.command(name="list")
    async def owo_syllables_list(self, ctx):
        """Show server-specific syllable corrections."""
        data = await self.config.guild(ctx.guild).features.syllables()
        await self._reply(
            ctx,
            "\n".join(f"**{word}** · {count}" for word, count in sorted(data.items()))
            or "No custom syllable counts.",
        )

    @owo_syllables.command(name="set")
    async def owo_syllables_set(self, ctx, word: str, count: int):
        """Set a word's syllable count for haiku detection."""
        word = valid_word(word)
        if not 1 <= count <= 10:
            raise redcommands.BadArgument("Choose 1 through 10 syllables.")
        async with self.config.guild(ctx.guild).features() as data:
            if word not in data["syllables"] and len(data["syllables"]) >= 500:
                raise redcommands.BadArgument("You can configure 500 syllable corrections.")
            data["syllables"][word] = count
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @owo_syllables.command(name="remove")
    async def owo_syllables_remove(self, ctx, word: str):
        """Remove a server-specific syllable correction."""
        async with self.config.guild(ctx.guild).features() as data:
            data["syllables"].pop(valid_word(word), None)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @owoplus.command(name="setup")
    async def owo_setup(self, ctx):
        """Choose transformation settings in a guided panel."""

        async def update(context, key, value):
            root, field = key.split(".")
            if field in {"allow_channel", "exclude_channel"}:
                async with self.config.guild(context.guild).features() as data:
                    target = "allowed" if field == "allow_channel" else "excluded"
                    if value not in data[target]:
                        data[target].append(value)
                    if field == "allow_channel":
                        data["channel_mode"] = "allowlist"
            elif root == "guild":
                await self.config.guild(context.guild).set_raw(field, value=value)
            else:
                await self._set_feature_setting(context.guild, field, value)
            self._settings_cache.pop(context.guild.id, None)

        view = SetupView(
            self,
            ctx,
            "owo setup",
            [
                ("guild.enabled", "automatic transformations", "toggle"),
                ("guild.haiku_enabled", "haiku formatting", "toggle"),
                ("features.keywords", "keyword replacements", "toggle"),
                ("features.allow_channel", "Allow a channel and enable allowlist mode", "text"),
                ("features.exclude_channel", "Exclude a channel", "text"),
            ],
            update,
        )
        view.message = await self._reply(
            ctx,
            "Choose automatic transformation options. Use owo channels for an allowlist/exclusions, owo words for custom replacements, and owo syllables for pronunciation corrections. Members can use owooptout at any time.",
            title="Owo setup",
            view=view,
        )

    async def red_delete_data_for_user(self, *, requester, user_id):
        await self._delete_fun_user(user_id)
        for guild_id in await self.config.all_guilds():
            group = self.config.guild_from_id(guild_id)
            async with group.user_probs() as probabilities:
                probabilities.pop(str(user_id), None)
            async with group.features() as data:
                data["optouts"].pop(str(user_id), None)
            self._settings_cache.pop(guild_id, None)
            self._transform_times.pop((guild_id, user_id), None)

    async def red_get_data_for_user(self, *, user_id):
        data = {
            str(gid): {
                "probability": conf["user_probs"].get(str(user_id)),
                "optout": str(user_id) in conf["features"]["optouts"],
            }
            for gid, conf in (await self.config.all_guilds()).items()
            if str(user_id) in conf.get("user_probs", {})
            or str(user_id) in conf["features"]["optouts"]
        }
        for gid, record in (await self._fun_user_data(user_id)).items():
            data.setdefault(gid, {}).update(record)
        return {"owoplus.json": io.BytesIO(json.dumps(data, indent=2).encode())} if data else {}

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        for view in tuple(self._undos.values()):
            if view.guild_id == guild.id:
                await view.on_timeout()
        self._settings_cache.pop(guild.id, None)
        self._settings_locks.pop(guild.id, None)
        for key in tuple(self._transform_times):
            if key[0] == guild.id:
                self._transform_times.pop(key, None)
        for channel_id, webhook in list(self._wh_cache.items()):
            if webhook.guild_id == guild.id:
                self._wh_cache.pop(channel_id, None)
                self._webhook_locks.pop(channel_id, None)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        for view in tuple(self._undos.values()):
            if view.channel_id == channel.id:
                await view.on_timeout()
        self._wh_cache.pop(channel.id, None)
        self._webhook_locks.pop(channel.id, None)
        async with self.config.guild(channel.guild).features() as data:
            data["channel_styles"].pop(str(channel.id), None)
        self._settings_cache.pop(channel.guild.id, None)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread):
        await self.on_guild_channel_delete(thread)

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        self._wh_cache.pop(channel.id, None)
