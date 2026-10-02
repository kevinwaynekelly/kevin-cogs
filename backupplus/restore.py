"""Reviewable, non-deleting restore plans and sequential Discord mutations."""

import asyncio
from dataclasses import dataclass, field

import discord

from .constants import API_TIMEOUT, MAX_CHANNELS, MAX_ROLES
from .snapshot import ROLE_FIELDS, emoji_record


@dataclass
class RestorePlan:
    snapshot: dict
    live: dict
    mappings: dict
    actions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    blockers: list = field(default_factory=list)

    def report(self):
        return {
            "actions": self.actions,
            "warnings": self.warnings,
            "blockers": self.blockers,
            "role_order": "Restore the relative order of editable backed-up roles within their current slots below the bot.",
            "scope": "No objects are deleted and no member roles or message histories are restored.",
        }


def target_id(mappings, kind, source):
    return mappings.get(kind, {}).get(source, source)


def permissions_for(base, row, bot_id, role_ids, guild_id):
    result = discord.Permissions(base)
    if result.administrator:
        return discord.Permissions.all()
    rows = row["overwrites"]
    everyone = next(
        (item for item in rows if item["kind"] == "role" and item["id"] == str(guild_id)), None
    )
    if everyone:
        result.handle_overwrite(everyone["allow"], everyone["deny"])
    allow = deny = 0
    for item in rows:
        if item["kind"] == "role" and item["id"] in role_ids and item["id"] != str(guild_id):
            allow |= item["allow"]
            deny |= item["deny"]
    result.handle_overwrite(allow, deny)
    member = next(
        (item for item in rows if item["kind"] == "member" and item["id"] == str(bot_id)), None
    )
    if member:
        result.handle_overwrite(member["allow"], member["deny"])
    return result


def mapped_channel(row, mappings):
    value = dict(row)
    value["category"] = (
        target_id(mappings, "channels", row["category"]) if row["category"] else None
    )
    value["overwrites"] = sorted(
        [
            {
                **item,
                "id": target_id(mappings, "roles", item["id"])
                if item["kind"] == "role"
                else item["id"],
            }
            for item in row["overwrites"]
        ],
        key=lambda item: (item["kind"], int(item["id"])),
    )
    if "available_tags" in row:
        value["available_tags"] = [
            {key: part for key, part in tag.items() if key != "id"} for tag in row["available_tags"]
        ]
    return value


