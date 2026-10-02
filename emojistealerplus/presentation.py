"""Shared Discord theme, vendored per cog for independent Downloader installs."""

from __future__ import annotations

import re
from copy import deepcopy

import discord
from redbot.core import commands

COLORS = {"info": 0x818CF8, "success": 0x34D399, "warning": 0xFBBF24, "error": 0xFB7185}
LABELS = {
    "diag": "Diagnostics",
    "pingnode": "Node diagnostics",
    "playerstate": "Player state",
    "debugvc": "Voice diagnostics",
    "shownode": "Node settings",
    "setnode": "Node settings",
    "connectnode": "Node connection",
    "fixvoice": "Voice recovery",
    "seenlist": "Last seen",
    "seendetail": "Last seen detail",
    "seenlistcsv": "Last seen export",
    "xp": "XP",
    "exportcsv": "CSV export",
    "importcsv": "CSV import",
    "importbynamecsv": "CSV import by name",
    "levelup": "Level-up notices",
    "ownerbypass": "Bot owner bypass",
    "onein": "Probability",
    "nochannels": "Excluded channels",
    "noroles": "Excluded roles",
    "vcsolo": "Solo voice cleanup",
    "audioset": "Music settings",
    "playlist": "Saved playlists",
    "favorite": "Favorites",
    "dj": "DJ role",
    "voteskip": "Vote skipping",
    "periodboard": "Calendar leaderboard",
    "dailycap": "Daily XP cap",
    "minwords": "Minimum word count",
    "rolemenu": "Self-service roles",
    "voicehours": "Voice hours",
    "exemptchannel": "Exempt channels",
    "exemptrole": "Exempt roles",
    "owooptout": "Your transformation preference",
    "owoify": "Manual transformation",
    "syllables": "Pronunciation corrections",
    "fairqueue": "Fair queues",
    "autoplay": "Autoplay",
    "rankcard": "Rank card",
    "achievements": "Achievements",
    "logsearch": "Search history",
    "logexport": "History export",
    "stylize": "Style preview",
}
LEGACY_COLORS = {
    **{color: tone for tone, color in COLORS.items()},
    discord.Color.green().value: "success",
    discord.Color.red().value: "error",
    discord.Color.orange().value: "warning",
    discord.Color.gold().value: "warning",
}


def units(text: str | None) -> int:
    return len((text or "").encode("utf-16-le")) // 2


def clip(text: str | None, limit: int) -> str:
    return (
        (text or "").encode("utf-16-le")[: max(0, limit) * 2].decode("utf-16-le", errors="ignore")
    )


def chunks(text: str, limit: int):
    """Split at line/word boundaries without losing text or splitting Unicode."""
    while units(text) > limit:
        part = clip(text, limit)
        boundary = max(part.rfind("\n"), part.rfind(" "))
        if boundary > len(part) // 2:
            part = part[: boundary + 1]
        yield part
        text = text[len(part) :]
    if text:
        yield text


def settings(text: str, *, lang: str = "ini") -> str:
    """Readable settings and lists, with working mentions instead of code fences."""
    lines = []
    for line in text.splitlines():
        match = re.fullmatch(r"([\w. ]+?)\s*=\s*(.*)", line)
        if match:
            key, value = match.groups()
            label = key.strip().replace("_", " ").replace(".", " · ").capitalize()
            if value in {"True", "False"}:
                value = "Enabled" if value == "True" else "Disabled"
            lines.append(f"**{label}** · {value}")
        else:
            lines.append(line)
    return "\n".join(lines) or "None"


