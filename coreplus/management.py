"""Native management invocation and context-local reply styling.

Vendored identically in CorePlus and DownloaderPlus for independent installs.
Never replace commands, callbacks, permission checks or Config drivers.
"""

from copy import copy

import discord
from discord.ext.commands.view import StringView
from redbot.core import commands

from .command_support import check_command


class ReplyTarget:
    """Delegate delivery to the saved sender without recursing into its theme."""

    def __init__(self, ctx, sender):
        self.ctx, self.send = ctx, sender

    def __getattr__(self, name):
        return getattr(self.ctx, name)


def reply_tone(content):
    text = str(content or "").strip().lower()
    if text.startswith(("error", "failed", "there was an error", "unable", "cannot")):
        return "error"
    if "failed to install" in text or "failed to load" in text:
        return "warning"
    if text.startswith(("successfully", "loaded ", "unloaded ", "reloaded ", "synced ")):
        return "success"
    return "info"


def style_replies(ctx, presentation):
    """Style only this invocation's Context.send; direct DMs stay with Red."""
    sender = getattr(ctx, "_kevin_management_sender", None)
    if sender is None:
        sender = ctx.send
        ctx._kevin_management_sender = sender
    target = ReplyTarget(ctx, sender)

    async def send(content=None, **kwargs):
        # Preserve upload-only and multi-embed payloads, native views and reference IDs.
        embeds = kwargs.pop("embeds", None)
        embed = kwargs.pop("embed", None)
        if embeds is not None:
            if embed is not None:
                raise TypeError("Cannot mix embed and embeds.")
            if not embeds:
                return await sender(content, embeds=embeds, **kwargs)
            styled = [
                presentation.apply_theme(
                    presentation.style(item, prefix=ctx.clean_prefix),
                    bot=ctx.bot,
                    guild=ctx.guild,
                )
                for item in embeds
            ]
            return await sender(content, embeds=styled, **kwargs)
        if content is None and embed is None:
            kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
            return await sender(**kwargs)
        content_filter = kwargs.pop("filter", None)
        if content_filter and content:
            content = content_filter(str(content))
        return await presentation.send(
            target,
            content,
            embed=embed,
            tone=None if embed is not None else reply_tone(content),
            **kwargs,
        )

    ctx.send = send


def words(value, *, required=True):
    """Split a bounded list using Discord's command quoting, never shell syntax."""
    if len(value) > 1000:
        raise commands.BadArgument("Use at most 1,000 characters for this list.")
    view = StringView(value)
    result = []
    while not view.eof:
        view.skip_ws()
        if not view.eof:
            result.append(view.get_quoted_word())
    if required and not result:
        raise commands.BadArgument("Provide at least one name.")
    if len(result) > 50:
        raise commands.BadArgument("Use at most 50 names at a time.")
    return result


def quoted(value):
    """Round-trip one argument through StringView without argument injection."""
    value = str(value)
    if len(value) > 2000 or any(ord(char) < 32 for char in value):
        raise commands.BadArgument("Arguments must be single-line and at most 2,000 characters.")
    # StringView only escapes quote characters, not backslashes. Select a quote
    # absent from the value so trailing backslashes cannot escape its terminator.
    for opening, closing in (('"', '"'), ("「", "」"), ("『", "』"), ("«", "»")):
        if opening not in value and closing not in value and not value.endswith("\\"):
            return opening + value + closing
    raise commands.BadArgument("Remove embedded quote characters or a trailing backslash.")


def native_command(bot, path, expected):
    command = bot.get_command(path)
    name = getattr(getattr(command, "cog", None), "qualified_name", None)
    if command is None or name not in expected:
        if "Downloader" in expected:
            raise commands.BadArgument(
                "Load Red's bundled Downloader first with [p]load downloader."
            )
        raise commands.BadArgument("The matching Red Core command is unavailable.")
    return command


async def invoke_native(cog, ctx, path, arguments=(), *, expected):
    """Use the actual native parser, parent callbacks, hooks and error handlers."""
    command = native_command(cog.bot, path, expected)
    root = command.root_parent or command
    parts = command.qualified_name.split()
    text = " ".join([*parts[1:], *(quoted(arg) for arg in arguments)])
    message = copy(ctx.message)
    message.content = f"{ctx.prefix or '/'}{root.name} {text}".rstrip()
    child = commands.Context(
        message=message,
        bot=cog.bot,
        view=StringView(text),
        prefix=ctx.prefix or "/",
        command=root,
        invoked_with=root.name,
        interaction=getattr(ctx, "interaction", None),
        assume_yes=getattr(ctx, "assume_yes", False),
    )
    # Retain the originating Red embed preference and actual delivery destination.
    child.embed_requested = ctx.embed_requested
    child.send = getattr(ctx, "_kevin_management_sender", ctx.send)
    style_replies(child, cog._presentation)
    await check_command(child, command)
    # Bot.invoke preserves local error handlers, cooldowns and completion events.
    # Prefix-only native commands parse our StringView even for a slash origin.
    await cog.bot.invoke(child)
    ctx.command_failed = child.command_failed
    return child
