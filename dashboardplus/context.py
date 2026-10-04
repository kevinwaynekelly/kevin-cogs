"""Run fixed cog commands with a real owner/channel Context and web-only replies."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import discord
from discord.ext.commands.view import StringView
from redbot.core import commands

from .command_support import check_command, finish_configuration_audit


def argument(value):
    value = str(value)
    if len(value) > 1000 or any(ord(char) < 32 for char in value):
        raise commands.BadArgument("Use a single line of at most 1,000 characters.")
    for opening, closing in (('"', '"'), ("「", "」"), ("『", "』"), ("«", "»")):
        if opening not in value and closing not in value and not value.endswith("\\"):
            return opening + value + closing
    raise commands.BadArgument("Remove embedded quotes or a trailing backslash.")


class WebReply:
    """A bounded web result, never an invented Discord message to edit/delete."""

    def __init__(self, ctx, content=None, **kwargs):
        self.ctx = ctx
        self.id = 0
        self.channel = ctx.channel
        self.guild = ctx.guild
        self.update(content, **kwargs)

    def update(self, content=None, **kwargs):
        self.record = {"content": str(content or "")[:4000], "embeds": []}
        embeds = kwargs.get("embeds") or ([kwargs["embed"]] if kwargs.get("embed") else [])
        self.record["embeds"] = [embed.to_dict() for embed in embeds[:10]]
        # No Discord component is registered or usable on a web-only response.
        if kwargs.get("view"):
            kwargs["view"].stop()

    async def edit(self, **kwargs):
        self.update(**kwargs)
        return self

    async def delete(self, **kwargs):
        self.record = {"content": "", "embeds": []}


class WebContext(commands.Context):
    async def send(self, content=None, **kwargs):
        reply = WebReply(self, content, **kwargs)
        self.results.append(reply)
        del self.results[:-20]
        return reply

    async def embed_requested(self):
        return True

    @asynccontextmanager
    async def typing(self, **kwargs):
        yield

    async def defer(self, **kwargs):
        return

    async def tick(self, *args, **kwargs):
        await self.send("Saved.")
        return True


def make_context(bot, member, channel, command, arguments=(), *, rest=False):
    root = command.root_parent or command
    parts = command.qualified_name.split()[1:]
    encoded = [argument(value) for value in arguments]
    if rest and arguments:
        # Keyword-only text is read verbatim by Red's parser. Do not add quotes.
        encoded[-1] = str(arguments[-1])
    text = " ".join([*parts, *encoded])
    message = SimpleNamespace(
        id=0,
        content=f"!{root.name} {text}".rstrip(),
        author=member,
        guild=member.guild,
        channel=channel,
        _state=bot._connection,
        created_at=discord.utils.utcnow(),
        edited_at=None,
        attachments=[],
        mentions=[],
        role_mentions=[],
        channel_mentions=[],
    )
    ctx = WebContext(
        message=message,
        bot=bot,
        view=StringView(text),
        prefix="!",
        command=root,
        invoked_with=root.name,
        assume_yes=False,
    )
    ctx.results = []
    return ctx


async def execute(ctx, command):
    """Original checks, converters, callbacks, hooks and cooldowns remain active."""
    try:
        await check_command(ctx, command)
        await ctx.command.invoke(ctx)
    finally:
        # Parent hooks can begin an audit before a child converter fails.
        while getattr(ctx, "_kevin_audit_scopes", []):
            finish_configuration_audit(ctx)
    return {"messages": [reply.record for reply in ctx.results], "failed": ctx.command_failed}
