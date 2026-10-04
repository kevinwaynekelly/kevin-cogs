"""Explicit, on-demand record projections. Never a raw Config browser."""

import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

from redbot.core import commands

from .command_support import check_command
from .schema import public_url, track_card


@dataclass(frozen=True)
class Dataset:
    id: str
    cog: str
    title: str
    description: str
    columns: tuple
    commands: tuple
    sort: str = ""

    def catalog(self):
        return {
            "id": self.id,
            "cog": self.cog,
            "label": f"{self.cog.removesuffix('Plus')} · {self.title}",
            "title": self.title,
            "description": self.description,
            "columns": [{"key": key, "label": label} for key, label in self.columns],
            "sort": self.sort,
        }


def _spec(identifier, cog, title, description, fields, paths, sort=""):
    return Dataset(identifier, cog, title, description, tuple(fields), tuple(paths), sort)


_SPECS = [
    _spec(
        "audio.history",
        "AudioPlus",
        "Listening history",
        "Retained playback starts, public videos and requesters. Expired history is hidden.",
        [
            ("title", "Song"),
            ("requester", "Requester"),
            ("author", "Artist"),
            ("duration", "Seconds"),
            ("at", "Started"),
        ],
        ["history", "play", "audio play"],
        "-at",
    ),
    _spec(
        "audio.collections",
        "AudioPlus",
        "Saved songs",
        "Personal playlists, favorites, shared playlists and pending suggestions in this server.",
        [
            ("member", "Member"),
            ("kind", "Collection type"),
            ("collection", "Collection"),
            ("position", "Position"),
            ("title", "Song"),
            ("duration", "Seconds"),
        ],
        ["playlist", "favorite", "serverplaylist", "audioset"],
        "member",
    ),
    _spec(
        "audio.recovery",
        "AudioPlus",
        "Queue checkpoint",
        "Public tracks retained for explicit recovery, with saved position and requester attribution.",
        [
            ("position", "Queue position"),
            ("title", "Song"),
            ("requester", "Requester"),
            ("duration", "Seconds"),
            ("saved", "Saved"),
        ],
        ["recoverqueue"],
        "position",
    ),
    _spec(
        "backup.snapshots",
        "BackupPlus",
        "Server backups",
        "Snapshot inventory and role, channel and overwrite counts. Administrator access is required.",
        [
            ("name", "Backup"),
            ("category", "Type"),
            ("at", "Created"),
            ("roles", "Roles"),
            ("channels", "Channels"),
            ("overwrites", "Overwrites"),
            ("pending", "Pending creates"),
        ],
        ["backup list", "backup show"],
        "-at",
    ),
    _spec(
        "community.activity",
        "CommunityPlus",
        "Member activity",
        "Stored message and voice counters, presence, last-seen times and lifetime recorded voice hours.",
        [
            ("member", "Member"),
            ("member_id", "Member ID"),
            ("messages", "Messages"),
            ("voice_hours", "Voice hours"),
            ("voice_joins", "Voice joins"),
            ("voice_moves", "Moves"),
            ("voice_leaves", "Leaves"),
            ("streams", "Streams"),
            ("video", "Video starts"),
            ("games", "Game launches"),
            ("presence", "Presence"),
            ("last_seen", "Last seen"),
        ],
        ["activity", "community stats", "seendetail", "community seendetail", "voicehours"],
        "-messages",
    ),
    _spec(
        "community.games",
        "CommunityPlus",
        "Game activity",
        "Every retained member/game launch counter. Paging does not trim the stored game catalog.",
        [
            ("member", "Member"),
            ("member_id", "Member ID"),
            ("game", "Game"),
            ("launches", "Launches"),
        ],
        ["activity", "community stats"],
        "-launches",
    ),
    _spec(
        "community.events",
        "CommunityPlus",
        "Community events",
        "Event schedules, attendance totals and native Discord event links. Private reminder choices are excluded.",
        [
            ("title", "Event"),
            ("at", "Starts"),
            ("status", "Status"),
            ("going", "Going"),
            ("wait", "Waiting"),
            ("maybe", "Maybe"),
            ("capacity", "Capacity"),
            ("native", "Discord event ID"),
        ],
        ["event"],
        "-at",
    ),
    _spec(
        "community.polls",
        "CommunityPlus",
        "Polls",
        "Poll questions, options and aggregate votes. Individual voter choices are excluded.",
        [
            ("title", "Poll"),
            ("option", "Option"),
            ("votes", "Votes"),
            ("at", "Closes"),
            ("status", "Status"),
        ],
        ["poll"],
        "-at",
    ),
    _spec(
        "community.rooms",
        "CommunityPlus",
        "Temporary rooms",
        "Current temporary voice rooms, ownership and creation times.",
        [
            ("channel", "Room"),
            ("channel_id", "Channel ID"),
            ("owner", "Owner"),
            ("created", "Created"),
        ],
        ["voiceroom"],
        "channel",
    ),
    _spec(
        "level.members",
        "LevelPlus",
        "Levels and XP",
        "Lifetime XP totals and calculated levels, including retained progress for members who left.",
        [
            ("member", "Member"),
            ("member_id", "Member ID"),
            ("xp", "Lifetime XP"),
            ("level", "Level"),
            ("season_xp", "Season XP"),
            ("today_xp", "Recorded daily XP"),
        ],
        ["leaderboard", "level leaderboard", "rank", "level show"],
        "-xp",
    ),
    _spec(
        "level.progress",
        "LevelPlus",
        "Achievements and streaks",
        "Earned XP, badge counts, challenge completions, custom goals and recorded streaks.",
        [
            ("member", "Member"),
            ("member_id", "Member ID"),
            ("earned_xp", "Earned XP"),
            ("badges", "Badge IDs"),
            ("completed", "Completed weekly goals"),
            ("custom", "Earned custom goal IDs"),
            ("streak", "Recorded streak"),
            ("day", "Last active date"),
            ("pending", "Pending bonus XP"),
        ],
        ["achievements", "challenges", "achievement"],
        "member",
    ),
    _spec(
        "level.seasons",
        "LevelPlus",
        "Season archives",
        "Archived season rankings and their saved XP, without changing current lifetime totals.",
        [
            ("season", "Season"),
            ("ended", "Ended"),
            ("member", "Member"),
            ("member_id", "Member ID"),
            ("xp", "Season XP"),
        ],
        ["level season history"],
        "-ended",
    ),
    _spec(
        "level.rewards",
        "LevelPlus",
        "Level rewards",
        "Configured reward-role thresholds and whether each role still exists.",
        [
            ("role", "Role"),
            ("role_id", "Role ID"),
            ("level", "Required level"),
            ("exists", "Exists"),
        ],
        ["level rewards"],
        "level",
    ),
    _spec(
        "emoji.copied",
        "EmojiStealerPlus",
        "Captured emoji",
        "External-to-server emoji mappings with image previews. Internal hashes are excluded.",
        [
            ("name", "Emoji"),
            ("external_id", "Original ID"),
            ("emoji_id", "Server emoji ID"),
            ("animated", "Animated"),
            ("available", "In server"),
        ],
        ["emoji"],
        "name",
    ),
    _spec(
        "export.jobs",
        "ExportPlus",
        "Chat export progress",
        "Your current export's progress and completion state. Transcript contents and local file paths are excluded.",
        [
            ("state", "State"),
            ("messages", "Messages"),
            ("channels", "Channels checked"),
            ("volumes", "Files"),
            ("created", "Started"),
            ("finished", "Finished"),
            ("complete", "Complete"),
            ("error", "Safe error"),
        ],
        ["export progress"],
    ),
    _spec(
        "intro.clips",
        "IntroPlus",
        "Saved intros",
        "Members with saved YouTube intros, clip timing and local copy readiness. Browsing never downloads or plays audio.",
        [
            ("member", "Member"),
            ("member_id", "Member ID"),
            ("title", "Video"),
            ("author", "Creator"),
            ("duration", "Intro seconds"),
            ("start", "Start second"),
            ("cache", "Local copy"),
        ],
        ["intro show"],
        "member",
    ),
    _spec(
        "log.history",
        "LogPlus",
        "Retained server events",
        "Events within the configured retention period and currently visible source channels.",
        [
            ("at", "Occurred"),
            ("category", "Category"),
            ("event", "Event"),
            ("title", "Title"),
            ("description", "Details"),
            ("source", "Source channel"),
        ],
        ["logsearch", "log"],
        "-at",
    ),
    _spec(
        "log.incidents",
        "LogPlus",
        "Incident cases",
        "Retained staff cases, subjects, notes and resolutions. Hidden source-channel events are omitted.",
        [
            ("title", "Case"),
            ("subject", "Subject"),
            ("created", "Created"),
            ("notes", "Notes"),
            ("events", "Visible linked events"),
            ("resolution", "Resolution"),
        ],
        ["incident"],
        "-created",
    ),
    _spec(
        "log.summary",
        "LogPlus",
        "Moderation totals",
        "Recorded event-category totals by day.",
        [("day", "Date"), ("category", "Category"), ("count", "Events")],
        ["logalerts"],
        "-day",
    ),
    _spec(
        "owo.members",
        "OwoPlus",
        "Member preferences",
        "Saved per-member transformation odds and opt-outs.",
        [
            ("member", "Member"),
            ("member_id", "Member ID"),
            ("one_in", "One in N"),
            ("opted_out", "Opted out"),
        ],
        ["owo prob list", "owo"],
        "member",
    ),
    _spec(
        "owo.words",
        "OwoPlus",
        "Word dictionaries",
        "Saved custom replacements and syllable corrections.",
        [("kind", "Dictionary"), ("word", "Word"), ("value", "Replacement / syllables")],
        ["owo words list", "owo syllables list"],
        "word",
    ),
    _spec(
        "owo.styles",
        "OwoPlus",
        "Custom styles",
        "Saved custom style decorations and replacements.",
        [
            ("style", "Style"),
            ("word", "Word"),
            ("replacement", "Replacement"),
            ("prefix", "Prefix"),
            ("suffix", "Suffix"),
            ("uppercase", "Uppercase"),
        ],
        ["customstyle"],
        "style",
    ),
    _spec(
        "owo.haiku",
        "OwoPlus",
        "Haiku hall",
        "Retained approved and pending haiku submissions with author and review status.",
        [("author", "Author"), ("text", "Haiku"), ("at", "Submitted"), ("approved", "Approved")],
        ["haikuhall", "haikuhall review"],
        "-at",
    ),
    _spec(
        "owo.contests",
        "OwoPlus",
        "Haiku contest entries",
        "Retained contests and entries, with aggregate votes and winner status. Individual votes are excluded.",
        [
            ("contest", "Contest"),
            ("author", "Author"),
            ("text", "Haiku"),
            ("votes", "Votes"),
            ("winner", "Winner"),
            ("ends", "Closes"),
            ("closed", "Closed"),
        ],
        ["haikucontest"],
        "-ends",
    ),
    _spec(
        "presence.profiles",
        "PresencePlus",
        "Presence profiles",
        "Bot-wide saved profile entries and availability. Changes are visible across all servers.",
        [
            ("profile", "Profile"),
            ("status", "Availability"),
            ("kind", "Activity type"),
            ("text", "Display template"),
            ("selected", "Base profile"),
        ],
        ["presence profile list"],
        "profile",
    ),
    _spec(
        "presence.schedules",
        "PresencePlus",
        "Presence schedules",
        "Bot-wide weekly rules, local start/end times and configured timezone.",
        [
            ("rule", "Rule"),
            ("profile", "Profile"),
            ("days", "Weekdays"),
            ("start", "Start"),
            ("end", "End"),
            ("timezone", "Timezone"),
        ],
        ["presence schedule list"],
        "rule",
    ),
    _spec(
        "core.cogs",
        "CorePlus",
        "Loaded cog inventory",
        "The current runtime cog inventory. CorePlus has no separate stored member database.",
        [("cog", "Cog"), ("commands", "Root commands"), ("enabled", "Enabled in server")],
        ["core cogs"],
        "cog",
    ),
    _spec(
        "core.status",
        "CorePlus",
        "Bot runtime",
        "Read-only bot versions, gateway state and startup time.",
        [("item", "Item"), ("value", "Value")],
        ["core"],
    ),
    _spec(
        "download.installed",
        "DownloaderPlus",
        "Installed packages",
        "Red Downloader's installed packages, repositories, revisions and pin status.",
        [
            ("package", "Package"),
            ("repo", "Repository"),
            ("commit", "Revision"),
            ("pinned", "Pinned"),
        ],
        ["download installed"],
        "package",
    ),
    _spec(
        "download.repos",
        "DownloaderPlus",
        "Cog repositories",
        "Red Downloader's repository names, branches, revisions and module counts. Credentials and paths are excluded.",
        [
            ("repo", "Repository"),
            ("branch", "Branch"),
            ("commit", "Revision"),
            ("modules", "Available modules"),
        ],
        ["download repos"],
        "repo",
    ),
    _spec(
        "dashboard.status",
        "DashboardPlus",
        "Dashboard listener",
        "Listener configuration and runtime state. Login codes, cookies and sessions are never shown.",
        [("item", "Item"), ("value", "Value")],
        ["dashboard"],
    ),
    _spec(
        "settings.history",
        "SettingsHub",
        "Configuration changes",
        "Visible retained configuration changes with actor and previous/new values. Original source permission checks apply.",
        [
            ("at", "Changed"),
            ("actor", "Changed by"),
            ("cog", "Cog"),
            ("command", "Command"),
            ("path", "Setting"),
            ("before", "Previous value"),
            ("after", "New value"),
        ],
        ["settings history", "settings history show"],
        "-at",
    ),
    _spec(
        "settings.snapshots",
        "SettingsHub",
        "Settings snapshots",
        "Snapshot IDs, dates and captured cog names. Stored settings bundles are not exposed as raw Config.",
        [("at", "Created"), ("cogs", "Captured cogs"), ("count", "Cog count")],
        ["snapshots"],
        "-at",
    ),
]
DATASETS = {item.id: item for item in _SPECS}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, (list, tuple)) else []


