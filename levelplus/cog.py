from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import random
import re
import time
from collections import OrderedDict, defaultdict
from contextlib import AsyncExitStack
from fractions import Fraction
from heapq import nlargest
from types import SimpleNamespace
from typing import List, Optional, Tuple

import discord
from discord.ext import commands, tasks
from redbot.core import app_commands
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config

from .command_support import (
    attach_prefix_groups,
    finish_configuration_audit,
    invoke_shortcut,
    prefix_group,
    prepare_hybrid,
)
from .constants import DEFAULTS_GUILD, WORD_RE
from .events import guild_enabled
from .features import (
    FEATURE_DEFAULTS_GUILD,
    boosted_amount,
    day_at,
    forget_user,
    message_allowed,
    period_totals,
    record_period,
    safe_role,
    user_periods,
    valid_timezone,
)
from .interactive import SetupView, close_views
from .levels import cumulative_xp, level_from_xp
from .milestones import MILESTONE_DEFAULTS, MilestoneCommands
from .presentation import Presentation, settings
from .progression import CALENDAR_DEFAULTS, PROGRESS_SETTINGS, ProgressionCommands
from .reward_preview import build_preview, preview_policy, reward_changes

log = logging.getLogger(__name__)


class LevelPlus(ProgressionCommands, MilestoneCommands, redcommands.Cog):
    """Member XP, levels, leaderboards, and announcements."""

    async def cog_command_error(self, ctx, error):
        await self._presentation.command_error(ctx, error)

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    def __init__(self, bot: Red) -> None:
        attach_prefix_groups(self)
        self.bot: Red = bot
        self._presentation = Presentation("LevelPlus", "level")
        self.config: Config = Config.get_conf(self, identifier=0x1EAF01, force_registration=True)
        self.config.register_guild(
            **DEFAULTS_GUILD,
            **FEATURE_DEFAULTS_GUILD,
            milestone_settings=MILESTONE_DEFAULTS,
            milestones={},
            progress_settings=PROGRESS_SETTINGS,
            progress={},
            season_calendar=CALENDAR_DEFAULTS,
        )

        self._settings_cache = {}
        self._settings_locks = defaultdict(asyncio.Lock)
        self._last_msg = OrderedDict()
        self._last_rxn = OrderedDict()
        self._last_voice = OrderedDict()
        self._recent_messages = OrderedDict()
        self._reaction_once = OrderedDict()
        self._reward_locks = defaultdict(asyncio.Lock)
        self._views = set()
        self._closing = False
        self._card_slots = asyncio.Semaphore(2)
        self._card_tasks = set()
        self._progress_task = None
        self._progress_log = log

    async def cog_unload(self) -> None:
        self._closing = True
        if self._progress_task:
            self._progress_task.cancel()
            await asyncio.gather(self._progress_task, return_exceptions=True)
        for task in tuple(self._card_tasks):
            task.cancel()
        await asyncio.gather(*self._card_tasks, return_exceptions=True)
        self._card_tasks.clear()
        self.voice_tick.cancel()
        await close_views(self)

    # ---------- helpers ----------
    async def _g(self, guild: discord.Guild):
        return await self.config.guild(guild).all()

    async def _lin(self, guild: discord.Guild):
        lin = await self.config.guild(guild).linear()
        return float(lin.get("base", 83.2)), float(lin.get("inc", 100.433))

    async def _remember_name(self, guild, member):
        group = self.config.guild(guild)
        uid = str(member.id)
        async with group.names.get_lock():
            if await group.get_raw("names", uid, default=None) is None:
                await group.set_raw("names", uid, value=member.display_name[:100])

    async def _get_xp(self, guild, user_id):
        return int(await self.config.guild(guild).get_raw("xp", str(user_id), default=0))

    async def _set_xp(self, guild, user_id, value):
        group = self.config.guild(guild)
        async with group.xp.get_lock():
            await group.set_raw("xp", str(user_id), value=max(0, int(value)))
        member = guild.get_member(user_id)
        if member:
            await self._sync_rewards(member)

    async def _add_xp(self, guild, user, amount, *, source=None, channel=None):
        return (await self._award_batch(guild, [(user, amount, channel)], source))[0][1:]

    async def _award_batch(self, guild, updates, source):
        settings = await self._settings(guild)
        group = self.config.guild(guild)
        features = settings["xp_features"]
        now = time.time()
        day = day_at(now, features["timezone"])
        results = []
        async with group.xp.get_lock():
            async with AsyncExitStack() as stack:
                cap = (
                    await stack.enter_async_context(group.earned_today())
                    if source and features["daily_cap"]
                    else None
                )
                periods = (
                    await stack.enter_async_context(group.period_xp())
                    if source and features["periods"]
                    else None
                )
                if periods is not None:
                    await self._roll_month(guild, periods, settings, day, now)
                if cap is not None and cap["day"] != day.isoformat():
                    cap.update(day=day.isoformat(), xp={})
                data = await group.xp() if len(updates) > 1 else None
                for member, amount, channel in updates:
                    uid = str(member.id)
                    amount = (
                        boosted_amount(amount, member, channel, features, now)
                        if source
                        else max(0, int(amount))
                    )
                    if cap is not None:
                        amount = min(amount, max(0, features["daily_cap"] - cap["xp"].get(uid, 0)))
                        cap["xp"][uid] = cap["xp"].get(uid, 0) + amount
                    old_xp = (
                        int(data.get(uid, 0))
                        if data is not None
                        else await self._get_xp(guild, member.id)
                    )
                    new_xp = old_xp + amount
                    if source:
                        budget = (
                            max(0, features["daily_cap"] - cap["xp"].get(uid, 0))
                            if cap is not None
                            else None
                        )
                        bonus = await self._milestone_award(
                            guild, member, amount, source, day, now, old_xp, settings, budget
                        )
                        remaining = max(0, budget - bonus) if budget is not None else None
                        bonus += await self._progress_award(
                            guild,
                            member,
                            amount,
                            source,
                            day,
                            now,
                            new_xp + bonus,
                            settings,
                            remaining,
                        )
                        amount += bonus
                        new_xp += bonus
                        if cap is not None:
                            cap["xp"][uid] += bonus
                    if amount:
                        if data is not None:
                            data[uid] = new_xp
                        else:
                            await group.set_raw("xp", uid, value=new_xp)
                        if periods is not None:
                            record_period(periods, uid, amount, day, now)
                    results.append(
                        (member, self._level(old_xp, settings), self._level(new_xp, settings))
                    )
                if data is not None:
                    await group.xp.set(data)
        return results

    async def _sync_rewards(self, member):
        settings = await self._settings(member.guild)
        rewards = settings["rewards"]
        if (
            (
                not rewards["roles"]
                and not any(
                    goal["role"] for goal in settings["progress_settings"]["goals"].values()
                )
            )
            or not member.guild.me
            or not member.guild.me.guild_permissions.manage_roles
        ):
            return
        async with self._reward_locks[(member.guild.id, member.id)]:
            level = await self.current_level(member.guild, member.id)
            add, remove, _ = reward_changes(
                member, level, rewards, await self._custom_reward_roles(member, settings)
            )
            try:
                if add:
                    await member.add_roles(*add, reason="Level milestone rewards")
                if remove:
                    await member.remove_roles(*remove, reason="Level milestone rewards")
            except discord.HTTPException:
                log.debug("Could not synchronize milestone roles", exc_info=True)

    async def current_level(self, guild, user_id):
        return self._level(await self._get_xp(guild, user_id), await self._settings(guild))

    async def maybe_announce_levelup(
        self, guild: discord.Guild, member: discord.Member, old: int, new: int
    ) -> None:
        await self._sync_rewards(member)
        if new <= old:
            return
        conf = (await self._settings(guild))["levelup"]
        if not conf["enabled"]:
            return
        ch: Optional[discord.TextChannel] = None
        cid = conf.get("channel_id")
        if cid:
            ch = guild.get_channel(cid) if isinstance(cid, int) else guild.get_channel(int(cid))
            if not isinstance(ch, discord.TextChannel):
                ch = None
        if not ch:
            ch = guild.system_channel
        if not ch:
            return
        template = conf.get("template", "{user.mention} leveled up to **{user.level}**!")
        u = SimpleNamespace(
            **{
                "mention": member.mention,
                "name": member.display_name,
                "level": new,
                "xp": await self._get_xp(guild, member.id),
            }
        )
        try:
            msg = template.format(user=u)
        except Exception:
            msg = f"{member.mention} has reached level **{new}**!"
        embed = self._levelup_card(member, new, u.xp)
        try:
            await self._presentation.send(ch, embed=embed, notification=msg, allowed_mentions=None)
        except discord.HTTPException:
            log.debug("Level-up announcement could not be sent", exc_info=True)

    def _levelup_card(self, member, level, xp, *, preview=False):
        embed = self._presentation.embed(
            "Level up preview" if preview else "Level up", tone="success"
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=f"{level:,}")
        embed.add_field(name="Total XP", value=f"{xp:,}")
        return embed

    # ---------- listeners ----------
    @commands.Cog.listener()
    @guild_enabled
    async def on_message(self, message):
        if not message.guild or message.author.bot:
            return
        settings = await self._settings(message.guild)
        conf = settings["message"]
        if (
            not conf["enabled"]
            or conf["mode"] == "none"
            or not self._eligible(message.author, message.channel, settings)
        ):
            return
        minimum = max(0, int(conf["min"]))
        maximum = max(minimum, int(conf["max"]))
        if conf["mode"] == "random":
            amount = random.randint(minimum, maximum)
        else:
            words = sum(1 for _ in WORD_RE.finditer(message.content or ""))
            amount = words * max(1, minimum)
            if maximum:
                amount = min(amount, maximum)
        if amount <= 0:
            return
        if not message_allowed(
            self._recent_messages,
            message.guild.id,
            message.author.id,
            message.content or "",
            settings["xp_features"],
            time.time(),
            remember=False,
        ):
            return
        if not self._cooldown(
            self._last_msg, (message.guild.id, message.author.id), int(conf["cooldown"])
        ):
            return
        message_allowed(
            self._recent_messages,
            message.guild.id,
            message.author.id,
            message.content or "",
            settings["xp_features"],
            time.time(),
        )
        await self._remember_name(message.guild, message.author)
        old, new = await self._add_xp(
            message.guild, message.author, amount, source="message", channel=message.channel
        )
        await self.maybe_announce_levelup(message.guild, message.author, old, new)

    @commands.Cog.listener()
    @guild_enabled
    async def on_raw_reaction_add(self, payload):
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        settings = await self._settings(guild)
        conf = settings["reaction"]
        awards = conf["awards"]
        channel = guild.get_channel_or_thread(payload.channel_id)
        reactor = payload.member or guild.get_member(payload.user_id)
        if (
            not conf["enabled"]
            or awards == "none"
            or not self._eligible(reactor, channel, settings)
        ):
            return
        if not self._cooldown(self._last_rxn, (guild.id, payload.user_id), int(conf["cooldown"])):
            return
        if settings["xp_features"]["reaction_once"]:
            key = (guild.id, payload.user_id, payload.message_id)
            now = time.monotonic()
            if now - self._reaction_once.get(key, -86401) < 86400:
                return
            self._reaction_once[key] = now
            while len(self._reaction_once) > 50000:
                self._reaction_once.popitem(last=False)
        targets = [reactor] if awards in {"both", "reactor"} else []
        if awards in {"both", "author"}:
            message = discord.utils.get(self.bot.cached_messages, id=payload.message_id)
            if message is None and isinstance(channel, (discord.TextChannel, discord.Thread)):
                try:
                    message = await channel.fetch_message(payload.message_id)
                except discord.HTTPException:
                    log.debug(
                        "Reaction message was unavailable; reactor XP can still be awarded",
                        exc_info=True,
                    )
            if (
                message is not None
                and message.author.id != payload.user_id
                and self._eligible(message.author, channel, settings)
            ):
                targets.append(message.author)
        value = random.randint(max(0, int(conf["min"])), max(0, int(conf["min"]), int(conf["max"])))
        for member in targets:
            await self._remember_name(guild, member)
            old, new = await self._add_xp(guild, member, value, source="reaction", channel=channel)
            await self.maybe_announce_levelup(guild, member, old, new)

    @tasks.loop(seconds=20.0)
    async def voice_tick(self):
        for guild in self.bot.guilds:
            try:
                if await self.bot.cog_disabled_in_guild(self, guild):
                    continue
                settings = await self._settings(guild)
                conf = settings["voice"]
                if not conf["enabled"]:
                    continue
                updates = []
                for channel in guild.voice_channels:
                    humans = [m for m in channel.members if not m.bot]
                    if len(humans) < int(conf["min_members"]):
                        continue
                    for member in humans:
                        # Text-in-voice settings do not disable voice participation itself.
                        restrictions = settings["restrictions"]
                        if channel.id in restrictions["no_channels"] or any(
                            r.id in restrictions["no_roles"] for r in member.roles
                        ):
                            continue
                        vs = member.voice
                        if conf["anti_afk"] and (
                            channel == guild.afk_channel
                            or not vs
                            or vs.afk
                            or vs.self_mute
                            or vs.mute
                            or vs.self_deaf
                            or vs.deaf
                        ):
                            continue
                        if self._cooldown(
                            self._last_voice,
                            (guild.id, member.id),
                            max(15, int(conf["cooldown"])),
                        ):
                            amount = random.randint(
                                max(0, int(conf["min"])),
                                max(0, int(conf["min"]), int(conf["max"])),
                            )
                            if amount:
                                updates.append((member, amount, channel))
                if not updates:
                    continue
                announcements = await self._award_batch(guild, updates, "voice")
                for member, old, new in announcements:
                    await self.maybe_announce_levelup(guild, member, old, new)
            except Exception:
                log.exception("Voice XP tick failed for guild %s", guild.id)

    @voice_tick.before_loop
    async def _before_voice(self):
        await self.bot.wait_until_red_ready()

    @commands.Cog.listener()
    @guild_enabled
    async def on_interaction(self, interaction):
        if (
            interaction.type is not discord.InteractionType.application_command
            or not interaction.guild
        ):
            return
        settings = await self._settings(interaction.guild)
        if not settings["restrictions"]["slash_command_xp"] or not self._eligible(
            interaction.user, interaction.channel, settings
        ):
            return
        if not self._cooldown(
            self._last_msg,
            (interaction.guild.id, interaction.user.id),
            int(settings["message"]["cooldown"]),
        ):
            return
        await self._remember_name(interaction.guild, interaction.user)
        old, new = await self._add_xp(
            interaction.guild,
            interaction.user,
            max(1, int(settings["message"]["min"])),
            source="slash",
            channel=interaction.channel,
        )
        await self.maybe_announce_levelup(interaction.guild, interaction.user, old, new)

    # ---------- commands ----------
    @redcommands.hybrid_group(name="level", invoke_without_command=True, fallback="status")
    @redcommands.guild_only()
    async def level(self, ctx: redcommands.Context):
        """Manage XP, levels, leaderboards, and announcements.

        Run this command alone to see XP settings. Use show or leaderboard for member levels,
        and the help subcommand for the command overview.
        """
        g = await self._g(ctx.guild)
        base, inc = await self._lin(ctx.guild)

        def enabled(value):
            return "Enabled" if value else "Disabled"

        e = self._presentation.embed("Status", "XP sources, level progression, and announcements.")
        e.add_field(
            name="Level progression",
            value=f"**Curve** · {g['curve'].title()}\n**Multiplier** · {g['multiplier']:g}×\n"
            f"**Maximum level** · {g['max_level'] or 'Unlimited'}\n"
            f"**Linear base / increment** · {base:.3f} / {inc:.3f}",
            inline=False,
        )
        for key, label in (
            ("message", "Message XP"),
            ("reaction", "Reaction XP"),
            ("voice", "Voice XP"),
        ):
            source = g[key]
            value = f"**Status** · {enabled(source['enabled'])}\n**Award** · {source['min']}–{source['max']} XP\n**Cooldown** · {source['cooldown']}s"
            if key == "message":
                value += f"\n**Mode** · {source['mode']}"
            elif key == "reaction":
                value += f"\n**Recipients** · {source['awards']}"
            else:
                value += f"\n**Minimum members** · {source['min_members']}\n**AFK protection** · {enabled(source['anti_afk'])}"
            e.add_field(name=label, value=value, inline=True)
        r = g["restrictions"]
        e.add_field(
            name="XP eligibility",
            value=f"**Excluded channels / roles** · {len(r['no_channels'])} / {len(r['no_roles'])}\n"
            f"**Threads** · {enabled(r['thread_xp'])}\n**Forums** · {enabled(r['forum_xp'])}\n"
            f"**Text in voice** · {enabled(r['text_in_voice_xp'])}\n**Slash commands** · {enabled(r['slash_command_xp'])}",
            inline=False,
        )
        lu = g["levelup"]
        channel = f"<#{lu['channel_id']}>" if lu["channel_id"] else "Current channel"
        e.add_field(
            name="Level-up announcements",
            value=f"**Status** · {enabled(lu['enabled'])}\n**Channel** · {channel}\n**Template**\n{lu['template']}",
            inline=False,
        )
        e.add_field(name="Members tracked", value=f"{len(g['xp']):,}", inline=True)
        await self._reply(ctx, embed=e)

    @level.command(name="help")
    async def level_help(self, ctx: redcommands.Context):
        """Show the leveling command overview."""
        p = ctx.clean_prefix
        e = discord.Embed(title="LevelPlus - Commands", color=discord.Color.blurple())
        e.description = f"Commands and examples use `{p}` as prefix."
        e.add_field(
            name="Achievements and challenges",
            value=f"`{p}achievements [@Member]` · `{p}challenges [@Member]` · `{p}rankcard [@Member]`\n`{p}level badges <enabled>` · `{p}level challenges enabled <enabled>` · `{p}level challenges goal <metric> <target> <reward>`",
            inline=False,
        )
        e.add_field(
            name="Core",
            value=f"• `{p}level` • `{p}level help` • `{p}level diag`\n• `{p}rank [@user]` • `{p}leaderboard [N]` • `{p}levellookup <query>`\n• `{p}level testmsg [@user]` • `{p}level testup [@user] [levels]`",
            inline=False,
        )
        e.add_field(
            name="Slash commands",
            value="Use `/rank`, `/leaderboard`, or `/level status`. `/level` also contains XP, formula, source, and announcement settings with the same permission checks.",
            inline=False,
        )
        e.add_field(
            name="Formula & Scale",
            value=f"• `{p}level formula curve <linear|exponential|constant>`\n• `{p}level formula multiplier <float>` • `{p}level formula maxlevel <0|N>`\n• `{p}level formula preset arcane` • `{p}level formula calibrate <L1> <XP1> <L2> <XP2>`\n• `{p}level formula linear base <float>` • `linear inc <float>`",
            inline=False,
        )
        e.add_field(
            name="Message XP",
            value=f"• `{p}level message enable [true|false]` • `mode <perword|random|none>`\n• `min <n>` `max <n>` `cooldown <sec>`",
            inline=False,
        )
        e.add_field(
            name="Reaction XP",
            value=f"• `{p}level reaction enable [true|false]` • `awards <both|author|reactor|none>`\n• `min <n>` `max <n>` `cooldown <sec>`",
            inline=False,
        )
        e.add_field(
            name="Voice XP",
            value=f"• `{p}level voice enable [true|false]` • `range <min> <max>` • `cooldown <sec>`\n• `minmembers <n>` • `antiafk [true|false]`",
            inline=False,
        )
        e.add_field(
            name="Restrictions",
            value=f"• `{p}level restrict nochannels add|remove|list|clear <#ch>`\n• `{p}level restrict noroles add|remove|list|clear <@role>`\n• `{p}level restrict toggles <threadxp|forumxp|textvoicexp|slashxp> [true|false]`",
            inline=False,
        )
        e.add_field(
            name="Level-up Message",
            value=f"• `{p}level levelup enable [true|false]` • `channel [#ch|none]`\n• `template <text with {{user.*}}>`",
            inline=False,
        )
        e.add_field(
            name="XP Admin & Migration",
            value=f"• `{p}level xp set @user <amt>` • `add @user <amt>` • `setid <id> <amt>`\n• `exportcsv` • `importcsv` • `importlines`\n• `remove @user` • `removeid <id>` • `purgebots` • `clear yes`",
            inline=False,
        )
        e.add_field(
            name="Lookup & Aliases",
            value=f"• `{p}level lookup <name|@|id>`\n• `{p}level name set @user <alias>` • `name setid <id> <alias>` • `name get <id>`",
            inline=False,
        )
        e.add_field(
            name="Rewards and seasons",
            value=f"`{p}level rewards add @Role <level>` · `stack <enabled>` · `sync [@Member]`\n"
            f"`{p}periodboard [week|month|season]` · `{p}level season start <name>` · `{p}level season history`",
            inline=False,
        )
        e.add_field(
            name="Earned XP controls",
            value=f"`{p}level boost [factor] [minutes] [@Role] [#channel]`\n"
            f"`{p}level guard repeat <seconds>` · `reactions <enabled>` · `dailycap <XP>` · `minwords <count>`\n"
            f"`{p}level setup`",
            inline=False,
        )
        await self._reply(ctx, embed=e)

    @level.command()
    async def diag(self, ctx: redcommands.Context):
        """Check XP settings, intents, permissions, and reactions."""
        g = await self._g(ctx.guild)
        base, inc = await self._lin(ctx.guild)
        ch = ctx.channel
        perms = (
            ch.permissions_for(ctx.guild.me)
            if isinstance(ch, (discord.TextChannel, discord.Thread))
            else None
        )  # type: ignore
        intents = self.bot.intents
        probe = "skip"
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try:
                m = await self._reply(ctx, "LevelPlus diag probe…")
                await m.add_reaction("✅")
                probe = "OK"
                await m.delete()
            except Exception as e:
                probe = f"FAIL:{type(e).__name__}"
        lines = [
            f"curve={g['curve']} mult={g['multiplier']} maxlvl={g['max_level']} linear(base={base:.3f}, inc={inc:.3f})",
            f"perms(send={getattr(perms, 'send_messages', None)} embed={getattr(perms, 'embed_links', None)} add_rxn={getattr(perms, 'add_reactions', None)} read_hist={getattr(perms, 'read_message_history', None)})",
            f"intents(voice_states={intents.voice_states} message_content={intents.message_content})",
            f"probe={probe}",
            "store_rw=OK" if isinstance(g["xp"], dict) else "store_rw=FAIL",
        ]
        await self._reply(
            ctx,
            settings(("\n".join(lines)), lang="ini"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---- test/view
    @level.command(name="testmsg")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def level_testmsg(
        self, ctx: redcommands.Context, member: Optional[discord.Member] = None
    ):
        """Send a sample level-up notice without changing XP."""
        m = member or ctx.author
        conf = await self.config.guild(ctx.guild).levelup()
        if not conf["enabled"]:
            return await self._reply(ctx, "Level-up messages are disabled.", tone="warning")
        ch: Optional[discord.TextChannel] = None
        cid = conf.get("channel_id")
        if cid:
            ch = (
                ctx.guild.get_channel(cid)
                if isinstance(cid, int)
                else ctx.guild.get_channel(int(cid))
            )
            if not isinstance(ch, discord.TextChannel):
                ch = None
        if not ch:
            ch = ctx.channel
        g = await self._g(ctx.guild)
        cur = await self.current_level(ctx.guild, m.id)
        next_level = cur + 1 if (g["max_level"] == 0 or cur < g["max_level"]) else cur
        u = SimpleNamespace(
            **{
                "mention": m.mention,
                "name": m.display_name,
                "level": next_level,
                "xp": await self._get_xp(ctx.guild, m.id),
            }
        )
        try:
            msg = conf.get("template", "{user.mention} has reached level **{user.level}**!").format(
                user=u
            )
        except Exception:
            msg = f"{m.mention} has reached level **{next_level}**!"
        await self._presentation.send(
            ch,
            embed=self._levelup_card(m, next_level, u.xp, preview=True),
            notification=f"[TEST] {msg}",
            allowed_mentions=None,
        )
        await self._presentation.confirm(ctx)

    @level.command(name="testup")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def level_testup(
        self,
        ctx: redcommands.Context,
        member: Optional[discord.Member] = None,
        levels: int = 1,
    ):
        """Award real XP to advance a member's level.

        Changes real XP and may send an announcement. Defaults to one level. Use testmsg for a
        notice without changing XP.
        """
        member = member or ctx.author
        settings = await self._settings(ctx.guild)
        xp = await self._get_xp(ctx.guild, member.id)
        current = self._level(xp, settings)
        target = current + max(1, min(levels, 10000))
        if settings["max_level"] > 0:
            target = min(target, settings["max_level"])
        linear = settings["linear"]
        threshold = cumulative_xp(
            target,
            settings["curve"],
            float(settings["multiplier"]),
            float(linear["base"]),
            float(linear["inc"]),
        )
        amount = max(0, threshold - xp)
        old, new = await self._add_xp(ctx.guild, member, amount)
        await self.maybe_announce_levelup(ctx.guild, member, old, new)
        await self._reply(
            ctx, f"Gave {member.mention} **{amount}** XP (L{old} to L{new}).", tone="success"
        )

    @level.command()
    async def show(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
        """Show a member's level, XP, and progress."""
        m = member or ctx.author
        xp = await self._get_xp(ctx.guild, m.id)
        g = await self._g(ctx.guild)
        base, inc = await self._lin(ctx.guild)
        lvl = level_from_xp(xp, g["curve"], float(g["multiplier"]), int(g["max_level"]), base, inc)
        e = self._presentation.embed("Member level", m.mention)
        e.set_thumbnail(url=m.display_avatar.url)
        e.add_field(name="Level", value=f"{lvl:,}")
        e.add_field(name="Total XP", value=f"{xp:,}")
        if g["max_level"] and lvl >= g["max_level"]:
            progress = "Maximum level reached."
        else:
            lower = cumulative_xp(lvl, g["curve"], float(g["multiplier"]), base, inc)
            upper = cumulative_xp(lvl + 1, g["curve"], float(g["multiplier"]), base, inc)
            if upper > lower:
                fraction = max(0.0, min(1.0, (xp - lower) / (upper - lower)))
                filled = int(fraction * 10)
                bar = "▰" * filled + "▱" * (10 - filled)
                progress = (
                    f"{bar} **{fraction:.0%}**\n{max(0, upper - xp):,} XP to level {lvl + 1:,}"
                )
            else:
                progress = "No XP threshold configured."
        e.add_field(name="Next level", value=progress, inline=False)
        await self._reply(ctx, embed=e)

    @level.command()
    async def leaderboard(self, ctx: redcommands.Context, top: int = 10):
        """Show the members with the highest XP totals.

        Defaults to 10 members. The result count is clamped to 1 through 50.
        """
        g = await self._g(ctx.guild)
        base, inc = await self._lin(ctx.guild)
        names = g.get("names", {})
        items = nlargest(
            max(1, min(50, top)),
            ((int(uid), int(xp)) for uid, xp in g["xp"].items()),
            key=lambda t: t[1],
        )
        if not items:
            return await self._reply(ctx, "No XP yet.", tone="warning")
        lines = []
        for i, (uid, xp) in enumerate(items, start=1):
            m = ctx.guild.get_member(uid)
            lvl = level_from_xp(
                xp, g["curve"], float(g["multiplier"]), int(g["max_level"]), base, inc
            )
            name = (m.display_name if m else names.get(str(uid))) or str(uid)
            lines.append(f"{i:>2}. {name} - L{lvl} ({xp} xp)")
        await self._reply(
            ctx,
            settings(("\n".join(lines)), lang="ini"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # Direct member commands share the grouped implementations and permission rules.
    @redcommands.hybrid_command(name="rank")
    @redcommands.guild_only()
    async def rank(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
        """Show a member's level, XP, and progress."""
        await invoke_shortcut(self, ctx, self.show, member=member)

    @redcommands.hybrid_command(name="leaderboard", aliases=["lb"])
    @redcommands.guild_only()
    async def direct_leaderboard(self, ctx: redcommands.Context, top: int = 10):
        """Show the server's highest XP totals, up to 50 members."""
        await invoke_shortcut(self, ctx, self.leaderboard, top=top)

    @redcommands.hybrid_command(name="levellookup")
    @redcommands.guild_only()
    async def direct_lookup(self, ctx: redcommands.Context, *, query: str):
        """Find a member's ID by mention, numeric ID, or name."""
        await invoke_shortcut(self, ctx, self.level_lookup, query=query)

    # ---- formula
    @level.group(name="formula", autohelp=False)
    @redcommands.admin_or_permissions(manage_guild=True)
    async def formula(self, ctx: redcommands.Context):
        """Configure level curves and XP thresholds."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @formula.command(name="curve")
    async def formula_curve(self, ctx: redcommands.Context, curve: str):
        """Choose a linear, exponential, or constant level curve."""
        curve = curve.lower()
        if curve not in {"linear", "exponential", "constant"}:
            return await self._reply(
                ctx, "Curve must be linear|exponential|constant.", tone="warning"
            )
        await self.config.guild(ctx.guild).curve.set(curve)
        await self._presentation.confirm(ctx)

    @formula.command(name="multiplier")
    async def formula_mult(self, ctx: redcommands.Context, mult: float):
        """Set the XP threshold multiplier."""
        if not math.isfinite(mult):
            return await self._reply(ctx, "Use a finite numeric value.", tone="warning")
        await self.config.guild(ctx.guild).multiplier.set(float(max(0.1, min(10.0, mult))))
        await self._presentation.confirm(ctx)

    @formula.command(name="maxlevel")
    async def formula_maxlvl(self, ctx: redcommands.Context, level: int):
        """Set the maximum level, or use zero for no cap."""
        await self.config.guild(ctx.guild).max_level.set(int(max(0, level)))
        await self._presentation.confirm(ctx)

    @prefix_group(formula, name="linear", autohelp=False)
    async def formula_linear(self, ctx: redcommands.Context):
        """Configure the base and increment of the linear curve."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @formula_linear.command(name="base")
    async def formula_linear_base(self, ctx: redcommands.Context, value: float):
        """Set the nonnegative linear curve base."""
        if not math.isfinite(value):
            return await self._reply(ctx, "Use a finite numeric value.", tone="warning")
        await self.config.guild(ctx.guild).linear.base.set(float(max(0.0, value)))
        await self._presentation.confirm(ctx)

    @formula_linear.command(name="inc")
    async def formula_linear_inc(self, ctx: redcommands.Context, value: float):
        """Set the nonnegative linear curve increment."""
        if not math.isfinite(value):
            return await self._reply(ctx, "Use a finite numeric value.", tone="warning")
        await self.config.guild(ctx.guild).linear.inc.set(float(max(0.0, value)))
        await self._presentation.confirm(ctx)

    @formula.command(name="preset")
    async def formula_preset(self, ctx: redcommands.Context, which: str):
        """Apply the Arcane-like linear curve preset."""
        which = which.lower()
        if which != "arcane":
            return await self._reply(ctx, "Only `arcane` preset is available.", tone="warning")
        await self.config.guild(ctx.guild).linear.set({"base": 83.2, "inc": 100.433})
        await self.config.guild(ctx.guild).curve.set("linear")
        await self._reply(
            ctx,
            "Set curve to **linear** with Arcane-like preset (base=83.2, inc=100.433).",
            tone="success",
        )

    @formula.command(name="calibrate")
    @app_commands.rename(L1="level1", XP1="xp1", L2="level2", XP2="xp2")
    @app_commands.describe(
        L1="Level at the first known XP total.",
        XP1="Cumulative XP needed to reach the first level.",
        L2="Level at the second known XP total.",
        XP2="Cumulative XP needed to reach the second level.",
    )
    async def formula_calibrate(
        self, ctx: redcommands.Context, L1: int, XP1: int, L2: int, XP2: int
    ):
        """Fit a linear curve to two level and total-XP points.

        Provide two different positive levels and their nonnegative cumulative XP thresholds.
        Keeps the current multiplier and rejects invalid or unrepresentable fits without
        changing saved settings.
        """
        if L1 <= 0 or L2 <= 0 or L1 == L2:
            return await self._reply(ctx, "Levels must be positive and different.", tone="warning")
        if XP1 < 0 or XP2 < 0:
            return await self._reply(ctx, "XP thresholds must be nonnegative.", tone="warning")
        group = self.config.guild(ctx.guild)
        multiplier = float(await group.multiplier())
        if not math.isfinite(multiplier) or multiplier <= 0:
            return await self._reply(
                ctx, "Set a finite positive multiplier before calibrating.", tone="warning"
            )
        factor = Fraction(str(multiplier))
        step = 2 * (Fraction(XP2, L2) - Fraction(XP1, L1)) / (L2 - L1) / factor
        start = Fraction(XP1, L1) / factor - (L1 - 1) * step / 2
        if step < 0 or start < 0:
            return await self._reply(
                ctx, "Calibration failed (negative base/inc). Check inputs.", tone="error"
            )
        try:
            b, d = float(start), float(step)
        except OverflowError:
            return await self._reply(
                ctx, "Calibration failed (coefficients are too large). Check inputs.", tone="error"
            )
        if not all(math.isfinite(value) for value in (b, d)) or any(
            cumulative_xp(level, "linear", multiplier, b, d) != xp
            for level, xp in ((L1, XP1), (L2, XP2))
        ):
            return await self._reply(
                ctx,
                "Calibration failed (coefficients cannot reproduce those XP thresholds). Check inputs.",
                tone="error",
            )
        await group.linear.set({"base": b, "inc": d})
        await group.curve.set("linear")
        await self._reply(
            ctx, f"Calibrated linear curve: base=**{b:.3f}**, inc=**{d:.3f}**", tone="success"
        )

    # ---- admin: message xp
    @level.group(name="message", autohelp=False)
    @redcommands.admin_or_permissions(manage_guild=True)
    async def message_grp(self, ctx: redcommands.Context):
        """Configure XP awards from messages."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @message_grp.command(name="mode")
    async def message_mode(self, ctx: redcommands.Context, mode: str):
        """Choose word-based, random, or no message XP."""
        mode = mode.lower()
        if mode not in {"none", "random", "perword"}:
            return await self._reply(ctx, "Mode: none|random|perword", tone="warning")
        await self.config.guild(ctx.guild).message.mode.set(mode)
        await self._presentation.confirm(ctx)

    @message_grp.command(name="min")
    async def message_min(self, ctx: redcommands.Context, value: int):
        """Set minimum message XP or the XP-per-word amount."""
        await self.config.guild(ctx.guild).message.min.set(int(max(0, value)))
        await self._presentation.confirm(ctx)

    @message_grp.command(name="max")
    async def message_max(self, ctx: redcommands.Context, value: int):
        """Set the random upper bound or per-message XP cap."""
        await self.config.guild(ctx.guild).message.max.set(int(max(0, value)))
        await self._presentation.confirm(ctx)

    @message_grp.command(name="cooldown")
    async def message_cd(self, ctx: redcommands.Context, seconds: int):
        """Set the message XP cooldown in seconds."""
        await self.config.guild(ctx.guild).message.cooldown.set(int(max(0, min(3600, seconds))))
        await self._presentation.confirm(ctx)

    @message_grp.command(name="enable")
    async def message_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        """Enable, disable, or toggle message XP.

        Omit true or false to toggle the current setting.
        """
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).message.enabled())
        await self.config.guild(ctx.guild).message.enabled.set(bool(enabled))
        await self._presentation.confirm(ctx)

    # ---- admin: reaction xp
    @level.group(name="reaction", autohelp=False)
    @redcommands.admin_or_permissions(manage_guild=True)
    async def rx_grp(self, ctx: redcommands.Context):
        """Configure XP awards from reactions."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @rx_grp.command(name="awards")
    async def rx_awards(self, ctx: redcommands.Context, awards: str):
        """Choose whether reactions reward authors or reactors."""
        awards = awards.lower()
        if awards not in {"none", "both", "author", "reactor"}:
            return await self._reply(ctx, "Awards: none|both|author|reactor", tone="warning")
        await self.config.guild(ctx.guild).reaction.awards.set(awards)
        await self._presentation.confirm(ctx)

    @rx_grp.command(name="min")
    async def rx_min(self, ctx: redcommands.Context, value: int):
        """Set the minimum random reaction XP award."""
        await self.config.guild(ctx.guild).reaction.min.set(int(max(0, value)))
        await self._presentation.confirm(ctx)

    @rx_grp.command(name="max")
    async def rx_max(self, ctx: redcommands.Context, value: int):
        """Set the maximum random reaction XP award."""
        await self.config.guild(ctx.guild).reaction.max.set(int(max(0, value)))
        await self._presentation.confirm(ctx)

    @rx_grp.command(name="cooldown")
    async def rx_cd(self, ctx: redcommands.Context, seconds: int):
        """Set the reaction XP cooldown in seconds."""
        await self.config.guild(ctx.guild).reaction.cooldown.set(int(max(0, min(3600, seconds))))
        await self._presentation.confirm(ctx)

    @rx_grp.command(name="enable")
    async def rx_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        """Enable, disable, or toggle reaction XP.

        Omit true or false to toggle the current setting.
        """
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).reaction.enabled())
        await self.config.guild(ctx.guild).reaction.enabled.set(bool(enabled))
        await self._presentation.confirm(ctx)

    # ---- admin: voice xp
    @level.group(name="voice", autohelp=False)
    @redcommands.admin_or_permissions(manage_guild=True)
    async def voice_grp(self, ctx: redcommands.Context):
        """Configure XP awards from voice participation."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @voice_grp.command(name="enable")
    async def voice_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        """Enable, disable, or toggle voice XP.

        Omit true or false to toggle the current setting.
        """
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).voice.enabled())
        await self.config.guild(ctx.guild).voice.enabled.set(bool(enabled))
        await self._presentation.confirm(ctx)

    @voice_grp.command(name="range")
    async def voice_range(self, ctx: redcommands.Context, min_points: int, max_points: int):
        """Set the minimum and maximum random voice XP award."""
        max_points = max(max_points, min_points)
        await self.config.guild(ctx.guild).voice.min.set(int(max(0, min_points)))
        await self.config.guild(ctx.guild).voice.max.set(int(max(0, max_points)))
        await self._presentation.confirm(ctx)

    @voice_grp.command(name="cooldown")
    async def voice_cd(self, ctx: redcommands.Context, seconds: int):
        """Set the voice XP award interval in seconds."""
        await self.config.guild(ctx.guild).voice.cooldown.set(int(max(15, min(3600, seconds))))
        await self._presentation.confirm(ctx)

    @voice_grp.command(name="minmembers")
    async def voice_minmembers(self, ctx: redcommands.Context, count: int):
        """Set the minimum human members needed for voice XP."""
        await self.config.guild(ctx.guild).voice.min_members.set(int(max(1, min(99, count))))
        await self._presentation.confirm(ctx)

    @voice_grp.command(name="antiafk")
    async def voice_antiafk(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        """Control exclusion of AFK, muted, or deafened members.

        Omit true or false to toggle the current setting.
        """
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).voice.anti_afk())
        await self.config.guild(ctx.guild).voice.anti_afk.set(bool(enabled))
        await self._presentation.confirm(ctx)

    # ---- restrictions
    @level.group(name="restrict", autohelp=False)
    @redcommands.admin_or_permissions(manage_guild=True)
    async def restrict(self, ctx: redcommands.Context):
        """Manage channel, role, and feature XP exclusions."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @prefix_group(restrict, name="nochannels", autohelp=False)
    async def res_noch(self, ctx: redcommands.Context):
        """Manage channels excluded from XP awards."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @res_noch.command(name="add")
    async def res_noch_add(self, ctx: redcommands.Context, channel: discord.TextChannel):
        """Exclude a channel from XP awards."""
        data = await self.config.guild(ctx.guild).restrictions.no_channels()
        if channel.id in data:
            return await self._reply(ctx, "Already set.", tone="warning")
        data.append(channel.id)
        await self.config.guild(ctx.guild).restrictions.no_channels.set(data)
        await self._presentation.confirm(ctx)

    @res_noch.command(name="remove")
    async def res_noch_remove(self, ctx: redcommands.Context, channel: discord.TextChannel):
        """Remove a channel from the XP exclusion list."""
        data = await self.config.guild(ctx.guild).restrictions.no_channels()
        if channel.id not in data:
            return await self._reply(ctx, "Not present.", tone="warning")
        data = [c for c in data if c != channel.id]
        await self.config.guild(ctx.guild).restrictions.no_channels.set(data)
        await self._presentation.confirm(ctx)

    @res_noch.command(name="list")
    async def res_noch_list(self, ctx: redcommands.Context):
        """List channels excluded from XP awards."""
        data = await self.config.guild(ctx.guild).restrictions.no_channels()
        await self._reply(
            ctx, "No-XP channels: " + (", ".join(f"<#{c}>" for c in data) if data else "none")
        )

    @res_noch.command(name="clear")
    async def res_noch_clear(self, ctx: redcommands.Context):
        """Clear all channel XP exclusions."""
        await self.config.guild(ctx.guild).restrictions.no_channels.set([])
        await self._presentation.confirm(ctx)

    @prefix_group(restrict, name="noroles", autohelp=False)
    async def res_noroles(self, ctx: redcommands.Context):
        """Manage roles whose members cannot earn XP."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @res_noroles.command(name="add")
    async def res_noroles_add(self, ctx: redcommands.Context, role: discord.Role):
        """Exclude members with a role from XP awards."""
        data = await self.config.guild(ctx.guild).restrictions.no_roles()
        if role.id in data:
            return await self._reply(ctx, "Already set.", tone="warning")
        data.append(role.id)
        await self.config.guild(ctx.guild).restrictions.no_roles.set(data)
        await self._presentation.confirm(ctx)

    @res_noroles.command(name="remove")
    async def res_noroles_remove(self, ctx: redcommands.Context, role: discord.Role):
        """Remove a role from the XP exclusion list."""
        data = await self.config.guild(ctx.guild).restrictions.no_roles()
        if role.id not in data:
            return await self._reply(ctx, "Not present.", tone="warning")
        data = [r for r in data if r != role.id]
        await self.config.guild(ctx.guild).restrictions.no_roles.set(data)
        await self._presentation.confirm(ctx)

    @res_noroles.command(name="list")
    async def res_noroles_list(self, ctx: redcommands.Context):
        """List roles excluded from XP awards."""
        data = await self.config.guild(ctx.guild).restrictions.no_roles()
        roles = [ctx.guild.get_role(r).mention for r in data if ctx.guild.get_role(r)] or ["none"]
        await self._reply(ctx, "No-XP roles: " + ", ".join(roles))

    @res_noroles.command(name="clear")
    async def res_noroles_clear(self, ctx: redcommands.Context):
        """Clear all role XP exclusions."""
        await self.config.guild(ctx.guild).restrictions.no_roles.set([])
        await self._presentation.confirm(ctx)

    @restrict.command(name="toggles")
    async def restrict_toggles(
        self, ctx: redcommands.Context, feature: str, enabled: Optional[bool] = None
    ):
        """Control XP in threads, forums, voice text, or slash.

        Feature names: threadxp, forumxp, textvoicexp, and slashxp. Omit true or false to toggle
        the feature.
        """
        feature = feature.lower()
        if feature not in {"threadxp", "forumxp", "textvoicexp", "slashxp"}:
            return await self._reply(
                ctx, "feature: threadxp|forumxp|textvoicexp|slashxp", tone="warning"
            )
        key = {
            "threadxp": "thread_xp",
            "forumxp": "forum_xp",
            "textvoicexp": "text_in_voice_xp",
            "slashxp": "slash_command_xp",
        }[feature]
        if enabled is None:
            enabled = not (await getattr(self.config.guild(ctx.guild).restrictions, key)())
        await getattr(self.config.guild(ctx.guild).restrictions, key).set(bool(enabled))
        await self._presentation.confirm(ctx)

    # ---- levelup config
    @level.group(name="levelup", autohelp=False)
    @redcommands.admin_or_permissions(manage_guild=True)
    async def levelup_grp(self, ctx: redcommands.Context):
        """Configure level-up announcements."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @levelup_grp.command(name="enable")
    async def levelup_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        """Enable, disable, or toggle level-up announcements.

        Omit true or false to toggle the current setting.
        """
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).levelup.enabled())
        await self.config.guild(ctx.guild).levelup.enabled.set(bool(enabled))
        await self._presentation.confirm(ctx)

    @levelup_grp.command(name="channel")
    async def levelup_channel(
        self, ctx: redcommands.Context, channel: Optional[discord.TextChannel]
    ):
        """Set or clear the level-up announcement channel.

        Omit the channel to clear the saved target. Automatic announcements then use the
        server's system channel.
        """
        await self.config.guild(ctx.guild).levelup.channel_id.set(channel.id if channel else None)
        await self._presentation.confirm(ctx)

    @levelup_grp.command(name="template")
    async def levelup_template(self, ctx: redcommands.Context, *, text: str):
        """Set the level-up message template.

        Available placeholders: {user.mention}, {user.name}, {user.level}, and {user.xp}. The
        template is limited to 500 characters.
        """
        await self.config.guild(ctx.guild).levelup.template.set(text[:500])
        await self._presentation.confirm(ctx)

    # ---- XP admin & migration
    @level.group(name="xp", autohelp=False)
    @redcommands.admin_or_permissions(manage_guild=True)
    async def xpgrp(self, ctx: redcommands.Context):
        """Manage member XP totals and CSV imports and exports."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @xpgrp.command(name="set")
    async def xp_set(self, ctx: redcommands.Context, member: discord.Member, amount: int):
        """Set a member's total XP."""
        await self._remember_name(ctx.guild, member)
        await self._set_xp(ctx.guild, member.id, amount)
        await self._presentation.confirm(ctx)

    @xpgrp.command(name="setid", with_app_command=False)
    async def xp_setid(self, ctx: redcommands.Context, user_id: int, amount: int):
        """Set total XP by user ID."""
        await self._set_xp(ctx.guild, user_id, amount)
        await self._presentation.confirm(ctx)

    @xpgrp.command(name="add")
    async def xp_add(self, ctx: redcommands.Context, member: discord.Member, amount: int):
        """Add XP and announce any resulting level increase."""
        await self._remember_name(ctx.guild, member)
        old, new = await self._add_xp(ctx.guild, member, amount)
        await self.maybe_announce_levelup(ctx.guild, member, old, new)
        await self._presentation.confirm(ctx)

    @xpgrp.command(name="remove")
    async def xp_remove(self, ctx: redcommands.Context, member: discord.Member):
        """Remove a member's XP row while keeping their alias.

        Removes only the XP row. The saved name or alias remains.
        """
        async with self.config.guild(ctx.guild).xp() as data:
            data.pop(str(member.id), None)
        await self._reply(ctx, f"Removed XP row for {member.mention}.", tone="success")

    @xpgrp.command(name="removeid", with_app_command=False)
    async def xp_removeid(self, ctx: redcommands.Context, user_id: int):
        """Remove an XP row by user ID while keeping its alias.

        Removes only the XP row. The saved name or alias remains.
        """
        async with self.config.guild(ctx.guild).xp() as data:
            data.pop(str(user_id), None)
        await self._reply(ctx, f"Removed XP row for `{user_id}`.", tone="success")

    @xpgrp.command(name="purgebots")
    async def xp_purgebots(self, ctx: redcommands.Context):
        """Remove XP rows for bots currently in the server."""
        async with self.config.guild(ctx.guild).xp() as data:
            before = len(data)
            bot_ids = {str(m.id) for m in ctx.guild.members if m.bot}
            for bid in bot_ids:
                data.pop(bid, None)
        await self._reply(ctx, f"Purged **{before - len(data)}** bot row(s).", tone="success")

    @xpgrp.command(name="clear")
    async def xp_clear(self, ctx: redcommands.Context, confirm: Optional[str] = None):
        """Clear this server's XP and aliases with confirmation.

        Requires the confirmation word yes. Removes both XP and saved aliases for this server.
        """
        if confirm != "yes":
            return await self._reply(
                ctx,
                "This will WIPE XP & aliases for this guild. Confirm with `level xp clear yes`.",
                tone="warning",
            )
        group = self.config.guild(ctx.guild)
        async with group.xp() as data:
            data.clear()
        async with group.names() as names:
            names.clear()
        await self._reply(ctx, "Cleared XP and aliases.", tone="success")

    @xpgrp.command(name="exportcsv")
    async def xp_export(self, ctx: redcommands.Context):
        """Export user IDs, XP totals, and aliases as CSV."""
        g = await self._g(ctx.guild)
        buff = io.StringIO()
        w = csv.writer(buff)
        w.writerow(["user_id", "xp", "alias"])
        for uid, xp in g["xp"].items():
            alias = g.get("names", {}).get(uid, "")
            w.writerow([uid, xp, alias])
        buff.seek(0)
        await self._reply(
            ctx,
            file=discord.File(
                fp=io.BytesIO(buff.getvalue().encode("utf-8")),
                filename=f"{ctx.guild.id}_xp_export.csv",
            ),
        )

    @xpgrp.command(name="importcsv")
    async def xp_import_csv(self, ctx: redcommands.Context, *, raw: str = ""):
        """Import XP from a UTF-8 CSV attachment or pasted text.

        Accepts the first UTF-8 attachment or pasted CSV with user_id,xp,alias columns. Matching
        user IDs are overwritten. Supports quoted fields and attachments up to 8 MB.
        """
        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            if attachment.size > 8_000_000:
                return await self._reply(ctx, "CSV exceeds the 8 MB import limit.", tone="error")
            try:
                raw = (await attachment.read()).decode("utf-8-sig")
            except (discord.HTTPException, UnicodeDecodeError):
                return await self._reply(
                    ctx, "Could not read a UTF-8 CSV attachment.", tone="error"
                )
        parsed = []
        try:
            for row in csv.reader(io.StringIO(raw), strict=True):
                if len(row) < 2 or row[0].strip().lower() == "user_id":
                    continue
                match = re.fullmatch(r"(?:<@!?)?(\d{15,25})>?", row[0].strip())
                if not match:
                    continue
                try:
                    xp = max(0, int(row[1].strip()))
                except ValueError:
                    continue
                parsed.append((match.group(1), xp, row[2][:100] if len(row) > 2 else None))
        except csv.Error:
            return await self._reply(
                ctx, "Invalid CSV quoting. No rows were imported.", tone="error"
            )
        if not parsed:
            return await self._reply(ctx, "No rows parsed.", tone="warning")
        group = self.config.guild(ctx.guild)
        async with group.xp() as xpmap:
            for uid, xp, _ in parsed:
                xpmap[uid] = xp
        async with group.names() as names:
            for uid, _, alias in parsed:
                if alias is not None:
                    names[uid] = alias
        await self._reply(ctx, f"Imported **{len(parsed)}** user(s).", tone="success")

    @xpgrp.command(name="importlines")
    async def xp_import_lines(self, ctx: redcommands.Context, *, lines: str):
        """Import identifier and integer XP rows from text.

        Use identifier,xp rows with a user ID, mention, or resolvable name. Matching users are
        overwritten; ambiguous names are skipped. XP values must be integers.
        """
        guild_names, global_names = {}, {}

        def index_user(index, user, names):
            if user.bot:
                return
            for name in names:
                if name:
                    index.setdefault(name.casefold(), set()).add(user.id)

        for member in ctx.guild.members:
            index_user(
                guild_names,
                member,
                (
                    member.display_name,
                    member.name,
                    member.global_name,
                    f"{member.name}#{member.discriminator}",
                ),
            )
        for user in self.bot.users:
            index_user(
                global_names,
                user,
                (user.name, user.global_name, f"{user.name}#{user.discriminator}"),
            )
        parsed, skipped = [], 0
        try:
            for row in csv.reader(io.StringIO(lines), strict=True):
                if not row:
                    continue
                if len(row) != 2:
                    skipped += 1
                    continue
                identifier = row[0].strip()
                match = re.fullmatch(r"(?:<@!?)?(\d{15,25})>?", identifier)
                candidates = (
                    {int(match.group(1))}
                    if match
                    else guild_names.get(
                        identifier.casefold(),
                        global_names.get(identifier.casefold(), set()),
                    )
                )
                try:
                    xp = max(0, int(row[1].strip()))
                except ValueError:
                    skipped += 1
                    continue
                if len(candidates) != 1:
                    skipped += 1
                    continue
                uid = next(iter(candidates))
                member = ctx.guild.get_member(uid)
                alias = member.display_name if member else identifier
                parsed.append((str(uid), xp, alias[:100]))
        except csv.Error:
            return await self._reply(
                ctx, "Invalid CSV quoting. No rows were imported.", tone="error"
            )
        if parsed:
            group = self.config.guild(ctx.guild)
            async with group.xp() as xpmap:
                for uid, xp, _ in parsed:
                    xpmap[uid] = xp
            async with group.names() as names:
                for uid, _, alias in parsed:
                    names[uid] = alias
        await self._reply(
            ctx, f"Imported **{len(parsed)}** row(s), skipped **{skipped}**.", tone="success"
        )

    # ---- lookup & aliases
    @level.command(name="lookup")
    async def level_lookup(self, ctx: redcommands.Context, *, query: str):
        """Find user IDs by mention, ID, or name fragment.

        Searches current server members and cached users. Accepts a mention, raw numeric ID, or
        case-insensitive name fragment.
        """
        q = query.strip()
        m = re.search(r"(\d{15,25})", q)
        if m:
            uid = int(m.group(1))
            mbr = ctx.guild.get_member(uid)
            name = (mbr.display_name if mbr else None) or (
                await self.config.guild(ctx.guild).names()
            ).get(str(uid), "unknown")
            return await self._reply(ctx, settings(f"1. {name} - `{uid}`", lang="ini"))

        ql = q.lower()
        results: List[Tuple[int, str]] = []
        for mbr in ctx.guild.members:
            if mbr.bot:
                continue
            names = [mbr.display_name, mbr.name, getattr(mbr, "global_name", None)]
            if any(n and ql in n.lower() for n in names):
                results.append((mbr.id, mbr.display_name))
        if not results:
            for u in self.bot.users:
                if getattr(u, "bot", False):
                    continue
                names = [u.name, getattr(u, "global_name", None)]
                if any(n and ql in n.lower() for n in names):
                    results.append((u.id, getattr(u, "global_name", None) or u.name))
        if not results:
            return await self._reply(ctx, "No matches.", tone="warning")
        lines = [f"{i:>2}. {name} - `{uid}`" for i, (uid, name) in enumerate(results[:20], start=1)]
        await self._reply(
            ctx,
            settings(("\n".join(lines)), lang="ini"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @level.group(name="name", autohelp=False)
    @redcommands.admin_or_permissions(manage_guild=True)
    async def namegrp(self, ctx: redcommands.Context):
        """Manage saved member names and leaderboard aliases."""
        if ctx.invoked_subcommand is None:
            await self._presentation.help(ctx)

    @namegrp.command(name="set")
    async def name_set(self, ctx: redcommands.Context, member: discord.Member, *, alias: str):
        """Save a member's leaderboard alias."""
        async with self.config.guild(ctx.guild).names() as names:
            names[str(member.id)] = alias[:100]
        await self._presentation.confirm(ctx)

    @namegrp.command(name="setid", with_app_command=False)
    async def name_setid(self, ctx: redcommands.Context, user_id: int, *, alias: str):
        """Save a leaderboard alias by user ID."""
        async with self.config.guild(ctx.guild).names() as names:
            names[str(user_id)] = alias[:100]
        await self._presentation.confirm(ctx)

    @namegrp.command(name="get", with_app_command=False)
    async def name_get(self, ctx: redcommands.Context, user_id: int):
        """Show the saved name or alias for a user ID."""
        names = await self.config.guild(ctx.guild).names()
        alias = names.get(str(user_id), "none")
        await self._reply(ctx, f"`{user_id}` → {alias}")

    async def cog_load(self):
        self._closing = False
        self.voice_tick.start()
        self._progress_task = asyncio.create_task(self._progress_loop())

    async def cog_before_invoke(self, ctx):
        await prepare_hybrid(ctx)
        self._settings_cache.pop(ctx.guild.id, None)

    async def cog_after_invoke(self, ctx):
        finish_configuration_audit(ctx)
        self._settings_cache.pop(ctx.guild.id, None)

    async def _settings(self, guild):
        now = time.monotonic()
        cached = self._settings_cache.get(guild.id)
        if cached is not None and now - cached[0] < 5:
            return cached[1]
        async with self._settings_locks[guild.id]:
            now = time.monotonic()
            cached = self._settings_cache.get(guild.id)
            if cached is not None and now - cached[0] < 5:
                return cached[1]
            group = self.config.guild(guild)
            keys = [key for key in DEFAULTS_GUILD if key not in {"xp", "names"}] + [
                "xp_features",
                "rewards",
                "milestone_settings",
                "progress_settings",
            ]
            values = await asyncio.gather(*(group.get_attr(key)() for key in keys))
            settings = dict(zip(keys, values))
            self._settings_cache[guild.id] = (now, settings)
            return settings

    @staticmethod
    def _level(xp, settings):
        linear = settings["linear"]
        return level_from_xp(
            xp,
            settings["curve"],
            float(settings["multiplier"]),
            int(settings["max_level"]),
            float(linear["base"]),
            float(linear["inc"]),
        )

    @staticmethod
    def _cooldown(cache, key, seconds):
        now = time.monotonic()
        previous = cache.get(key)
        if previous is not None and now - previous < seconds:
            return False
        cache[key] = now
        cache.move_to_end(key)
        while cache and (len(cache) > 20000 or now - next(iter(cache.values())) > 3600):
            cache.popitem(last=False)
        return True

    @staticmethod
    def _eligible(member, channel, settings):
        if not isinstance(member, discord.Member) or member.bot or channel is None:
            return False
        restrictions = settings["restrictions"]
        excluded = set(restrictions["no_channels"])
        if channel.id in excluded or getattr(channel, "parent_id", None) in excluded:
            return False
        if any(role.id in restrictions["no_roles"] for role in member.roles):
            return False
        if isinstance(channel, discord.Thread):
            if not restrictions["thread_xp"]:
                return False
            if isinstance(channel.parent, discord.ForumChannel) and not restrictions["forum_xp"]:
                return False
        if (
            isinstance(channel, (discord.VoiceChannel, discord.StageChannel))
            and not restrictions["text_in_voice_xp"]
        ):
            return False
        return True

    async def _set_feature_setting(self, guild, section, key, value):
        group = self.config.guild(guild).get_attr(section)
        async with group.get_lock():
            await group.get_attr(key).set(value)
        self._settings_cache.pop(guild.id, None)

    @level.group(name="rewards", autohelp=False)
    @redcommands.admin_or_permissions(manage_guild=True)
    async def rewards(self, ctx):
        """Configure roles awarded at level milestones."""
        data = await self.config.guild(ctx.guild).rewards()
        lines = [
            f"<@&{role}> · Level {threshold}"
            for role, threshold in sorted(data["roles"].items(), key=lambda item: item[1])
        ]
        await self._reply(
            ctx,
            f"Keep all qualified rewards: {data['stack']}\n"
            + ("\n".join(lines) or "No reward roles. Use level rewards add <role> <level>."),
        )

    @rewards.command(name="add")
    async def rewards_add(self, ctx, role: discord.Role, threshold: int):
        """Award a role at the specified level."""
        if not 1 <= threshold <= 100000:
            raise redcommands.BadArgument("Choose a level from 1 through 100000.")
        safe_role(ctx.guild, role)
        async with self.config.guild(ctx.guild).rewards() as data:
            if len(data["roles"]) >= 100 and str(role.id) not in data["roles"]:
                raise redcommands.BadArgument("You can configure up to 100 reward roles.")
            data["roles"][str(role.id)] = threshold
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @rewards.command(name="remove")
    async def rewards_remove(self, ctx, role: discord.Role):
        """Stop managing a configured reward role."""
        async with self.config.guild(ctx.guild).rewards() as data:
            data["roles"].pop(str(role.id), None)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._reply(
            ctx, "Reward removed. Existing assignments of this role are retained.", tone="success"
        )

    @rewards.command(name="stack")
    async def rewards_stack(self, ctx, enabled: bool):
        """Keep all qualified rewards or only the highest."""
        await self._set_feature_setting(ctx.guild, "rewards", "stack", enabled)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @rewards.command(name="sync")
    async def rewards_sync(self, ctx, member: Optional[discord.Member] = None):
        """Reconcile a member's milestone reward roles."""
        await self._sync_rewards(member or ctx.author)
        await self._reply(
            ctx,
            "Checked milestone roles. The bot needs Manage Roles and a higher role to assign them.",
            tone="success",
        )

    @rewards.command(name="preview")
    async def rewards_preview(
        self,
        ctx,
        curve: Optional[str] = None,
        multiplier: Optional[float] = None,
        base: Optional[float] = None,
        increment: Optional[float] = None,
        max_level: Optional[int] = None,
        role: Optional[discord.Role] = None,
        threshold: Optional[int] = None,
        stack: Optional[bool] = None,
        member: Optional[discord.Member] = None,
    ):
        """Preview levels and reward roles using optional proposed settings."""
        current = await self._settings(ctx.guild)
        proposed = preview_policy(
            current,
            curve=curve,
            multiplier=multiplier,
            base=base,
            increment=increment,
            max_level=max_level,
            role=role,
            threshold=threshold,
            stack=stack,
        )
        report = await build_preview(self, ctx.guild, current, proposed, member)
        lines = [
            f"Reviewed {report['reviewed']} cached members · {report['changed']} level changes · "
            f"{report['adds']} role additions · {report['removes']} role removals.",
            f"Proposed curve: {proposed['curve']} · Multiplier: {proposed['multiplier']:g} · "
            f"Linear base/increment: {proposed['linear']['base']:g}/{proposed['linear']['inc']:g} · "
            f"Maximum level: {proposed['max_level'] or 'Unlimited'} · Stack: {proposed['rewards']['stack']}",
        ]
        if not ctx.guild.me or not ctx.guild.me.guild_permissions.manage_roles:
            lines.append(
                "Role changes require the bot's Manage Roles permission before they can be applied."
            )
        if report["blocked"]:
            lines.append(
                "Skipped unavailable or unsafe roles: "
                + ", ".join(f"<@&{rid}>" for rid in sorted(report["blocked"]))
            )
        for row in report["rows"]:
            text = f"<@{row['member']}> · Level {row['old']} → {row['new']}"
            if row["add"]:
                text += "\nGain: " + ", ".join(f"<@&{rid}>" for rid in row["add"])
            if row["remove"]:
                text += "\nLose: " + ", ".join(f"<@&{rid}>" for rid in row["remove"])
            lines.append(text)
        lines.append(
            "Dry run. Settings, XP and Discord roles remain unchanged. Details show at most 100 members; totals cover everyone reviewed. Custom earned role rewards are preserved. Use slash options to select only the candidate fields you want to test."
        )
        await self._reply(ctx, "\n\n".join(lines), title="Reward preview")

    @redcommands.hybrid_command(name="periodboard")
    @redcommands.guild_only()
    async def periodboard(self, ctx, period: str = "week", top: int = 10):
        """Show weekly, monthly, or current-season earned XP."""
        conf = await self.config.guild(ctx.guild).xp_features()
        data = await self.config.guild(ctx.guild).period_xp()
        totals = period_totals(data, period.lower(), day_at(time.time(), conf["timezone"]))
        ordered = nlargest(max(1, min(50, top)), totals.items(), key=lambda item: item[1])
        names = await self.config.guild(ctx.guild).names()
        lines = []
        for index, (uid, xp) in enumerate(ordered, 1):
            member = ctx.guild.get_member(int(uid))
            name = member.display_name if member else names.get(uid, uid)
            lines.append(f"{index}. **{discord.utils.escape_markdown(name)}** · {xp:,} earned XP")
        label = data["season_name"] if period == "season" else period.title()
        await self._reply(
            ctx,
            "\n".join(lines) or "No earned XP recorded in this period yet.",
            title=f"{label} leaderboard",
        )

    @level.group(name="season", autohelp=False)
    async def season(self, ctx):
        """Manage seasonal and calendar XP leaderboards."""
        data = await self.config.guild(ctx.guild).period_xp()
        conf = await self.config.guild(ctx.guild).xp_features()
        await self._reply(
            ctx,
            f"Season: **{data['season_name']}**\nCalendar tracking: {conf['periods']}\nTimezone: {conf['timezone']}\nUse periodboard week, month, or season. Lifetime XP stays separate.",
        )

    @season.command(name="start")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def season_start(self, ctx, *, name: str):
        """Archive the current season and start another."""
        name = name.strip()
        if not name or len(name) > 40:
            raise redcommands.BadArgument("Use a season name of 1 to 40 characters.")
        async with self.config.guild(ctx.guild).period_xp() as data:
            if data["season"]:
                data["archives"].append(
                    {
                        "name": data["season_name"],
                        "ended": int(time.time()),
                        "xp": dict(nlargest(50, data["season"].items(), key=lambda item: item[1])),
                    }
                )
                data["archives"] = data["archives"][-5:]
            data.update(season_name=name, season_start=int(time.time()), season={})
        await self._reply(
            ctx,
            f"Started **{discord.utils.escape_markdown(name)}**. Lifetime and calendar XP are preserved.",
            tone="success",
        )

    @season.command(name="history")
    async def season_history(self, ctx):
        """Show the five most recently archived seasons."""
        data = await self.config.guild(ctx.guild).period_xp()
        lines = [
            f"**{discord.utils.escape_markdown(item['name'])}** · <t:{item['ended']}:d> · {len(item['xp'])} archived leaders"
            for item in reversed(data["archives"])
        ]
        await self._reply(ctx, "\n".join(lines) or "No archived seasons.")

    @season.command(name="enable")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def season_enable(self, ctx, enabled: bool):
        """Enable or pause calendar and seasonal XP tracking."""
        await self._set_feature_setting(ctx.guild, "xp_features", "periods", enabled)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @season.command(name="timezone")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def season_timezone(self, ctx, zone: str):
        """Choose an IANA timezone for calendar boundaries."""
        await self._set_feature_setting(ctx.guild, "xp_features", "timezone", valid_timezone(zone))
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @level.command(name="boost")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def boost(
        self,
        ctx,
        factor: Optional[float] = None,
        minutes: int = 60,
        role: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        """Show or schedule a temporary earned-XP boost."""
        if factor is None:
            conf = await self.config.guild(ctx.guild).xp_features()
            lines = [
                f"{item['factor']:g}× until <t:{int(item['expires'])}:f> · Role {item['role'] or 'Any'} · Channel {item['channel'] or 'Any'}"
                for item in conf["boosts"]
                if item["expires"] > time.time()
            ]
            return await self._reply(
                ctx, "\n".join(lines) or "No active XP boosts. Factor 1 clears all boosts."
            )
        if not math.isfinite(factor) or not 1 <= factor <= 10 or not 1 <= minutes <= 43200:
            raise redcommands.BadArgument(
                "Use a factor from 1 to 10 and a duration of 1 to 43200 minutes."
            )
        async with self.config.guild(ctx.guild).xp_features() as data:
            data["boosts"] = [item for item in data["boosts"] if item["expires"] > time.time()]
            if factor == 1:
                data["boosts"] = []
            else:
                if len(data["boosts"]) >= 5:
                    raise redcommands.BadArgument(
                        "Up to five boosts can be active. Clear them with factor 1."
                    )
                data["boosts"].append(
                    {
                        "factor": factor,
                        "expires": time.time() + minutes * 60,
                        "role": role.id if role else None,
                        "channel": channel.id if channel else None,
                    }
                )
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @level.group(name="guard", autohelp=False)
    @redcommands.admin_or_permissions(manage_guild=True)
    async def guard(self, ctx):
        """Configure XP farming limits."""
        data = await self.config.guild(ctx.guild).xp_features()
        await self._reply(
            ctx,
            f"Repeat-message window: {data['repeat_seconds']}s\nReaction once per message/24h: {data['reaction_once']}\nDaily earned XP cap: {data['daily_cap'] or 'Unlimited'}\nMinimum message words: {data['min_words']}",
        )

    @guard.command(name="repeat")
    async def guard_repeat(self, ctx, seconds: int):
        """Reject repeated messages within a time window."""
        if not 0 <= seconds <= 86400:
            raise redcommands.BadArgument("Choose 0 through 86400 seconds; 0 disables detection.")
        await self._set_feature_setting(ctx.guild, "xp_features", "repeat_seconds", seconds)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @guard.command(name="reactions")
    async def guard_reactions(self, ctx, enabled: bool):
        """Limit reactions per message for 24 hours."""
        await self._set_feature_setting(ctx.guild, "xp_features", "reaction_once", enabled)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @guard.command(name="dailycap")
    async def guard_dailycap(self, ctx, xp: int):
        """Cap earned XP per member per local day."""
        if not 0 <= xp <= 1000000000:
            raise redcommands.BadArgument("Choose 0 through 1000000000 XP; 0 disables the cap.")
        await self._set_feature_setting(ctx.guild, "xp_features", "daily_cap", xp)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @guard.command(name="minwords")
    async def guard_minwords(self, ctx, count: int):
        """Require a minimum word count for message XP."""
        if not 0 <= count <= 100:
            raise redcommands.BadArgument("Choose 0 through 100 words.")
        await self._set_feature_setting(ctx.guild, "xp_features", "min_words", count)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @level.command(name="setup")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def level_setup(self, ctx):
        """Choose leveling options in a guided panel."""

        async def update(context, key, value):
            root, field = key.split(".")
            await self._set_feature_setting(context.guild, root, field, value)
            self._settings_cache.pop(context.guild.id, None)

        view = SetupView(
            self,
            ctx,
            "level setup",
            [
                ("levelup.channel_id", "Level-up channel", "text"),
                ("message.enabled", "message XP", "toggle"),
                ("voice.enabled", "voice XP", "toggle"),
                ("xp_features.periods", "calendar rankings", "toggle"),
                ("xp_features.reaction_once", "reaction farming protection", "toggle"),
            ],
            update,
        )
        view.message = await self._reply(
            ctx,
            "Choose XP sources, the announcement channel, calendar rankings, and reaction protection. Configure rewards, boosts, and daily caps with level rewards, level boost, and level guard.",
            title="Level setup",
            view=view,
        )

    @commands.Cog.listener()
    @guild_enabled
    async def on_member_update(self, before, after):
        if before.roles != after.roles and not after.bot:
            await self._sync_rewards(after)

    @commands.Cog.listener()
    @guild_enabled
    async def on_member_join(self, member):
        if not member.bot:
            await self._sync_rewards(member)

    @level.command(name="badges")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def badge_setting(self, ctx, enabled: bool):
        """Enable or pause new achievement badges, retaining earned badges."""
        await self._milestone_setting(ctx.guild, "badges", enabled)
        await self._presentation.confirm(ctx)

    @level.group(name="challenges", invoke_without_command=True, fallback="status")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def challenge_settings(self, ctx):
        """Configure weekly goals and earned XP rewards."""
        await self._challenge_settings(ctx)

    @challenge_settings.command(name="enabled")
    async def challenge_enable(self, ctx, enabled: bool):
        """Enable or pause weekly challenge collection."""
        await self._milestone_setting(ctx.guild, "challenges", enabled)
        await self._presentation.confirm(ctx)

    @challenge_settings.command(name="goal")
    async def challenge_goal(self, ctx, metric: str, target: int, reward: int):
        """Set a message, reaction, voice, or earned-XP weekly goal."""
        metric = metric.lower()
        if (
            metric not in {"message", "reaction", "voice", "xp"}
            or not 1 <= target <= 100000
            or not 0 <= reward <= 10000
        ):
            raise redcommands.BadArgument(
                "Choose message, reaction, voice, or xp, a target from 1 to 100,000, and a reward from 0 to 10,000 XP."
            )
        async with self.config.guild(ctx.guild).milestone_settings() as data:
            data["goals"][metric] = {"target": target, "reward": reward}
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    async def red_delete_data_for_user(self, *, requester, user_id):
        uid = str(user_id)
        for guild_id in await self.config.all_guilds():
            group = self.config.guild_from_id(guild_id)
            async with group.xp.get_lock():
                await group.clear_raw("xp", uid)
                async with group.progress.get_lock():
                    await group.progress.clear_raw(uid)
                async with group.milestones.get_lock():
                    await group.milestones.clear_raw(uid)
            async with group.names.get_lock():
                await group.clear_raw("names", uid)
            async with group.period_xp() as data:
                forget_user(data, uid)
            async with group.earned_today() as data:
                data["xp"].pop(uid, None)
        await self._calendar_user_data(user_id, delete=True)
        for cache in (
            self._last_msg,
            self._last_rxn,
            self._last_voice,
            self._recent_messages,
            self._reaction_once,
        ):
            for key in list(cache):
                if key[1] == user_id:
                    cache.pop(key, None)

    async def red_get_data_for_user(self, *, user_id):
        data = {}
        for guild_id, config in (await self.config.all_guilds()).items():
            uid = str(user_id)
            periods = user_periods(config["period_xp"], uid)
            if (
                uid in config.get("xp", {})
                or uid in config.get("names", {})
                or periods["days"]
                or periods["season"]
                or periods["archives"]
                or uid in config["milestones"]
                or uid in config["progress"]
            ):
                data[str(guild_id)] = {
                    "xp": config.get("xp", {}).get(uid, 0),
                    "name": config.get("names", {}).get(uid),
                    "periods": periods,
                    "daily_earned": config["earned_today"]["xp"].get(uid, 0),
                    "milestones": config["milestones"].get(uid, {}),
                    "progress": config["progress"].get(uid, {}),
                }
        calendar = await self._calendar_user_data(user_id)
        if calendar:
            data["pending_season_announcements"] = calendar
        return {"levelplus.json": io.BytesIO(json.dumps(data, indent=2).encode())} if data else {}

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        self._settings_cache.pop(guild.id, None)
        self._settings_locks.pop(guild.id, None)
        for cache in (
            self._last_msg,
            self._last_rxn,
            self._last_voice,
            self._recent_messages,
            self._reaction_once,
        ):
            for key in list(cache):
                if key[0] == guild.id:
                    cache.pop(key, None)
