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

# Optional syllable libs (best-first). Soft imports keep cog usable without them.
try:
    import pronouncing  # type: ignore
except Exception:
    pronouncing = None  # type: ignore
try:
    from g2p_en import G2p  # type: ignore
except Exception:
    G2p = None  # type: ignore
try:
    import pyphen  # type: ignore
except Exception:
    pyphen = None  # type: ignore

__red_end_user_data_statement__ = (
    "This cog stores per-guild preferences for webhook-based message transformation "
    "(enable flag, 1-in-N owo probability, per-user overrides, owner bypass, and haiku toggle). "
    "It does not store message contents."
)

DEFAULTS_GUILD = {
    "enabled": False,
    "one_in": 1000,
    "user_probs": {},
    "owner_bypass": True,
    "haiku_enabled": True,
}

# ---------- mapping & triggers ----------
KEY_MAP: Dict[str, str] = {"now": "meow", "bro": "bwo", "dude": "duwde", "bud": "bwud"}
KEY_RX = re.compile(r"\b(" + "|".join(map(re.escape, KEY_MAP.keys())) + r")\b", re.IGNORECASE)
TARGETS = sorted({v for v in KEY_MAP.values()})

CODE_SPLIT = re.compile(r"(```[\s\S]*?```|`[^`]*?`)", re.MULTILINE)

OWO_FACES = ["uwu", "owo", ">w<", "^w^", "x3", "~", "nya~", "(⁄˘⁄⁄ ω⁄ ⁄˘⁄)♡"]
HAIKU_SUFFIX = " 🌸"

EMO = {"ok": "✅", "bad": "⚠️", "core": "🛠️", "msg": "💬", "prob": "🎲", "diag": "🧪", "spark": "✨"}


def _embed(title: str, *, color: int | discord.Color = discord.Color.blurple(), desc: Optional[str] = None) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color)


def _bool_emoji(v: bool) -> str:
    return "🟢" if v else "🔴"


# ===================== Syllables: multi-backend engine =====================

def _norm_word(w: str) -> str:
    return re.sub(r"[^a-z']", "", w.lower())

_SPECIALS: Dict[str, int] = {
    "the": 1, "queue": 1, "people": 2, "business": 2, "beautiful": 3,
    "everyone": 3, "breathe": 1, "every": 2, "evening": 3, "gentle": 2,
    "quiet": 2, "deploys": 2, "bro": 1, "dude": 1, "now": 1, "bud": 1,
    "failure": 2, "teaches": 2, "learn": 1, "strength": 1, "focus": 2,
}

class _PronouncingBackend:
    name = "pronouncing"
    def count(self, word: str) -> Optional[int]:
        if pronouncing is None:
            return None
        w = _norm_word(word)
        if not w:
            return 0
        if w in _SPECIALS:
            return _SPECIALS[w]
        try:
            phones = pronouncing.phones_for_word(w)  # type: ignore[attr-defined]
            if not phones and len(w) > 3:
                for suf in ("'s", "es", "s", "ed", "ing", "er", "est"):
                    if w.endswith(suf) and len(w) - len(suf) >= 3:
                        phones = pronouncing.phones_for_word(w[:-len(suf)])  # type: ignore[attr-defined]
                        if phones:
                            break
            if not phones:
                return None
            return min(sum(tok[-1].isdigit() for tok in ph.split()) for ph in phones)
        except Exception:
            return None

class _G2PBackend:
    name = "g2p_en"
    def __init__(self) -> None:
        self._g2p = G2p() if G2p else None
    def count(self, word: str) -> Optional[int]:
        if self._g2p is None:
            return None
        w = _norm_word(word)
        if not w:
            return 0
        try:
            toks = self._g2p(w)  # type: ignore[operator]
            if not toks:
                return None
            return max(1, sum(1 for t in toks if any(d in t for d in "012")))
        except Exception:
            return None

class _PyphenBackend:
    name = "pyphen"
    def __init__(self) -> None:
        self._hyph = pyphen.Pyphen(lang="en_US") if pyphen else None
    def count(self, word: str) -> Optional[int]:
        if self._hyph is None:
            return None
        w = _norm_word(word)
        if not w:
            return 0
        try:
            s = self._hyph.inserted(w)
            return max(1, s.count("-") + 1) if s else None
        except Exception:
            return None