def _text(value, limit=1000):
    return str(value)[:limit] if isinstance(value, (str, int, float, bool)) else ""


def _number(value, default=0):
    return value if type(value) is int or type(value) is float and math.isfinite(value) else default


def _id(value):
    value = str(value)
    return value if value.isascii() and value.isdecimal() and len(value) <= 20 else ""


def _time(value):
    try:
        stamp = _number(value)
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp > 0 else ""
    except (ValueError, OSError, OverflowError):
        return ""


def _member(ctx, identifier, fallback=""):
    identifier = _id(identifier)
    member = ctx.guild.get_member(int(identifier)) if identifier else None
    return _text(
        getattr(member, "display_name", None) or fallback or identifier or "Unattributed", 200
    )


def _visible(ctx, identifier):
    identifier = _id(identifier)
    if not identifier or identifier == "0":
        return True
    channel = ctx.guild.get_channel_or_thread(int(identifier))
    if channel is None:
        # Deleted-channel text cannot be re-authorized through current permissions.
        return False
    return bool(
        channel.permissions_for(ctx.author).view_channel
        and channel.permissions_for(ctx.guild.me).view_channel
    )


def _channel(ctx, identifier):
    identifier = _id(identifier)
    channel = ctx.guild.get_channel_or_thread(int(identifier)) if identifier else None
    return _text(getattr(channel, "name", None) or identifier or "Server", 200)