def build_plan(guild, snapshot, live, mappings, pending=None):
    plan = RestorePlan(snapshot, live, mappings)
    plan.warnings.extend(snapshot["warnings"])
    current_roles = {row["id"]: row for row in live["roles"]}
    current_channels = {row["id"]: row for row in live["channels"]}
    saved_roles = {row["id"]: row for row in snapshot["roles"]}
    bot_roles = {str(role.id) for role in guild.me.roles} | {str(guild.id)}
    top = max((current_roles[r]["position"], -int(r)) for r in bot_roles if r in current_roles)
    base = 0
    for identifier in bot_roles:
        base |= current_roles.get(identifier, {}).get("permissions", 0)
    bot_permissions = discord.Permissions(base)
    if not bot_permissions.manage_roles or not bot_permissions.manage_channels:
        plan.blockers.append("The bot needs Manage Roles and Manage Channels.")
    final_roles = dict(current_roles)
    editable = []
    new_roles = 0
    for row in snapshot["roles"]:
        identifier = target_id(mappings, "roles", row["id"])
        current = current_roles.get(identifier)
        if current and current["managed"] != row["managed"]:
            plan.blockers.append(f"Role {row['id']} has incompatible managed status.")
            continue
        protected = row["managed"] or (
            current
            and identifier != str(guild.id)
            and (current["position"], -int(identifier)) >= top
        )
        if protected:
            if current is None or any(current[key] != row[key] for key in ROLE_FIELDS):
                plan.warnings.append(
                    f"Protected role {row['name']} ({row['id']}) is left unchanged."
                )
            continue
        changes = {
            key: {"before": current[key] if current else None, "after": row[key]}
            for key in sorted(ROLE_FIELDS)
            if current is None or current[key] != row[key]
        }
        if identifier != str(guild.id):
            editable.append(row["id"])
        if changes:
            if not bot_permissions.administrator and row["permissions"] & ~base:
                plan.blockers.append(
                    f"Role {row['name']} requests permissions the bot cannot grant."
                )
            if current is None:
                new_roles += 1
            plan.actions.append(
                {
                    "kind": "role",
                    "action": "edit" if current else "create",
                    "id": row["id"],
                    "target": identifier if current else None,
                    "name": row["name"],
                    "changes": changes,
                }
            )
            final_roles[identifier] = row
    if len(current_roles) + new_roles > MAX_ROLES:
        plan.blockers.append("There are not enough free role slots.")
    desired_order = sorted(
        editable, key=lambda identifier: (saved_roles[identifier]["position"], int(identifier))
    )
    existing_order = sorted(
        [
            identifier
            for identifier in editable
            if target_id(mappings, "roles", identifier) in current_roles
        ],
        key=lambda identifier: (
            current_roles[target_id(mappings, "roles", identifier)]["position"],
            int(target_id(mappings, "roles", identifier)),
        ),
    )
    if new_roles or existing_order != desired_order:
        plan.actions.append(
            {
                "kind": "order",
                "action": "edit",
                "id": "roles",
                "target": None,
                "name": "Role order below the bot",
                "changes": {"order": {"before": existing_order, "after": desired_order}},
            }
        )
    final_base = 0
    for identifier in bot_roles:
        final_base |= final_roles.get(identifier, {}).get("permissions", 0)
    final_permissions = discord.Permissions(final_base)
    if not final_permissions.administrator and not (
        final_permissions.manage_roles and final_permissions.manage_channels
    ):
        plan.blockers.append("Restoring role permissions would remove the bot's management access.")
    new_channels = 0
    changed_categories = {}
    saved_targets = {target_id(mappings, "channels", row["id"]) for row in snapshot["channels"]}
    categories = sorted(
        snapshot["channels"],
        key=lambda row: (row["kind"] != "category", row["position"], int(row["id"])),
    )
    for row in categories:
        identifier = target_id(mappings, "channels", row["id"])
        current = current_channels.get(identifier)
        if (
            current
            and current["kind"] != row["kind"]
            and {current["kind"], row["kind"]} != {"text", "news"}
        ):
            plan.blockers.append(
                f"Channel {row['id']} has a different type. Choose a correct mapping."
            )
            continue
        wanted = mapped_channel(row, mappings)
        actual = mapped_channel(current, {}) if current else {}
        changes = {
            key: {"before": actual.get(key), "after": value}
            for key, value in wanted.items()
            if key != "id" and (not current or actual.get(key) != value)
        }
        # Discord propagates category overwrites to synced children. Include an
        # explicit reapplication when a backed-up child must keep its old values,
        # and refuse to silently alter a channel outside this snapshot.
        if current and current["category"] in changed_categories:
            parent = current_channels[current["category"]]
            if current["overwrites"] == parent["overwrites"]:
                changes["overwrites"] = {
                    "before": actual["overwrites"],
                    "after": wanted["overwrites"],
                }
        if row["kind"] == "category" and current and "overwrites" in changes:
            changed_categories[identifier] = changes["overwrites"]
            for child in current_channels.values():
                if (
                    child["category"] == identifier
                    and child["overwrites"] == current["overwrites"]
                    and child["id"] not in saved_targets
                ):
                    plan.blockers.append(
                        f"Category {row['name']} would change synced channel {child['name']} outside this snapshot. Preserve that channel before restoring."
                    )
        if not changes:
            continue
        if current is None:
            new_channels += 1
        if row["kind"] in {"news", "forum", "media"} and "COMMUNITY" not in guild.features:
            plan.blockers.append(f"Channel {row['name']} needs Discord Community enabled.")
        if row.get("bitrate", 0) > guild.bitrate_limit:
            plan.blockers.append(
                f"Channel {row['name']} exceeds this server's current bitrate limit."
            )
        for item in row["overwrites"]:
            role = saved_roles.get(item["id"]) if item["kind"] == "role" else None
            if (
                role
                and role["managed"]
                and target_id(mappings, "roles", role["id"]) not in current_roles
            ):
                plan.blockers.append(
                    f"Channel {row['name']} references a missing managed role; its overwrites cannot be preserved."
                )
            if not bot_permissions.administrator and (item["allow"] | item["deny"]) & ~base:
                plan.blockers.append(
                    f"Channel {row['name']} uses overwrite permissions the bot cannot apply."
                )
        effective = permissions_for(final_base, wanted, guild.me.id, bot_roles, guild.id)
        if not effective.administrator and not (
            effective.view_channel and effective.manage_channels and effective.manage_roles
        ):
            plan.blockers.append(
                f"Channel {row['name']} would remove the bot's channel management access."
            )
        if current:
            effective = permissions_for(base, current, guild.me.id, bot_roles, guild.id)
            if not effective.administrator and not (
                effective.view_channel and effective.manage_channels and effective.manage_roles
            ):
                plan.blockers.append(f"The bot cannot manage existing channel {row['name']}.")
        plan.actions.append(
            {
                "kind": "channel",
                "action": "edit" if current else "create",
                "id": row["id"],
                "target": identifier if current else None,
                "name": row["name"],
                "changes": changes,
            }
        )
    if len(current_channels) + len(live["warnings"]) + new_channels > MAX_CHANNELS:
        plan.blockers.append("There are not enough free channel slots.")
    for key in pending or {}:
        plan.blockers.append(
            f"A previous create request for {key} has an uncertain result. Bind its existing Discord object before retrying."
        )
    plan.blockers = list(dict.fromkeys(plan.blockers))
    return plan


