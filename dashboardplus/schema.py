"""Reviewed editors and fixed music actions, never a raw Config/command console."""

import math
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from redbot.core import commands


@dataclass(frozen=True)
class Setting:
    id: str
    cog: str
    label: str
    path: str
    key: str
    kind: str = "bool"
    minimum: float = 0
    maximum: float = 0
    off: str = ""
    global_scope: bool = False
    choices: tuple = ()

    def command(self, value=True):
        return self.off if self.off and (not value or value == "0") else self.path

    def arguments(self, value):
        if self.off and self.kind in {"bool", "channel"}:
            return [] if self.kind == "bool" or not value or value == "0" else [value]
        if self.kind == "onoff":
            return ["on" if value else "off"]
        if self.kind == "limits":
            return [value["max_seconds"], value["per_member"]]
        return [value]

    def validate(self, value):
        if self.kind in {"bool", "onoff"}:
            valid = type(value) is bool
        elif self.kind in {"int", "float"}:
            valid = (
                type(value) in ({int} if self.kind == "int" else {int, float})
                and math.isfinite(value)
                and self.minimum <= value <= self.maximum
            )
        elif self.kind == "limits":
            valid = (
                isinstance(value, dict)
                and set(value) == {"max_seconds", "per_member"}
                and type(value["max_seconds"]) is int
                and 0 <= value["max_seconds"] <= 86400
                and type(value["per_member"]) is int
                and 0 <= value["per_member"] <= 100
            )
        elif self.kind == "channel":
            valid = (
                isinstance(value, str)
                and value.isascii()
                and value.isdecimal()
                and len(value) <= 20
            )
        elif self.kind == "choice":
            valid = value in self.choices
        else:
            valid = (
                isinstance(value, str)
                and 0 < len(value) <= 1000
                and all(ord(char) >= 32 for char in value)
            )
        if not valid:
            raise commands.BadArgument(f"Invalid value for {self.label}.")
        return value


EDITORS = [
    Setting("music.panel", "AudioPlus", "Now-playing panel", "audioset panel", "music.panel"),
    Setting(
        "music.votes", "AudioPlus", "Listener skip votes", "audioset voteskip", "music.vote_skip"
    ),
    Setting(
        "music.fair", "AudioPlus", "Fair request order", "audioset fairqueue", "music.fair_queue"
    ),
    Setting(
        "music.autoplay", "AudioPlus", "Autoplay suggestions", "audioset autoplay", "music.autoplay"
    ),
    Setting("music.history", "AudioPlus", "Listening history", "audioset history", "music.history"),
    Setting(
        "music.limits",
        "AudioPlus",
        "Song length / requests per person",
        "audioset limits",
        "music",
        "limits",
    ),
    Setting(
        "music.recovery", "AudioPlus", "Queue recovery", "audioset recovery", "continuity.recovery"
    ),
    Setting(
        "music.normalize",
        "AudioPlus",
        "Volume normalization",
        "audioset normalize",
        "continuity.normalize",
    ),
    Setting(
        "community.sticky",
        "CommunityPlus",
        "Restore returning members' roles",
        "community sticky enable",
        "sticky.enabled",
        off="community sticky disable",
    ),
    Setting(
        "community.welcome",
        "CommunityPlus",
        "Welcome messages",
        "community welcome enable",
        "welcome.enabled",
        off="community welcome disable",
    ),
    Setting(
        "community.tracking",
        "CommunityPlus",
        "Activity tracking",
        "community tracking",
        "seen.enabled",
    ),
    Setting(
        "community.solo",
        "CommunityPlus",
        "Solo voice cleanup",
        "community vcsolo enable",
        "vcsolo.enabled",
        off="community vcsolo disable",
    ),
    Setting(
        "community.idle",
        "CommunityPlus",
        "Solo voice timeout (seconds)",
        "community vcsolo idle",
        "vcsolo.idle_seconds",
        "int",
        60,
        604800,
    ),
    Setting("level.message", "LevelPlus", "Message XP", "level message enable", "message.enabled"),
    Setting(
        "level.reaction", "LevelPlus", "Reaction XP", "level reaction enable", "reaction.enabled"
    ),
    Setting("level.voice", "LevelPlus", "Voice XP", "level voice enable", "voice.enabled"),
    Setting(
        "level.announce",
        "LevelPlus",
        "Level-up announcements",
        "level levelup enable",
        "levelup.enabled",
    ),
    Setting(
        "level.multiplier",
        "LevelPlus",
        "Level threshold multiplier",
        "level formula multiplier",
        "multiplier",
        "float",
        0.1,
        10,
    ),
    Setting(
        "log.channel",
        "LogPlus",
        "Default log channel",
        "log setchannel",
        "log_channel",
        "channel",
        off="log clearchannel",
    ),
    Setting(
        "log.compact",
        "LogPlus",
        "Compact event headers",
        "log style compact",
        "style.compact",
        "onoff",
    ),
    Setting(
        "log.history",
        "LogPlus",
        "Retain local event history",
        "log history enabled",
        "history_settings.enabled",
    ),
    Setting(
        "log.days",
        "LogPlus",
        "History retention (days)",
        "log history retention",
        "history_settings.days",
        "int",
        1,
        90,
    ),
    Setting(
        "emoji.capture",
        "EmojiStealerPlus",
        "Capture external emoji",
        "emoji enabled",
        "capture.enabled",
    ),
    Setting(
        "emoji.reactions",
        "EmojiStealerPlus",
        "Capture reaction emoji",
        "emoji reactions",
        "capture.reactions",
    ),
    Setting(
        "emoji.notify",
        "EmojiStealerPlus",
        "Capture notifications",
        "emoji notify",
        "capture.notify",
    ),
    Setting(
        "owo.enabled",
        "OwoPlus",
        "Automatic transformations",
        "owo enable",
        "enabled",
        off="owo disable",
    ),
    Setting(
        "owo.chance",
        "OwoPlus",
        "Full transformation: one in N",
        "owo onein",
        "one_in",
        "int",
        1,
        1000000,
    ),
    Setting("intro.enabled", "IntroPlus", "Automatic voice intros", "intro enable", "enabled"),
    Setting(
        "intro.volume", "IntroPlus", "Intro volume (%)", "intro volume", "volume", "int", 1, 100
    ),
    Setting(
        "intro.cooldown",
        "IntroPlus",
        "Intro cooldown (seconds)",
        "intro cooldown",
        "cooldown",
        "int",
        10,
        3600,
    ),
    Setting(
        "presence.enabled",
        "PresencePlus",
        "Global presence automation",
        "presence enable",
        "settings.enabled",
        global_scope=True,
    ),
    Setting(
        "presence.interval",
        "PresencePlus",
        "Global rotation interval (seconds)",
        "presence interval",
        "settings.interval",
        "int",
        60,
        86400,
        global_scope=True,
    ),
]
SETTINGS = {setting.id: setting for setting in EDITORS}