def _row(identifier, **values):
    return {
        "id": _text(identifier, 300),
        "values": {
            key: value if value is None or type(value) in {str, int, float, bool} else ""
            for key, value in values.items()
        },
    }


def _track(record):
    record = _dict(record)
    return track_card(
        SimpleNamespace(
            uri=_text(record.get("uri"), 2048),
            title=_text(record.get("title"), 500),
            author=_text(record.get("author"), 300),
            length=max(0, _number(record.get("length"))),
        )
    )


def _art(row, track):
    if track["thumbnail"]:
        row["image"] = track["thumbnail"]
    if track["url"]:
        row["url"] = track["url"]
    return row


async def _native(ctx, path, name):
    command = ctx.bot.get_command(path)
    if command is None or getattr(command.cog, "qualified_name", None) != name:
        raise commands.CheckFailure(f"{name} is unavailable.")
    await check_command(ctx, command)
    return command.cog


async def _audio(identifier, cog, ctx):
    group = cog.config.guild(ctx.guild)
    if identifier == "audio.history":
        records = await group.listening_history()
        rows = []
        for index, record in enumerate(_list(records)):
            record = _dict(record)
            if _number(record.get("at")) < time.time() - 30 * 86400:
                continue
            track = _track(record.get("track"))
            rows.append(
                _art(
                    _row(
                        record.get("id", index),
                        title=track["title"],
                        requester=_member(ctx, record.get("requester", 0)),
                        author=track["author"],
                        duration=track["duration"] / 1000,
                        at=_time(record.get("at")),
                    ),
                    track,
                )
            )
        return rows
    if identifier == "audio.recovery":
        await cog._playlist_manager(ctx)
        saved = _dict(await group.recovery())
        if _number(saved.get("at")) < time.time() - 7 * 86400:
            return []
        requesters = _list(saved.get("requesters"))
        return [
            _art(
                _row(
                    index,
                    position=index + 1,
                    title=track["title"],
                    requester=_member(ctx, requesters[index] if index < len(requesters) else 0),
                    duration=track["duration"] / 1000,
                    saved=_time(saved.get("at")),
                ),
                track,
            )
            for index, record in enumerate(_list(saved.get("tracks")))
            if (track := _track(record))
        ]
    playlists, favorites, shared = await asyncio.gather(
        group.playlists(), group.favorites(), group.server_playlists()
    )
    rows = []

    def append(owner, kind, collection, records):
        for index, record in enumerate(_list(records)):
            record = _dict(record)
            owner_id = record.get("user", owner) if kind == "Suggestion" else owner
            track = _track(record.get("track", record))
            rows.append(
                _art(
                    _row(
                        f"{kind}:{owner}:{collection}:{index}",
                        member=_member(ctx, owner_id),
                        kind=kind,
                        collection=_text(collection, 100),
                        position=index + 1,
                        title=track["title"],
                        duration=track["duration"] / 1000,
                    ),
                    track,
                )
            )

    for owner, collections in _dict(playlists).items():
        for name, records in _dict(collections).items():
            append(owner, "Personal playlist", name, records)
    for owner, records in _dict(favorites).items():
        append(owner, "Favorites", "Favorites", records)
    for name, collection in _dict(shared).items():
        append(0, "Shared playlist", name, _dict(collection).get("tracks"))
        append(0, "Suggestion", name, _dict(collection).get("suggestions"))
    return rows


