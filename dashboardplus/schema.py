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
    description: str = ""

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
    Setting(
        "music.panel",
        "AudioPlus",
        "Now-playing panel",
        "audioset panel",
        "music.panel",
        description="Post and update an automatic player panel while music plays. Turning this off closes the current panel.",
    ),
    Setting(
        "music.votes",
        "AudioPlus",
        "Listener skip votes",
        "audioset voteskip",
        "music.vote_skip",
        description="Require listener votes to skip a song for members without DJ privileges. Changing this setting clears current votes.",
    ),
    Setting(
        "music.fair",
        "AudioPlus",
        "Fair request order",
        "audioset fairqueue",
        "music.fair_queue",
        description="Alternate between requesters while keeping each person's songs in their original order. Applies to the current queue too.",
    ),
    Setting(
        "music.autoplay",
        "AudioPlus",
        "Autoplay suggestions",
        "audioset autoplay",
        "music.autoplay",
        description="Find another song when the queue ends and listeners remain. If no suggestion is available, the bot uses its normal idle departure.",
    ),
    Setting(
        "music.history",
        "AudioPlus",
        "Listening history",
        "audioset history",
        "music.history",
        description="Save recent song starts for browsing and replay. Turning this off also erases this server's saved listening history.",
    ),
    Setting(
        "music.limits",
        "AudioPlus",
        "Song length / requests per person",
        "audioset limits",
        "music",
        "limits",
        description="Limit song length in seconds and active tracks per person. Zero means unlimited. DJs, members with Manage Server and bot owners are exempt; existing tracks stay queued.",
    ),
    Setting(
        "music.recovery",
        "AudioPlus",
        "Queue recovery",
        "audioset recovery",
        "continuity.recovery",
        description="Save the current song, position and queue for manual recovery after a restart. Turning this off clears the saved checkpoint.",
    ),
    Setting(
        "music.normalize",
        "AudioPlus",
        "Volume normalization",
        "audioset normalize",
        "continuity.normalize",
        description="Use FFmpeg loudness normalization to reduce volume differences between songs. Applies when the next audio decoder starts.",
    ),
    Setting(
        "community.sticky",
        "CommunityPlus",
        "Restore returning members' roles",
        "community sticky enable",
        "sticky.enabled",
        off="community sticky disable",
        description="Restore saved roles when a member rejoins this server, subject to ignored roles and the bot's role permissions and hierarchy.",
    ),
    Setting(
        "community.welcome",
        "CommunityPlus",
        "Welcome messages",
        "community welcome enable",
        "welcome.enabled",
        off="community welcome disable",
        description="Send the configured welcome message when someone joins. A welcome channel must also be selected and writable by the bot.",
    ),
    Setting(
        "community.tracking",
        "CommunityPlus",
        "Activity tracking",
        "community tracking",
        "seen.enabled",
        description="Record member activity, last-seen details, games and voice time. Turning this off stops new collection and keeps previously saved records.",
    ),
    Setting(
        "community.solo",
        "CommunityPlus",
        "Solo voice cleanup",
        "community vcsolo enable",
        "vcsolo.enabled",
        off="community vcsolo disable",
        description="Disconnect eligible members left alone in voice after the timeout. Bots do not count as companions; configured exemptions still apply.",
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
        description="Seconds a member can remain alone before solo voice cleanup disconnects them. Minimum 60 seconds; changing this restarts active solo timers.",
    ),
    Setting(
        "level.message",
        "LevelPlus",
        "Message XP",
        "level message enable",
        "message.enabled",
        description="Award XP for eligible messages using the configured amount, cooldown and exclusions. Turning this off keeps existing XP.",
    ),
    Setting(
        "level.reaction",
        "LevelPlus",
        "Reaction XP",
        "level reaction enable",
        "reaction.enabled",
        description="Award reaction XP to authors, reactors or both according to the saved policy, cooldown and exclusions. Existing XP is retained when disabled.",
    ),
    Setting(
        "level.voice",
        "LevelPlus",
        "Voice XP",
        "level voice enable",
        "voice.enabled",
        description="Award XP for eligible voice participation using the configured interval, member minimum and AFK rules. Turning this off keeps existing XP.",
    ),
    Setting(
        "level.announce",
        "LevelPlus",
        "Level-up announcements",
        "level levelup enable",
        "levelup.enabled",
        description="Announce level increases using the saved template and announcement channel, or the server's system channel when no target is selected.",
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
        description="Scale the XP needed to reach each level from 0.1 to 10 times the selected curve. Higher values make leveling slower without changing saved XP.",
    ),
    Setting(
        "log.channel",
        "LogPlus",
        "Default log channel",
        "log setchannel",
        "log_channel",
        "channel",
        off="log clearchannel",
        description="Fallback destination for enabled event logs when no more specific route matches. Clearing this leaves existing channel and category routes active.",
    ),
    Setting(
        "log.compact",
        "LogPlus",
        "Compact event headers",
        "log style compact",
        "style.compact",
        "onoff",
        description="Include each event's configured emoji in its log title. Turning this off removes these emoji headers.",
    ),
    Setting(
        "log.history",
        "LogPlus",
        "Retain local event history",
        "log history enabled",
        "history_settings.enabled",
        description="Collect local event records for searches and timelines. Turning this off pauses collection; old Discord messages are never imported.",
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
        description="Keep local event records for 1 to 90 days. Saving a shorter retention period immediately removes records older than that limit.",
    ),
    Setting(
        "emoji.capture",
        "EmojiStealerPlus",
        "Capture external emoji",
        "emoji enabled",
        "capture.enabled",
        description="Automatically copy external custom emoji into this server when members use them, subject to the capture channel, permissions and available emoji slots.",
    ),
    Setting(
        "emoji.reactions",
        "EmojiStealerPlus",
        "Capture reaction emoji",
        "emoji reactions",
        "capture.reactions",
        description="Also capture external custom emoji used in member reactions. Automatic emoji capture must be enabled for reaction capture to run.",
    ),
    Setting(
        "emoji.notify",
        "EmojiStealerPlus",
        "Capture notifications",
        "emoji notify",
        "capture.notify",
        description="Announce successful emoji copies in the channel where the emoji was used. Turning this off keeps capture running silently.",
    ),
    Setting(
        "owo.enabled",
        "OwoPlus",
        "Automatic transformations",
        "owo enable",
        "enabled",
        off="owo disable",
        description="Allow automatic message transformations in this server. Channel restrictions, member opt-outs and the configured chances still apply.",
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
        description="Chance of a full transformation for an eligible message: 1 means every message, 10 means about one in ten. Larger values make transformations rarer.",
    ),
    Setting(
        "intro.enabled",
        "IntroPlus",
        "Automatic voice intros",
        "intro enable",
        "enabled",
        description="Play a member's saved intro when they join an eligible voice channel. Turning this off stops current and queued intros but keeps saved clips.",
    ),
    Setting(
        "intro.volume",
        "IntroPlus",
        "Intro volume (%)",
        "intro volume",
        "volume",
        "int",
        1,
        100,
        description="Playback volume for upcoming intro clips, from 1 to 100 percent. Does not change AudioPlus music volume.",
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
        description="Minimum seconds between automatic intros for the same member in this server. Choose 10 to 3600 seconds to avoid repeated join clips.",
    ),
    Setting(
        "presence.enabled",
        "PresencePlus",
        "Global presence automation",
        "presence enable",
        "settings.enabled",
        global_scope=True,
        description="Run saved presence profiles, rotations, schedules and enabled music overrides across every server. Turning this off restores the presence from before automation.",
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
        description="Seconds between rotating presence messages, from 60 to 86400. This is global, so the bot's displayed status changes in every server.",
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