class Presentation:
    def __init__(self, cog: str, command: str):
        self.cog = cog
        self.command = command

    def embed(self, title: str, description=None, *, tone="info", timestamp=None):
        embed = discord.Embed(
            title=title, description=description, color=COLORS[tone], timestamp=timestamp
        )
        return self.style(embed)

    def style(self, embed: discord.Embed, *, prefix=None, tone=None):
        embed = deepcopy(embed)
        title = embed.title or "Update"
        for separator in (" - ", " · "):
            title = title.removeprefix(self.cog + separator)
        embed.title = clip(f"{self.cog} · {title}", 256)
        tone = tone or LEGACY_COLORS.get(getattr(embed.color, "value", None), "info")
        embed.color = COLORS[tone]
        footer = embed.footer.text or ""
        if footer.startswith("Kevin's Cogs"):
            footer = footer.partition(" · ")[2]
        if prefix is not None:
            footer = footer.replace("[p]", prefix)
        elif "[p]" in footer:
            footer = ""
        footer = footer.rstrip(".")
        embed.set_footer(text=clip("Kevin's Cogs" + (f" · {footer}" if footer else ""), 1900))
        return embed

    def pages(self, embed: discord.Embed):
        """Paginate descriptions and fields within Discord's UTF-16 limits."""
        base = deepcopy(embed)
        base.description = None
        base.clear_fields()
        if base.author.name:
            base.set_author(
                name=clip(base.author.name, 256),
                url=base.author.url,
                icon_url=base.author.icon_url,
            )
        # Leave room for page labels and a later 80-character server brand on retries.
        overhead = units(base.title) + units(base.footer.text) + units(base.author.name) + 192
        budget = 6000 - overhead
        pages = []
        descriptions = list(chunks(embed.description or "", min(3000, budget)))
        for description in descriptions:
            page = deepcopy(base)
            page.description = description
            pages.append(page)
        if not pages:
            pages.append(deepcopy(base))
        page = pages[-1]
        used = units(page.description)
        for field in embed.fields:
            name = clip(field.name, 240) or "Details"
            values = list(chunks(field.value or "\u200b", 1024))
            for index, value in enumerate(values):
                label = name if index == 0 else f"{name} (continued)"
                size = units(label) + units(value)
                if len(page.fields) >= 25 or used + size > budget:
                    page = deepcopy(base)
                    pages.append(page)
                    used = 0
                page.add_field(name=label, value=value, inline=field.inline and len(values) == 1)
                used += size
        if len(pages) > 1:
            for index, page in enumerate(pages, 1):
                page.set_footer(text=f"{base.footer.text} · Page {index}/{len(pages)}")
        return pages

    def apply_theme(self, embed: discord.Embed, *, bot, guild, tone=None):
        """Apply the current optional server theme to a styled card or live edit."""
        embed = deepcopy(embed)
        themes = getattr(bot, "_kevin_cogs_themes", None)
        theme = themes.get(guild.id) if guild and isinstance(themes, dict) else None
        if not isinstance(theme, dict):
            return embed
        colors = theme.get("colors", {})
        selected = tone or LEGACY_COLORS.get(getattr(embed.color, "value", None))
        if selected is None:
            selected = next(
                (
                    key
                    for key, value in colors.items()
                    if value == getattr(embed.color, "value", None)
                ),
                "info",
            )
        color = colors.get(selected)
        if type(color) is int and 0 <= color <= 0xFFFFFF:
            embed.color = color
        brand = theme.get("footer")
        if isinstance(brand, str) and 1 <= len(brand) <= 80:
            footer = embed.footer.text or ""
            if footer == "Kevin's Cogs" or footer == brand:
                footer = ""
            elif footer.startswith("Kevin's Cogs · ") or footer.startswith(brand + " · "):
                footer = footer.partition(" · ")[2]
            embed.set_footer(text=clip(brand + (" · " + footer if footer else ""), 1900))
        return embed

    async def send(
        self,
        target,
        content=None,
        *,
        embed=None,
        title=None,
        tone=None,
        notification=None,
        theme_guild=None,
        theme_bot=None,
        **kwargs,
    ):
        prefix = getattr(target, "clean_prefix", None)
        command = getattr(getattr(target, "command", None), "qualified_name", self.command)
        if title is None:
            tail = command.removeprefix(self.command).strip()
            title = (
                " · ".join(
                    LABELS.get(part, part.replace("_", " ").title()) for part in tail.split()
                )
                if tail
                else "Status"
            )
        if embed is None:
            embed = self.embed(title, str(content) if content is not None else "Export attached.")
        elif content:
            embed = deepcopy(embed)
            embed.description = str(content) + "\n" + (embed.description or "")
        embed = self.style(embed, prefix=prefix, tone=tone)
        if prefix is not None and embed.footer.text == "Kevin's Cogs":
            help_command = f"help {self.cog}" if self.command == "audio" else f"{self.command} help"
            if self.command == "settings":
                help_command = "help settings"
            hint = f"Use {prefix}{help_command} for commands"
            if self.command == "audio" and getattr(target, "interaction", None) is not None:
                hint = "Use /play to queue music"
            embed.set_footer(text=clip(f"Kevin's Cogs · {hint}", 1900))
        use_embeds = True
        channel = getattr(target, "channel", target)
        guild = getattr(target, "guild", None)
        bot = theme_bot or getattr(target, "bot", None)
        if bot is None:
            state = getattr(channel, "_state", None)
            client = getattr(state, "_get_client", None)
            bot = client() if callable(client) else None
        embed = self.apply_theme(embed, bot=bot, guild=theme_guild or guild, tone=tone)
        if guild and hasattr(channel, "permissions_for"):
            use_embeds = channel.permissions_for(guild.me).embed_links
        # Respect Red's server/user embed preference as well as Discord permissions.
        requested = getattr(target, "embed_requested", None)
        if requested is not None:
            use_embeds = use_embeds and await requested()
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        first_message = None
        if notification and units(notification) > 2000:
            for part in chunks(notification, 2000):
                message = await target.send(part, **kwargs)
                if first_message is None:
                    first_message = message
                kwargs.pop("file", None)
                kwargs.pop("files", None)
            notification = None
        for page in self.pages(embed):
            if use_embeds:
                if notification:
                    message = await target.send(notification, embed=page, **kwargs)
                else:
                    message = await target.send(embed=page, **kwargs)
            else:
                text = f"**{page.title}**\n{page.description or ''}"
                for field in page.fields:
                    text += f"\n\n**{field.name}**\n{field.value}"
                text += f"\n\n{page.footer.text}"
                if notification:
                    text = notification + "\n\n" + text
                message = None
                for part in chunks(text, 2000):
                    message = await target.send(part, **kwargs)
                    if first_message is None:
                        first_message = message
                    kwargs.pop("file", None)
                    kwargs.pop("files", None)
            if first_message is None:
                first_message = message
            kwargs.pop("file", None)
            kwargs.pop("files", None)
            notification = None
        return first_message

    async def confirm(self, ctx):
        return await self.send(ctx, "Settings saved.", tone="success")

    async def help(self, ctx):
        """Render nested command help without changing Red's global help formatter."""
        command = ctx.command
        embed = self.embed("Commands", f"Tools in `{ctx.clean_prefix}{command.qualified_name}`.")
        for child in sorted(command.commands, key=lambda child: child.name):
            original_command = ctx.command
            original_state = ctx.permission_state
            try:
                visible = not child.hidden and await child.can_run(ctx, check_all_parents=True)
            except commands.CommandError:
                visible = False
            finally:
                ctx.command = original_command
                ctx.permission_state = original_state
            if visible:
                usage = f"{ctx.clean_prefix}{child.qualified_name} {child.signature}".strip()
                embed.add_field(
                    name=child.name.replace("_", " ").title(), value=f"`{usage}`", inline=False
                )
        if not embed.fields:
            embed.description = "No available subcommands here."
        return await self.send(ctx, embed=embed)

    async def command_error(self, ctx, error):
        original = getattr(error, "original", error)
        if isinstance(original, commands.UserInputError) or type(original) is commands.CommandError:
            embed = self.embed(
                "Check this command", str(original) or "Check the command arguments.", tone="error"
            )
            usage = (
                f"{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}".strip()
            )
            embed.add_field(name="Usage", value=f"`{usage}`", inline=False)
            return await self.send(ctx, embed=embed)
        # Preserve Red's permission rules, disabled-command behavior, and exception logging.
        return await ctx.bot.on_command_error(ctx, error, unhandled_by_cog=True)