async def _community(identifier, cog, ctx):
    if identifier in {"community.activity", "community.games"}:
        members = await cog.config.all_members(ctx.guild)
        rows = []
        for uid, data in _dict(members).items():
            if not _id(uid):
                continue
            data = _dict(data)
            if identifier == "community.games":
                rows.extend(
                    _row(
                        f"{uid}:{index}",
                        member=_member(ctx, uid),
                        member_id=str(uid),
                        game=_text(game, 500),
                        launches=_number(count),
                    )
                    for index, (game, count) in enumerate(_dict(data.get("activity_names")).items())
                )
                continue
            stats, seen, participation = (
                _dict(data.get(key)) for key in ("stats", "seen", "participation")
            )
            rows.append(
                _row(
                    uid,
                    member=_member(ctx, uid),
                    member_id=str(uid),
                    messages=_number(stats.get("messages")),
                    voice_hours=round(_number(participation.get("voice_seconds")) / 3600, 2),
                    voice_joins=_number(stats.get("voice_joins")),
                    voice_moves=_number(stats.get("voice_moves")),
                    voice_leaves=_number(stats.get("voice_leaves")),
                    streams=_number(stats.get("stream_starts")),
                    video=_number(stats.get("video_starts")),
                    games=_number(stats.get("game_launches")),
                    presence=_text(_dict(seen.get("presence")).get("status"), 50),
                    last_seen=_time(seen.get("any")),
                )
            )
        return rows
    group = cog.config.guild(ctx.guild)
    if identifier == "community.rooms":
        rooms = await group.voice_rooms()
        return [
            _row(
                cid,
                channel=_channel(ctx, cid),
                channel_id=str(cid),
                owner=_member(ctx, _dict(record).get("owner", 0)),
                created=_time(_dict(record).get("created")),
            )
            for cid, record in _dict(rooms).items()
            if _visible(ctx, cid)
        ]
    social = _dict(await group.social())
    kind = "events" if identifier == "community.events" else "polls"
    rows = []
    for key, record in _dict(social.get(kind)).items():
        record = _dict(record)
        if not _visible(ctx, record.get("channel", 0)):
            continue
        status = (
            "Closed" if record.get("closed") or _number(record.get("at")) <= time.time() else "Open"
        )
        if kind == "events":
            attendance = _dict(record.get("rsvps"))
            native = _dict(record.get("native"))
            row = _row(
                key,
                title=_text(record.get("title")),
                at=_time(record.get("at")),
                status=status,
                going=sum(v == "yes" for v in attendance.values()),
                wait=sum(v == "wait" for v in attendance.values()),
                maybe=sum(v == "maybe" for v in attendance.values()),
                capacity=_number(record.get("capacity")),
                native=_id(native.get("id", 0)),
            )
            if _id(native.get("id")):
                row["url"] = f"https://discord.com/events/{ctx.guild.id}/{native['id']}"
            rows.append(row)
        else:
            votes = _dict(record.get("votes"))
            for index, option in enumerate(_list(record.get("options")), 1):
                rows.append(
                    _row(
                        f"{key}:{index}",
                        title=_text(record.get("title")),
                        option=_text(option),
                        votes=sum(value == index for value in votes.values()),
                        at=_time(record.get("at")),
                        status=status,
                    )
                )
    return rows


