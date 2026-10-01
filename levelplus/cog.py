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
from fractions import Fraction
from heapq import nlargest
from types import SimpleNamespace
from typing import List, Optional, Tuple

import discord
from discord.ext import commands, tasks
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config

from .constants import DEFAULTS_GUILD, WORD_RE
from .events import guild_enabled
from .levels import cumulative_xp, level_from_xp
from .presentation import Presentation, settings

log = logging.getLogger(__name__)


class LevelPlus(redcommands.Cog):
    """Arcane-style leveling: messages/reactions/voice/slash XP, leaderboard, CSV import, tests, calibration, and purge tools."""

    async def _reply(self, ctx, content=None, **kwargs):
        return await self._presentation.send(ctx, content, **kwargs)

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self._presentation = Presentation("LevelPlus", "level")
        self.config: Config = Config.get_conf(self, identifier=0x1EAF01, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)

        self._settings_cache = {}
        self._settings_locks = defaultdict(asyncio.Lock)
        self._last_msg = OrderedDict()
        self._last_rxn = OrderedDict()
        self._last_voice = OrderedDict()

    def cog_unload(self) -> None:
        self.voice_tick.cancel()

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

    async def _add_xp(self, guild, user, amount):
        settings = await self._settings(guild)
        group = self.config.guild(guild)
        async with group.xp.get_lock():
            old_xp = await self._get_xp(guild, user.id)
            new_xp = old_xp + max(0, int(amount))
            if new_xp != old_xp:
                await group.set_raw("xp", str(user.id), value=new_xp)
        return self._level(old_xp, settings), self._level(new_xp, settings)

    async def current_level(self, guild, user_id):
        return self._level(await self._get_xp(guild, user_id), await self._settings(guild))

    async def maybe_announce_levelup(
        self, guild: discord.Guild, member: discord.Member, old: int, new: int
    ) -> None:
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
        try:
            await self._presentation.send(
                ch, msg, title="Level up", tone="success", allowed_mentions=None
            )
        except discord.HTTPException:
            log.debug("Level-up announcement could not be sent", exc_info=True)

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
        if amount <= 0 or not self._cooldown(
            self._last_msg, (message.guild.id, message.author.id), int(conf["cooldown"])
        ):
            return
        await self._remember_name(message.guild, message.author)
        old, new = await self._add_xp(message.guild, message.author, amount)
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
            old, new = await self._add_xp(guild, member, value)
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
                                updates.append((member, amount))
                if not updates:
                    continue
                announcements = []
                async with self.config.guild(guild).xp() as data:
                    for member, amount in updates:
                        uid = str(member.id)
                        old_xp = int(data.get(uid, 0))
                        data[uid] = old_xp + amount
                        old, new = (
                            self._level(old_xp, settings),
                            self._level(data[uid], settings),
                        )
                        if new > old:
                            announcements.append((member, old, new))
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
            interaction.guild, interaction.user, max(1, int(settings["message"]["min"]))
        )
        await self.maybe_announce_levelup(interaction.guild, interaction.user, old, new)

    # ---------- commands ----------
    @redcommands.group(name="level", invoke_without_command=True)
    @redcommands.guild_only()
    async def level(self, ctx: redcommands.Context):
        g = await self._g(ctx.guild)
        base, inc = await self._lin(ctx.guild)
        lu_chan = f"<#{g['levelup']['channel_id']}>" if g["levelup"]["channel_id"] else "current"
        lu_tpl = g["levelup"]["template"][:40]
        lines = [
            f"Curve={g['curve']} Mult={g['multiplier']} MaxLevel={g['max_level'] or '∞'}  Linear(base={base:.3f}, inc={inc:.3f})",
            f"Message: enabled={g['message']['enabled']} mode={g['message']['mode']} min={g['message']['min']} max={g['message']['max']} cd={g['message']['cooldown']}s",
            f"Reaction: enabled={g['reaction']['enabled']} awards={g['reaction']['awards']} min={g['reaction']['min']} max={g['reaction']['max']} cd={g['reaction']['cooldown']}s",
            f"Voice: enabled={g['voice']['enabled']} min={g['voice']['min']} max={g['voice']['max']} tick={g['voice']['cooldown']}s min_members={g['voice']['min_members']} anti_afk={g['voice']['anti_afk']}",
            f"Restrictions: no_channels={len(g['restrictions']['no_channels'])} no_roles={len(g['restrictions']['no_roles'])} thread={g['restrictions']['thread_xp']} forum={g['restrictions']['forum_xp']} TIV={g['restrictions']['text_in_voice_xp']} slash={g['restrictions']['slash_command_xp']}",
            f"LevelUp: enabled={g['levelup']['enabled']} channel={lu_chan} template={lu_tpl}…",
            f"Users tracked: {len(g['xp'])}",
        ]
        await self._reply(
            ctx,
            settings(("\n".join(lines)), lang="ini"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @level.command(name="help")
    async def level_help(self, ctx: redcommands.Context):
        """Pretty, sectioned help."""
        p = ctx.clean_prefix
        e = discord.Embed(title="LevelPlus - Commands", color=discord.Color.blurple())
        e.description = f"✨ Cleaner help • examples use `{p}` as prefix."
        e.add_field(
            name="🧩 Core",
            value=f"• `{p}level` • `{p}level help` • `{p}level diag`\n• `{p}level show [@user]` • `{p}level leaderboard [N]`\n• `{p}level testmsg [@user]` • `{p}level testup [@user] [levels]`",
            inline=False,
        )
        e.add_field(
            name="📈 Formula & Scale",
            value=f"• `{p}level formula curve <linear|exponential|constant>`\n• `{p}level formula multiplier <float>` • `{p}level formula maxlevel <0|N>`\n• `{p}level formula preset arcane` • `{p}level formula calibrate <L1> <XP1> <L2> <XP2>`\n• `{p}level formula linear base <float>` • `linear inc <float>`",
            inline=False,
        )
        e.add_field(
            name="💬 Message XP",
            value=f"• `{p}level message enable [true|false]` • `mode <perword|random|none>`\n• `min <n>` `max <n>` `cooldown <sec>`",
            inline=False,
        )
        e.add_field(
            name="➕ Reaction XP",
            value=f"• `{p}level reaction enable [true|false]` • `awards <both|author|reactor|none>`\n• `min <n>` `max <n>` `cooldown <sec>`",
            inline=False,
        )
        e.add_field(
            name="🎧 Voice XP",
            value=f"• `{p}level voice enable [true|false]` • `range <min> <max>` • `cooldown <sec>`\n• `minmembers <n>` • `antiafk [true|false]`",
            inline=False,
        )
        e.add_field(
            name="🚫 Restrictions",
            value=f"• `{p}level restrict nochannels add|remove|list|clear <#ch>`\n• `{p}level restrict noroles add|remove|list|clear <@role>`\n• `{p}level restrict toggles <threadxp|forumxp|textvoicexp|slashxp> [true|false]`",
            inline=False,
        )
        e.add_field(
            name="🎉 Level-up Message",
            value=f"• `{p}level levelup enable [true|false]` • `channel [#ch|none]`\n• `template <text with {{user.*}}>`",
            inline=False,
        )
        e.add_field(
            name="🗃️ XP Admin & Migration",
            value=f"• `{p}level xp set @user <amt>` • `add @user <amt>` • `setid <id> <amt>`\n• `exportcsv` • `importcsv` • `importlines`\n• `remove @user` • `removeid <id>` • `purgebots` • `clear yes`",
            inline=False,
        )
        e.add_field(
            name="🔍 Lookup & Aliases",
            value=f"• `{p}level lookup <name|@|id>`\n• `{p}level name set @user <alias>` • `name setid <id> <alias>` • `name get <id>`",
            inline=False,
        )
        await self._reply(ctx, embed=e)

    @level.command()
    async def diag(self, ctx: redcommands.Context):
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
        m = member or ctx.author
        conf = await self.config.guild(ctx.guild).levelup()
        if not conf["enabled"]:
            return await self._reply(ctx, "Level-up messages are disabled.")
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
            ch, f"[TEST] {msg}", title="Level up preview", tone="success", allowed_mentions=None
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
        await self._reply(ctx, f"Gave {member.mention} **{amount}** XP (L{old} to L{new}).")

    @level.command()
    async def show(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
        m = member or ctx.author
        xp = await self._get_xp(ctx.guild, m.id)
        g = await self._g(ctx.guild)
        base, inc = await self._lin(ctx.guild)
        lvl = level_from_xp(xp, g["curve"], float(g["multiplier"]), int(g["max_level"]), base, inc)
        await self._reply(ctx, f"{m.mention} - XP: **{xp}**, Level: **{lvl}**")

    @level.command()
    async def leaderboard(self, ctx: redcommands.Context, top: int = 10):
        g = await self._g(ctx.guild)
        base, inc = await self._lin(ctx.guild)
        names = g.get("names", {})
        items = nlargest(
            max(1, min(50, top)),
            ((int(uid), int(xp)) for uid, xp in g["xp"].items()),
            key=lambda t: t[1],
        )
        if not items:
            return await self._reply(ctx, "No XP yet.")
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

    # ---- formula
    @level.group(name="formula")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def formula(self, ctx: redcommands.Context): ...

    @formula.command(name="curve")
    async def formula_curve(self, ctx: redcommands.Context, curve: str):
        curve = curve.lower()
        if curve not in {"linear", "exponential", "constant"}:
            return await self._reply(ctx, "Curve must be linear|exponential|constant.")
        await self.config.guild(ctx.guild).curve.set(curve)
        await self._presentation.confirm(ctx)

    @formula.command(name="multiplier")
    async def formula_mult(self, ctx: redcommands.Context, mult: float):
        if not math.isfinite(mult):
            return await self._reply(ctx, "Use a finite numeric value.")
        await self.config.guild(ctx.guild).multiplier.set(float(max(0.1, min(10.0, mult))))
        await self._presentation.confirm(ctx)

    @formula.command(name="maxlevel")
    async def formula_maxlvl(self, ctx: redcommands.Context, level: int):
        await self.config.guild(ctx.guild).max_level.set(int(max(0, level)))
        await self._presentation.confirm(ctx)

    @formula.group(name="linear")
    async def formula_linear(self, ctx: redcommands.Context): ...

    @formula_linear.command(name="base")
    async def formula_linear_base(self, ctx: redcommands.Context, value: float):
        if not math.isfinite(value):
            return await self._reply(ctx, "Use a finite numeric value.")
        await self.config.guild(ctx.guild).linear.base.set(float(max(0.0, value)))
        await self._presentation.confirm(ctx)

    @formula_linear.command(name="inc")
    async def formula_linear_inc(self, ctx: redcommands.Context, value: float):
        if not math.isfinite(value):
            return await self._reply(ctx, "Use a finite numeric value.")
        await self.config.guild(ctx.guild).linear.inc.set(float(max(0.0, value)))
        await self._presentation.confirm(ctx)

    @formula.command(name="preset")
    async def formula_preset(self, ctx: redcommands.Context, which: str):
        which = which.lower()
        if which != "arcane":
            return await self._reply(ctx, "Only `arcane` preset is available.")
        await self.config.guild(ctx.guild).linear.set({"base": 83.2, "inc": 100.433})
        await self.config.guild(ctx.guild).curve.set("linear")
        await self._reply(
            ctx, "Set curve to **linear** with Arcane-like preset (base=83.2, inc=100.433)."
        )

    @formula.command(name="calibrate")
    async def formula_calibrate(
        self, ctx: redcommands.Context, L1: int, XP1: int, L2: int, XP2: int
    ):
        if L1 <= 0 or L2 <= 0 or L1 == L2:
            return await self._reply(ctx, "Levels must be positive and different.")
        if XP1 < 0 or XP2 < 0:
            return await self._reply(ctx, "XP thresholds must be nonnegative.")
        group = self.config.guild(ctx.guild)
        multiplier = float(await group.multiplier())
        if not math.isfinite(multiplier) or multiplier <= 0:
            return await self._reply(ctx, "Set a finite positive multiplier before calibrating.")
        factor = Fraction(str(multiplier))
        step = 2 * (Fraction(XP2, L2) - Fraction(XP1, L1)) / (L2 - L1) / factor
        start = Fraction(XP1, L1) / factor - (L1 - 1) * step / 2
        if step < 0 or start < 0:
            return await self._reply(ctx, "Calibration failed (negative base/inc). Check inputs.")
        try:
            b, d = float(start), float(step)
        except OverflowError:
            return await self._reply(
                ctx, "Calibration failed (coefficients are too large). Check inputs."
            )
        if not all(math.isfinite(value) for value in (b, d)) or any(
            cumulative_xp(level, "linear", multiplier, b, d) != xp
            for level, xp in ((L1, XP1), (L2, XP2))
        ):
            return await self._reply(
                ctx,
                "Calibration failed (coefficients cannot reproduce those XP thresholds). Check inputs.",
            )
        await group.linear.set({"base": b, "inc": d})
        await group.curve.set("linear")
        await self._reply(ctx, f"Calibrated linear curve: base=**{b:.3f}**, inc=**{d:.3f}**")

    # ---- admin: message xp
    @level.group(name="message")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def message_grp(self, ctx: redcommands.Context): ...

    @message_grp.command(name="mode")
    async def message_mode(self, ctx: redcommands.Context, mode: str):
        mode = mode.lower()
        if mode not in {"none", "random", "perword"}:
            return await self._reply(ctx, "Mode: none|random|perword")
        await self.config.guild(ctx.guild).message.mode.set(mode)
        await self._presentation.confirm(ctx)

    @message_grp.command(name="min")
    async def message_min(self, ctx: redcommands.Context, value: int):
        await self.config.guild(ctx.guild).message.min.set(int(max(0, value)))
        await self._presentation.confirm(ctx)

    @message_grp.command(name="max")
    async def message_max(self, ctx: redcommands.Context, value: int):
        await self.config.guild(ctx.guild).message.max.set(int(max(0, value)))
        await self._presentation.confirm(ctx)

    @message_grp.command(name="cooldown")
    async def message_cd(self, ctx: redcommands.Context, seconds: int):
        await self.config.guild(ctx.guild).message.cooldown.set(int(max(0, min(3600, seconds))))
        await self._presentation.confirm(ctx)

    @message_grp.command(name="enable")
    async def message_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).message.enabled())
        await self.config.guild(ctx.guild).message.enabled.set(bool(enabled))
        await self._presentation.confirm(ctx)

    # ---- admin: reaction xp
    @level.group(name="reaction")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def rx_grp(self, ctx: redcommands.Context): ...

    @rx_grp.command(name="awards")
    async def rx_awards(self, ctx: redcommands.Context, awards: str):
        awards = awards.lower()
        if awards not in {"none", "both", "author", "reactor"}:
            return await self._reply(ctx, "Awards: none|both|author|reactor")
        await self.config.guild(ctx.guild).reaction.awards.set(awards)
        await self._presentation.confirm(ctx)

    @rx_grp.command(name="min")
    async def rx_min(self, ctx: redcommands.Context, value: int):
        await self.config.guild(ctx.guild).reaction.min.set(int(max(0, value)))
        await self._presentation.confirm(ctx)

    @rx_grp.command(name="max")
    async def rx_max(self, ctx: redcommands.Context, value: int):
        await self.config.guild(ctx.guild).reaction.max.set(int(max(0, value)))
        await self._presentation.confirm(ctx)

    @rx_grp.command(name="cooldown")
    async def rx_cd(self, ctx: redcommands.Context, seconds: int):
        await self.config.guild(ctx.guild).reaction.cooldown.set(int(max(0, min(3600, seconds))))
        await self._presentation.confirm(ctx)

    @rx_grp.command(name="enable")
    async def rx_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).reaction.enabled())
        await self.config.guild(ctx.guild).reaction.enabled.set(bool(enabled))
        await self._presentation.confirm(ctx)

    # ---- admin: voice xp
    @level.group(name="voice")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def voice_grp(self, ctx: redcommands.Context): ...

    @voice_grp.command(name="enable")
    async def voice_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).voice.enabled())
        await self.config.guild(ctx.guild).voice.enabled.set(bool(enabled))
        await self._presentation.confirm(ctx)

    @voice_grp.command(name="range")
    async def voice_range(self, ctx: redcommands.Context, min_points: int, max_points: int):
        max_points = max(max_points, min_points)
        await self.config.guild(ctx.guild).voice.min.set(int(max(0, min_points)))
        await self.config.guild(ctx.guild).voice.max.set(int(max(0, max_points)))
        await self._presentation.confirm(ctx)

    @voice_grp.command(name="cooldown")
    async def voice_cd(self, ctx: redcommands.Context, seconds: int):
        await self.config.guild(ctx.guild).voice.cooldown.set(int(max(15, min(3600, seconds))))
        await self._presentation.confirm(ctx)

    @voice_grp.command(name="minmembers")
    async def voice_minmembers(self, ctx: redcommands.Context, count: int):
        await self.config.guild(ctx.guild).voice.min_members.set(int(max(1, min(99, count))))
        await self._presentation.confirm(ctx)

    @voice_grp.command(name="antiafk")
    async def voice_antiafk(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).voice.anti_afk())
        await self.config.guild(ctx.guild).voice.anti_afk.set(bool(enabled))
        await self._presentation.confirm(ctx)

    # ---- restrictions
    @level.group(name="restrict")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def restrict(self, ctx: redcommands.Context): ...

    @restrict.group(name="nochannels")
    async def res_noch(self, ctx: redcommands.Context): ...

    @res_noch.command(name="add")
    async def res_noch_add(self, ctx: redcommands.Context, channel: discord.TextChannel):
        data = await self.config.guild(ctx.guild).restrictions.no_channels()
        if channel.id in data:
            return await self._reply(ctx, "Already set.")
        data.append(channel.id)
        await self.config.guild(ctx.guild).restrictions.no_channels.set(data)
        await self._presentation.confirm(ctx)

    @res_noch.command(name="remove")
    async def res_noch_remove(self, ctx: redcommands.Context, channel: discord.TextChannel):
        data = await self.config.guild(ctx.guild).restrictions.no_channels()
        if channel.id not in data:
            return await self._reply(ctx, "Not present.")
        data = [c for c in data if c != channel.id]
        await self.config.guild(ctx.guild).restrictions.no_channels.set(data)
        await self._presentation.confirm(ctx)

    @res_noch.command(name="list")
    async def res_noch_list(self, ctx: redcommands.Context):
        data = await self.config.guild(ctx.guild).restrictions.no_channels()
        await self._reply(
            ctx, "No-XP channels: " + (", ".join(f"<#{c}>" for c in data) if data else "none")
        )

    @res_noch.command(name="clear")
    async def res_noch_clear(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).restrictions.no_channels.set([])
        await self._presentation.confirm(ctx)

    @restrict.group(name="noroles")
    async def res_noroles(self, ctx: redcommands.Context): ...

    @res_noroles.command(name="add")
    async def res_noroles_add(self, ctx: redcommands.Context, role: discord.Role):
        data = await self.config.guild(ctx.guild).restrictions.no_roles()
        if role.id in data:
            return await self._reply(ctx, "Already set.")
        data.append(role.id)
        await self.config.guild(ctx.guild).restrictions.no_roles.set(data)
        await self._presentation.confirm(ctx)

    @res_noroles.command(name="remove")
    async def res_noroles_remove(self, ctx: redcommands.Context, role: discord.Role):
        data = await self.config.guild(ctx.guild).restrictions.no_roles()
        if role.id not in data:
            return await self._reply(ctx, "Not present.")
        data = [r for r in data if r != role.id]
        await self.config.guild(ctx.guild).restrictions.no_roles.set(data)
        await self._presentation.confirm(ctx)

    @res_noroles.command(name="list")
    async def res_noroles_list(self, ctx: redcommands.Context):
        data = await self.config.guild(ctx.guild).restrictions.no_roles()
        roles = [ctx.guild.get_role(r).mention for r in data if ctx.guild.get_role(r)] or ["none"]
        await self._reply(ctx, "No-XP roles: " + ", ".join(roles))

    @res_noroles.command(name="clear")
    async def res_noroles_clear(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).restrictions.no_roles.set([])
        await self._presentation.confirm(ctx)

    @restrict.command(name="toggles")
    async def restrict_toggles(
        self, ctx: redcommands.Context, feature: str, enabled: Optional[bool] = None
    ):
        feature = feature.lower()
        if feature not in {"threadxp", "forumxp", "textvoicexp", "slashxp"}:
            return await self._reply(ctx, "feature: threadxp|forumxp|textvoicexp|slashxp")
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
    @level.group(name="levelup")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def levelup_grp(self, ctx: redcommands.Context): ...

    @levelup_grp.command(name="enable")
    async def levelup_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).levelup.enabled())
        await self.config.guild(ctx.guild).levelup.enabled.set(bool(enabled))
        await self._presentation.confirm(ctx)

    @levelup_grp.command(name="channel")
    async def levelup_channel(
        self, ctx: redcommands.Context, channel: Optional[discord.TextChannel]
    ):
        await self.config.guild(ctx.guild).levelup.channel_id.set(channel.id if channel else None)
        await self._presentation.confirm(ctx)

    @levelup_grp.command(name="template")
    async def levelup_template(self, ctx: redcommands.Context, *, text: str):
        await self.config.guild(ctx.guild).levelup.template.set(text[:500])
        await self._presentation.confirm(ctx)

    # ---- XP admin & migration
    @level.group(name="xp")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def xpgrp(self, ctx: redcommands.Context): ...

    @xpgrp.command(name="set")
    async def xp_set(self, ctx: redcommands.Context, member: discord.Member, amount: int):
        await self._remember_name(ctx.guild, member)
        await self._set_xp(ctx.guild, member.id, amount)
        await self._presentation.confirm(ctx)

    @xpgrp.command(name="setid")
    async def xp_setid(self, ctx: redcommands.Context, user_id: int, amount: int):
        await self._set_xp(ctx.guild, user_id, amount)
        await self._presentation.confirm(ctx)

    @xpgrp.command(name="add")
    async def xp_add(self, ctx: redcommands.Context, member: discord.Member, amount: int):
        await self._remember_name(ctx.guild, member)
        old, new = await self._add_xp(ctx.guild, member, amount)
        await self.maybe_announce_levelup(ctx.guild, member, old, new)
        await self._presentation.confirm(ctx)

    @xpgrp.command(name="remove")
    async def xp_remove(self, ctx: redcommands.Context, member: discord.Member):
        async with self.config.guild(ctx.guild).xp() as data:
            data.pop(str(member.id), None)
        await self._reply(ctx, f"Removed XP row for {member.mention}.")

    @xpgrp.command(name="removeid")
    async def xp_removeid(self, ctx: redcommands.Context, user_id: int):
        async with self.config.guild(ctx.guild).xp() as data:
            data.pop(str(user_id), None)
        await self._reply(ctx, f"Removed XP row for `{user_id}`.")

    @xpgrp.command(name="purgebots")
    async def xp_purgebots(self, ctx: redcommands.Context):
        async with self.config.guild(ctx.guild).xp() as data:
            before = len(data)
            bot_ids = {str(m.id) for m in ctx.guild.members if m.bot}
            for bid in bot_ids:
                data.pop(bid, None)
        await self._reply(ctx, f"Purged **{before - len(data)}** bot row(s).")

    @xpgrp.command(name="clear")
    async def xp_clear(self, ctx: redcommands.Context, confirm: Optional[str] = None):
        if confirm != "yes":
            return await self._reply(
                ctx,
                "This will WIPE XP & aliases for this guild. Confirm with `level xp clear yes`.",
            )
        group = self.config.guild(ctx.guild)
        async with group.xp() as data:
            data.clear()
        async with group.names() as names:
            names.clear()
        await self._reply(ctx, "Cleared XP and aliases.")

    @xpgrp.command(name="exportcsv")
    async def xp_export(self, ctx: redcommands.Context):
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
        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            if attachment.size > 8_000_000:
                return await self._reply(ctx, "CSV exceeds the 8 MB import limit.")
            try:
                raw = (await attachment.read()).decode("utf-8-sig")
            except (discord.HTTPException, UnicodeDecodeError):
                return await self._reply(ctx, "Could not read a UTF-8 CSV attachment.")
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
            return await self._reply(ctx, "Invalid CSV quoting. No rows were imported.")
        if not parsed:
            return await self._reply(ctx, "No rows parsed.")
        group = self.config.guild(ctx.guild)
        async with group.xp() as xpmap:
            for uid, xp, _ in parsed:
                xpmap[uid] = xp
        async with group.names() as names:
            for uid, _, alias in parsed:
                if alias is not None:
                    names[uid] = alias
        await self._reply(ctx, f"Imported **{len(parsed)}** user(s).")

    @xpgrp.command(name="importlines")
    async def xp_import_lines(self, ctx: redcommands.Context, *, lines: str):
        """Import identifier,integer XP rows, rejecting ambiguous names."""
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
            return await self._reply(ctx, "Invalid CSV quoting. No rows were imported.")
        if parsed:
            group = self.config.guild(ctx.guild)
            async with group.xp() as xpmap:
                for uid, xp, _ in parsed:
                    xpmap[uid] = xp
            async with group.names() as names:
                for uid, _, alias in parsed:
                    names[uid] = alias
        await self._reply(ctx, f"Imported **{len(parsed)}** row(s), skipped **{skipped}**.")

    # ---- lookup & aliases
    @level.command(name="lookup")
    async def level_lookup(self, ctx: redcommands.Context, *, query: str):
        """
        Lookup IDs by **@mention**, **raw ID**, or name fragment.
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
            return await self._reply(ctx, "No matches.")
        lines = [f"{i:>2}. {name} - `{uid}`" for i, (uid, name) in enumerate(results[:20], start=1)]
        await self._reply(
            ctx,
            settings(("\n".join(lines)), lang="ini"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @level.group(name="name")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def namegrp(self, ctx: redcommands.Context): ...

    @namegrp.command(name="set")
    async def name_set(self, ctx: redcommands.Context, member: discord.Member, *, alias: str):
        async with self.config.guild(ctx.guild).names() as names:
            names[str(member.id)] = alias[:100]
        await self._presentation.confirm(ctx)

    @namegrp.command(name="setid")
    async def name_setid(self, ctx: redcommands.Context, user_id: int, *, alias: str):
        async with self.config.guild(ctx.guild).names() as names:
            names[str(user_id)] = alias[:100]
        await self._presentation.confirm(ctx)

    @namegrp.command(name="get")
    async def name_get(self, ctx: redcommands.Context, user_id: int):
        names = await self.config.guild(ctx.guild).names()
        alias = names.get(str(user_id), "none")
        await self._reply(ctx, f"`{user_id}` → {alias}")

    async def cog_load(self):
        self.voice_tick.start()

    async def cog_before_invoke(self, ctx):
        self._settings_cache.pop(ctx.guild.id, None)

    async def cog_after_invoke(self, ctx):
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
            keys = [key for key in DEFAULTS_GUILD if key not in {"xp", "names"}]
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

    async def red_delete_data_for_user(self, *, requester, user_id):
        uid = str(user_id)
        for guild_id in await self.config.all_guilds():
            group = self.config.guild_from_id(guild_id)
            async with group.xp.get_lock():
                await group.clear_raw("xp", uid)
            async with group.names.get_lock():
                await group.clear_raw("names", uid)
        for cache in (self._last_msg, self._last_rxn, self._last_voice):
            for key in list(cache):
                if key[1] == user_id:
                    cache.pop(key, None)

    async def red_get_data_for_user(self, *, user_id):
        data = {}
        for guild_id, config in (await self.config.all_guilds()).items():
            uid = str(user_id)
            if uid in config.get("xp", {}) or uid in config.get("names", {}):
                data[str(guild_id)] = {
                    "xp": config.get("xp", {}).get(uid, 0),
                    "name": config.get("names", {}).get(uid),
                }
        return {"levelplus.json": io.BytesIO(json.dumps(data, indent=2).encode())} if data else {}

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        self._settings_cache.pop(guild.id, None)
        self._settings_locks.pop(guild.id, None)
        for cache in (self._last_msg, self._last_rxn, self._last_voice):
            for key in list(cache):
                if key[0] == guild.id:
                    cache.pop(key, None)
