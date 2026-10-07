"""Sync enabled application commands after updates without redundant uploads."""

import asyncio
import hashlib
import json
import time

from redbot.core import commands

from .command_support import check_command
from .management import native_command


async def schema_hash(tree):
    payloads = []
    for command in tree.get_commands():
        payloads.append(
            await command.get_translated_payload(tree, tree.translator)
            if tree.translator
            else command.to_dict(tree)
        )
    payloads.sort(key=lambda item: (item.get("type", 1), item["name"]))
    return hashlib.sha256(json.dumps(payloads, sort_keys=True).encode()).hexdigest()


async def sync_enabled(bot, config, ctx):
    lock = bot.__dict__.setdefault("_kevin_downloader_sync_lock", asyncio.Lock())
    async with lock:
        source = native_command(bot, "slash sync", {"Core"})
        await check_command(ctx, source)
        if not await bot.is_owner(ctx.author) or not await bot.can_run(ctx, call_once=True):
            raise commands.CheckFailure("Slash sync requires the current bot owner and checks.")
        await bot.tree.red_check_enabled()
        digest = await schema_hash(bot.tree)
        policy = await config.slash_sync()
        if policy["fingerprint"] == digest:
            return "Slash commands are already synced; no definitions changed."
        # Red's manual command rejects automated assume_yes contexts to avoid
        # repeated uploads. Use the tree API with explicit permission checks,
        # schema deduplication and a persisted one-minute rate budget instead.
        delay = max(
            0,
            min(60, 60 - (time.time() - policy["last_attempt"])),
            source.get_cooldown_retry_after(ctx),
        )
        if delay:
            await asyncio.sleep(delay)
            await check_command(ctx, source)
            if not await bot.is_owner(ctx.author) or not await bot.can_run(ctx, call_once=True):
                raise commands.CheckFailure("The bot owner's slash sync access changed.")
            await bot.tree.red_check_enabled()
            digest = await schema_hash(bot.tree)
        async with config.slash_sync() as policy:
            policy["last_attempt"] = time.time()
        synced = await bot.tree.sync()
        async with config.slash_sync() as policy:
            policy["fingerprint"] = digest
            policy["last_success"] = time.time()
        return f"Synced {len(synced)} enabled application commands."
