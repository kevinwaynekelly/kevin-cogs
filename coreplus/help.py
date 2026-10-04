"""Checked category help with one card, a selector and page buttons."""

import logging
from copy import copy

import discord
from redbot.core import commands
from redbot.core.commands.help import HelpFormatterABC, HelpSettings, RedHelpFormatter

from .command_support import check_command
from .presentation import chunks, clip

log = logging.getLogger("red.kevin_cogs.coreplus.help")

CATEGORY_ALIASES = {"Core": "CorePlus", "CogManagerUI": "CorePlus"}
SUMMARIES = {
    "AudioPlus": "Music, queues, playlists and voice playback",
    "IntroPlus": "Personal voice entrance clips",
    "PresencePlus": "Bot status profiles and schedules",
    "CommunityPlus": "Roles, activity, rooms, birthdays and events",
    "LevelPlus": "XP, rankings, achievements and role rewards",
    "LogPlus": "Event logs, history and staff incidents",
    "OwoPlus": "Text styles, transformations and haiku",
    "EmojiStealerPlus": "External emoji capture",
    "ExportPlus": "Private readable chat exports",
    "BackupPlus": "Server structure backups and restore previews",
    "SettingsHub": "Shared setup, themes and diagnostics",
    "CorePlus": "Bot information, help and Red management",
    "DownloaderPlus": "Cog installation, updates and repositories",
}
SHORTCUTS = {
    **{
        name: (f"audio {name}",)
        for name in (
            "play",
            "join",
            "skip",
            "stop",
            "pause",
            "resume",
            "volume",
            "np",
            "queue",
            "shuffle",
            "repeat",
            "tone",
            "playerstate",
            "debugvc",
            "speak",
            "undeafen",
            "fixvoice",
            "rejoin",
        )
    },
    "disconnect": ("audio leave",),
    "audiostatus": ("audio pingnode",),
    "replay": ("play", "audio play"),
    "history": ("play", "audio play"),
    "rank": ("level show",),
    "rankcard": ("level show",),
    "leaderboard": ("level leaderboard",),
    "levellookup": ("level lookup",),
    "seen": ("community seen",),
    "seendetail": ("community seendetail",),
    "activity": ("community stats",),
    "seenlist": ("community seenlist",),
    "logstatus": ("log",),
    "logchannel": ("log channel",),
    "lograte": ("log rate",),
}


def category(bot, command):
    name = getattr(command.cog, "qualified_name", None) or "Other commands"
    if name == "Downloader" and bot.get_cog("DownloaderPlus") is not None:
        return "DownloaderPlus"
    return CATEGORY_ALIASES.get(name, name)


def slash_usage(command):
    app = getattr(command, "app_command", None)
    if app is None:
        return None
    fallback = getattr(command, "fallback", None)
    if isinstance(app, discord.app_commands.Group):
        if fallback:
            app = app.get_command(fallback)
        else:
            return None
    return "/" + app.qualified_name if app is not None else None