class _HeuristicBackend:
    name = "heuristic"
    _vowels = "aeiouy"
    def count(self, word: str) -> Optional[int]:
        w = _norm_word(word)
        if not w:
            return 0
        if w in _SPECIALS:
            return _SPECIALS[w]
        prev = False; count = 0
        for ch in w:
            v = ch in self._vowels
            if v and not prev:
                count += 1
            prev = v
        if w.endswith("e") and not w.endswith(("le", "ye")) and count > 1:
            count -= 1
        if w.endswith("ed") and len(w) > 3 and w[-3] not in self._vowels and count > 1:
            count -= 1
        if (w.endswith("es") and len(w) > 3 and w[-3] not in self._vowels
            and not re.search(r"(ches|shes|xes|zes|sses)$", w) and count > 1):
            count -= 1
        return max(1, count)

class _SyllableEngine:
    """Priority order: pronouncing → g2p_en → pyphen → heuristic."""
    def __init__(self) -> None:
        self.backends: List[object] = []
        if pronouncing: self.backends.append(_PronouncingBackend())
        if G2p: self.backends.append(_G2PBackend())
        if pyphen: self.backends.append(_PyphenBackend())
        self.backends.append(_HeuristicBackend())
        self._cache: Dict[str, int] = {}
    def count(self, word: str) -> int:
        w = _norm_word(word)
        if not w:
            return 0
        if w in self._cache:
            return self._cache[w]
        if w in _SPECIALS:
            self._cache[w] = _SPECIALS[w]; return _SPECIALS[w]
        for b in self.backends:
            try:
                v = b.count(w)  # type: ignore[attr-defined]
            except Exception:
                v = None
            if isinstance(v, int):
                self._cache[w] = max(1, v)
                return self._cache[w]
        self._cache[w] = 1
        return 1

# ===================== Haiku detection (engine wired) =====================

class HaikuMeter:
    _engine = _SyllableEngine()
    _cache: Dict[str, int] = {}
    @classmethod
    def count(cls, word: str) -> int:
        w = word.lower()
        if w in cls._cache:
            return cls._cache[w]
        v = cls._engine.count(w)
        cls._cache[w] = v
        return v


class Haiku:
    _WORD_RX = re.compile(r"[A-Za-z']+")
    _DASHES_RX = re.compile(r"[\u2010-\u2015\u2212\-]+")
    # Keep punctuation that comes *immediately* after the last word on the same line
    _PUNCT_TAIL_RX = re.compile(r"^([,.;:!?\u2026\)\]\}\u2019\u201D\"']+)(\s*)")

    @staticmethod
    def normalize_text(s: str) -> str:
        s = Haiku._DASHES_RX.sub(" ", s)
        s = s.replace("\n", " ")
        s = re.sub(r"\s+", " ", s).strip()
        return s

    @staticmethod
    def words(text: str) -> List[str]:
        return Haiku._WORD_RX.findall(text)

    @staticmethod
    def clean_lines(out: str) -> str:
        """Final safeguard: per-line strip + whitespace normalization."""
        lines = out.split("\n")
        norm = [re.sub(r"\s+", " ", ln).strip() for ln in lines]
        norm = [ln for ln in norm if ln]  # drop empties
        if len(norm) >= 3:
            norm = norm[:3]
        return "\n".join(norm).strip()

    @staticmethod
    def detect_breaks(text: str) -> Optional[Tuple[int, int]]:
        t = Haiku.normalize_text(text)
        words = Haiku.words(t)
        if not (3 <= len(words) <= 32):
            return None
        if len(t) > 300:
            return None
        syl = [HaikuMeter.count(w) for w in words]

        acc = i = 0
        while i < len(syl) and acc < 5:
            acc += syl[i]; i += 1
        if acc != 5:
            return None
        cut1 = i

        s2 = 0; j = cut1
        while j < len(syl) and s2 < 7:
            s2 += syl[j]; j += 1
        if s2 != 7:
            return None
        cut2 = j

        if sum(syl[cut2:]) != 5:
            return None
        return (cut1, cut2)

    @staticmethod
    def reflow(rendered: str, cuts: Tuple[int, int]) -> str:
        """Insert line breaks at word boundaries; keep trailing punctuation with the prior word."""
        words = list(Haiku._WORD_RX.finditer(rendered))
        if len(words) < cuts[1]:
            out = re.sub(r"\s+", " ", rendered).strip()
            return out

        parts: List[str] = []
        last = 0
        idx = 0
        marks = {cuts[0], cuts[1]}

        for m in words:
            parts.append(rendered[last:m.start()])
            parts.append(m.group(0))
            idx += 1

            if idx in marks:
                tail = rendered[m.end():]
                mv = Haiku._PUNCT_TAIL_RX.match(tail)
                consumed = 0
                if mv:
                    parts.append(mv.group(1))  # punctuation stays on this line
                    consumed = len(mv.group(0))  # also skip the spaces after it
                parts.append("\n")
                last = m.end() + consumed
            else:
                last = m.end()

        parts.append(rendered[last:])
        out = "".join(parts)
        return Haiku.clean_lines(out)  # ensure no leading spaces

