"""Bounded, in-memory hourly summaries of observed member presence.

Only the current hour and the latest completed hour are retained per member.
Disconnects and disabled collection must clear the relevant state externally;
this tracker never reads historical presence or writes collected data to disk.
"""

import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

HOUR_SECONDS = 3600
MAX_MEMBERS = 5000
MAX_GUILD_MEMBERS = 1000
MAX_ACTIVITY_LABELS = 20
MAX_ACTIVE_ACTIVITIES = 5
MAX_LABEL_LENGTH = 120
OTHER_ACTIVITIES = "Other activities"
STATUSES = ("online", "idle", "dnd", "offline")
ACTIVITY_TYPES = {
    0: "Playing",
    1: "Streaming",
    2: "Listening",
    3: "Watching",
    5: "Competing",
}


def _timestamp(now):
    now = float(now)
    if not math.isfinite(now):
        raise ValueError("Presence timestamps must be finite.")
    return now


def _hour_start(now):
    return math.floor(now / HOUR_SECONDS) * HOUR_SECONDS


def _status(member):
    status = str(getattr(member, "status", "offline"))
    return status if status in STATUSES else "offline"


def _clean_text(value, limit=MAX_LABEL_LENGTH):
    # Names are enough to describe an activity. Do not collect custom text,
    # rich-presence details, party information, or streaming URLs.
    return " ".join(str(value).split())[:limit]


def _activity_labels(member):
    labels = []
    for activity in getattr(member, "activities", ()) or ():
        kind = getattr(activity, "type", None)
        kind = getattr(kind, "value", kind)
        prefix = ACTIVITY_TYPES.get(kind)
        name = getattr(activity, "name", None)
        if kind == 1 and hasattr(activity, "platform"):
            # discord.Streaming aliases its details into .name; .platform
            # preserves the original activity name without that private text.
            name = activity.platform
        if prefix is None or not isinstance(name, str):
            continue
        name = _clean_text(name)
        if not name:
            continue
        label = f"{prefix} {name}"[:MAX_LABEL_LENGTH]
        if label not in labels:
            labels.append(label)
        if len(labels) == MAX_ACTIVE_ACTIVITIES:
            break
    return tuple(labels)


@dataclass(frozen=True)
class Summary:
    """One observed UTC clock-hour window with immutable duration mappings."""

    guild_id: int
    user_id: int
    name: str
    start: float
    end: float
    status_seconds: Mapping[str, float]
    activity_seconds: Mapping[str, float]
    status_changes: int
    generation: int = field(repr=False)

    @property
    def partial(self):
        return self.start > _hour_start(self.end - 1)


@dataclass
class _Hour:
    start: float
    end: float
    status_seconds: dict = field(default_factory=lambda: dict.fromkeys(STATUSES, 0.0))
    activity_seconds: dict = field(default_factory=dict)
    status_changes: int = 0

    def accrue(self, status, activities, seconds):
        if seconds <= 0:
            return
        self.status_seconds[status] += seconds
        for label in activities:
            if label not in self.activity_seconds:
                if OTHER_ACTIVITIES in self.activity_seconds:
                    label = OTHER_ACTIVITIES
                elif len(self.activity_seconds) >= MAX_ACTIVITY_LABELS:
                    # Keep at most 20 entries including the overflow bucket.
                    # Preserve the duration of the replaced label as well.
                    _, previous = self.activity_seconds.popitem()
                    self.activity_seconds[OTHER_ACTIVITIES] = previous
                    label = OTHER_ACTIVITIES
            self.activity_seconds[label] = self.activity_seconds.get(label, 0.0) + seconds


@dataclass
class _Member:
    guild_id: int
    user_id: int
    name: str
    generation: int
    observed_from: float
    cursor: float
    status: str
    activities: tuple
    hour: _Hour
    pending: Summary | None = None

    def summarize(self):
        hour = self.hour
        if (
            not any(hour.status_seconds[status] for status in STATUSES[:-1])
            and not hour.activity_seconds
            and not hour.status_changes
        ):
            return None
        return Summary(
            guild_id=self.guild_id,
            user_id=self.user_id,
            name=self.name,
            start=hour.start,
            end=hour.end,
            status_seconds=MappingProxyType(dict(hour.status_seconds)),
            activity_seconds=MappingProxyType(dict(hour.activity_seconds)),
            status_changes=hour.status_changes,
            generation=self.generation,
        )

    def advance(self, now):
        if now <= self.cursor:
            return
        if now < self.hour.end:
            self.hour.accrue(self.status, self.activities, now - self.cursor)
            self.cursor = now
            return

        self.hour.accrue(self.status, self.activities, self.hour.end - self.cursor)
        self.pending = self.summarize()
        current_start = _hour_start(now)
        if current_start > self.hour.end:
            # Missed ticks do not produce historical batches. Keep only the
            # latest completed window, carrying the last observed state.
            self.hour = _Hour(current_start - HOUR_SECONDS, current_start)
            self.hour.accrue(self.status, self.activities, HOUR_SECONDS)
            self.pending = self.summarize()
        self.hour = _Hour(current_start, current_start + HOUR_SECONDS)
        self.cursor = current_start
        self.hour.accrue(self.status, self.activities, now - self.cursor)
        self.cursor = now