async def _level(identifier, cog, ctx):
    group = cog.config.guild(ctx.guild)
    xp, names, periods = await asyncio.gather(group.xp(), group.names(), group.period_xp())
    names, periods = _dict(names), _dict(periods)
    if identifier == "level.members":
        settings, daily = await asyncio.gather(cog._settings(ctx.guild), group.earned_today())
        today = _dict(_dict(daily).get("xp"))
        season = _dict(periods.get("season"))
        rows = []
        for uid, amount in _dict(xp).items():
            if not _id(uid):
                continue
            value = max(0, _number(amount))
            rows.append(
                _row(
                    uid,
                    member=_member(ctx, uid, names.get(str(uid), "")),
                    member_id=str(uid),
                    xp=value,
                    level=cog._level(value, settings),
                    season_xp=_number(season.get(str(uid))),
                    today_xp=_number(today.get(str(uid))),
                )
            )
        return rows
    if identifier == "level.progress":
        milestones, progress = await asyncio.gather(group.milestones(), group.progress())
        milestones, progress = _dict(milestones), _dict(progress)
        rows = []
        for uid in milestones.keys() | progress.keys():
            record, custom = _dict(milestones.get(uid)), _dict(progress.get(uid))
            rows.append(
                _row(
                    uid,
                    member=_member(ctx, uid, names.get(str(uid), "")),
                    member_id=str(uid),
                    earned_xp=_number(record.get("earned_xp")),
                    badges=", ".join(_text(key, 100) for key in _dict(record.get("badges"))),
                    completed=", ".join(_text(key, 100) for key in _list(record.get("completed"))),
                    custom=", ".join(_text(key, 100) for key in _dict(custom.get("earned"))),
                    streak=_number(custom.get("streak")),
                    day=_text(custom.get("day"), 50),
                    pending=_number(record.get("pending")) + _number(custom.get("pending")),
                )
            )
        return rows
    if identifier == "level.seasons":
        return [
            _row(
                f"{index}:{uid}",
                season=_text(_dict(archive).get("name")),
                ended=_time(_dict(archive).get("ended")),
                member=_member(ctx, uid, names.get(str(uid), "")),
                member_id=str(uid),
                xp=_number(value),
            )
            for index, archive in enumerate(_list(periods.get("archives")))
            for uid, value in _dict(_dict(archive).get("xp")).items()
        ]
    rewards = _dict(await group.rewards())
    rows = []
    for rid, level in _dict(rewards.get("roles")).items():
        role = ctx.guild.get_role(int(rid)) if _id(rid) else None
        rows.append(
            _row(
                rid,
                role=_text(getattr(role, "name", None) or rid),
                role_id=str(rid),
                level=_number(level),
                exists=role is not None,
            )
        )
    return rows


async def _intro(cog, ctx):
    records = await cog.config.all_members(ctx.guild)
    rows = []
    for uid, data in _dict(records).items():
        clip = _dict(_dict(data).get("clip"))
        if not clip or not _id(uid):
            continue
        track = _track(clip.get("track"))
        peek = getattr(cog._cache, "peek_status", None)
        cache = "Update IntroPlus to see readiness"
        try:
            if peek is not None:
                cache = _text(peek(ctx.guild.id, int(uid), clip), 100)
        except (KeyError, TypeError, ValueError):
            cache = "Invalid saved clip"
        rows.append(
            _art(
                _row(
                    uid,
                    member=_member(ctx, uid),
                    member_id=str(uid),
                    title=track["title"],
                    author=track["author"],
                    duration=_number(clip.get("duration")),
                    start=_number(clip.get("start")),
                    cache=cache,
                ),
                track,
            )
        )
    return rows


