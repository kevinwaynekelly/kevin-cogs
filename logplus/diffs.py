"""Readable permission changes with inherited overwrite states."""

import discord


def permission_label(key):
    return {
        "read_messages": "view channel",
        "external_emojis": "use external emojis",
        "external_stickers": "use external stickers",
    }.get(key, key.replace("_", " "))


def permission_changes(before, after):
    old, new = dict(before), dict(after)
    added = [permission_label(key) for key, value in new.items() if value and not old[key]]
    removed = [permission_label(key) for key, value in old.items() if value and not new[key]]
    result = []
    if added:
        result.append("**Allowed** · " + ", ".join(added))
    if removed:
        result.append("**Removed** · " + ", ".join(removed))
    return result


def attribute_changes(before, after, keys):
    return [
        f"**{key.replace('_', ' ').capitalize()}** · {getattr(before, key)} → {getattr(after, key)}"
        for key in keys
        if getattr(before, key, None) != getattr(after, key, None)
    ]


def overwrite_changes(before, after):
    def mapping(channel):
        values = getattr(channel, "overwrites", {})
        return (
            {target.id: (target, overwrite) for target, overwrite in values.items()}
            if isinstance(values, dict)
            else {}
        )

    old, new = mapping(before), mapping(after)
    states = {True: "Allow", False: "Deny", None: "Inherit"}
    result = []
    for uid in sorted(old.keys() | new.keys()):
        target = (new.get(uid) or old[uid])[0]
        old_values = dict(old[uid][1]) if uid in old else dict(discord.PermissionOverwrite())
        new_values = dict(new[uid][1]) if uid in new else dict(discord.PermissionOverwrite())
        changes = [
            f"{permission_label(key)} · {states[old_values[key]]} → {states[value]}"
            for key, value in new_values.items()
            if old_values[key] != value
        ]
        if changes:
            result.append((f"Overwrite · {target} ({uid})", "\n".join(changes)))
    return result
