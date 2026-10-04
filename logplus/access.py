"""Current source visibility for retained records, including archived private threads."""

import asyncio

import discord


async def visible_source(ctx, source):
    """Never replace actual channel visibility with Red's administrator exemption."""
    guild = ctx.guild
    member = guild.get_member(ctx.author.id)
    bot_member = guild.me
    if member is None or bot_member is None:
        return False
    # Explicit None/zero source means a server event, not a missing channel.
    if source in (None, 0):
        return True
    if not isinstance(source, int) or isinstance(source, bool) or source < 1:
        return False
    channel = guild.get_channel_or_thread(source)
    if channel is None:
        try:
            channel = await asyncio.wait_for(guild.fetch_channel(source), 10)
        except (discord.HTTPException, discord.ClientException, asyncio.TimeoutError):
            return False
    if channel is None or channel.guild.id != guild.id:
        return False
    for person in (member, bot_member):
        try:
            permissions = channel.permissions_for(person)
        except discord.ClientException:
            return False
        if not permissions.view_channel or not permissions.read_message_history:
            return False
        if (
            isinstance(channel, discord.Thread)
            and channel.is_private()
            and not permissions.manage_threads
        ):
            # Fetch membership rather than trusting possibly stale thread-member cache.
            try:
                await asyncio.wait_for(channel.fetch_member(person.id), 10)
            except (discord.HTTPException, discord.ClientException, asyncio.TimeoutError):
                return False
    return True


async def visible_records(ctx, records, *, missing_source=False):
    """Cache each source decision only for this request, before limits/counts apply."""
    allowed = {}
    result = []
    for record in records:
        if "source" not in record:
            if missing_source:
                continue
            source = None
        else:
            source = record["source"]
        # Unknown incident provenance uses an explicit negative marker.
        if not isinstance(source, (int, type(None))) or isinstance(source, bool):
            continue
        if source not in allowed:
            allowed[source] = await visible_source(ctx, source)
        if allowed[source]:
            result.append(record)
    return result