class CoreHelpFormatter(HelpFormatterABC):
    def __init__(self, cog):
        self.cog = cog
        self.fallback = RedHelpFormatter()

    async def visible(self, ctx, command, *, explicit=False):
        settings = await HelpSettings.from_context(ctx)
        if command.hidden and not (settings.show_hidden or explicit):
            return False
        try:
            await check_command(ctx, command)
            native = getattr(command.cog, "native_help_paths", {}).get(command.qualified_name)
            if native:
                source = ctx.bot.get_command(native)
                expected = getattr(command.cog, "native_cog_names", ())
                if source is None or getattr(source.cog, "qualified_name", None) not in expected:
                    return False
                await check_command(ctx, source)
            sources = SHORTCUTS.get(command.qualified_name, ())
            # Match only our feature packages, never unrelated same-name commands.
            if command.cog and command.cog.qualified_name in {
                "AudioPlus",
                "CommunityPlus",
                "LevelPlus",
                "LogPlus",
            }:
                for path in sources:
                    source = ctx.bot.get_command(path)
                    if source is not None and source.cog is command.cog:
                        await check_command(ctx, source)
            return True
        except (commands.CommandError, discord.DiscordException):
            return False
        except Exception as error:
            # A broken third-party check must not break help for every cog.
            log.debug("Help skipped %s after %s", command.qualified_name, type(error).__name__)
            return False

    async def categories(self, ctx):
        result = {}
        for command in sorted(ctx.bot.commands, key=lambda entry: entry.name):
            if await self.visible(ctx, command):
                result.setdefault(category(ctx.bot, command), []).append(command)
        return dict(sorted(result.items()))

    async def cards(self, ctx, target):
        groups = await self.categories(ctx)
        command = ctx.bot.get_command(target) if target else None
        selected = next((name for name in groups if name.casefold() == target.casefold()), None)
        theme = self.cog._presentation
        if command is not None:
            if not await self.visible(ctx, command, explicit=True):
                raise commands.CheckFailure("That command is unavailable here.")
            card = theme.embed(
                command.qualified_name,
                command.format_help_for_context(ctx) or "No additional help is supplied.",
            )
            usage = f"{ctx.clean_prefix}{command.qualified_name} {command.signature}".strip()
            card.add_field(name="Text usage", value=f"`{usage}`", inline=False)
            slash = slash_usage(command)
            if slash:
                card.add_field(name="Slash command", value=f"`{slash}`", inline=False)
            settings = await HelpSettings.from_context(ctx)
            if command.aliases and settings.show_aliases:
                card.add_field(name="Text aliases", value=", ".join(command.aliases), inline=False)
            for child in sorted(getattr(command, "commands", ()), key=lambda entry: entry.name):
                if await self.visible(ctx, child):
                    usage = f"{ctx.clean_prefix}{child.qualified_name} {child.signature}".strip()
                    card.add_field(
                        name=child.name,
                        value=f"`{usage}`\n{child.format_shortdoc_for_context(ctx) or 'Open subcommands.'}",
                        inline=False,
                    )
            cards = theme.pages(card)
        elif selected is not None:
            cards = []
            entries = groups[selected]
            for offset in range(0, len(entries), 8):
                card = theme.embed(selected, SUMMARIES.get(selected, "Available commands"))
                for entry in entries[offset : offset + 8]:
                    slash = slash_usage(entry)
                    label = f"`{ctx.clean_prefix}{entry.qualified_name}`"
                    if slash:
                        label += f" · `{slash}`"
                    card.add_field(
                        name=entry.name,
                        value=label
                        + "\n"
                        + (entry.format_shortdoc_for_context(ctx) or "Open subcommands."),
                        inline=False,
                    )
                cards.extend(theme.pages(card))
        elif target:
            raise commands.BadArgument("No available command or category matches that name.")
        else:
            card = theme.embed(
                "Scarlet help",
                "Choose a category below.\n"
                f"Use `/help` or `{ctx.clean_prefix}help <command>` for arguments and details.",
            )
            for name, entries in groups.items():
                card.add_field(
                    name=name,
                    value=f"{SUMMARIES.get(name, 'Available commands')} · {len(entries)} commands",
                    inline=False,
                )
            cards = theme.pages(card)
        payloads = []
        embeds = await ctx.bot.embed_requested(channel=ctx, command=ctx.bot.get_command("help"))
        if ctx.guild is not None:
            embeds = embeds and ctx.channel.permissions_for(ctx.guild.me).embed_links
        for card in cards:
            card = theme.apply_theme(card, bot=ctx.bot, guild=ctx.guild)
            if embeds:
                payloads.append({"content": None, "embed": card})
            else:
                text = f"**{card.title}**\n{card.description or ''}"
                for field in card.fields:
                    text += f"\n\n**{field.name}**\n{field.value}"
                text += "\n\n" + (card.footer.text or "Kevin's Cogs")
                payloads.extend({"content": part, "embed": None} for part in chunks(text, 1900))
        return groups, payloads

    async def send_help(self, ctx, help_for=None, *, from_help_command=False):
        if self.cog._closing or await ctx.bot.cog_disabled_in_guild(self.cog, ctx.guild):
            return await self.fallback.send_help(ctx, help_for, from_help_command=from_help_command)
        if getattr(ctx, "interaction", None) is not None:
            ctx = copy(ctx)
            prefixes = await ctx.bot.get_valid_prefixes(ctx.guild)
            ctx.prefix = next(
                (prefix for prefix in prefixes if not prefix.startswith("<@")), prefixes[0]
            )
        if isinstance(help_for, commands.Command):
            target = help_for.qualified_name
        elif isinstance(help_for, commands.Cog):
            target = CATEGORY_ALIASES.get(help_for.qualified_name, help_for.qualified_name)
        elif help_for is None or help_for is ctx.bot:
            target = ""
        else:
            target = str(help_for).strip()
            # The old CogManagerUI category lives inside the CorePlus menu.
            target = CATEGORY_ALIASES.get(target, target)
            if target == "Downloader" and ctx.bot.get_cog("DownloaderPlus"):
                target = "DownloaderPlus"
        try:
            groups, payloads = await self.cards(ctx, target)
        except commands.CommandError as error:
            return await self.cog._presentation.send(ctx, str(error), title="Help", tone="warning")
        view = HelpView(self.cog, ctx, target, groups, len(payloads))
        self.cog._views.add(view)
        try:
            view.message = await ctx.send(
                **payloads[0], view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except BaseException:
            self.cog._views.discard(view)
            view.stop()
            raise
        return view.message


class HelpView(discord.ui.View):
    def __init__(self, cog, ctx, target, groups, page_count):
        super().__init__(timeout=180)
        self.cog, self.owner_id = cog, ctx.author.id
        self.guild_id = ctx.guild.id if ctx.guild else None
        self.channel_id = ctx.channel.id
        self.prefix = ctx.clean_prefix
        self.target, self.page, self.category_page = target, 0, 0
        self.message = None
        self.populate(groups, page_count)

    def populate(self, groups, page_count):
        self.clear_items()
        names = list(groups)
        category_pages = max(1, (len(names) + 24) // 25)
        self.category_page %= category_pages
        if names:
            selector = discord.ui.Select(
                placeholder="Choose a command category",
                options=[
                    discord.SelectOption(
                        label=clip(name, 100),
                        value=name,
                        description=clip(SUMMARIES.get(name, "Available commands"), 100),
                    )
                    for name in names[self.category_page * 25 : (self.category_page + 1) * 25]
                ],
            )

            async def select(interaction):
                await self.change(interaction, target=selector.values[0])

            selector.callback = select
            self.add_item(selector)
        for label, action, disabled in (
            ("Categories", "home", not self.target and self.page == 0),
            ("Previous", "previous", self.page == 0),
            (f"{self.page + 1} / {page_count}", "counter", True),
            ("Next", "next", self.page + 1 >= page_count),
        ):
            button = discord.ui.Button(label=label, disabled=disabled, row=1)

            async def click(interaction, action=action):
                await self.change(interaction, action=action)

            button.callback = click
            self.add_item(button)
        if category_pages > 1:
            button = discord.ui.Button(label="More categories", row=2)

            async def more(interaction):
                await self.change(interaction, action="categories")

            button.callback = more
            self.add_item(button)

    async def change(self, interaction, *, target=None, action=None):
        try:
            if interaction.user.id != self.owner_id:
                raise commands.CheckFailure("This help menu belongs to another member. Run /help.")
            guild_id = interaction.guild.id if interaction.guild else None
            if guild_id != self.guild_id or interaction.channel_id != self.channel_id:
                raise commands.CheckFailure("This help menu belongs to another channel.")
            if self.cog._closing or self.cog.bot.get_cog("CorePlus") is not self.cog:
                raise commands.CheckFailure("This help menu expired. Run /help again.")
            if self.cog.bot.get_command("helpme") is not self.cog.help_menu:
                raise commands.CheckFailure("This help menu expired. Run /help again.")
            if interaction.message is None:
                raise commands.CheckFailure("The help message is unavailable.")
            message = copy(interaction.message)
            message.author, message.content = interaction.user, ""
            ctx = await self.cog.bot.get_context(message)
            ctx.prefix, ctx.command = self.prefix, self.cog.help_menu
            await check_command(ctx, ctx.command)
            if not interaction.response.is_done():
                await interaction.response.defer()
            proposed = target if target is not None else self.target
            proposed = "" if action == "home" else proposed
            page = 0 if target is not None or action == "home" else self.page
            page += 1 if action == "next" else -1 if action == "previous" else 0
            groups, payloads = await self.cog._formatter.cards(ctx, proposed)
            self.target, self.page = proposed, max(0, min(page, len(payloads) - 1))
            if action == "categories":
                self.category_page += 1
            self.populate(groups, len(payloads))
            payload = payloads[self.page]
            # An embed=None edit removes the old embed when text fallback is selected.
            await interaction.message.edit(
                **payload, view=self, allowed_mentions=discord.AllowedMentions.none()
            )
        except commands.CommandError as error:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(
                str(error), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )

    async def on_error(self, interaction, error, item):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("Help could not update. Run /help again.", ephemeral=True)

    async def on_timeout(self):
        self.cog._views.discard(self)
        self.clear_items()
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass
