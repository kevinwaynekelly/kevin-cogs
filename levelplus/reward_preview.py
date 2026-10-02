"""Read-only progression scenarios using the same role plan as reconciliation."""

import asyncio
import math
from copy import deepcopy

from redbot.core import commands

from .features import safe_role


def reward_changes(member, level, rewards, extra=()):
    eligible, managed, blocked = [], [], []
    for key, threshold in rewards["roles"].items():
        role = member.guild.get_role(int(key))
        if role is None:
            blocked.append(int(key))
            continue
        try:
            safe_role(member.guild, role)
        except commands.BadArgument:
            blocked.append(role.id)
            continue
        managed.append(role)
        if level >= threshold:
            eligible.append((threshold, role))
    desired = (
        [role for _, role in eligible]
        if rewards["stack"]
        else [max(eligible, key=lambda item: (item[0], item[1].id))[1]]
        if eligible
        else []
    )
    desired.extend(extra)
    add = list(dict.fromkeys(role for role in desired if role not in member.roles))
    remove = [role for role in managed if role not in desired and role in member.roles]
    return add, remove, blocked


def preview_policy(
    settings,
    *,
    curve=None,
    multiplier=None,
    base=None,
    increment=None,
    max_level=None,
    role=None,
    threshold=None,
    stack=None,
):
    proposed = deepcopy(settings)
    if curve is not None:
        if curve.lower() not in {"linear", "exponential", "constant"}:
            raise commands.BadArgument("Choose linear, exponential or constant.")
        proposed["curve"] = curve.lower()
    for key, value, minimum, maximum in (
        ("multiplier", multiplier, 0.1, 10),
        ("base", base, 0, 1000000000),
        ("inc", increment, 0, 1000000000),
    ):
        if value is not None:
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise commands.BadArgument(f"Choose a finite {key} from {minimum} to {maximum}.")
            (proposed if key == "multiplier" else proposed["linear"])[key] = value
    if max_level is not None:
        if not 0 <= max_level <= 100000:
            raise commands.BadArgument("Choose maximum level 0 to 100000; zero is unlimited.")
        proposed["max_level"] = max_level
    if (role is None) != (threshold is None):
        raise commands.BadArgument("Supply both the candidate reward role and threshold.")
    if role is not None:
        if not 0 <= threshold <= 100000:
            raise commands.BadArgument(
                "Choose threshold 1 to 100000, or zero to remove its definition."
            )
        if threshold:
            safe_role(role.guild, role)
            if (
                str(role.id) not in proposed["rewards"]["roles"]
                and len(proposed["rewards"]["roles"]) >= 100
            ):
                raise commands.BadArgument("You can configure up to 100 reward roles.")
            proposed["rewards"]["roles"][str(role.id)] = threshold
        else:
            proposed["rewards"]["roles"].pop(str(role.id), None)
    if stack is not None:
        proposed["rewards"]["stack"] = stack
    return proposed


async def build_preview(cog, guild, current, proposed, member=None):
    members = [member] if member else [item for item in guild.members if not item.bot]
    if len(members) > 10000:
        raise commands.BadArgument("Preview one member in servers with over 10000 cached humans.")
    xp = await cog.config.guild(guild).xp()
    report = {
        "reviewed": len(members),
        "changed": 0,
        "adds": 0,
        "removes": 0,
        "rows": [],
        "blocked": set(),
    }
    for index, target in enumerate(members):
        amount = int(xp.get(str(target.id), 0))
        old, new = cog._level(amount, current), cog._level(amount, proposed)
        extra = await cog._custom_reward_roles(target, proposed)
        add, remove, blocked = reward_changes(target, new, proposed["rewards"], extra)
        report["blocked"].update(blocked)
        report["changed"] += old != new
        report["adds"] += len(add)
        report["removes"] += len(remove)
        if (old != new or add or remove or member) and len(report["rows"]) < 100:
            report["rows"].append(
                {
                    "member": target.id,
                    "old": old,
                    "new": new,
                    "add": [r.id for r in add],
                    "remove": [r.id for r in remove],
                }
            )
        if index % 50 == 0:
            await asyncio.sleep(0)
    return report