class PresenceTracker:
    """Collect presence synchronously and drain each completed window once.

    A full tracker first evicts quiet offline members, then rejects additional
    members if all remaining slots have activity or an undrained transition.
    Each member has at most five simultaneous activities and twenty hourly
    activity entries, including an overflow bucket when needed. Activity time
    can overlap because Discord can expose multiple simultaneous activities.
    """

    def __init__(self, *, max_members=MAX_MEMBERS, max_guild_members=MAX_GUILD_MEMBERS):
        self.max_members = max(0, min(int(max_members), MAX_MEMBERS))
        self.max_guild_members = max(0, min(int(max_guild_members), MAX_GUILD_MEMBERS))
        self._members = {}
        self._guild_counts = {}
        self._generation = 0

    def __len__(self):
        return len(self._members)

    @staticmethod
    def _quiet_offline(state):
        return (
            state.status == "offline"
            and not state.activities
            and state.pending is None
            and not state.hour.status_changes
            and not state.hour.activity_seconds
            and not any(state.hour.status_seconds[status] for status in STATUSES[:-1])
        )

    def _make_room(self, guild_id):
        if self._guild_counts.get(guild_id, 0) >= self.max_guild_members:
            for key, state in self._members.items():
                if state.guild_id == guild_id and self._quiet_offline(state):
                    self.drop_member(*key)
                    break
            if self._guild_counts.get(guild_id, 0) >= self.max_guild_members:
                return
        if len(self._members) >= self.max_members:
            for key, state in self._members.items():
                if self._quiet_offline(state):
                    self.drop_member(*key)
                    break

    def observe(self, member, now):
        """Observe current state without assuming any time before this call."""
        if getattr(member, "bot", False):
            return
        now = _timestamp(now)
        guild_id, user_id = int(member.guild.id), int(member.id)
        key = guild_id, user_id
        state = self._members.get(key)
        if state is None:
            status, activities = _status(member), _activity_labels(member)
            if status == "offline" and not activities:
                return
            self._make_room(guild_id)
            if (
                len(self._members) >= self.max_members
                or self._guild_counts.get(guild_id, 0) >= self.max_guild_members
            ):
                return
            self._generation += 1
            self._members[key] = _Member(
                guild_id=guild_id,
                user_id=user_id,
                name=_clean_text(getattr(member, "display_name", str(user_id))),
                generation=self._generation,
                observed_from=now,
                cursor=now,
                status=status,
                activities=activities,
                hour=_Hour(now, _hour_start(now) + HOUR_SECONDS),
            )
            self._guild_counts[guild_id] = self._guild_counts.get(guild_id, 0) + 1
            return
        if now < state.cursor:
            return
        state.advance(now)
        status = _status(member)
        if state.status != status:
            state.hour.status_changes += 1
        state.status = status
        state.activities = _activity_labels(member)
        state.name = _clean_text(getattr(member, "display_name", str(user_id)))

    def drain(self, now):
        """Return the latest completed nonquiet hour per member exactly once."""
        now = _timestamp(now)
        summaries = []
        quiet_members = []
        for state in self._members.values():
            completed_hour = now >= state.hour.end
            state.advance(now)
            if state.pending is not None and state.pending.end <= now:
                summaries.append(state.pending)
                state.pending = None
            elif completed_hour and self._quiet_offline(state):
                # Keep a just-drained transition's state valid for delivery.
                # A later quiet completed hour needs no report or tracking slot.
                quiet_members.append((state.guild_id, state.user_id))
        for key in quiet_members:
            self.drop_member(*key)
        return summaries

    def is_current(self, summary):
        """Reject a drained summary if its state was removed or recreated."""
        state = self._members.get((summary.guild_id, summary.user_id))
        return state is not None and state.generation == summary.generation

    def drop_member(self, guild_id, user_id):
        state = self._members.pop((int(guild_id), int(user_id)), None)
        if state is None:
            return
        remaining = self._guild_counts[state.guild_id] - 1
        if remaining:
            self._guild_counts[state.guild_id] = remaining
        else:
            self._guild_counts.pop(state.guild_id, None)

    def drop_guild(self, guild_id):
        guild_id = int(guild_id)
        for key in [key for key in self._members if key[0] == guild_id]:
            self._members.pop(key)
        self._guild_counts.pop(guild_id, None)

    def drop_user(self, user_id):
        user_id = int(user_id)
        for guild_id, uid in [key for key in self._members if key[1] == user_id]:
            self.drop_member(guild_id, uid)

    def export_user(self, user_id):
        """Return a detached JSON-serializable copy of currently held data."""
        user_id = int(user_id)
        data = {}
        for state in self._members.values():
            if state.user_id != user_id:
                continue
            hour = state.hour
            pending = state.pending
            data[str(state.guild_id)] = {
                "user_id": state.user_id,
                "name": state.name,
                "observed_from": state.observed_from,
                "last_accounted_at": state.cursor,
                "status": state.status,
                "activities": list(state.activities),
                "current_hour": {
                    "start": hour.start,
                    "end": hour.end,
                    "status_seconds": dict(hour.status_seconds),
                    "activity_seconds": dict(hour.activity_seconds),
                    "status_changes": hour.status_changes,
                },
                "pending_hour": {
                    "start": pending.start,
                    "end": pending.end,
                    "status_seconds": dict(pending.status_seconds),
                    "activity_seconds": dict(pending.activity_seconds),
                    "status_changes": pending.status_changes,
                }
                if pending is not None
                else None,
            }
        return data

    def clear(self):
        self._members.clear()
        self._guild_counts.clear()