# Each action is constrained to an expected loaded cog and a fixed command path.
MUSIC = {
    "play": ("play", "text"),
    "pause": ("pause", "none"),
    "resume": ("resume", "none"),
    "skip": ("skip", "none"),
    "stop": ("stop", "none"),
    "disconnect": ("disconnect", "none"),
    "shuffle": ("shuffle", "none"),
    "volume": ("volume", "volume"),
    "repeat": ("repeat", "repeat"),
    "seek": ("seek", "text"),
    "remove": ("remove", "position"),
}


def music_arguments(action, value):
    kind = MUSIC[action][1]
    if kind == "none":
        if value is not None:
            raise commands.BadArgument("This action does not take a value.")
        return []
    if kind == "text":
        return [Setting("", "", "Song or timestamp", "", "", "text").validate(value)]
    if kind == "repeat":
        return [
            Setting(
                "", "", "Repeat mode", "", "", "choice", choices=("off", "track", "queue")
            ).validate(value)
        ]
    return [
        Setting(
            "",
            "",
            "Volume" if kind == "volume" else "Queue position",
            "",
            "",
            "int",
            0 if kind == "volume" else 1,
            1000 if kind == "volume" else 100,
        ).validate(value)
    ]


def public_url(value):
    try:
        parsed = urlsplit(value or "")
        return (
            value
            if parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            else ""
        )
    except (ValueError, TypeError):
        return ""


def track_card(track):
    if track is None:
        return None
    # Original public page/artwork only; never expose resolver URLs or HTTP headers.
    thumbnail = public_url(getattr(track, "artwork", "") or getattr(track, "thumbnail", ""))
    if not thumbnail:
        parsed = urlsplit(public_url(track.uri))
        try:
            if parsed.hostname in {"youtu.be", "www.youtu.be"}:
                video = parsed.path.strip("/")
            elif parsed.hostname in {
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
                "music.youtube.com",
            }:
                video = parse_qs(parsed.query, max_num_fields=50).get("v", [""])[0]
                path = parsed.path.strip("/").split("/")
                if not video and len(path) == 2 and path[0] in {"shorts", "live", "embed"}:
                    video = path[1]
            else:
                video = ""
        except ValueError:
            video = ""
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
            thumbnail = f"https://i.ytimg.com/vi/{video}/hqdefault.jpg"
    return {
        "title": str(track.title)[:500],
        "author": str(track.author)[:300],
        "url": public_url(track.uri),
        "thumbnail": thumbnail,
        "duration": max(0, int(track.length)),
    }
