"""Earned XP policies and calendar rankings, without changing lifetime totals."""

import hashlib
import re
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from redbot.core import commands

FEATURE_DEFAULTS_GUILD = {
    "rewards": {"roles": {}, "stack": True},
    "xp_features": {
        "periods": True,
        "timezone": "America/Chicago",
        "boosts": [],
        "repeat_seconds": 0,
        "reaction_once": False,
        "daily_cap": 0,
        "min_words": 0,
    },
    "period_xp": {
        "days": {},
        "season_name": "Current season",
        "season_start": 0,
        "season": {},
        "archives": [],
    },
    "earned_today": {"day": "", "xp": {}},
}


def day_at(timestamp, zone):
    try:
        tz = ZoneInfo(zone)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    return datetime.fromtimestamp(timestamp, tz).date()


def valid_timezone(zone):
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise commands.BadArgument(
            "Use an installed IANA timezone, such as America/Chicago or UTC."
        ) from error
    return zone


def safe_role(guild, role):
    dangerous = (
        "administrator",
        "manage_roles",
        "manage_guild",
        "manage_channels",
        "ban_members",
        "kick_members",
        "moderate_members",
        "manage_webhooks",
    )
    if (
        role.is_default()
        or role.managed
        or role >= guild.me.top_role
        or any(getattr(role.permissions, key) for key in dangerous)
    ):
        raise commands.BadArgument(
            "Choose an unmanaged role below the bot without server-management or moderation permissions."
        )


def boosted_amount(amount, member, channel, features, now):
    factor = 1
    role_ids = {role.id for role in member.roles}
    channel_ids = {getattr(channel, "id", None), getattr(channel, "parent_id", None)}
    for boost in features["boosts"]:
        if boost["expires"] <= now:
            continue
        if boost["role"] and boost["role"] not in role_ids:
            continue
        if boost["channel"] and boost["channel"] not in channel_ids:
            continue
        factor = max(factor, boost["factor"])
    scale = Fraction(str(factor))
    return max(0, int(amount)) * scale.numerator // scale.denominator


def record_period(data, uid, amount, day, now):
    key = day.isoformat()
    cutoff = (day - timedelta(days=34)).isoformat()
    for old in tuple(data["days"]):
        if old < cutoff:
            data["days"].pop(old)
    bucket = data["days"].setdefault(key, {})
    bucket[uid] = bucket.get(uid, 0) + amount
    data["season"][uid] = data["season"].get(uid, 0) + amount
    if not data["season_start"]:
        data["season_start"] = now


def period_totals(data, period, today):
    if period == "season":
        return data["season"]
    if period not in {"week", "month"}:
        raise commands.BadArgument("Choose week, month, or season.")
    start = today - timedelta(days=today.weekday()) if period == "week" else today.replace(day=1)
    totals = {}
    for day, users in data["days"].items():
        if start.isoformat() <= day <= today.isoformat():
            for uid, amount in users.items():
                totals[uid] = totals.get(uid, 0) + amount
    return totals


def message_allowed(cache, guild_id, uid, text, settings, now, *, remember=True):
    if settings["min_words"] and len(re.findall(r"\w+", text)) < settings["min_words"]:
        return False
    seconds = settings["repeat_seconds"]
    if not seconds:
        return True
    normalized = " ".join(re.findall(r"\w+", text.casefold()))
    digest = hashlib.sha256(normalized.encode()).digest()
    key = (guild_id, uid)
    history = [(ts, value) for ts, value in cache.get(key, []) if now - ts < seconds]
    accepted = all(value != digest for _, value in history)
    if not remember:
        return accepted
    cache.pop(key, None)
    if accepted:
        history.append((now, digest))
    cache[key] = history[-10:]
    while len(cache) > 50000:
        cache.popitem(last=False)
    return accepted


def forget_user(data, uid):
    for bucket in data["days"].values():
        bucket.pop(uid, None)
    data["season"].pop(uid, None)
    for archive in data["archives"]:
        archive["xp"].pop(uid, None)


def user_periods(data, uid):
    return {
        "days": {day: bucket[uid] for day, bucket in data["days"].items() if uid in bucket},
        "season": data["season"].get(uid, 0),
        "archives": [
            {"name": archive["name"], "ended": archive["ended"], "xp": archive["xp"][uid]}
            for archive in data["archives"]
            if uid in archive["xp"]
        ],
    }
