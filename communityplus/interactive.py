"""Checked Discord component contexts and small setup panels, vendored per cog."""

from copy import copy

import discord
from redbot.core import commands


async def component_context(cog, interaction, path, *, owner_id=None):
    """Apply current Red checks to the clicking member, never the message author."""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    if owner_id is not None and interaction.user.id != owner_id:
        raise commands.CheckFailure("This setup panel belongs to another member.")
    if interaction.guild is None or await cog.bot.cog_disabled_in_guild(cog, interaction.guild):
        raise commands.CheckFailure("This cog is disabled here.")
    command = cog.bot.get_command(path)
    if command is None or command.cog is not cog:
        raise commands.CheckFailure("This control expired. Run the command again.")
    message = copy(interaction.message)
    message.author = interaction.user
    message.content = ""
    ctx = await cog.bot.get_context(message)
    ctx.command, ctx.prefix = command, "/"
    for entry in [command, *command.parents]:
        if not entry.is_enabled(interaction.guild):
            raise commands.DisabledCommand(f"{entry.qualified_name} is disabled.")
    if not await command.can_run(ctx, check_all_parents=True):
        raise commands.CheckFailure("You do not have permission to use this control.")
    ctx.interaction = interaction
    return ctx


async def component_error(interaction, error):
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    text = (
        str(error)
        if isinstance(error, commands.CommandError)
        else "This control failed. Try the text command."
    )
    await interaction.followup.send(
        text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


class SetupView(discord.ui.View):
    """A requester-bound panel whose every mutation repeats the command checks."""

    def __init__(self, cog, ctx, path, fields, update):
        super().__init__(timeout=180)
        self.cog, self.owner_id, self.path = cog, ctx.author.id, path
        self.guild_id = ctx.guild.id
        self.update = update
        self.message = None
        for key, label, kind in fields:
            if kind == "role":
                item = discord.ui.RoleSelect(placeholder=label, min_values=1, max_values=1)
            elif kind in {"text", "voice"}:
                types = (
                    [discord.ChannelType.text] if kind == "text" else [discord.ChannelType.voice]
                )
                item = discord.ui.ChannelSelect(
                    placeholder=label, channel_types=types, min_values=1, max_values=1
                )
            else:
                item = discord.ui.Select(
                    placeholder=label,
                    options=[
                        discord.SelectOption(label="Enable " + label, value="on"),
                        discord.SelectOption(label="Disable " + label, value="off"),
                    ],
                )

            async def callback(interaction, item=item, key=key, kind=kind):
                try:
                    if interaction.guild is None or interaction.guild.id != self.guild_id:
                        raise commands.CheckFailure("This setup panel belongs to another server.")
                    ctx = await component_context(
                        self.cog, interaction, self.path, owner_id=self.owner_id
                    )
                    value = item.values[0]
                    if kind in {"role", "text", "voice"}:
                        value = value.id
                    else:
                        value = value == "on"
                    await self.update(ctx, key, value)
                    await interaction.followup.send("Settings saved.", ephemeral=True)
                except commands.CommandError as error:
                    await component_error(interaction, error)

            item.callback = callback
            self.add_item(item)
        cog._views.add(self)

    async def on_timeout(self):
        self.cog._views.discard(self)
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction, error, item):
        await component_error(interaction, error)


async def close_views(cog):
    for view in tuple(cog._views):
        view.stop()
        await view.on_timeout()
    cog._views.clear()
