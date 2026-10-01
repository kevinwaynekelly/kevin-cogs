"""Respect Red's per-guild cog disable setting for event handlers."""

from functools import wraps

import discord


def guild_enabled(listener):
    @wraps(listener)
    async def wrapped(self, *events, **kwargs):
        guild = None
        for event in events:
            candidate = event if isinstance(event, discord.Guild) else getattr(event, "guild", None)
            guild_id = getattr(candidate, "id", None) or getattr(event, "guild_id", None)
            if guild_id:
                guild = self.bot.get_guild(guild_id)
                if guild is not None:
                    break
        if guild is None or await self.bot.cog_disabled_in_guild(self, guild):
            return
        return await listener(self, *events, **kwargs)

    return wrapped