async def _backup(cog, ctx):
    await cog._authorize(ctx)
    state = _dict(await cog.config.guild(ctx.guild).state())
    rows = []
    for name, record in _dict(state.get("snapshots")).items():
        record, data = _dict(record), _dict(_dict(record).get("data"))
        channels = _list(data.get("channels"))
        rows.append(
            _row(
                name,
                name=_text(name),
                category=_text(record.get("category")),
                at=_time(data.get("created_at")),
                roles=len(_list(data.get("roles"))),
                channels=len(channels),
                overwrites=sum(
                    len(_list(_dict(channel).get("overwrites"))) for channel in channels
                ),
                pending=len(_dict(record.get("pending"))),
            )
        )
    return rows


async def _emoji(cog, ctx):
    copied = _dict(await cog.config.guild(ctx.guild).copied())
    available = {emoji.id for emoji in ctx.guild.emojis}
    rows = []
    for original, record in copied.items():
        record = _dict(record)
        eid = _id(record.get("emoji", 0))
        row = _row(
            original,
            name=_text(record.get("name"), 100),
            external_id=str(original),
            emoji_id=eid,
            animated=bool(record.get("animated")),
            available=int(eid) in available if eid else False,
        )
        if eid and eid != "0":
            row["image"] = (
                f"https://cdn.discordapp.com/emojis/{eid}.{'gif' if record.get('animated') else 'png'}"
            )
        rows.append(row)
    return rows


async def _export(cog, ctx):
    async with cog._lock:
        job = cog._jobs.get(ctx.guild.id)
        if job is None or job.owner_id != ctx.author.id:
            return []
        await cog._authorize(job)
        return [
            _row(
                ctx.guild.id,
                state=_text(job.state, 100),
                messages=_number(job.messages),
                channels=len(job.channels),
                volumes=len(job.volumes),
                created=_time(job.created),
                finished=_time(job.finished),
                complete=bool(job.complete),
                error=_text(job.error, 200),
            )
        ]


async def _log(identifier, cog, ctx):
    group = cog.config.guild(ctx.guild)
    if identifier == "log.summary":
        summary = _dict(await group.moderation_summary())
        return [
            _row(
                f"{day}:{category}",
                day=_text(day, 50),
                category=_text(category, 100),
                count=_number(count),
            )
            for day, counts in _dict(summary.get("days")).items()
            for category, count in _dict(counts).items()
        ]
    if identifier == "log.incidents":
        cases = _dict(await group.incident_cases())
        rows = []
        for key, record in cases.items():
            record = _dict(record)
            if _number(record.get("created")) < time.time() - 90 * 86400:
                continue
            events = await _authorized_log_records(cog, ctx, _list(record.get("events")))
            rows.append(
                _row(
                    key,
                    title=_text(record.get("title")),
                    subject=_member(ctx, record.get("subject", 0)),
                    created=_time(record.get("created")),
                    notes="\n".join(
                        f"{_member(ctx, _dict(note).get('user', 0))}: {_text(_dict(note).get('text'), 1000)}"
                        for note in _list(record.get("notes"))
                    )[:4000],
                    events=len(events),
                    resolution=_text(_dict(record.get("resolution")).get("text"), 2000),
                )
            )
        return rows
    records, policy = await asyncio.gather(group.history_records(), group.history_settings())
    cutoff = time.time() - min(90, max(1, _number(_dict(policy).get("days"), 30))) * 86400
    rows = []
    for index, record in enumerate(await _authorized_log_records(cog, ctx, _list(records))):
        record = _dict(record)
        if _number(record.get("time")) < cutoff:
            continue
        details = [_text(record.get("description"), 2000)]
        details.extend(
            f"{_text(_dict(field).get('name'), 100)}: {_text(_dict(field).get('value'), 1000)}"
            for field in _list(record.get("fields"))[:25]
        )
        rows.append(
            _row(
                index,
                at=_time(record.get("time")),
                category=_text(record.get("category"), 100),
                event=_text(record.get("event"), 200),
                title=_text(record.get("title")),
                description="\n".join(value for value in details if value)[:6000],
                source=_channel(ctx, record.get("source", 0)),
            )
        )
    return rows


async def _authorized_log_records(cog, ctx, records):
    records = [_dict(record) for record in records]
    authorize = getattr(cog, "_visible_history", None)
    if callable(authorize):
        return await authorize(ctx, records)
    # Older LogPlus versions lack current history/private-thread checks. Expose only
    # records explicitly marked as server events until LogPlus is updated.
    return [
        record
        for record in records
        if "source" in record
        and not isinstance(record["source"], bool)
        and record["source"] in (None, 0)
    ]


