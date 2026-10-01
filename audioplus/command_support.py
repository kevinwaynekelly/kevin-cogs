"""Preserve Red permissions when invoking hybrid groups and direct shortcuts.

Vendored per cog so Downloader can install each package independently.
"""

from contextlib import asynccontextmanager

from redbot.core import commands


def prefix_group(parent, **kwargs):
    """Declare a legacy prefix branch to attach after the cog's command copies."""

    def decorator(callback):
        kwargs.setdefault("extras", {})["prefix_parent"] = parent.callback.__name__
        command = commands.group(**kwargs)(callback)
        return command

    return decorator


def attach_prefix_groups(cog):
    """Attach legacy branches after copying, outside the limited slash tree.

    HybridGroup.copy() only accepts hybrid children, so these ordinary groups
    remain separate during Cog construction and join the prefix tree here.
    """
    for command in cog.get_commands():
        parent_name = command.extras.get("prefix_parent")
        if parent_name:
            commands.Group.add_command(getattr(cog, parent_name), command)


async def check_command(ctx, command):
    """Check the original command and every parent without leaking context state."""
    original_command = ctx.command
    original_state = ctx.permission_state
    original_interaction = ctx.interaction
    # Check the prefix command path, including Red's saved permission rules.
    # Discord's hybrid leaf checks alone do not invoke parent prefix groups.
    ctx.interaction = None
    try:
        for entry in [command, *command.parents]:
            if not entry.is_enabled(ctx.guild):
                raise commands.DisabledCommand(f"{entry.qualified_name} is disabled.")
        if not await command.can_run(ctx, check_all_parents=True):
            raise commands.CheckFailure("You do not have permission to use this command.")
    finally:
        ctx.command = original_command
        ctx.permission_state = original_state
        ctx.interaction = original_interaction


async def prepare_hybrid(ctx):
    """Apply parent checks and acknowledge slash requests before Config I/O."""
    if getattr(ctx, "interaction", None) is not None:
        await check_command(ctx, ctx.command)
        if not ctx.interaction.response.is_done():
            await ctx.defer()
    hub = configuration_hub(getattr(ctx, "bot", None))
    if hub is not None:
        token = await hub.begin_configuration_action(ctx)
        if token is not None:
            if not hasattr(ctx, "_kevin_audit_scopes"):
                ctx._kevin_audit_scopes = []
            ctx._kevin_audit_scopes.append((hub, token))


def configuration_hub(bot):
    """Use the optional runtime protocol without importing SettingsHub."""
    getter = getattr(bot, "get_cog", None)
    hub = getter("SettingsHub") if getter else None
    return hub if callable(getattr(hub, "begin_configuration_action", None)) else None


def finish_configuration_audit(ctx):
    scopes = getattr(ctx, "_kevin_audit_scopes", [])
    if scopes:
        hub, token = scopes.pop()
        hub.end_configuration_action(token)


@asynccontextmanager
async def configuration_action(cog, ctx):
    """Attribute checked component writes to the clicking member's task."""
    hub = configuration_hub(cog.bot)
    token = await hub.begin_configuration_action(ctx) if hub is not None else None
    try:
        yield
    finally:
        if hub is not None:
            hub.end_configuration_action(token)


async def invoke_shortcut(cog, ctx, command, **kwargs):
    """Reuse an existing callback while respecting its disabled/permission state."""
    await check_command(ctx, command)
    return await command.callback(cog, ctx, **kwargs)
