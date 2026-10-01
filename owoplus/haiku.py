"""Syllable counting and haiku detection without a Discord connection."""

from __future__ import annotations

import logging
import re
import threading
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

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


# ---------- mapping & triggers ----------


# ===================== Syllables: multi-backend engine =====================


def _norm_word(w: str) -> str:
    return re.sub(r"[^a-z']", "", w.lower())


_SPECIALS: Dict[str, int] = {
    "the": 1,
    "queue": 1,
    "people": 2,
    "business": 2,
    "beautiful": 3,
    "everyone": 3,
    "breathe": 1,
    "every": 2,
    "evening": 3,
    "gentle": 2,
    "quiet": 2,
    "deploys": 2,
    "bro": 1,
    "dude": 1,
    "now": 1,
    "bud": 1,
    "failure": 2,
    "teaches": 2,
    "learn": 1,
    "strength": 1,
    "focus": 2,
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
                        phones = pronouncing.phones_for_word(w[: -len(suf)])  # type: ignore[attr-defined]
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
        prev = False
        count = 0
        for ch in w:
            v = ch in self._vowels
            if v and not prev:
                count += 1
            prev = v
        if w.endswith("e") and not w.endswith(("le", "ye")) and count > 1:
            count -= 1
        if w.endswith("ed") and len(w) > 3 and w[-3] not in self._vowels and count > 1:
            count -= 1
        if (
            w.endswith("es")
            and len(w) > 3
            and w[-3] not in self._vowels
            and not re.search(r"(ches|shes|xes|zes|sses)$", w)
            and count > 1
        ):
            count -= 1
        return max(1, count)


class _SyllableEngine:
    def __init__(self):
        self.backends = []
        for enabled, backend in (
            (pronouncing, _PronouncingBackend),
            (G2p, _G2PBackend),
            (pyphen, _PyphenBackend),
        ):
            if enabled:
                try:
                    self.backends.append(backend())
                except Exception:
                    log.debug("Optional syllable backend could not initialize", exc_info=True)
        self.backends.append(_HeuristicBackend())

    def count(self, word):
        word = _norm_word(word)
        if not word:
            return 0
        if word in _SPECIALS:
            return _SPECIALS[word]
        for backend in self.backends:
            try:
                result = backend.count(word)
                if isinstance(result, int):
                    return max(1, result)
            except Exception:
                log.debug("Syllable lookup failed", exc_info=True)
        return 1


class HaikuMeter:
    _engine = None
    _lock = threading.Lock()

    @classmethod
    def initialize(cls):
        if cls._engine is None:
            with cls._lock:
                if cls._engine is None:
                    cls._engine = _SyllableEngine()
        return cls._engine

    @classmethod
    @lru_cache(maxsize=4096)
    def count(cls, word):
        return cls.initialize().count(word.lower())


# ===================== Haiku detection (engine wired) =====================


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
    def detect_breaks(text: str, overrides=None) -> Optional[Tuple[int, int]]:
        t = Haiku.normalize_text(text)
        words = Haiku.words(t)
        if not (3 <= len(words) <= 32):
            return None
        if len(t) > 300:
            return None
        syl = [
            overrides[_norm_word(w)]
            if overrides and _norm_word(w) in overrides
            else HaikuMeter.count(w)
            for w in words
        ]

        acc = i = 0
        while i < len(syl) and acc < 5:
            acc += syl[i]
            i += 1
        if acc != 5:
            return None
        cut1 = i

        s2 = 0
        j = cut1
        while j < len(syl) and s2 < 7:
            s2 += syl[j]
            j += 1
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
            parts.append(rendered[last : m.start()])
            parts.append(m.group(0))
            idx += 1

            if idx in marks:
                tail = rendered[m.end() :]
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


def _normalize_for_haiku(s: str) -> str:
    return Haiku.normalize_text(s)


def _count_syllables(word: str) -> int:
    return HaikuMeter.count(word)


def _detect_haiku_breaks(text: str, overrides=None) -> Optional[Tuple[int, int]]:
    return Haiku.detect_breaks(text, overrides)


def _reflow_text_as_haiku(rendered: str, cuts: Tuple[int, int]) -> str:
    return Haiku.reflow(rendered, cuts)


# ============================================================================