def _normalize_for_haiku(s: str) -> str: return Haiku.normalize_text(s)
def _count_syllables(word: str) -> int: return HaikuMeter.count(word)
def _detect_haiku_breaks(text: str) -> Optional[Tuple[int, int]]: return Haiku.detect_breaks(text)
def _reflow_text_as_haiku(rendered: str, cuts: Tuple[int, int]) -> str: return Haiku.reflow(rendered, cuts)
# ============================================================================


class OwoPlus(redcommands.Cog):
    """Webhook-only cute/owo replacer with auto-intensity 1..5, keys-only fallback, and optional haiku formatting."""

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
            s = OwoPlus._sub_prob(s, r"(?i)n([aeiou])", r"ny\1", prof["ny"])
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
        out: List[str] = []
        wordish = re.compile(r"[A-Za-z0-9]")
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, original, transformed).get_opcodes():
            if tag == "equal":
                out.append(transformed[j1:j2]); continue
            if tag == "insert":
                out.append(transformed[j1:j2]); continue
            seg = transformed[j1:j2]
            if not seg or not wordish.search(seg):
                out.append(seg); continue
            l = len(seg) - len(seg.lstrip())
            r = len(seg) - len(seg.rstrip())
            left = seg[:l]; core = seg[l:len(seg)-r] if r else seg[l:]; right = seg[len(seg)-r:] if r else ""
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

    # ---------- auto intensity 1..5 ----------
    @staticmethod
    def _auto_intensity(nchars: int) -> int:
        if nchars <= 80:   return 5
        if nchars <= 160:  return 4
        if nchars <= 400:  return 3
        if nchars <= 1200: return 2
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
        return any(KEY_RX.search(seg) for seg, is_code in OwoPlus._split_code_segments(text) if not is_code)

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
                    haiku = self._format_haiku_lines(haiku)        # strip leading spaces
                    haiku = self._add_haiku_suffix(haiku)
                    haiku = self._format_haiku_lines(haiku)        # final pass
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
                        if (candidate.count("*") % 2) or (candidate.count("`") % 2) or (candidate.count("_") % 2):
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
            desc="Keys → *meow/bwo/duwde/bwud*. RNG hit ⇒ full OWO; else keys-only. Haiku override outputs three italic lines and ends with 🌸.",
        )
        e.add_field(
            name=f"{EMO['core']} Core",
            value=box(
                f"enabled = {cfg['enabled']}\n"
                f"one_in  = 1/{cfg['one_in']}\n"
                f"owner_bypass= {cfg['owner_bypass']}\n"
                f"haiku_enabled= {cfg['haiku_enabled']}",
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
            value=(f"• `{p}owoplus onein <N>` (default 1000)\n"
                   f"• `{p}owoplus prob add @user <N>` • `remove @user` • `list`"),
            inline=False,
        )
        e.add_field(
            name="Toggles & Tools",
            value=(f"• `{p}owoplus ownerbypass <on|off>`\n"
                   f"• `{p}owoplus poem on|off`  — toggle haiku reflow\n"
                   f"• `{p}owoplus poem diag <text>` — syllables & breaks"),
            inline=False,
        )
        e.add_field(name="Behavior", value="Haiku detected ⇒ three italic lines with a blossom; otherwise RNG full vs keys-only.", inline=False)
        await ctx.send(embed=e)

    @owoplus.command(name="ownerbypass")
    async def owoplus_ownerbypass(self, ctx: redcommands.Context, state: Optional[str] = None) -> None:
        if state is None:
            cur = await self.config.guild(ctx.guild).owner_bypass()
            return await ctx.send(embed=_embed(f"Owner bypass is **{'on' if cur else 'off'}**"))
        val = state.lower() in {"on", "true", "yes", "1"}
        await self.config.guild(ctx.guild).owner_bypass.set(val)
        await ctx.tick()

    # ------- poem group (haiku tools) -------
    @owoplus.group(name="poem", invoke_without_command=True)
    async def owoplus_poem(self, ctx: redcommands.Context) -> None:
        cur = await self.config.guild(ctx.guild).haiku_enabled()
        await ctx.send(embed=_embed(f"Haiku formatting is **{'on' if cur else 'off'}**. Use `poem on|off` or `poem diag <text>`"))

    @owoplus_poem.command(name="on")
    async def owoplus_poem_on(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).haiku_enabled.set(True)
        await ctx.tick()

    @owoplus_poem.command(name="off")
    async def owoplus_poem_off(self, ctx: redcommands.Context) -> None:
        await self.config.guild(ctx.guild).haiku_enabled.set(False)
        await ctx.tick()

    @owoplus_poem.command(name="diag")
    async def owoplus_poem_diag(self, ctx: redcommands.Context, *, text: str) -> None:
        norm = self._normalize_for_haiku(text)
        words = [w for w in re.findall(r"[A-Za-z']+", norm)]
        syl = [self._count_syllables(w) for w in words]
        cum = []
        c = 0
        for s in syl:
            c += s
            cum.append(c)
        cuts = self._detect_haiku_breaks(norm)
        cut1, cut2 = (cuts if cuts else (-1, -1))
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
        e = _embed("OwoPlus — Haiku Diag", desc=box("\n".join(lines), lang="ini"))
        e.add_field(name="Haiku Render", value=box(out, lang="ini"), inline=False)
        await ctx.send(embed=e)
    # ---------------------------------------

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
        conf = await self.config.guild(ctx.guild).all()
        n = conf["one_in"]
        forced = self._has_key_trigger(text)
        roll = 0 if n <= 1 else random.randrange(n)
        full = (n <= 1) or (roll == 0)
        mode = "full" if full else ("keys" if forced else "none")
        out = self._render_message_mode(text, mode=mode, use_haiku=bool(conf.get("haiku_enabled", True)))
        e = _embed(
            "OwoPlus — Preview",
            desc=f"mode={mode} (n=1/{n}{', key seen' if forced else ''}) • haiku={'on' if conf.get('haiku_enabled', True) else 'off'}",
        )
        e.add_field(name="OUTPUT", value=box(out, lang="ini"), inline=False)
        await ctx.send(embed=e)

    @owoplus.command(name="diag")
    async def owoplus_diag(self, ctx: redcommands.Context) -> None:
        g = await self.config.guild(ctx.guild).all()
        perms = ctx.channel.permissions_for(ctx.guild.me) if isinstance(ctx.channel, (discord.TextChannel, discord.Thread)) else None  # type: ignore
        payload = "\n".join(
            [
                f"enabled={g['enabled']} one_in=1/{g['one_in']} owner_bypass={g['owner_bypass']} haiku_enabled={g.get('haiku_enabled', True)}",
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
        full = (n <= 1) or (random.randrange(n) == 0)
        mode = "full" if full else ("keys" if forced else "none")

        content_full = self._render_message_mode(last.content or "", mode=mode, use_haiku=bool(conf.get("haiku_enabled", True)))
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
            lines.append(f"send: OK ({len(parts)} part{'s' if len(parts)!=1 else ''}) mode={mode} haiku={conf.get('haiku_enabled', True)}")
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

        # Haiku early exit — fully italicized and ends with 🌸
        if conf.get("haiku_enabled", True):
            plain = self._plain_text_if_no_code(original)
            if plain:
                cuts = self._detect_haiku_breaks(plain)
                if cuts:
                    content = self._reflow_text_as_haiku(plain, cuts)
                    content = self._format_haiku_lines(content)
                    content = self._add_haiku_suffix(content)
                    content = self._format_haiku_lines(content)
                    hook = await self._ensure_webhook(message.channel)
                    if not hook:
                        return
                    files: List[discord.File] = []
                    for a in message.attachments[:5]:
                        try:
                            files.append(await a.to_file())
                        except Exception:
                            pass
                    parts = self._chunk_message(content)
                    try:
                        for idx, part in enumerate(parts):
                            await self._send_via_webhook(
                                hook, channel=message.channel, author=message.author,
                                content=part, files=files if idx == 0 else None, wait=False
                            )
                    except Exception:
                        self._wh_cache.pop(getattr(message.channel, "id", 0), None)
                        return
                    try:
                        await message.delete()
                    except discord.Forbidden:
                        pass
                    return

        has_key = self._has_key_trigger(original)
        n = self._one_in(message.author, conf)
        full = (n <= 1) or (random.randrange(n) == 0)
        mode = "full" if full else ("keys" if has_key else "none")

        content = self._render_message_mode(original, mode=mode, use_haiku=False).strip()
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

        parts = self._chunk_message(content)
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