def partial_emoji(row):
    return (
        discord.PartialEmoji(
            name=row["name"], animated=row["animated"], id=int(row["id"]) if row["id"] else None
        )
        if row
        else None
    )


def role_kwargs(row):
    values = {key: row[key] for key in ROLE_FIELDS}
    values["permissions"] = discord.Permissions(row["permissions"])
    for key in ("colour", "secondary_colour", "tertiary_colour"):
        values[key] = discord.Colour(row[key]) if row[key] is not None else None
    return values


def channel_kwargs(row, mappings, roles, channels, *, current=None):
    values = {
        key: value
        for key, value in row.items()
        if key not in {"id", "kind", "overwrites", "available_tags"}
    }
    if row["kind"] == "category":
        values.pop("category")
    else:
        values["category"] = (
            channels.get(target_id(mappings, "channels", row["category"]))
            if row["category"]
            else None
        )
        if row["category"] and values["category"] is None:
            raise ValueError("A restored category is missing")
    overwrites = {}
    for item in row["overwrites"]:
        if item["kind"] == "role":
            target = roles.get(target_id(mappings, "roles", item["id"]))
            if target is None:
                raise ValueError("A restored overwrite role is missing")
        else:
            # Type-aware public Objects preserve uncached member overwrites exactly.
            target = discord.Object(id=int(item["id"]), type=discord.User)
        overwrites[target] = discord.PermissionOverwrite.from_pair(
            discord.Permissions(item["allow"]), discord.Permissions(item["deny"])
        )
    values["overwrites"] = overwrites
    if "video_quality_mode" in values:
        values["video_quality_mode"] = discord.VideoQualityMode(values["video_quality_mode"])
    if row["kind"] in {"forum", "media"}:
        values["default_reaction_emoji"] = partial_emoji(row["default_reaction_emoji"])
        values["default_sort_order"] = (
            discord.ForumOrderType(row["default_sort_order"])
            if row["default_sort_order"] is not None
            else None
        )
        values["default_layout"] = discord.ForumLayoutType(row["default_layout"])
        existing = list(current.available_tags) if current else []
        tags = []
        for item in row["available_tags"]:
            tag = discord.ForumTag(
                name=item["name"], moderated=item["moderated"], emoji=partial_emoji(item["emoji"])
            )
            previous = next((old for old in existing if str(old.id) == item["id"]), None)
            if previous is None:
                previous = next(
                    (
                        old
                        for old in existing
                        if old.name == item["name"]
                        and old.moderated == item["moderated"]
                        and emoji_record(old.emoji) == item["emoji"]
                    ),
                    None,
                )
            if previous is not None:
                tag.id = previous.id
                existing.remove(previous)
            tags.append(tag)
        values["available_tags"] = tags
    return values