async def _owo(identifier, cog, ctx):
    group = cog.config.guild(ctx.guild)
    features = _dict(await group.features())
    if identifier == "owo.members":
        odds, default = await asyncio.gather(group.user_probs(), group.one_in())
        odds, optouts = _dict(odds), _dict(features.get("optouts"))
        return [
            _row(
                uid,
                member=_member(ctx, uid),
                member_id=str(uid),
                one_in=_number(odds.get(uid), _number(default)),
                opted_out=bool(optouts.get(uid)),
            )
            for uid in odds.keys() | optouts.keys()
            if _id(uid)
        ]
    if identifier == "owo.words":
        return [
            _row(f"{kind}:{word}", kind=kind, word=_text(word), value=_text(value))
            for kind, key in (("Replacement", "words"), ("Syllables", "syllables"))
            for word, value in _dict(features.get(key)).items()
        ]
    if identifier == "owo.styles":
        return [
            _row(
                f"{name}:{index}",
                style=_text(name),
                word=_text(word),
                replacement=_text(value),
                prefix=_text(_dict(style).get("prefix")),
                suffix=_text(_dict(style).get("suffix")),
                uppercase=bool(_dict(style).get("uppercase")),
            )
            for name, style in _dict(features.get("custom_styles")).items()
            for index, (word, value) in enumerate(
                _dict(_dict(style).get("words")).items() or [("", "")]
            )
        ]
    poetry = _dict(await group.poetry())
    if identifier == "owo.haiku":
        return [
            _row(
                key,
                author=_member(ctx, _dict(record).get("author", 0)),
                text=_text(_dict(record).get("text"), 2000),
                at=_time(_dict(record).get("at")),
                approved=bool(_dict(record).get("approved")),
            )
            for key, record in _dict(poetry.get("hall")).items()
            if _number(_dict(record).get("at")) >= time.time() - 90 * 86400
        ]
    rows = []
    for key, contest in _dict(poetry.get("contests")).items():
        contest = _dict(contest)
        if (
            not _visible(ctx, contest.get("channel", 0))
            or _number(contest.get("at")) < time.time() - 90 * 86400
        ):
            continue
        entries = _dict(contest.get("entries"))
        for eid, record in entries.items() or [("", {})]:
            record = _dict(record)
            rows.append(
                _row(
                    f"{key}:{eid}",
                    contest=_text(contest.get("title")),
                    author=_member(ctx, record.get("author", 0)),
                    text=_text(record.get("text"), 2000),
                    votes=sum(value == eid for value in _dict(contest.get("votes")).values()),
                    winner=bool(eid and contest.get("winner") == eid),
                    ends=_time(contest.get("ends")),
                    closed=bool(contest.get("closed")),
                )
            )
    return rows


async def _presence(identifier, cog):
    settings = _dict(await cog.config.settings())
    if identifier == "presence.profiles":
        return [
            _row(
                f"{name}:{index}",
                profile=_text(name),
                status=_text(_dict(profile).get("status")),
                kind=_text(_dict(entry).get("kind")),
                text=_text(_dict(entry).get("text")),
                selected=name == settings.get("selected"),
            )
            for name, profile in _dict(settings.get("profiles")).items()
            for index, entry in enumerate(_list(_dict(profile).get("entries")) or [{}])
        ]
    rows = []
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    for name, rule in _dict(settings.get("schedules")).items():
        rule = _dict(rule)
        start, end = int(_number(rule.get("start"))), int(_number(rule.get("end")))
        rows.append(
            _row(
                name,
                rule=_text(name),
                profile=_text(rule.get("profile")),
                days=", ".join(
                    weekdays[day]
                    for day in _list(rule.get("days"))
                    if type(day) is int and 0 <= day < 7
                ),
                start=f"{start // 60:02}:{start % 60:02}",
                end=f"{end // 60:02}:{end % 60:02}",
                timezone=_text(settings.get("timezone")),
            )
        )
    return rows


async def _settings(identifier, cog, ctx):
    group = cog.config.guild(ctx.guild)
    visible = {"SettingsHub"}
    for name, source in cog._loaded().items():
        try:
            await cog._source_check(ctx, source)
        except (commands.CheckFailure, commands.DisabledCommand):
            continue
        visible.add(name)
    if identifier == "settings.snapshots":
        snapshots = _dict(await group.snapshots())
        rows = []
        for record in _list(snapshots.get("records")):
            record = _dict(record)
            names = sorted(set(_dict(_dict(record.get("bundle")).get("cogs"))) & visible)
            rows.append(
                _row(
                    record.get("id", ""),
                    at=_time(record.get("at")),
                    cogs=", ".join(names),
                    count=len(names),
                )
            )
        return rows
    records, policy = await asyncio.gather(group.configuration_history(), group.audit_policy())
    cutoff = time.time() - min(90, max(1, _number(_dict(policy).get("days"), 30))) * 86400
    rows = []
    for record in _list(records):
        record = _dict(record)
        if record.get("cog") not in visible or _number(record.get("at")) < cutoff:
            continue
        for index, change in enumerate(_list(record.get("changes"))):
            change = _dict(change)
            rows.append(
                _row(
                    f"{record.get('id', '')}:{index}",
                    at=_time(record.get("at")),
                    actor=_member(ctx, record.get("actor", 0)),
                    cog=_text(record.get("cog")),
                    command=_text(record.get("command")),
                    path=_text(change.get("path")),
                    before=json.dumps(change.get("before"), ensure_ascii=False, allow_nan=False)[
                        :4000
                    ],
                    after=json.dumps(change.get("after"), ensure_ascii=False, allow_nan=False)[
                        :4000
                    ],
                )
            )
    return rows


