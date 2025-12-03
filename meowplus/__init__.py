# path: cogs/owoplus/__init__.py
from __future__ import annotations

import difflib
import random
import re
from typing import Callable, Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config
from redbot.core.utils.chat_formatting import box

__red_end_user_data_statement__ = (
    "This cog stores per-guild preferences for webhook-based message transformation "
    "(enable flag, 1-in-N owo probability, per-user overrides, and owner bypass). "
    "It does not store message contents."
)

DEFAULTS_GUILD = {
    "enabled": False,
    "one_in": 1000,
    "user_probs": {},
    "owner_bypass": True,
}

# ---------- mapping & triggers ----------
KEY_MAP: Dict[str, str] = {"now": "meow", "bro": "bwo", "dude": "duwde", "bud": "bwud"}
KEY_RX = re.compile(r"\b(" + "|".join(map(re.escape, KEY_MAP.keys())) + r")\b", re.IGNORECASE)
TARGETS = sorted({v for v in KEY_MAP.values()})

CODE_SPLIT = re.compile(r"(```[\s\S]*?```|`[^`]*?`)", re.MULTILINE)

OWO_FACES = ["uwu", "owo", ">w<", "^w^", "x3", "~", "nya~", "(⁄˘⁄⁄ ω⁄ ⁄˘⁄)♡"]

EMO = {"ok": "✅", "bad": "⚠️", "core": "🛠️", "msg": "💬", "prob": "🎲", "diag": "🧪", "spark": "✨"}


def _embed(title: str, *, color: int | discord.Color = discord.Color.blurple(), desc: Optional[str] = None) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color)


def _bool_emoji(v: bool) -> str:
    return "🟢" if v else "🔴"