async def execute(guild, plan, roles, channels, before, remember, progress, *, reason):
    """Stop on the first failure; persist every created ID before subsequent work."""
    mappings = plan.mappings
    saved_roles = {row["id"]: row for row in plan.snapshot["roles"]}
    saved_channels = {row["id"]: row for row in plan.snapshot["channels"]}

    async def create(kind, source, call):
        await remember(kind, source, None, uncertain=True)
        try:
            return await asyncio.wait_for(call(), API_TIMEOUT)
        except discord.HTTPException as error:
            if 400 <= error.status < 500:
                await remember(kind, source, None, clear=True)
            raise
        except (TypeError, ValueError):
            await remember(kind, source, None, clear=True)
            raise

    for action in plan.actions:
        await before()
        kind, source = action["kind"], action["id"]
        if kind == "order":
            # New roles shift the bot's position. Fetch the current slots and reorder
            # only the backed-up editable roles inside those slots.
            latest = await asyncio.wait_for(guild.fetch_roles(), API_TIMEOUT)
            roles.update({str(role.id): role for role in latest})
            targets = [
                roles[target_id(mappings, "roles", identifier)]
                for identifier in action["changes"]["order"]["after"]
            ]
            slots = sorted(role.position for role in targets)
            await asyncio.wait_for(
                guild.edit_role_positions(positions=dict(zip(targets, slots)), reason=reason),
                API_TIMEOUT,
            )
        elif kind == "role":
            row = saved_roles[source]
            values = role_kwargs(row)
            if action["action"] != "create":
                values = {key: value for key, value in values.items() if key in action["changes"]}
            # Discord.py 2.7 combines colour aliases with `or`. Supply both None
            # aliases when clearing a secondary/tertiary colour.
            for field in ("secondary", "tertiary"):
                if field + "_colour" in values and values[field + "_colour"] is None:
                    values[field + "_color"] = None
            if action["action"] == "create":
                result = await create(
                    kind, source, lambda: guild.create_role(**values, reason=reason)
                )
                await remember(kind, source, str(result.id))
                roles[str(result.id)] = result
            else:
                identifier = target_id(mappings, "roles", source)
                result = await asyncio.wait_for(
                    roles[identifier].edit(**values, reason=reason), API_TIMEOUT
                )
                if result is not None:
                    roles[identifier] = result
        else:
            row = saved_channels[source]
            identifier = target_id(mappings, "channels", source)
            current = channels.get(identifier)
            values = channel_kwargs(row, mappings, roles, channels, current=current)
            if action["action"] == "create":
                methods = {
                    "category": guild.create_category,
                    "text": guild.create_text_channel,
                    "news": guild.create_text_channel,
                    "voice": guild.create_voice_channel,
                    "stage": guild.create_stage_channel,
                    "forum": guild.create_forum,
                    "media": guild.create_forum,
                }
                if row["kind"] in {"text", "news"}:
                    values["news"] = row["kind"] == "news"
                require_tag = values.pop("require_tag", None)
                if row["kind"] in {"forum", "media"}:
                    values["media"] = row["kind"] == "media"
                result = await create(
                    kind, source, lambda: methods[row["kind"]](**values, reason=reason)
                )
                await remember(kind, source, str(result.id))
                channels[str(result.id)] = result
                if require_tag:
                    await before()
                    result = await asyncio.wait_for(
                        result.edit(require_tag=True, reason=reason), API_TIMEOUT
                    )
                    if result is not None:
                        channels[str(result.id)] = result
            else:
                selected = {key: value for key, value in values.items() if key in action["changes"]}
                if "kind" in action["changes"]:
                    selected["type"] = (
                        discord.ChannelType.news
                        if row["kind"] == "news"
                        else discord.ChannelType.text
                    )
                result = await asyncio.wait_for(
                    current.edit(**selected, reason=reason), API_TIMEOUT
                )
                if result is not None:
                    channels[identifier] = result
        await progress(action)
