"""Read-only checks of configured or candidate feature prerequisites."""

import discord
from redbot.core import commands

from .schema import MAX_FILE, server_id

FEATURES = {
    "playback": ("AudioPlus", "Music playback"),
    "welcome": ("CommunityPlus", "Welcome announcements"),
    "autorole": ("CommunityPlus", "Join roles"),
    "roles": ("CommunityPlus", "Self-service roles"),
    "onboarding": ("CommunityPlus", "Rules acceptance"),
    "voicerooms": ("CommunityPlus", "Temporary voice rooms"),
    "birthdays": ("CommunityPlus", "Birthday announcements"),
    "messagexp": ("LevelPlus", "Message XP"),
    "voicexp": ("LevelPlus", "Voice XP"),
    "levelrewards": ("LevelPlus", "Level reward roles"),
    "logging": ("LogPlus", "Event logging"),
    "logalerts": ("LogPlus", "Staff alerts and digests"),
    "transformations": ("OwoPlus", "Automatic message transformations"),
    "snapshots": ("SettingsHub", "Settings snapshots"),
    "emojis": ("EmojiStealerPlus", "External emoji capture"),
    "nativeevents": ("CommunityPlus", "Native Discord events"),
}


class ReadinessReport:
    def __init__(self, feature):
        self.feature = feature
        self.checks = []

    def add(self, name, ok, detail, *, required=True):
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail, "required": required})

    @property
    def ready(self):
        return all(item["ok"] for item in self.checks if item["required"])

    def text(self):
        label = "Ready" if self.ready else "Needs attention"
        if self.ready and any(not item["ok"] for item in self.checks):
            label = "Ready with notes"
        lines = [f"**{FEATURES[self.feature][1]} · {label}**"]
        for item in self.checks:
            marker = "✓" if item["ok"] else "✕" if item["required"] else "!"
            lines.append(f"{marker} **{item['name']}** · {item['detail']}")
        return "\n".join(lines)


def check_intent(report, bot, key, label, *, required=True):
    ready = getattr(bot.intents, key, False)
    report.add(
        label,
        ready,
        "Enabled"
        if ready
        else "Enable this intent in Red and, when privileged, Discord's developer portal.",
        required=required,
    )


def check_permissions(report, guild, channel, permissions):
    current = channel.permissions_for(guild.me)
    for key in permissions:
        allowed = getattr(current, key, False)
        report.add(
            key.replace("_", " ").title(),
            allowed,
            f"{channel.mention}: {'allowed' if allowed else 'missing'}",
        )
    return current


def check_text_channel(report, guild, channel, *, permissions=()):
    valid = (
        isinstance(channel, (discord.TextChannel, discord.Thread)) and channel.guild.id == guild.id
    )
    report.add(
        "Destination",
        valid,
        channel.mention
        if valid
        else "Select or configure an available text channel in this server.",
    )
    if not valid:
        return False
    send = "send_messages_in_threads" if isinstance(channel, discord.Thread) else "send_messages"
    current = check_permissions(report, guild, channel, ("view_channel", send, *permissions))
    report.add(
        "Embed Links",
        current.embed_links,
        f"{channel.mention}: {'available' if current.embed_links else 'text fallback will be used'}",
        required=False,
    )
    if isinstance(channel, discord.Thread) and channel.archived and channel.locked:
        report.add(
            "Locked archived thread",
            current.manage_threads,
            "Manage Threads is needed to reopen this locked thread.",
        )
    return True


def check_role(report, guild, role):
    if role is None or role.guild.id != guild.id:
        report.add("Role", False, "Select or configure an available role in this server.")
        return
    report.add(
        "Manage Roles",
        guild.me.guild_permissions.manage_roles,
        "Required to assign/remove this role.",
    )
    report.add(
        "Role hierarchy",
        not role.is_default() and not role.managed and role < guild.me.top_role,
        f"{role.mention} must be unmanaged, below my highest role and different from @everyone.",
    )
    try:
        server_id(guild, role.id, "readiness.role", role=True, grant=True)
    except commands.BadArgument:
        report.add(
            "Role grant policy",
            False,
            f"{role.mention} fails the shared automatic/self-service role grant policy; remove management/moderation permissions or choose a member role.",
        )
    else:
        report.add(
            "Role grant policy",
            True,
            f"{role.mention} passes the automatic/self-service grant policy.",
        )