class OwoPlus(redcommands.Cog):
    """Webhook-only cute/owo replacer with auto-intensity 1..5 (short ⇒ stronger, long ⇒ lighter-but-cute)."""

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0x5E0F1A, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._wh_cache: Dict[int, discord.Webhook] = {}

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
                segs.append((text[i:m.start()], False))
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
        """Per-match probabilistic replacement. Expands backrefs for string repls."""
        if prob <= 0.0:
            return s
        rx = re.compile(pattern, flags)

        def _choose(m: re.Match) -> str:
            if random.random() < prob:
                # IMPORTANT: expand backrefs like \1 or \g<1> when repl is a string.
                return m.expand(repl) if isinstance(repl, str) else repl(m)
            return m.group(0)

        return rx.sub(_choose, s)

    @staticmethod
    def _owoify_plain(text: str, intensity: int) -> str:
        # Re-tuned 1..5. Levels 1–2 are still visibly cute.
        prof = {
            # per-match probs for rl/ny/uv/th/tt; others are per-word/per-sentence
            1: dict(rl=0.35, ny=0.85, uv=0.85, th=0.30, tt=0.15, lc_first=False,
                    stutter=0.08, elong=0.06, face=0.18, tilde=0.08),
            2: dict(rl=0.55, ny=0.95, uv=0.95, th=0.40, tt=0.25, lc_first=False,
                    stutter=0.10, elong=0.10, face=0.20, tilde=0.10),
            3: dict(rl=0.75, ny=1.00, uv=1.00, th=0.60, tt=0.45, lc_first=True,
                    stutter=0.14, elong=0.14, face=0.22, tilde=0.12),
            4: dict(rl=0.90, ny=1.00, uv=1.00, th=0.80, tt=0.65, lc_first=True,
                    stutter=0.18, elong=0.18, face=0.25, tilde=0.14),
            5: dict(rl=1.00, ny=1.00, uv=1.00, th=1.00, tt=1.00, lc_first=True,
                    stutter=0.22, elong=0.24, face=0.28, tilde=0.16),
        }[max(1, min(5, int(intensity)))]

        def transliterate(s: str) -> str:
            s = OwoPlus._sub_prob(s, r"[rl]", "w", prof["rl"])
            s = OwoPlus._sub_prob(s, r"[RL]", "W", prof["rl"])
            s = OwoPlus._sub_prob(s, r"(?i)n([aeiou])", r"ny\1", prof["ny"])  # backref expanded via m.expand
            s = OwoPlus._sub_prob(s, r"(?i)ove", "uv", prof["uv"])
            s = OwoPlus._sub_prob(s, r"(?i)th", lambda m: "d" if m.group(0).islower() else "D", prof["th"])
            s = OwoPlus._sub_prob(s, r"(?i)tt", lambda m: "dd" if m.group(0).islower() else "DD", prof["tt"])

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

        return "".join(seg if is_code else transliterate(seg) for seg, is_code in OwoPlus._split_code_segments(text))

    @staticmethod
    def _italicize_changes(original: str, transformed: str) -> str:
        # only italicize replacements/deletions containing word chars
        out: List[str] = []
        wordish = re.compile(r"[A-Za-z0-9]")
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, original, transformed).get_opcodes():
            if tag == "equal":
                out.append(transformed[j1:j2])
            elif tag == "insert":
                out.append(transformed[j1:j2])
            else:
                seg = transformed[j1:j2]
                out.append(f"*{seg}*" if seg and wordish.search(seg) else seg)
        return "".join(out)

    # ---------- italics helpers ----------
    @staticmethod
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
                    out.append(m.group(0) if OwoPlus._inside_italics(seg, start, end) else f"*{m.group(0)}*")
                    i = end
                out.append(seg[i:])
                seg = "".join(out)
            return seg
        parts: List[str] = []
        for seg, is_code in OwoPlus._split_code_segments(text):
            parts.append(seg if is_code else apply(seg))
        return "".join(parts)

    @staticmethod
    def _sanitize_italics_and_ticks(text: str) -> str:
        return text.replace("*`", "* `").replace("`*", "` *")

    # ---------- auto intensity 1..5 ----------
    @staticmethod
    def _auto_intensity(nchars: int) -> int:
        if nchars <= 80:   return 5
        if nchars <= 160:  return 4
        if nchars <= 400:  return 3
        if nchars <= 1200: return 2
        return 1

    @staticmethod
    def _has_key_trigger(text: str) -> bool:
        return any(KEY_RX.search(seg) for seg, is_code in OwoPlus._split_code_segments(text) if not is_code)

    def _render_message(self, raw: str, apply_owo: bool) -> str:
        length = len(raw or "")
        intensity = self._auto_intensity(length)
        result: List[str] = []
        for seg, is_code in self._split_code_segments(raw):
            if is_code:
                result.append(seg); continue
            if not apply_owo:
                result.append(seg); continue
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
        if nl != -1: candidates.append(nl + 1)
        sp = window.rfind(" ")
        if sp != -1: candidates.append(sp + 1)
        return max(candidates) if candidates else len(window)

    def _chunk_message(self, text: str, limit: int = 2000) -> List[str]:
        chunks: List[str] = []
        cur = ""
        for seg, is_code in self._split_code_segments(text):
            piece = seg
            if is_code:
                if len(cur) + len(piece) <= limit:
                    cur += piece
                else:
                    if cur: chunks.append(cur); cur = ""
                    if len(piece) <= limit:
                        cur = piece
                    else:
                        start = 0
                        while start < len(piece):
                            take = piece.find("\n", start, start + limit)
                            if take == -1 or take <= start:
                                take = min(start + limit, len(piece))
                            chunks.append(piece[start:take]); start = take
            else:
                remaining = piece
                while remaining:
                    space = limit - len(cur)
                    if space <= 0:
                        chunks.append(cur); cur = ""; space = limit
                    if len(remaining) <= space:
                        cur += remaining; remaining = ""
                    else:
                        window = remaining[:space]
                        cut = self._find_breakpoint(window)
                        candidate = remaining[:cut]
                        if (candidate.count("*") % 2) or (candidate.count("`") % 2):
                            fb = candidate.rfind(" ")
                            if fb > 0: cut = fb + 1
                        cur += remaining[:cut].rstrip()
                        chunks.append(cur); cur = ""
                        remaining = remaining[cut:].lstrip()
        if cur: chunks.append(cur)
        return [c[:limit] for c in chunks]

    # ---------- gating ----------
    @staticmethod
    def _starts_with_prefixes(text: str, prefixes: List[str]) -> bool:
        return any(p and (text.startswith(p) or text.startswith(p + " ")) for p in prefixes)

    async def _should_process(self, message: discord.Message) -> bool:
        if not message.guild or message.author.bot or message.webhook_id:
            return False
        conf = await self.config.guild(message.guild).all()
        try:
            if conf.get("owner_bypass", True) and await self.bot.is_owner(message.author):
                return False
        except Exception:
            pass
        if not conf["enabled"]:
            return False
        try:
            prefixes = await self.bot.get_valid_prefixes(message.guild)
        except Exception:
            prefixes = []
        if self._starts_with_prefixes(message.content or "", prefixes):
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
            hook = hooks[0] if hooks else await base_ch.create_webhook(name="OwoPlus", reason="OwoPlus")
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
        e = _embed(
            f"OwoPlus — Status {_bool_emoji(cfg['enabled'])}",
            desc="Triggers now/bro/dude/bud → *meow/bwo/duwde/bwud* • any trigger forces uwu • random owo 1/N • auto-intensity 1..5 (1 still cute).",
        )
        e.add_field(
            name=f"{EMO['core']} Core",
            value=box(
                f"enabled = {cfg['enabled']}\n"
                f"one_in  = 1/{cfg['one_in']}\n"
                f"owner_bypass= {cfg['owner_bypass']}",
                lang="ini",
            ),
            inline=False,
        )
        e.add_field(name=f"{EMO['prob']} Overrides", value=(f"user_overrides={len(cfg['user_probs'])}"), inline=False)
        e.set_footer(text="Use `[p]owoplus help` for commands.")
        return e

    # ---------- commands ----------
    @redcommands.group(name="owoplus", invoke_without_command=True)
    @redcommands.guild_only()
    @redcommands.admin_or_permissions(manage_guild=True)
    async def owoplus(self, ctx: redcommands.Context) -> None:
        e = await self._status_embed(ctx.guild)
        await ctx.send(embed=e)

    @owoplus.command(name="help")
    async def owoplus_help(self, ctx: redcommands.Context) -> None:
        p = ctx.clean_prefix
        e = _embed("OwoPlus — Commands", desc=f"{EMO['spark']} Examples use `{p}` as prefix.")
        e.add_field(
            name=f"{EMO['core']} Core",
            value=(
                f"• `{p}owoplus` • `{p}owoplus help` • `{p}owoplus diag`\n"
                f"• `{p}owoplus enable` • `{p}owoplus disable`\n"
                f"• `{p}owoplus test` • `{p}owoplus preview <text>`"
            ),
            inline=False,
        )
        e.add_field(
            name=f"{EMO['prob']} Probability",
            value=(f"• `{p}owoplus onein <N>` (default 1000)\n" f"• `{p}owoplus prob add @user <N>` • `remove @user` • `list`"),
            inline=False,
        )
        e.add_field(name="Auto Intensity", value="Short → strong; Long → light (still cute).", inline=False)
        e.add_field(name="Owner Bypass", value=f"• `{p}owoplus ownerbypass <on|off>`", inline=False)
        await ctx.send(embed=e)

    @owoplus.command(name="ownerbypass")
    async def owoplus_ownerbypass(self, ctx: redcommands.Context, state: Optional[str] = None) -> None:
        if state is None:
            cur = await self.config.guild(ctx.guild).owner_bypass()
            return await ctx.send(embed=_embed(f"Owner bypass is **{'on' if cur else 'off'}**"))
        val = state.lower() in {"on", "true", "yes", "1"}
        await self.config.guild(ctx.guild).owner_bypass.set(val)
        await ctx.tick()

    @owoplus.command(name="enable")
    async def owoplus_enable(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).enabled.set(True)
        await ctx.send(embed=_embed(f"{EMO['ok']} OwoPlus enabled (guild-wide).", color=discord.Color.green()))

    @owoplus.command(name="disable")
    async def owoplus_disable(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).enabled.set(False)
        await ctx.send(embed=_embed(f"{EMO['ok']} OwoPlus disabled (guild-wide).", color=discord.Color.green()))

    @owoplus.command(name="onein")
    async def owoplus_onein(self, ctx: redcommands.Context, n: int) -> None:
        if n < 1 or n > 1_000_000:
            return await ctx.send(embed=_embed("Use 1..1,000,000 (probability = 1/N).", color=discord.Color.orange()))
        await self.config.guild(ctx.guild).one_in.set(int(n))
        await ctx.tick()

    @owoplus.group(name="prob")
    async def owoplus_prob(self, ctx: redcommands.Context) -> None:
        pass

    @owoplus_prob.command(name="add")
    async def owoplus_prob_add(self, ctx: redcommands.Context, member: discord.Member, n: int) -> None:
        if n < 1 or n > 1_000_000:
            return await ctx.send(embed=_embed("Use 1..1,000,000 (probability = 1/N).", color=discord.Color.orange()))
        data = await self.config.guild(ctx.guild).user_probs()
        data[str(member.id)] = int(n)
        await self.config.guild(ctx.guild).user_probs.set(data)
        await ctx.send(embed=_embed(f"{EMO['ok']} Set {member.mention} to 1/{n}.", color=discord.Color.green()))

    @owoplus_prob.command(name="remove")
    async def owoplus_prob_remove(self, ctx: redcommands.Context, member: discord.Member) -> None:
        data = await self.config.guild(ctx.guild).user_probs()
        removed = data.pop(str(member.id), None) is not None
        await self.config.guild(ctx.guild).user_probs.set(data)
        msg = "Removed." if removed else "No override was set."
        await ctx.send(embed=_embed(msg, color=discord.Color.green() if removed else discord.Color.orange()))

    @owoplus_prob.command(name="list")
    async def owoplus_prob_list(self, ctx: redcommands.Context) -> None:
        data = await self.config.guild(ctx.guild).user_probs()
        if not data:
            return await ctx.send(embed=_embed("No overrides.", color=discord.Color.orange()))
        out = []
        for uid, n in data.items():
            m = ctx.guild.get_member(int(uid))
            out.append(f"- {(m.mention if m else uid)}: 1/{n}")
        await ctx.send(embed=_embed("Probability Overrides", desc=box("\n".join(out), lang="ini")))

    @owoplus.command(name="preview")
    async def owoplus_preview(self, ctx: redcommands.Context, *, text: str) -> None:
        meow_only = self._render_message(text, apply_owo=False)
        meow_owo = self._render_message(text, apply_owo=True)
        used = self._auto_intensity(len(text))
        e = _embed("OwoPlus — Preview", desc=f"Auto intensity for this text: **{used}**")
        e.add_field(name="MEOW", value=box(meow_only, lang="ini"), inline=False)
        e.add_field(name="OWO", value=box(meow_owo, lang="ini"), inline=False)
        await ctx.send(embed=e)

    @owoplus.command(name="diag")
    async def owoplus_diag(self, ctx: redcommands.Context) -> None:
        g = await self.config.guild(ctx.guild).all()
        perms = ctx.channel.permissions_for(ctx.guild.me) if isinstance(ctx.channel, (discord.TextChannel, discord.Thread)) else None  # type: ignore
        payload = "\n".join(
            [
                f"enabled={g['enabled']} one_in=1/{g['one_in']} owner_bypass={g['owner_bypass']}",
                f"here perms: view={getattr(perms,'view_channel',None)} send={getattr(perms,'send_messages',None)} manage_messages={getattr(perms,'manage_messages',None)} manage_webhooks={getattr(perms,'manage_webhooks',None)}",
                f"overrides={len(g['user_probs'])}",
            ]
        )
        await ctx.send(embed=_embed("OwoPlus — Diag", desc=box(payload, lang="ini")))

    @owoplus.command(name="test")
    async def owoplus_test(self, ctx: redcommands.Context) -> None:
        try:
            prefixes = await self.bot.get_valid_prefixes(ctx.guild)
        except Exception:
            prefixes = []
        last: Optional[discord.Message] = None
        async for m in ctx.channel.history(limit=50, before=ctx.message.created_at):  # type: ignore
            if m.author.id == ctx.author.id and not m.author.bot and not m.webhook_id and not self._starts_with_prefixes(m.content or "", prefixes):
                last = m; break

        ch = ctx.channel
        perms = ch.permissions_for(ctx.guild.me) if isinstance(ch, (discord.TextChannel, discord.Thread)) else None  # type: ignore
        lines = [
            f"channel={getattr(ch, 'id', None)} type={ch.__class__.__name__}",
            f"perms: send={getattr(perms,'send_messages',None)} manage_messages={getattr(perms,'manage_messages',None)} manage_webhooks={getattr(perms,'manage_webhooks',None)}",
            f"last_msg={'found' if last else 'not found'}",
        ]
        if not last:
            return await ctx.send(embed=_embed("OwoPlus — Test", desc=box("\n".join(lines), lang="ini")))
        if not (last.content and last.content.strip()):
            lines.append("skip: last message has no text (attachments-only)")
            return await ctx.send(embed=_embed("OwoPlus — Test", desc=box("\n".join(lines), lang="ini")))

        hook = await self._ensure_webhook(ch)
        if not hook:
            lines.append("hook: none (missing Manage Webhooks?)")
            return await ctx.send(embed=_embed("OwoPlus — Test", desc=box("\n".join(lines), lang="ini")))
        lines.append(f"hook: {hook.id}:{hook.name}")

        conf = await self.config.guild(ctx.guild).all()
        forced = self._has_key_trigger(last.content or "")
        n = self._one_in(last.author, conf)
        apply_owo = forced or (n <= 1) or (random.randrange(n) == 0)

        content_full = self._render_message(last.content or "", apply_owo=apply_owo)
        parts = self._chunk_message(content_full)

        files: List[discord.File] = []
        for a in last.attachments[:5]:
            try:
                files.append(await a.to_file())
            except Exception as e:
                lines.append(f"attach_fail:{a.id}:{type(e).__name__}")

        try:
            for idx, part in enumerate(parts):
                await self._send_via_webhook(
                    hook,
                    channel=ch,
                    author=last.author,
                    content=part,
                    files=files if idx == 0 else None,
                    wait=False,
                )
            lines.append(f"send: OK ({len(parts)} part{'s' if len(parts)!=1 else ''})")
        except Exception as e:
            lines.append(f"send: FAIL {type(e).__name__}: {e}")
            return await ctx.send(embed=_embed("OwoPlus — Test", desc=box("\n".join(lines), lang="ini")))

        try:
            await last.delete()
        except discord.Forbidden:
            pass

        await ctx.send(embed=_embed("OwoPlus — Test", desc=box("\n".join(lines), lang="ini")))

    # ---------- listener ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not await self._should_process(message):
            return

        original = (message.content or "").strip()
        if not original:
            return

        conf = await self.config.guild(message.guild).all()
        forced = self._has_key_trigger(original)
        n = self._one_in(message.author, conf)
        apply_owo = forced or (n <= 1) or (random.randrange(n) == 0)

        content_full = self._render_message(original, apply_owo=apply_owo).strip()
        if content_full == original:
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

        parts = self._chunk_message(content_full)
        try:
            for idx, part in enumerate(parts):
                await self._send_via_webhook(
                    hook,
                    channel=message.channel,
                    author=message.author,
                    content=part,
                    files=files if idx == 0 else None,
                    wait=False,
                )
        except Exception:
            self._wh_cache.pop(getattr(message.channel, "id", 0), None)
            return

        try:
            await message.delete()
        except discord.Forbidden:
            pass


async def setup(bot: Red) -> None:
    await bot.add_cog(OwoPlus(bot))