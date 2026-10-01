"""Guild-scoped transformation options and explicit member participation."""

import re
from functools import lru_cache

from redbot.core import commands

from .constants import KEY_MAP

FEATURE_DEFAULTS = {
    "channel_mode": "all",
    "allowed": [],
    "excluded": [],
    "optouts": {},
    "keywords": True,
    "words": {},
    "intensity": 0,
    "cooldown": 0,
    "syllables": {},
}


def channel_allowed(channel, features):
    ids = {channel.id, getattr(channel, "parent_id", None)}
    if ids.intersection(features["excluded"]):
        return False
    return features["channel_mode"] == "all" or bool(ids.intersection(features["allowed"]))


def word_map(features):
    if not features["keywords"]:
        return {}
    words = {**KEY_MAP, **features["words"]}
    return {key: value for key, value in words.items() if value is not None}


@lru_cache(maxsize=256)
def pattern(words):
    return re.compile(r"\b(" + "|".join(re.escape(word) for word in words) + r")\b", re.IGNORECASE)


def keyword_match(text, features):
    words = word_map(features)
    return bool(words) and bool(pattern(tuple(sorted(words))).search(text))


def replace_keywords(text, features, case_like):
    words = word_map(features)
    if not words:
        return text
    return pattern(tuple(sorted(words))).sub(
        lambda match: case_like(match[1], words[match[1].lower()]), text
    )


def valid_word(word):
    word = word.strip().lower()
    if not re.fullmatch(r"[a-z]+(?:'[a-z]+)?", word) or len(word) > 40:
        raise commands.BadArgument("Use one English word, up to 40 letters.")
    return word