class ReadinessCommands:
    async def _feature_readiness(self, ctx, feature, *, channel=None, voice=None, role=None):
        name, _ = FEATURES[feature]
        report = ReadinessReport(feature)
        cog = self if name == "SettingsHub" else self.bot.get_cog(name)
        if cog is None:
            report.add("Cog", False, f"Load {name.lower()} first.")
            return report
        if cog is not self:
            await self._source_check(ctx, cog)
        report.add("Cog", True, name.removesuffix("Plus") + " is loaded and available to you.")
        if ctx.guild.me is None:
            report.add("Bot membership", False, "The bot's server member is unavailable.")
            return report
        group = cog.config.guild(ctx.guild)
        find = ctx.guild.get_channel_or_thread

        if feature == "playback":
            current = getattr(getattr(ctx.author, "voice", None), "channel", None)
            destination = voice or current
            if destination is None:
                try:
                    destination = cog._busiest_voice_channel(ctx.guild)
                except commands.UserInputError:
                    pass
            valid = (
                isinstance(destination, (discord.VoiceChannel, discord.StageChannel))
                and destination.guild.id == ctx.guild.id
            )
            report.add(
                "Voice destination",
                valid,
                destination.mention if valid else "Choose an available voice channel with room.",
            )
            if valid:
                perms = check_permissions(
                    report, ctx.guild, destination, ("view_channel", "connect", "speak")
                )
                connected = ctx.guild.voice_client and ctx.guild.voice_client.channel == destination
                report.add(
                    "Channel capacity",
                    not destination.user_limit
                    or len(destination.members) < destination.user_limit
                    or perms.move_members
                    or connected,
                    "The channel must have room, or allow Move Members to bypass its limit.",
                )
                state = ctx.guild.me.voice
                if state and state.channel == destination:
                    report.add(
                        "Bot voice flags",
                        not state.mute and not state.self_mute,
                        "Clear server/self mute before playback.",
                    )
                    if isinstance(destination, discord.StageChannel):
                        report.add(
                            "Stage speaking",
                            not state.suppress or perms.mute_members,
                            "A Stage moderator must allow the bot to speak if it remains in the audience.",
                        )
                elif isinstance(destination, discord.StageChannel):
                    report.add(
                        "Stage admission",
                        perms.mute_members,
                        "A Stage moderator may need to approve the bot's request to speak.",
                        required=False,
                    )
            check_intent(report, self.bot, "voice_states", "Voice state intent")
            diagnostics = await cog.diagnostic_report(ctx.guild.id)
            report.add(
                "Native voice APIs / Opus",
                not diagnostics["voice_error"],
                diagnostics["voice_error"] or "Native voice prerequisites detected.",
            )
            report.add(
                "FFmpeg",
                diagnostics["ffmpeg"] not in {"missing", "unavailable", "timed out"},
                diagnostics["ffmpeg"],
            )
            for package in ("yt-dlp", "yt-dlp-ejs"):
                version = diagnostics["packages"].get(package, "missing")
                report.add(package, version != "missing", version)
            report.add(
                "YouTube JavaScript runtime",
                bool(diagnostics["runtimes"]),
                ", ".join(diagnostics["runtimes"])
                or "Install Deno 2.3+, Node.js 22+ or a supported QuickJS runtime.",
            )

        elif feature == "emojis":
            conf = await group.capture()
            report.add(
                "Create Expressions",
                ctx.guild.me.guild_permissions.create_expressions,
                "Required to copy custom emoji into this server.",
            )
            check_intent(report, self.bot, "message_content", "Message Content intent")
            check_intent(report, self.bot, "guild_messages", "Server message intent")
            if conf["reactions"]:
                check_intent(report, self.bot, "guild_reactions", "Server reaction intent")
            destination = channel or find(conf["channel"]) if conf["channel"] or channel else None
            if destination is not None or conf["channel"]:
                valid = (
                    isinstance(destination, (discord.TextChannel, discord.Thread))
                    and destination.guild.id == ctx.guild.id
                )
                report.add(
                    "Capture channel",
                    valid,
                    destination.mention
                    if valid
                    else "Choose an available text channel in this server.",
                )
                if valid:
                    perms = check_permissions(report, ctx.guild, destination, ("view_channel",))
                    if conf["notify"]:
                        send = (
                            "send_messages_in_threads"
                            if isinstance(destination, discord.Thread)
                            else "send_messages"
                        )
                        report.add(
                            "Copy notifications",
                            getattr(perms, send),
                            "Sending notices is optional; emoji capture does not require it.",
                            required=False,
                        )
            counts = {False: 0, True: 0}
            for emoji in ctx.guild.emojis:
                counts[emoji.animated] += 1
            for animated, label in ((False, "Static emoji slots"), (True, "Animated emoji slots")):
                report.add(
                    label,
                    counts[animated] < ctx.guild.emoji_limit,
                    f"{counts[animated]} / {ctx.guild.emoji_limit} cached slots used.",
                    required=False,
                )
            report.add(
                "Capture policy",
                conf["enabled"],
                "Automatic capture is enabled."
                if conf["enabled"]
                else "Automatic capture is paused; yoink remains available.",
                required=False,
            )

        elif feature == "nativeevents":
            conf = (await group.community_tools())["native_events"]
            destination = voice or find(conf["channel"]) if conf["channel"] or voice else None
            if destination is not None or conf["channel"]:
                valid = (
                    isinstance(destination, discord.VoiceChannel)
                    and destination.guild.id == ctx.guild.id
                )
                report.add(
                    "Event voice channel",
                    valid,
                    destination.mention
                    if valid
                    else "Choose an ordinary voice channel in this server.",
                )
                if valid:
                    check_permissions(
                        report, ctx.guild, destination, ("view_channel", "connect", "create_events")
                    )
            else:
                report.add(
                    "Create Events",
                    getattr(ctx.guild.me.guild_permissions, "create_events", False),
                    "Required for an external native Discord event.",
                )
                check_text_channel(report, ctx.guild, channel or ctx.channel)
            check_intent(report, self.bot, "guild_scheduled_events", "Scheduled event intent")
            report.add(
                "Automatic native events",
                conf["enabled"],
                "Enabled for new events."
                if conf["enabled"]
                else "Off by default; use event native for an existing event.",
                required=False,
            )

        elif feature == "welcome":
            check_intent(report, self.bot, "members", "Server Members intent")
            conf = await group.welcome()
            check_text_channel(report, ctx.guild, channel or find(conf["channel_id"]))

        elif feature in {"autorole", "roles", "onboarding", "birthdays"}:
            check_intent(report, self.bot, "members", "Server Members intent")
            if feature == "autorole":
                conf = await group.autorole()
                check_role(report, ctx.guild, role or ctx.guild.get_role(conf["role_id"]))
            elif feature == "roles":
                conf = await group.features()
                candidates = (
                    [role] if role else [ctx.guild.get_role(rid) for rid in conf["self_roles"]]
                )
                report.add(
                    "Role offers", bool(candidates), "Configure roles with community rolemenu add."
                )
                for candidate in candidates:
                    check_role(report, ctx.guild, candidate)
                check_text_channel(report, ctx.guild, channel or ctx.channel)
            else:
                conf = (await group.community_tools())[
                    feature if feature == "birthdays" else "onboarding"
                ]
                if feature == "onboarding":
                    report.add(
                        "Rules text",
                        bool(conf["rules"].strip()),
                        "Configure rules with onboard configure <role> <rules>.",
                    )
                    check_role(report, ctx.guild, role or ctx.guild.get_role(conf["role"]))
                    check_text_channel(report, ctx.guild, channel or ctx.channel)
                else:
                    check_text_channel(report, ctx.guild, channel or find(conf["channel"]))
                    if role or conf["role"]:
                        check_role(report, ctx.guild, role or ctx.guild.get_role(conf["role"]))

        elif feature == "voicerooms":
            conf = await group.community_tools()
            hub = voice or find(conf["voice_hub"])
            valid = isinstance(hub, discord.VoiceChannel) and hub.guild.id == ctx.guild.id
            report.add(
                "Voice hub",
                valid,
                hub.mention if valid else "Configure an ordinary hub with voiceroom configure.",
            )
            check_intent(report, self.bot, "voice_states", "Voice state intent")
            if valid:
                check_permissions(
                    report, ctx.guild, hub, ("view_channel", "connect", "move_members")
                )
                category = find(conf["voice_category"]) if conf["voice_category"] else hub.category
                if conf["voice_category"]:
                    report.add(
                        "Room category",
                        isinstance(category, discord.CategoryChannel),
                        "The configured room category must still exist.",
                    )
                permissions = (
                    category.permissions_for(ctx.guild.me)
                    if category
                    else ctx.guild.me.guild_permissions
                )
                report.add(
                    "Create rooms",
                    permissions.manage_channels,
                    f"Manage Channels is required in {category.mention if category else 'the server'}.",
                )
                report.add(
                    "Room admission",
                    permissions.view_channel and permissions.connect,
                    "The category must allow the bot to view/connect to created rooms.",
                )

        elif feature in {"messagexp", "voicexp"}:
            check_intent(
                report,
                self.bot,
                "guild_messages" if feature == "messagexp" else "voice_states",
                "Message events" if feature == "messagexp" else "Voice state intent",
            )
            if feature == "messagexp":
                conf = await group.message()
                policies = await group.xp_features()
                needs_content = (
                    conf["mode"] == "perword" or policies["min_words"] or policies["repeat_seconds"]
                )
                check_intent(
                    report,
                    self.bot,
                    "message_content",
                    "Message Content intent",
                    required=bool(needs_content),
                )
            else:
                check_intent(report, self.bot, "members", "Member cache", required=False)

        elif feature == "levelrewards":
            conf = await group.rewards()
            goals = (await group.progress_settings())["goals"]
            ids = set(conf["roles"]) | {
                str(goal["role"]) for goal in goals.values() if goal["role"]
            }
            candidates = [role] if role else [ctx.guild.get_role(int(rid)) for rid in sorted(ids)]
            report.add(
                "Reward roles",
                bool(candidates),
                "Configure level rewards add or achievement create with a role.",
            )
            check_intent(report, self.bot, "members", "Server Members intent")
            for candidate in candidates:
                check_role(report, ctx.guild, candidate)

        elif feature in {"logging", "logalerts"}:
            if feature == "logalerts":
                target = (await group.alert_settings())["channel"]
                check_text_channel(report, ctx.guild, channel or find(target))
            else:
                destinations = [channel or find(await group.log_channel())]
                destinations.extend(find(cid) for cid in (await group.overrides()).values())
                destinations.extend(
                    find(cid) for cid in (await group.features())["routes"].values()
                )
                for destination in dict.fromkeys(destinations):
                    check_text_channel(report, ctx.guild, destination)
            report.add(
                "Audit actors",
                ctx.guild.me.guild_permissions.view_audit_log,
                "View Audit Log improves attribution; events can still be logged with an unknown actor.",
                required=False,
            )
            check_intent(report, self.bot, "message_content", "Message text access", required=False)
            check_intent(report, self.bot, "members", "Member event access", required=False)

        elif feature == "transformations":
            conf = await group.features()
            destination = channel or ctx.channel
            check_text_channel(
                report, ctx.guild, destination, permissions=("manage_messages", "manage_webhooks")
            )
            ids = {destination.id, getattr(destination, "parent_id", None)}
            allowed = not ids.intersection(conf["excluded"]) and (
                conf["channel_mode"] == "all" or ids.intersection(conf["allowed"])
            )
            report.add(
                "Transformation scope",
                allowed,
                "The selected channel/parent must be allowed and not excluded.",
            )
            check_intent(report, self.bot, "message_content", "Message Content intent")

        elif feature == "snapshots":
            import json

            try:
                bundle = await self._backup_bundle(ctx)
            except (commands.CheckFailure, commands.BadArgument) as error:
                report.add("Accessible source cogs", False, str(error))
                return report
            report.add(
                "Accessible source cogs",
                bool(bundle["cogs"]),
                ", ".join(name.removesuffix("Plus") for name in bundle["cogs"])
                or "Load a source cog you can configure.",
            )
            size = len(json.dumps(bundle, sort_keys=True, ensure_ascii=False).encode())
            report.add("Snapshot budget", size <= MAX_FILE, f"{size} / {MAX_FILE} bytes.")
        return report

    async def _readiness_reply(self, ctx, feature, *, channel=None, voice=None, role=None):
        if feature != "all" and feature not in FEATURES:
            raise commands.BadArgument("Choose all or one of: " + ", ".join(FEATURES))
        reports = []
        for key in FEATURES if feature == "all" else (feature,):
            try:
                reports.append(
                    await self._feature_readiness(ctx, key, channel=channel, voice=voice, role=role)
                )
            except (commands.CheckFailure, commands.DisabledCommand):
                if feature != "all":
                    raise
            except commands.BadArgument as error:
                report = ReadinessReport(key)
                report.add("Configuration", False, str(error))
                reports.append(report)
        await self._reply(
            ctx,
            "\n\n".join(report.text() for report in reports)
            + "\n\nChecks inspect local prerequisites and current permissions. They do not connect to voice, play media, assign roles or enable features. Use audiocheck now for the configured live playback probe.",
            title="Feature readiness",
            tone="success" if reports and all(report.ready for report in reports) else "warning",
        )
