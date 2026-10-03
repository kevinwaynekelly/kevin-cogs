"""Owner-only saved presence profiles, schedules and optional music status."""

import asyncio
from copy import deepcopy
from typing import Literal

from redbot.core import Config, commands

from .command_support import check_command, finish_configuration_audit, prepare_hybrid
from .constants import (
    DEFAULT_COMMAND_HINT,
    DEFAULTS,
    LEGACY_COMMAND_HINT,
    MAX_ENTRIES,
    MAX_PROFILES,
    MAX_SCHEDULES,
)
from .controller import PresenceController
from .presentation import Presentation
from .profiles import describe_rule, minute, name, template, validate, weekdays, zone

Activity = Literal["custom", "playing", "listening", "watching", "competing"]
Availability = Literal["online", "idle", "dnd", "invisible"]


class PresencePlus(commands.Cog):
    """Manage Scarlet's saved status, rotation, schedules and music presence."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=702035013, force_registration=True)
        self.config.register_global(settings=deepcopy(DEFAULTS), command_hint_version=0)
        self._presentation = Presentation("PresencePlus", "presence")
        self._controller = PresenceController(self)
        self._commands = set()
        self._closing = False

    async def cog_load(self):
        async with self.config.settings.get_lock():
            if await self.config.command_hint_version() == 0:
                settings = await self.config.settings()
                profiles = settings.get("profiles", {}) if isinstance(settings, dict) else {}
                profile = profiles.get("default", {}) if isinstance(profiles, dict) else {}
                entries = profile.get("entries", []) if isinstance(profile, dict) else []
                changed = False
                if isinstance(entries, list):
                    for entry in entries:
                        if entry == {"kind": "custom", "text": LEGACY_COMMAND_HINT}:
                            entry["text"] = DEFAULT_COMMAND_HINT
                            changed = True
                if changed:
                    await self.config.settings.set(settings)
                # A later owner choice of the old text must survive future reloads.
                await self.config.command_hint_version.set(1)
        self._controller.start()

    async def cog_unload(self):
        self._closing = True
        tasks = [task for task in self._commands if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._commands.clear()
        await self._controller.close()

    async def cog_check(self, ctx):
        if self._closing:
            raise commands.CheckFailure("PresencePlus is unloading.")
        if not await self.bot.is_owner(ctx.author):
            raise commands.NotOwner("Only the bot owner can change or inspect global presence.")
        return True

    async def cog_before_invoke(self, ctx):
        await self.cog_check(ctx)
        if ctx.interaction is not None:
            await check_command(ctx, ctx.command)
            if not ctx.interaction.response.is_done():
                await ctx.defer(ephemeral=True)
        await prepare_hybrid(ctx)
        self._commands.add(asyncio.current_task())

    async def cog_after_invoke(self, ctx):
        self._commands.discard(asyncio.current_task())
        finish_configuration_audit(ctx)

    async def cog_command_error(self, ctx, error):
        self._commands.discard(asyncio.current_task())
        finish_configuration_audit(ctx)
        original = getattr(error, "original", error)
        if isinstance(original, ValueError):
            error = commands.BadArgument(str(original))
        await self._presentation.command_error(ctx, error)

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    async def _mutate(self, ctx, action, *, reset=False):
        async with self.config.settings.get_lock():
            # Owner status and saved Red rules may change while waiting for a writer.
            await self.cog_check(ctx)
            await check_command(ctx, ctx.command)
            settings = deepcopy(DEFAULTS) if reset else await self.config.settings()
            action(settings)
            settings = validate(settings)
            await self.config.settings.set(settings)
            self._controller.request()
        await self._reply(
            ctx,
            "Saved. Presence updates within 15 seconds when enabled.",
            tone="success",
        )

    def _profile(self, settings):
        return settings["profiles"][settings["selected"]]

    @commands.Cog.listener()
    async def on_ready(self):
        self._controller.request(force=True)

    @commands.Cog.listener()
    async def on_resumed(self):
        self._controller.request(force=True)

    @commands.Cog.listener()
    async def on_shard_ready(self, shard_id):
        self._controller.request(force=True)

    @commands.Cog.listener()
    async def on_shard_resumed(self, shard_id):
        self._controller.request(force=True)

    @commands.hybrid_group(name="presence", fallback="show", invoke_without_command=True)
    @commands.is_owner()
    async def presence(self, ctx):
        """Manage saved status profiles, rotation, schedules and music presence."""
        settings = validate(await self.config.settings())
        display = await self._controller.display(settings, preview=True)
        music = settings["music"]
        last = self._controller.last_display
        lines = [
            "Bot presence is global and visible across all its servers.",
            f"**Automation** · {'Enabled' if settings['enabled'] else 'Disabled'}",
            f"**Base profile** · {settings['selected']}",
            f"**Effective profile** · {display.profile}",
            f"**Schedule** · {display.rule or 'Base profile'}",
            f"**Availability** · {display.status}",
            f"**Rotation** · Every {settings['interval']} seconds",
            f"**Timezone** · {settings['timezone']}",
            f"**Profiles / rules** · {len(settings['profiles'])} / {len(settings['schedules'])}",
            f"**Music override** · {'Enabled' if music['enabled'] else 'Disabled'}",
            f"**Music server** · {music['guild_id'] or 'Not selected'}",
            f"**Preview** · {display.kind or 'No activity'}: {display.text or 'None'}",
            f"**Last sent** · {last.text or 'No activity' if last else 'No update sent this session'}",
            f"**Latest error** · {self._controller.last_error or 'None'}",
            "Edits apply to the base profile; schedules and active music can override its display.",
        ]
        await self._reply(ctx, "\n".join(lines), title="Status")

    @presence.command(name="progress", aliases=["show"], with_app_command=False)
    async def presence_progress(self, ctx):
        """Show global presence configuration and its current preview."""
        await self.presence.callback(self, ctx)

    @presence.command(name="help")
    async def presence_help(self, ctx):
        """List presence controls and template placeholders."""
        await self._reply(
            ctx,
            "**Messages** · `presence set <activity> <text>`, `add`, `list`, `remove <index>`, "
            "`clear`, `next`, `preview`\n"
            "**Automation** · `presence enable <true/false>`, `interval <seconds>`, "
            "`status <online/idle/dnd/invisible>`, `timezone <IANA zone>`\n"
            "**Profiles** · `presence profile create <name>`, `use <name>`, `list`, `delete <name>`\n"
            "**Schedules** · `presence schedule add <name> <profile> <HH:MM> <HH:MM> [days]`, "
            "`list`, `remove <name>`, `clear`\n"
            "**Music** · `presence music <true/false> [server_id]`, `musictext <text>`\n"
            "**Reset** · `presence reset` previews; `presence reset true` clears all settings.\n\n"
            "Activities: `custom`, `playing`, `listening`, `watching`, `competing`. "
            "Templates: `{servers}`, `{members}`, `{uptime}`, `{song}`, `{listeners}`. "
            "Days: `all` or `mon,tue,wed`. Slash controls use `/presence`.",
            title="Commands",
        )

    @presence.command(name="set")
    async def presence_set(self, ctx, activity: Activity, *, text: str):
        """Replace base-profile messages with one status and enable automation."""
        entry = {"kind": activity, "text": template(text)}

        def update(settings):
            self._profile(settings)["entries"] = [entry]
            settings["enabled"] = True

        await self._mutate(ctx, update)

    @presence.command(name="add")
    async def presence_add(self, ctx, activity: Activity, *, text: str):
        """Append a rotating base-profile message and enable automation."""
        entry = {"kind": activity, "text": template(text)}

        def update(settings):
            entries = self._profile(settings)["entries"]
            if len(entries) >= MAX_ENTRIES:
                raise ValueError(f"Keep at most {MAX_ENTRIES} messages in each profile.")
            entries.append(entry)
            settings["enabled"] = True

        await self._mutate(ctx, update)

    @presence.command(name="remove")
    async def presence_remove(self, ctx, index: int):
        """Remove a base-profile message by its one-based list number."""

        def update(settings):
            entries = self._profile(settings)["entries"]
            if not 1 <= index <= len(entries):
                raise ValueError("Choose a message number shown by presence list.")
            entries.pop(index - 1)

        await self._mutate(ctx, update)

    @presence.command(name="clear")
    async def presence_clear(self, ctx):
        """Clear base-profile activity messages, keeping its availability."""
        await self._mutate(ctx, lambda settings: self._profile(settings)["entries"].clear())

    @presence.command(name="list")
    async def presence_list(self, ctx):
        """List the editable base profile's saved rotating messages."""
        settings = validate(await self.config.settings())
        lines = [f"**Base profile** · {settings['selected']}"]
        lines.extend(
            f"**{index} · {entry['kind']}** · {entry['text']}"
            for index, entry in enumerate(self._profile(settings)["entries"], 1)
        )
        await self._reply(ctx, "\n".join(lines), title="Saved messages")

    @presence.command(name="interval")
    async def presence_interval(self, ctx, seconds: int):
        """Set message rotation to 60-86400 seconds."""
        await self._mutate(ctx, lambda settings: settings.update(interval=seconds))

    @presence.command(name="status")
    async def presence_status(self, ctx, availability: Availability):
        """Set the base profile's online, idle, dnd or invisible availability."""
        await self._mutate(
            ctx, lambda settings: self._profile(settings).update(status=availability)
        )

    @presence.command(name="enable")
    async def presence_enable(self, ctx, enabled: bool):
        """Enable saved automation or restore the pre-automation presence."""
        await self._mutate(ctx, lambda settings: settings.update(enabled=enabled))

    @presence.command(name="music")
    async def presence_music(self, ctx, enabled: bool, server_id: str = ""):
        """Use one selected server's active AudioPlus song as global listening status."""

        def update(settings):
            music = settings["music"]
            if enabled:
                if server_id:
                    if not server_id.isascii() or not server_id.isdecimal():
                        raise ValueError("Use the numeric source server ID.")
                    guild_id = int(server_id)
                else:
                    guild_id = ctx.guild.id if ctx.guild else music["guild_id"]
                guild = self.bot.get_guild(guild_id)
                audio = self.bot.get_cog("AudioPlus")
                if not guild:
                    raise ValueError(
                        "Choose a server Scarlet is in, or run this command in that server."
                    )
                if not callable(getattr(audio, "music_presence", None)):
                    raise ValueError(
                        "Install/update and reload AudioPlus first, then enable music status."
                    )
                music["guild_id"] = guild_id
                settings["enabled"] = True
            music["enabled"] = enabled

        await self._mutate(ctx, update)

    @presence.command(name="musictext")
    async def presence_musictext(self, ctx, *, text: str):
        """Set the listening override template, for example {song} with {listeners} listeners."""
        value = template(text)
        await self._mutate(ctx, lambda settings: settings["music"].update(text=value))

    @presence.command(name="preview")
    async def presence_preview(self, ctx):
        """Render the effective scheduled/music status without changing Discord presence."""
        settings = validate(await self.config.settings())
        display = await self._controller.display(settings, preview=True)
        await self._reply(
            ctx,
            f"**Profile** · {display.profile}\n**Rule** · {display.rule or 'Base profile'}\n"
            f"**Availability** · {display.status}\n**Activity** · {display.kind or 'None'}\n"
            f"**Text** · {display.text or 'None'}\n"
            f"**Music override** · {'Active' if display.music else 'Inactive'}\n"
            f"**Message** · {display.index + 1 if display.count else 0}/{display.count}\n"
            "This preview does not send a presence update or advance rotation.",
            title="Preview",
        )

    @presence.command(name="next")
    async def presence_next(self, ctx):
        """Advance the effective profile's rotation; active music still overrides it."""
        async with self.config.settings.get_lock():
            await self.cog_check(ctx)
            await check_command(ctx, ctx.command)
            settings = validate(await self.config.settings())
            if not settings["enabled"]:
                raise ValueError("Enable presence automation first.")
            await self._controller.advance(settings)
        await self._reply(
            ctx, "Rotation advanced. Presence updates within 15 seconds.", tone="success"
        )

    @presence.command(name="reset")
    async def presence_reset(self, ctx, confirm: bool = False):
        """Preview clearing all global presence settings; confirm explicitly with true."""
        if not confirm:
            await self._reply(
                ctx,
                "This clears all saved presence profiles, schedule rules and music settings, "
                "and disables automation. Run `presence reset true` to confirm.",
                title="Reset preview",
                tone="warning",
            )
            return
        await self._mutate(ctx, lambda settings: None, reset=True)

    @presence.command(name="timezone")
    async def presence_timezone(self, ctx, timezone: str):
        """Set the IANA timezone used for weekly status schedules."""
        zone(timezone)
        await self._mutate(ctx, lambda settings: settings.update(timezone=timezone))

    @presence.group(name="profile", invoke_without_command=True)
    async def presence_profile(self, ctx):
        """Save, select and remove named global status profiles."""
        await self.presence_profile_list.callback(self, ctx)

    @presence_profile.command(name="list")
    async def presence_profile_list(self, ctx):
        """List named profiles, their availability and rotating message counts."""
        settings = validate(await self.config.settings())
        await self._reply(
            ctx,
            "\n".join(
                f"**{key}** · {profile['status']} · {len(profile['entries'])} messages"
                + (" · Base profile" if key == settings["selected"] else "")
                for key, profile in settings["profiles"].items()
            ),
            title="Profiles",
        )

    @presence_profile.command(name="create")
    async def presence_profile_create(self, ctx, profile: str):
        """Save a copy of the base profile under a new name, without selecting it."""
        key = name(profile)

        def update(settings):
            if key in settings["profiles"]:
                raise ValueError("That profile already exists.")
            if len(settings["profiles"]) >= MAX_PROFILES:
                raise ValueError(f"Keep at most {MAX_PROFILES} profiles.")
            settings["profiles"][key] = deepcopy(self._profile(settings))

        await self._mutate(ctx, update)

    @presence_profile.command(name="use")
    async def presence_profile_use(self, ctx, profile: str):
        """Select the editable base profile and enable its automation."""
        key = name(profile)

        def update(settings):
            if key not in settings["profiles"]:
                raise ValueError("Choose a saved profile shown by presence profile list.")
            settings.update(selected=key, enabled=True)

        await self._mutate(ctx, update)

    @presence_profile.command(name="delete")
    async def presence_profile_delete(self, ctx, profile: str):
        """Delete a profile that is neither selected nor referenced by a schedule."""
        key = name(profile)

        def update(settings):
            if key not in settings["profiles"]:
                raise ValueError("That profile does not exist.")
            if key == settings["selected"] or any(
                rule["profile"] == key for rule in settings["schedules"].values()
            ):
                raise ValueError(
                    "Select another base profile and remove its schedules before deleting it."
                )
            del settings["profiles"][key]

        await self._mutate(ctx, update)

    @presence.group(name="schedule", invoke_without_command=True)
    async def presence_schedule(self, ctx):
        """Manage non-overlapping weekly profile windows in the configured timezone."""
        await self.presence_schedule_list.callback(self, ctx)

    @presence_schedule.command(name="list")
    async def presence_schedule_list(self, ctx):
        """List schedule windows; overnight rules use the starting weekday."""
        settings = validate(await self.config.settings())
        lines = [f"**Timezone** · {settings['timezone']}"]
        lines.extend(
            f"**{key}** · {describe_rule(rule)}" for key, rule in settings["schedules"].items()
        )
        await self._reply(ctx, "\n".join(lines), title="Schedules")

    @presence_schedule.command(name="add")
    async def presence_schedule_add(
        self, ctx, rule: str, profile: str, start: str, end: str, days: str = "all"
    ):
        """Add a named HH:MM profile window on all days or comma-separated weekdays."""
        key = name(rule)
        value = {
            "profile": name(profile),
            "start": minute(start),
            "end": minute(end),
            "days": weekdays(days),
        }

        def update(settings):
            if key in settings["schedules"]:
                raise ValueError("That rule already exists; remove it before changing its window.")
            if len(settings["schedules"]) >= MAX_SCHEDULES:
                raise ValueError(f"Keep at most {MAX_SCHEDULES} rules.")
            settings["schedules"][key] = value

        await self._mutate(ctx, update)

    @presence_schedule.command(name="remove")
    async def presence_schedule_remove(self, ctx, rule: str):
        """Remove one named schedule window."""
        key = name(rule)

        def update(settings):
            if key not in settings["schedules"]:
                raise ValueError("That schedule does not exist.")
            del settings["schedules"][key]

        await self._mutate(ctx, update)

    @presence_schedule.command(name="clear")
    async def presence_schedule_clear(self, ctx):
        """Remove all scheduled windows, returning to the base profile."""
        await self._mutate(ctx, lambda settings: settings["schedules"].clear())

    async def red_get_data_for_user(self, *, user_id):
        return {}

    async def red_delete_data_for_user(self, *, requester, user_id):
        # Settings are bot-owned global configuration, with no member attribution.
        return None