async def _operational(identifier, cog, ctx):
    if identifier.startswith("core."):
        if identifier == "core.cogs":
            await _native(ctx, "cogs", "CogManagerUI")
            return [
                _row(
                    name,
                    cog=_text(name),
                    commands=len(source.get_commands()),
                    enabled=not await ctx.bot.cog_disabled_in_guild(source, ctx.guild),
                )
                for name, source in ctx.bot.cogs.items()
            ]
        import platform

        import discord
        from redbot.core import version_info

        startup = getattr(ctx.bot, "uptime", None)
        rows = {
            "Red": str(version_info),
            "Discord.py": discord.__version__,
            "Python": platform.python_version(),
            "Gateway ready": bool(ctx.bot.is_ready()),
            "Gateway latency (ms)": round(_number(ctx.bot.latency) * 1000, 1),
            "Started": startup.isoformat() if isinstance(startup, datetime) else "",
            "Loaded cogs": len(ctx.bot.cogs),
        }
    elif identifier.startswith("download."):
        source = await _native(
            ctx, "cog list" if identifier == "download.installed" else "repo list", "Downloader"
        )
        await source.cog_before_invoke(ctx)
        if identifier == "download.installed":
            return [
                _row(
                    f"{item.repo_name}:{item.name}",
                    package=_text(item.name),
                    repo=_text(item.repo_name),
                    commit=_text(item.commit, 100),
                    pinned=bool(item.pinned),
                )
                for item in await source.installed_cogs()
            ]
        return [
            _row(
                item.name,
                repo=_text(item.name),
                branch=_text(item.branch),
                commit=_text(item.commit, 100),
                modules=len(item.available_modules),
            )
            for item in source._repo_manager.repos
        ]
    else:
        settings = _dict(await cog.config.settings())
        rows = {
            "Enabled on startup": bool(settings.get("enabled")),
            "Listening now": cog._runner is not None,
            "Bind address": _text(settings.get("bind")),
            "Port": _number(settings.get("port")),
            "Dashboard URL": public_url(settings.get("url")) or cog._url(),
            "Allowed hostnames": ", ".join(
                _text(host, 253) for host in _list(settings.get("hosts"))
            ),
        }
    return [_row(key, item=key, value=value) for key, value in rows.items()]


async def read_dataset(spec, cog, ctx, *, page=1, page_size=25, query="", sort=""):
    """Read a checked dataset, project safe fields, and page without changing stored data.

    The HTTP boundary checks every ``spec.commands`` path against ``spec.cog`` before
    calling this function. Adapters also preserve custom authorization inside source
    command bodies, such as BackupPlus's actual Discord Administrator requirement.
    """
    if isinstance(spec, str):
        spec = DATASETS[spec]
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100:
        raise commands.BadArgument("Choose a positive page and 1 to 100 rows per page.")
    if not isinstance(query, str) or len(query) > 200:
        raise commands.BadArgument("Use at most 200 search characters.")
    sort = sort or spec.sort
    if not isinstance(sort, str) or sort.removeprefix("-") not in {
        key for key, _ in spec.columns
    } | {""}:
        raise commands.BadArgument("Choose a displayed column to sort.")
    identifier = spec.id
    if identifier.startswith("audio."):
        rows = await _audio(identifier, cog, ctx)
    elif identifier.startswith("community."):
        rows = await _community(identifier, cog, ctx)
    elif identifier.startswith("level."):
        rows = await _level(identifier, cog, ctx)
    elif identifier.startswith("intro."):
        rows = await _intro(cog, ctx)
    elif identifier.startswith("backup."):
        rows = await _backup(cog, ctx)
    elif identifier.startswith("emoji."):
        rows = await _emoji(cog, ctx)
    elif identifier.startswith("export."):
        rows = await _export(cog, ctx)
    elif identifier.startswith("log."):
        rows = await _log(identifier, cog, ctx)
    elif identifier.startswith("owo."):
        rows = await _owo(identifier, cog, ctx)
    elif identifier.startswith("presence."):
        rows = await _presence(identifier, cog)
    elif identifier.startswith("settings."):
        rows = await _settings(identifier, cog, ctx)
    else:
        rows = await _operational(identifier, cog, ctx)
    needle = query.casefold().strip()
    if needle:
        rows = [
            row
            for row in rows
            if needle
            in (
                row["id"] + " " + " ".join(str(value) for value in row["values"].values())
            ).casefold()
        ]
    if sort:
        key = sort.removeprefix("-")

        def ordering(row):
            value = row["values"].get(key)
            return (
                (0, value)
                if type(value) in {int, float, bool}
                else (1, str(value or "").casefold())
            )

        rows.sort(key=ordering, reverse=sort.startswith("-"))
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    return {
        **spec.catalog(),
        "rows": rows[(page - 1) * page_size : page * page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
    }
