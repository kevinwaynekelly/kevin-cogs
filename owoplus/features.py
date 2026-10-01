"""Guild-scoped transformation options and explicit member participation."""

import re
import time
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
    "channel_styles": {},
}

STYLE_WORDS = {
    "owo": KEY_MAP,
    "pirate": {
        "hello": "ahoy",
        "hi": "ahoy",
        "my": "me",
        "friend": "matey",
        "your": "yer",
        "you": "ye",
        "yes": "aye",
        "money": "booty",
        "stop": "avast",
    },
    "robot": {
        "hello": "greetings",
        "hi": "greetings",
        "yes": "affirmative",
        "no": "negative",
        "maybe": "probability uncertain",
        "thanks": "acknowledged",
        "friend": "human companion",
    },
}
PROTECTED = re.compile(r"(https?://\S+|<[^>\n]+>|:[A-Za-z0-9_]+:)")


def channel_features(channel, features, now=None):
    """Resolve live thread/parent overrides without mutating saved preferences."""
    now = time.time() if now is None else now
    style = "owo"
    for cid in (getattr(channel, "id", None), getattr(channel, "parent_id", None)):
        entry = features.get("channel_styles", {}).get(str(cid))
        if entry and (not entry["expires"] or entry["expires"] > now):
            style = entry["style"]
            break
    return {**features, "style": style}


def transform_style(text, features, case_like, *, full):
    """Keep URLs, mentions and emoji intact in the additional text styles."""
    parts = PROTECTED.split(text)
    for index in range(0, len(parts), 2):
        mapped = replace_keywords(parts[index], features, case_like)
        parts[index] = mapped.upper() if full and features["style"] == "robot" else mapped
    output = "".join(parts)
    if full and text.strip():
        return (
            f"Ahoy! {output} Arrr!" if features["style"] == "pirate" else f"[TRANSMISSION] {output}"
        )
    return output


def channel_allowed(channel, features):
    ids = {channel.id, getattr(channel, "parent_id", None)}
    if ids.intersection(features["excluded"]):
        return False
    return features["channel_mode"] == "all" or bool(ids.intersection(features["allowed"]))


def word_map(features):
    if not features["keywords"]:
        return {}
    words = {**STYLE_WORDS[features.get("style", "owo")], **features["words"]}
    return {key: value for key, value in words.items() if value is not None}


@lru_cache(maxsize=256)
def pattern(words):
    return re.compile(r"\b(" + "|".join(re.escape(word) for word in words) + r")\b", re.IGNORECASE)


def keyword_match(text, features):
    words = word_map(features)
    if features.get("style", "owo") != "owo":
        text = " ".join(PROTECTED.split(text)[::2])
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
