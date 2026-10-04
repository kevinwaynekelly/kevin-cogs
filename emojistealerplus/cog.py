"""Automatically copy external custom emoji without modifying messages."""

import asyncio
import hashlib
import logging
import re
from collections import Counter, defaultdict
from types import SimpleNamespace
from typing import Optional

import aiohttp
import discord
from redbot.core import Config, commands

from .command_support import finish_configuration_audit, prepare_hybrid
from .constants import DEFAULTS_GUILD, MAX_COPIES, MAX_IMAGE
from .presentation import Presentation
from .queue import MAX_GUILD_PENDING, MAX_PENDING, CaptureQueue

log = logging.getLogger(__name__)
EMOJI_RX = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:[0-9]{15,22}>")


def emoji_name(name, identifier, existing):
    base = re.sub(r"[^A-Za-z0-9_]", "_", name or "emoji")[:32]
    if len(base) < 2:
        base = "emoji_" + base
    if base not in existing:
        return base
    suffix = "_" + str(identifier)[-8:]
    candidate = base[: 32 - len(suffix)] + suffix
    number = 1
    while candidate in existing:
        suffix = f"_{identifier}_{number}"
        candidate = base[: max(2, 32 - len(suffix))] + suffix
        number += 1
    return candidate[:32]


class EmojiStealerPlus(commands.Cog):
    """Yoink external custom emojis into the server where they are used."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=702035010, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)
        self._presentation = Presentation("EmojiStealerPlus", "emoji")
        self._queue = CaptureQueue(maxsize=MAX_PENDING)
        self._pending = set()
        self._guild_pending = Counter()
        self._locks = defaultdict(asyncio.Lock)
        self._worker = None
        self._session = None
        self._closing = False

    async def cog_load(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def cog_unload(self):
        self._closing = True
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if self._session:
            await self._session.close()
        self._pending.clear()
        self._guild_pending.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._locks.clear()

    async def cog_before_invoke(self, ctx):
        await prepare_hybrid(ctx)

    async def cog_after_invoke(self, ctx):
        finish_configuration_audit(ctx)

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    async def _enabled(self, guild):
        return bool(
            guild
            and not self._closing
            and not await self.bot.cog_disabled_in_guild(self, guild)
            and (await self.config.guild(guild).capture())["enabled"]
        )

    async def _automatic_allowed(self, guild, channel_id=None, *, reaction=False):
        if not await self._enabled(guild):
            return False
        policy = await self.config.guild(guild).capture()
        channel = guild.get_channel_or_thread(channel_id) if channel_id else None
        return not (reaction and not policy["reactions"]) and (
            not policy["channel"]
            or channel is not None
            and policy["channel"] in {channel.id, getattr(channel, "parent_id", None)}
        )

    async def _queue_emoji(self, guild, channel, emoji, *, reaction=False):
        if not emoji.id or not await self._enabled(guild):
            return
        settings = await self.config.guild(guild).capture()
        if settings["channel"] and settings["channel"] not in {
            channel.id,
            getattr(channel, "parent_id", None),
        }:
            return
        if any(item.id == emoji.id for item in guild.emojis):
            return
        copies = await self.config.guild(guild).copied()
        known = copies.get(str(emoji.id))
        if known and any(item.id == known["emoji"] for item in guild.emojis):
            return
        token = (guild.id, emoji.id)
        if (
            self._closing
            or token in self._pending
            or self._queue.full()
            or self._guild_pending[guild.id] >= MAX_GUILD_PENDING
        ):
            return
        self._pending.add(token)
        self._guild_pending[guild.id] += 1
        self._queue.put_nowait((guild.id, channel.id, emoji, reaction))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="EmojiStealerPlusCapture")

    async def _run(self):
        while not self._closing:
            guild_id, channel_id, emoji, reaction = await self._queue.get()
            try:
                guild = self.bot.get_guild(guild_id)
                channel = guild.get_channel_or_thread(channel_id) if guild else None
                if channel and await self._enabled(guild):
                    result, created = await self._copy(
                        guild, emoji, automatic=True, channel_id=channel_id, reaction=reaction
                    )
                    if created and (await self.config.guild(guild).capture())["notify"]:
                        ctx = SimpleNamespace(guild=guild, channel=channel, send=channel.send)
                        await self._reply(
                            ctx,
                            f"Yoinked {result} into this server!",
                            title="Yoinked",
                            tone="success",
                        )
            except (
                commands.CommandError,
                discord.HTTPException,
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as error:
                if guild:
                    await self.config.guild(guild).last_error.set(type(error).__name__)
                log.warning("Emoji capture failed in guild %s (%s)", guild_id, type(error).__name__)
            except Exception as error:
                log.warning(
                    "Emoji capture interrupted in guild %s (%s)", guild_id, type(error).__name__
                )
            finally:
                self._pending.discard((guild_id, emoji.id))
                self._guild_pending[guild_id] -= 1
                if not self._guild_pending[guild_id]:
                    del self._guild_pending[guild_id]
                self._queue.task_done()
            await asyncio.sleep(2)

    async def _download(self, emoji):
        if self._session is None:
            raise commands.CommandError("Emoji capture is not loaded. Reload emojistealerplus.")
        extension = "gif" if emoji.animated else "png"
        url = f"https://cdn.discordapp.com/emojis/{emoji.id}.{extension}?size=128&quality=lossless"
        async with self._session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise commands.BadArgument("Discord could not provide this emoji image.")
            data = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                data.extend(chunk)
                if len(data) > MAX_IMAGE:
                    raise commands.BadArgument("This emoji image exceeds Discord's 256 KiB limit.")
        image = bytes(data)
        if not image.startswith((b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"\xff\xd8\xff")):
            raise commands.BadArgument("Discord returned an unsupported emoji image.")
        if emoji.animated and not image.startswith((b"GIF87a", b"GIF89a")):
            raise commands.BadArgument("The animated emoji could not be preserved.")
        return image

    async def _copy(self, guild, emoji, *, automatic=False, channel_id=None, reaction=False):
        if not emoji.id or not 0 < emoji.id < 2**64:
            raise commands.BadArgument(
                "Choose a custom Discord emoji, such as <:name:123456789012345678>."
            )
        async with self._locks[guild.id]:
            if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
                raise commands.CheckFailure("Emoji capture is unavailable in this server.")
            if automatic and not await self._automatic_allowed(
                guild, channel_id, reaction=reaction
            ):
                raise commands.CheckFailure("Automatic emoji capture is disabled.")
            if not guild.me or not guild.me.guild_permissions.create_expressions:
                raise commands.CheckFailure(
                    "Scarlet needs Create Expressions permission to add emojis."
                )
            emojis = await asyncio.wait_for(guild.fetch_emojis(), 10)
            by_id = {item.id: item for item in emojis}
            if emoji.id in by_id:
                return by_id[emoji.id], False
            section = self.config.guild(guild).copied
            async with section.get_lock():
                copies = {
                    key: value
                    for key, value in (await section()).items()
                    if value["emoji"] in by_id
                }
                known = copies.get(str(emoji.id))
                if known:
                    return by_id[known["emoji"]], False
                image = await self._download(emoji)
                fingerprint = hashlib.sha256(image).hexdigest()
                duplicate = next(
                    (
                        value
                        for value in copies.values()
                        if value["hash"] == fingerprint and value["animated"] == emoji.animated
                    ),
                    None,
                )
                if duplicate:
                    result, created = by_id[duplicate["emoji"]], False
                else:
                    if sum(item.animated == emoji.animated for item in emojis) >= guild.emoji_limit:
                        raise commands.BadArgument(
                            "This server has no free slots for this emoji type. Remove an emoji first."
                        )
                    if (
                        self._closing
                        or await self.bot.cog_disabled_in_guild(self, guild)
                        or (
                            automatic
                            and not await self._automatic_allowed(
                                guild, channel_id, reaction=reaction
                            )
                        )
                    ):
                        raise commands.CheckFailure("Emoji capture stopped before uploading.")
                    if not guild.me.guild_permissions.create_expressions:
                        raise commands.CheckFailure(
                            "Create Expressions permission changed before upload."
                        )
                    result = await asyncio.wait_for(
                        guild.create_custom_emoji(
                            name=emoji_name(emoji.name, emoji.id, {item.name for item in emojis}),
                            image=image,
                            reason=f"EmojiStealerPlus copied external emoji {emoji.id}",
                        ),
                        30,
                    )
                    created = True
                copies[str(emoji.id)] = {
                    "emoji": result.id,
                    "name": result.name,
                    "animated": result.animated,
                    "hash": fingerprint,
                }
                await section.set(dict(list(copies.items())[-MAX_COPIES:]))
            await self.config.guild(guild).last_error.clear()
            return result, created

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.author.bot or message.webhook_id:
            return
        for value in dict.fromkeys(EMOJI_RX.findall(message.content)):
            await self._queue_emoji(
                message.guild, message.channel, discord.PartialEmoji.from_str(value)
            )

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if before.content != after.content:
            await self.on_message(after)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not guild or payload.user_id == self.bot.user.id or not payload.emoji.id:
            return
        member = payload.member or guild.get_member(payload.user_id)
        if (
            member is None
            or member.bot
            or not (await self.config.guild(guild).capture())["reactions"]
        ):
            return
        channel = guild.get_channel_or_thread(payload.channel_id)
        if channel:
            await self._queue_emoji(guild, channel, payload.emoji, reaction=True)

    @commands.hybrid_group(name="emoji", invoke_without_command=True, fallback="status")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def emoji(self, ctx):
        """Manage automatic external emoji capture."""
        state = await self.config.guild(ctx.guild).capture()
        error = await self.config.guild(ctx.guild).last_error()
        await self._reply(
            ctx,
            f"Automatic capture: {state['enabled']}\nReactions: {state['reactions']}\nNotifications: {state['notify']}\nCapture channel: {('<#' + str(state['channel']) + '>') if state['channel'] else 'All channels'}\nCopied mappings: {len(await self.config.guild(ctx.guild).copied())}\nLast failure: {error or 'None'}",
            title="Emoji capture",
        )

    async def _set_capture(self, guild, key, value):
        async with self._locks[guild.id]:
            async with self.config.guild(guild).capture() as state:
                state[key] = value

    @emoji.command(name="enabled")
    async def emoji_enabled(self, ctx, enabled: bool):
        """Enable or pause automatic emoji capture."""
        await self._set_capture(ctx.guild, "enabled", enabled)
        await self._presentation.confirm(ctx)

    @emoji.command(name="reactions")
    async def emoji_reactions(self, ctx, enabled: bool):
        """Enable or pause capture from member reactions."""
        await self._set_capture(ctx.guild, "reactions", enabled)
        await self._presentation.confirm(ctx)

    @emoji.command(name="notify")
    async def emoji_notify(self, ctx, enabled: bool):
        """Announce successfully copied emojis in their source channel."""
        await self._set_capture(ctx.guild, "notify", enabled)
        await self._presentation.confirm(ctx)

    @emoji.command(name="channel")
    async def emoji_channel(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Restrict automatic capture to a channel and its threads, or clear."""
        await self._set_capture(ctx.guild, "channel", channel.id if channel else None)
        await self._presentation.confirm(ctx)

    @commands.hybrid_command(name="yoink")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def yoink(self, ctx, emoji: str):
        """Copy one external custom emoji into this server."""
        result, created = await self._copy(ctx.guild, discord.PartialEmoji.from_str(emoji))
        await self._reply(
            ctx,
            f"{'Yoinked' if created else 'Already available'} {result}.",
            title="Yoink",
            tone="success",
        )

    async def red_get_data_for_user(self, *, user_id):
        return {}

    async def red_delete_data_for_user(self, *, requester, user_id):
        return None

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        self._locks.pop(guild.id, None)
