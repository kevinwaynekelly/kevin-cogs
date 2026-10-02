"""Discover accessible channels/threads and stream official Discord history APIs."""

from __future__ import annotations

import asyncio

import discord

from .constants import MAX_CHANNELS
from .transcript import ExportLimit, message_record, stamp

HISTORY_TYPES = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread)
PARENT_TYPES = (discord.TextChannel, discord.ForumChannel)


def channel_row(channel):
    return {
        "id": str(channel.id),
        "name": channel.name,
        "parent_id": str(channel.parent_id) if isinstance(channel, discord.Thread) else None,
        "type": str(channel.type),
        "status": "pending",
        "messages": 0,
        "first": None,
        "last": None,
    }


async def accessible(channel, member, bot_member):
    """Thread.permissions_for inherits parent flags but does not check membership."""
    if member is None or bot_member is None:
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
            if not any(item.id == person.id for item in channel.members):
                try:
                    await asyncio.wait_for(channel.fetch_member(person.id), 20)
                except (discord.HTTPException, asyncio.TimeoutError):
                    return False
    return True


async def discover(job, guild, member, *, scope=None, threads=True):
    """Yield parents first, then active and archived threads, once each."""
    seen = set()
    parents = [scope] if scope and not isinstance(scope, discord.Thread) else list(guild.channels)

    def candidate(channel):
        if channel.id in seen:
            return False
        if len(seen) >= MAX_CHANNELS:
            raise ExportLimit(
                "The export reached its 10,000 channel/thread limit. Export a narrower scope."
            )
        seen.add(channel.id)
        return True

    if isinstance(scope, discord.Thread):
        yield scope
        return
    for parent in parents:
        if isinstance(parent, HISTORY_TYPES) and candidate(parent):
            yield parent
    if not threads:
        return
    try:
        active = await asyncio.wait_for(guild.active_threads(), 60)
    except (discord.HTTPException, asyncio.TimeoutError) as error:
        job.warn(
            f"Active thread discovery failed ({type(error).__name__}); cached threads were used."
        )
        active = guild.threads
    parent_ids = {item.id for item in parents if isinstance(item, PARENT_TYPES)}
    for thread in active:
        if thread.parent_id in parent_ids and candidate(thread):
            yield thread
    for parent in parents:
        if not isinstance(parent, PARENT_TYPES):
            continue
        if not await accessible(parent, member, guild.me):
            job.warn(
                f"Archived thread discovery skipped for parent {parent.id}: missing history access."
            )
            continue
        variants = [{}]
        if isinstance(parent, discord.TextChannel):
            if not parent.permissions_for(guild.me).manage_threads:
                job.warn(
                    f"Private archived thread discovery for parent {parent.id} is limited to threads the bot joined."
                )
            variants.append(
                {"private": True, "joined": not parent.permissions_for(guild.me).manage_threads}
            )
        for options in variants:
            try:
                iterator = parent.archived_threads(limit=None, **options).__aiter__()
                while True:
                    try:
                        thread = await asyncio.wait_for(anext(iterator), 60)
                    except StopAsyncIteration:
                        break
                    if candidate(thread):
                        yield thread
            except (discord.HTTPException, asyncio.TimeoutError) as error:
                job.warn(
                    f"Archived thread discovery failed for parent {parent.id} ({type(error).__name__})."
                )


async def scan_channel(job, channel, guild, member):
    row = channel_row(channel)
    job.channels.append(row)
    if not await accessible(channel, member, guild.me):
        row["status"] = "skipped: missing history access or private-thread membership"
        job.complete = False
        return
    job.current = channel.id
    after = discord.Object(discord.utils.time_snowflake(job.after) - 1) if job.after else None
    before = discord.Object(discord.utils.time_snowflake(job.before))
    iterator = channel.history(
        limit=None, oldest_first=True, after=after, before=before
    ).__aiter__()
    try:
        while True:
            try:
                message = await asyncio.wait_for(anext(iterator), 60)
            except StopAsyncIteration:
                break
            if job.messages % 100 == 0:
                # Recheck live visibility, membership and Red's disabled state during long scans.
                await job.check_access()
                if not await accessible(channel, guild.get_member(member.id), guild.me):
                    raise PermissionError
                await asyncio.sleep(0)
            if not job.include_bots and (message.author.bot or message.webhook_id):
                continue
            job.writer.add(message_record(message), row)
            row["messages"] += 1
            job.messages += 1
            row["first"] = row["first"] or stamp(message.created_at)
            row["last"] = stamp(message.created_at)
        row["status"] = "complete"
    except ExportLimit:
        row["status"] = "partial: output limit reached"
        raise
    except (discord.HTTPException, asyncio.TimeoutError, PermissionError) as error:
        row["status"] = f"partial: {type(error).__name__}"
        job.warn(f"History stopped in channel {channel.id} ({type(error).__name__}).")
    finally:
        job.current = None
