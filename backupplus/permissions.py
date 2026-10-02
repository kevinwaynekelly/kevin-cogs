"""Preserve unknown permission bits through Discord.py's public mutation APIs."""

import discord


class RawPermissionOverwrite(discord.PermissionOverwrite):
    __slots__ = ("_unknown_allow", "_unknown_deny")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._unknown_allow = 0
        self._unknown_deny = 0

    @classmethod
    def from_pair(cls, allow, deny):
        overwrite = super().from_pair(allow, deny)
        known = discord.Permissions.all().value
        overwrite._unknown_allow = allow.value & ~known
        overwrite._unknown_deny = deny.value & ~known
        return overwrite

    def pair(self):
        allow, deny = super().pair()
        allow.value |= self._unknown_allow
        deny.value |= self._unknown_deny
        return allow, deny
