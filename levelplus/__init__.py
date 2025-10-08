# path: cogs/levelplus/__init__.py
from __future__ import annotations

import asyncio
import csv
import io
import math
import random
import re
import time
from typing import Dict, Iterable, List, Optional, Tuple

import discord
from discord.ext import commands, tasks
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config
from redbot.core.utils.chat_formatting import box

__red_end_user_data_statement__ = (
    "This cog stores per-guild leveling settings and per-user XP totals. "
    "Data persists across leaves/joins to preserve user progress. "
    "Admins may export or erase specific users via commands."
)

# ---------------------------- defaults ----------------------------
DEFAULTS_GUILD = {
    "curve": "linear",
    "multiplier": 1.0,
    "max_level": 0,  # 0 => unlimited
    "message": {"enabled": True, "mode": "perword", "min": 1, "max": 1, "cooldown": 60},
    "reaction": {"enabled": True, "awards": "both", "min": 25, "max": 25, "cooldown": 300},
    "voice": {"enabled": True, "min": 15, "max": 40, "cooldown": 180, "min_members": 1, "anti_afk": False},
    "restrictions": {
        "no_channels": [],
        "no_roles": [],
        "thread_xp": True,
        "forum_xp": True,
        "text_in_voice_xp": True,
        "slash_command_xp": True,
    },
    "levelup": {
        "enabled": True,
        "channel_id": None,
        "template": "{user.mention} has reached level **{user.level}**! GG!",
        "image": False,
    },
    "xp": {},  # {user_id(str): int}
}

WORD_RE = re.compile(r"\b\w+\b", re.UNICODE)


# ---------------------------- util: math ----------------------------
def level_thresholds(curve: str, mult: float, max_level: int) -> List[int]:
    curve = (curve or "linear").lower()

    def per_level_cost(lvl: int) -> float:
        if curve == "constant":
            return 100.0 * mult
        if curve == "exponential":
            return 100.0 * (1.25 ** (lvl - 1)) * mult
        return (100.0 + 20.0 * (lvl - 1)) * mult  # linear

    cap = max(1, max_level) if max_level > 0 else 200
    out, total = [0], 0.0
    for lvl in range(1, cap + 1):
        total += per_level_cost(lvl)
        out.append(int(round(total)))
    return out


def level_from_xp(xp: int, curve: str, mult: float, max_level: int) -> int:
    th = level_thresholds(curve, mult, max_level)
    lo, hi = 0, len(th) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if xp >= th[mid]:
            lo = mid + 1
        else:
            hi = mid - 1
    lvl = max(0, hi)
    if max_level > 0:
        lvl = min(lvl, max_level)
    return lvl


# ---------------------------- cog ----------------------------
class LevelPlus(redcommands.Cog):
    """Arcane-style leveling with messages/reactions/voice/slash XP, leaderboards, and CSV import."""

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        # fixed: use a real hex literal
        self.config: Config = Config.get_conf(self, identifier=0x1EAF01, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)

        self._last_msg: Dict[Tuple[int, int], float] = {}
        self._last_rxn: Dict[Tuple[int, int], float] = {}
        self._last_voice: Dict[Tuple[int, int], float] = {}

        self.voice_tick.start()

    def cog_unload(self) -> None:
        self.voice_tick.cancel()

    # ----------- core getters/setters -----------
    async def _get_xp(self, guild: discord.Guild, user_id: int) -> int:
        data = await self.config.guild(guild).xp()
        return int(data.get(str(user_id), 0))

    async def _set_xp(self, guild: discord.Guild, user_id: int, value: int) -> None:
        data = await self.config.guild(guild).xp()
        data[str(user_id)] = int(max(0, value))
        await self.config.guild(guild).xp.set(data)

    async def _add_xp(self, guild: discord.Guild, user: discord.abc.User, amount: int) -> Tuple[int, int]:
        if amount <= 0:
            lvl = await self.current_level(guild, user.id)
            return lvl, lvl
        gconf = await self.config.guild(guild).all()
        data = gconf["xp"]
        old_xp = int(data.get(str(user.id), 0))
        new_xp = old_xp + int(amount)
        data[str(user.id)] = new_xp
        await self.config.guild(guild).xp.set(data)

        old_lvl = level_from_xp(old_xp, gconf["curve"], float(gconf["multiplier"]), int(gconf["max_level"]))
        new_lvl = level_from_xp(new_xp, gconf["curve"], float(gconf["multiplier"]), int(gconf["max_level"]))
        return old_lvl, new_lvl

    async def current_level(self, guild: discord.Guild, user_id: int) -> int:
        gconf = await self.config.guild(guild).all()
        xp = int(gconf["xp"].get(str(user_id), 0))
        return level_from_xp(xp, gconf["curve"], float(gconf["multiplier"]), int(gconf["max_level"]))

    async def maybe_announce_levelup(self, guild: discord.Guild, member: discord.Member, old: int, new: int) -> None:
        if new <= old:
            return
        conf = await self.config.guild(guild).levelup()
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
        u = type("U", (), {
            "mention": member.mention,
            "name": member.display_name,
            "level": await self.current_level(guild, member.id),
            "xp": await self._get_xp(guild, member.id),
        })()
        try:
            msg = template.format(user=u)
        except Exception:
            msg = f"{member.mention} has reached level **{u.level}**!"
        try:
            await ch.send(msg)
        except discord.Forbidden:
            pass

    # ---------------------- listeners: message ----------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        guild = message.guild
        member = message.author
        gconf = await self.config.guild(guild).all()
        if not gconf["message"]["enabled"]:
            return
        if message.channel.id in set(gconf["restrictions"]["no_channels"]):
            return
        role_ids = {r.id for r in getattr(member, "roles", [])}
        if role_ids.intersection(set(gconf["restrictions"]["no_roles"])):
            return
        if isinstance(message.channel, discord.Thread) and not gconf["restrictions"]["thread_xp"]:
            return
        if getattr(message.channel, "is_forum", False) and not gconf["restrictions"]["forum_xp"]:
            return

        key = (guild.id, member.id)
        cd = int(gconf["message"]["cooldown"])
        last = self._last_msg.get(key, 0.0)
        if cd and time.time() - last < cd:
            return
        self._last_msg[key] = time.time()

        mode = (gconf["message"]["mode"] or "perword").lower()
        msg_min = int(gconf["message"]["min"])
        msg_max = max(int(gconf["message"]["max"]), msg_min)
        if mode == "none":
            return
        elif mode == "random":
            amount = random.randint(msg_min, msg_max)
        else:
            words = len(WORD_RE.findall(message.content or ""))
            per = max(1, msg_min)
            amount = min(words * per, msg_max if msg_max > 0 else words * per)

        old, new = await self._add_xp(guild, member, amount)
        await self.maybe_announce_levelup(guild, member, old, new)

    # ---------------------- listeners: reactions ----------------------
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not guild:
            return
        gconf = await self.config.guild(guild).all()
        if not gconf["reaction"]["enabled"]:
            return
        channel = guild.get_channel(payload.channel_id)
        if isinstance(channel, discord.abc.GuildChannel):
            if channel.id in set(gconf["restrictions"]["no_channels"]):
                return

        rx_min = int(gconf["reaction"]["min"]); rx_max = max(int(gconf["reaction"]["max"]), rx_min)
        value = random.randint(rx_min, rx_max)

        reactor = guild.get_member(payload.user_id)
        if reactor and not reactor.bot:
            key_r = (guild.id, reactor.id)
            cd = int(gconf["reaction"]["cooldown"])
            if cd and time.time() - self._last_rxn.get(key_r, 0.0) < cd:
                reactor = None
            else:
                self._last_rxn[key_r] = time.time()

        author = None
        try:
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                msg = await channel.fetch_message(payload.message_id)
                author = msg.author if msg and msg.author and not msg.author.bot else None
        except Exception:
            author = None

        awards = (gconf["reaction"]["awards"] or "both").lower()
        targets: List[discord.Member] = []
        if awards in ("both", "reactor") and reactor:
            targets.append(reactor)
        if awards in ("both", "author") and author and isinstance(author, discord.Member) and author.id != payload.user_id:
            targets.append(author)

        for m in targets:
            role_ids = {r.id for r in getattr(m, "roles", [])}
            if role_ids.intersection(set(gconf["restrictions"]["no_roles"])):
                continue
            old, new = await self._add_xp(guild, m, value)
            await self.maybe_announce_levelup(guild, m, old, new)

    # ---------------------- voice ticker ----------------------
    @tasks.loop(seconds=20.0)
    async def voice_tick(self):
        await self.bot.wait_until_red_ready()
        for guild in list(self.bot.guilds):
            try:
                gconf = await self.config.guild(guild).all()
            except Exception:
                continue
            vconf = gconf["voice"]
            if not vconf["enabled"]:
                continue
            tick = max(15, int(vconf["cooldown"]))
            for vc in guild.voice_channels:
                members = [m for m in vc.members if not m.bot]
                if len(members) < int(vconf["min_members"]):
                    continue
                for m in members:
                    if vconf["anti_afk"]:
                        vs = m.voice
                        if not vs or vs.afk or vs.self_mute or vs.mute or vs.self_deaf or vs.deaf:
                            continue
                    key = (guild.id, m.id)
                    last = self._last_voice.get(key, 0.0)
                    if time.time() - last < tick:
                        continue
                    self._last_voice[key] = time.time()
                    val = random.randint(int(vconf["min"]), max(int(vconf["max"]), int(vconf["min"])))
                    old, new = await self._add_xp(guild, m, val)
                    await self.maybe_announce_levelup(guild, m, old, new)

    @voice_tick.before_loop
    async def _before_voice(self):
        await self.bot.wait_until_red_ready()

    # ---------------------- slash (any bot) ----------------------
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.user.bot:
            return
        guild = interaction.guild
        gconf = await self.config.guild(guild).all()
        if not gconf["restrictions"]["slash_command_xp"]:
            return
        key = (guild.id, interaction.user.id)
        cd = int(gconf["message"]["cooldown"])
        if cd and time.time() - self._last_msg.get(key, 0.0) < cd:
            return
        self._last_msg[key] = time.time()
        val = max(1, int(gconf["message"]["min"]))
        old, new = await self._add_xp(guild, interaction.user, val)
        if isinstance(interaction.user, discord.Member):
            await self.maybe_announce_levelup(guild, interaction.user, old, new)

    # ---------------------- commands ----------------------
    @redcommands.group(name="level", invoke_without_command=True)
    @redcommands.guild_only()
    async def level(self, ctx: redcommands.Context):
        g = await self.config.guild(ctx.guild).all()
        lines = [
            f"Curve={g['curve']} Mult={g['multiplier']} MaxLevel={g['max_level'] or '∞'}",
            f"Message: enabled={g['message']['enabled']} mode={g['message']['mode']} min={g['message']['min']} max={g['message']['max']} cd={g['message']['cooldown']}s",
            f"Reaction: enabled={g['reaction']['enabled']} awards={g['reaction']['awards']} min={g['reaction']['min']} max={g['reaction']['max']} cd={g['reaction']['cooldown']}s",
            f"Voice: enabled={g['voice']['enabled']} min={g['voice']['min']} max={g['voice']['max']} tick={g['voice']['cooldown']}s min_members={g['voice']['min_members']} anti_afk={g['voice']['anti_afk']}",
            f"Restrictions: no_channels={len(g['restrictions']['no_channels'])} no_roles={len(g['restrictions']['no_roles'])} thread={g['restrictions']['thread_xp']} forum={g['restrictions']['forum_xp']} TIV={g['restrictions']['text_in_voice_xp']} slash={g['restrictions']['slash_command_xp']}",
            f"LevelUp: enabled={g['levelup']['enabled']} channel={(f'<#{g['levelup']['channel_id']}>' if g['levelup']['channel_id'] else 'current')} template={g['levelup']['template'][:40]}…",
            f"Users tracked: {len(g['xp'])}",
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @level.command()
    async def diag(self, ctx: redcommands.Context):
        """Deep self-check: perms, intents, reaction probe, simulated awards."""
        g = await self.config.guild(ctx.guild).all()
        ch = ctx.channel
        perms = ch.permissions_for(ctx.guild.me) if isinstance(ch, (discord.TextChannel, discord.Thread)) else None  # type: ignore
        intents = self.bot.intents

        # reaction probe
        probe_result = "skip"
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try:
                m = await ctx.send("LevelPlus diag probe…")
                try:
                    await m.add_reaction("✅")
                    probe_result = "OK"
                except Exception as e:
                    probe_result = f"add_reaction FAIL: {type(e).__name__}"
                try:
                    await m.delete()
                except Exception:
                    pass
            except Exception as e:
                probe_result = f"send FAIL: {type(e).__name__}"

        # simulate awards (no state change)
        msg_words = 12
        msg_award = (msg_words * max(1, int(g["message"]["min"]))) if g["message"]["mode"] == "perword" else random.randint(int(g["message"]["min"]), max(int(g["message"]["max"]), int(g["message"]["min"]))) if g["message"]["mode"] == "random" else 0
        rx_award = random.randint(int(g["reaction"]["min"]), max(int(g["reaction"]["max"]), int(g["reaction"]["min"]))) if g["reaction"]["enabled"] else 0
        voice_award = random.randint(int(g["voice"]["min"]), max(int(g["voice"]["max"]), int(g["voice"]["min"]))) if g["voice"]["enabled"] else 0

        lines = [
            f"perms(send={getattr(perms,'send_messages',None)} embed={getattr(perms,'embed_links',None)} add_rxn={getattr(perms,'add_reactions',None)} read_hist={getattr(perms,'read_message_history',None)})",
            f"intents(voice_states={intents.voice_states} message_content={intents.message_content})",
            f"reaction_probe={probe_result}",
            f"sim_awards(message≈{msg_award}, reaction≈{rx_award}, voice_tick≈{voice_award})",
            "store_rw=OK" if isinstance(g["xp"], dict) else "store_rw=FAIL",
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @level.command()
    async def show(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
        m = member or ctx.author
        xp = await self._get_xp(ctx.guild, m.id)
        g = await self.config.guild(ctx.guild).all()
        lvl = level_from_xp(xp, g["curve"], float(g["multiplier"]), int(g["max_level"]))
        await ctx.send(f"{m.mention} — XP: **{xp}**, Level: **{lvl}**")

    @level.command()
    async def leaderboard(self, ctx: redcommands.Context, top: int = 10):
        g = await self.config.guild(ctx.guild).all()
        items = sorted(((int(uid), xp) for uid, xp in g["xp"].items()), key=lambda t: t[1], reverse=True)[:max(1, min(50, top))]
        if not items:
            return await ctx.send("No XP yet.")
        lines = []
        for i, (uid, xp) in enumerate(items, start=1):
            m = ctx.guild.get_member(uid)
            lvl = level_from_xp(xp, g["curve"], float(g["multiplier"]), int(g["max_level"]))
            lines.append(f"{i:>2}. {(m.display_name if m else uid)} — L{lvl} ({xp} xp)")
        await ctx.send(box("\n".join(lines), lang="ini"))

    # ---- admin: formula
    @level.group(name="formula")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def formula(self, ctx: redcommands.Context): ...

    @formula.command(name="curve")
    async def formula_curve(self, ctx: redcommands.Context, curve: str):
        curve = curve.lower()
        if curve not in {"linear", "exponential", "constant"}:
            return await ctx.send("Curve must be linear|exponential|constant.")
        await self.config.guild(ctx.guild).curve.set(curve); await ctx.tick()

    @formula.command(name="multiplier")
    async def formula_mult(self, ctx: redcommands.Context, mult: float):
        await self.config.guild(ctx.guild).multiplier.set(float(max(0.1, min(10.0, mult)))); await ctx.tick()

    @formula.command(name="maxlevel")
    async def formula_maxlvl(self, ctx: redcommands.Context, level: int):
        await self.config.guild(ctx.guild).max_level.set(int(max(0, level))); await ctx.tick()

    # ---- admin: message xp
    @level.group(name="message")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def message_grp(self, ctx: redcommands.Context): ...

    @message_grp.command(name="mode")
    async def message_mode(self, ctx: redcommands.Context, mode: str):
        mode = mode.lower()
        if mode not in {"none", "random", "perword"}:
            return await ctx.send("Mode: none|random|perword")
        await self.config.guild(ctx.guild).message.mode.set(mode); await ctx.tick()

    @message_grp.command(name="min")
    async def message_min(self, ctx: redcommands.Context, value: int):
        await self.config.guild(ctx.guild).message.min.set(int(max(0, value))); await ctx.tick()

    @message_grp.command(name="max")
    async def message_max(self, ctx: redcommands.Context, value: int):
        await self.config.guild(ctx.guild).message.max.set(int(max(0, value))); await ctx.tick()

    @message_grp.command(name="cooldown")
    async def message_cd(self, ctx: redcommands.Context, seconds: int):
        await self.config.guild(ctx.guild).message.cooldown.set(int(max(0, min(3600, seconds)))); await ctx.tick()

    @message_grp.command(name="enable")
    async def message_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).message.enabled())
        await self.config.guild(ctx.guild).message.enabled.set(bool(enabled)); await ctx.tick()

    # ---- admin: reaction xp
    @level.group(name="reaction")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def rx_grp(self, ctx: redcommands.Context): ...

    @rx_grp.command(name="awards")
    async def rx_awards(self, ctx: redcommands.Context, awards: str):
        awards = awards.lower()
        if awards not in {"none", "both", "author", "reactor"}:
            return await ctx.send("Awards: none|both|author|reactor")
        await self.config.guild(ctx.guild).reaction.awards.set(awards); await ctx.tick()

    @rx_grp.command(name="min")
    async def rx_min(self, ctx: redcommands.Context, value: int):
        await self.config.guild(ctx.guild).reaction.min.set(int(max(0, value))); await ctx.tick()

    @rx_grp.command(name="max")
    async def rx_max(self, ctx: redcommands.Context, value: int):
        await self.config.guild(ctx.guild).reaction.max.set(int(max(0, value))); await ctx.tick()

    @rx_grp.command(name="cooldown")
    async def rx_cd(self, ctx: redcommands.Context, seconds: int):
        await self.config.guild(ctx.guild).reaction.cooldown.set(int(max(0, min(3600, seconds)))); await ctx.tick()

    @rx_grp.command(name="enable")
    async def rx_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).reaction.enabled())
        await self.config.guild(ctx.guild).reaction.enabled.set(bool(enabled)); await ctx.tick()

    # ---- admin: voice xp
    @level.group(name="voice")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def voice_grp(self, ctx: redcommands.Context): ...

    @voice_grp.command(name="enable")
    async def voice_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).voice.enabled())
        await self.config.guild(ctx.guild).voice.enabled.set(bool(enabled)); await ctx.tick()

    @voice_grp.command(name="range")
    async def voice_range(self, ctx: redcommands.Context, min_points: int, max_points: int):
        max_points = max(max_points, min_points)
        await self.config.guild(ctx.guild).voice.min.set(int(max(0, min_points)))
        await self.config.guild(ctx.guild).voice.max.set(int(max(0, max_points)))
        await ctx.tick()

    @voice_grp.command(name="cooldown")
    async def voice_cd(self, ctx: redcommands.Context, seconds: int):
        await self.config.guild(ctx.guild).voice.cooldown.set(int(max(15, min(3600, seconds)))); await ctx.tick()

    @voice_grp.command(name="minmembers")
    async def voice_minmembers(self, ctx: redcommands.Context, count: int):
        await self.config.guild(ctx.guild).voice.min_members.set(int(max(1, min(99, count)))); await ctx.tick()

    @voice_grp.command(name="antiafk")
    async def voice_antiafk(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).voice.anti_afk())
        await self.config.guild(ctx.guild).voice.anti_afk.set(bool(enabled)); await ctx.tick()

    # ---- restrictions
    @level.group(name="restrict")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def restrict(self, ctx: redcommands.Context): ...

    @restrict.group(name="nochannels")
    async def res_noch(self, ctx: redcommands.Context): ...

    @res_noch.command(name="add")
    async def res_noch_add(self, ctx: redcommands.Context, channel: discord.TextChannel):
        data = await self.config.guild(ctx.guild).restrictions.no_channels()
        if channel.id in data: return await ctx.send("Already set.")
        data.append(channel.id); await self.config.guild(ctx.guild).restrictions.no_channels.set(data); await ctx.tick()

    @res_noch.command(name="remove")
    async def res_noch_remove(self, ctx: redcommands.Context, channel: discord.TextChannel):
        data = await self.config.guild(ctx.guild).restrictions.no_channels()
        if channel.id not in data: return await ctx.send("Not present.")
        data = [c for c in data if c != channel.id]; await self.config.guild(ctx.guild).restrictions.no_channels.set(data); await ctx.tick()

    @res_noch.command(name="list")
    async def res_noch_list(self, ctx: redcommands.Context):
        data = await self.config.guild(ctx.guild).restrictions.no_channels()
        await ctx.send("No-XP channels: " + (", ".join(f"<#{c}>" for c in data) if data else "none"))

    @res_noch.command(name="clear")
    async def res_noch_clear(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).restrictions.no_channels.set([]); await ctx.tick()

    @restrict.group(name="noroles")
    async def res_noroles(self, ctx: redcommands.Context): ...

    @res_noroles.command(name="add")
    async def res_noroles_add(self, ctx: redcommands.Context, role: discord.Role):
        data = await self.config.guild(ctx.guild).restrictions.no_roles()
        if role.id in data: return await ctx.send("Already set.")
        data.append(role.id); await self.config.guild(ctx.guild).restrictions.no_roles.set(data); await ctx.tick()

    @res_noroles.command(name="remove")
    async def res_noroles_remove(self, ctx: redcommands.Context, role: discord.Role):
        data = await self.config.guild(ctx.guild).restrictions.no_roles()
        if role.id not in data: return await ctx.send("Not present.")
        data = [r for r in data if r != role.id]; await self.config.guild(ctx.guild).restrictions.no_roles.set(data); await ctx.tick()

    @res_noroles.command(name="list")
    async def res_noroles_list(self, ctx: redcommands.Context):
        data = await self.config.guild(ctx.guild).restrictions.no_roles()
        roles = [ctx.guild.get_role(r).mention for r in data if ctx.guild.get_role(r)] or ["none"]
        await ctx.send("No-XP roles: " + ", ".join(roles))

    @res_noroles.command(name="clear")
    async def res_noroles_clear(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).restrictions.no_roles.set([]); await ctx.tick()

    @restrict.command(name="toggles")
    async def restrict_toggles(self, ctx: redcommands.Context, feature: str, enabled: Optional[bool] = None):
        feature = feature.lower()
        if feature not in {"threadxp", "forumxp", "textvoicexp", "slashxp"}:
            return await ctx.send("feature: threadxp|forumxp|textvoicexp|slashxp")
        key = {"threadxp": "thread_xp", "forumxp": "forum_xp", "textvoicexp": "text_in_voice_xp", "slashxp": "slash_command_xp"}[feature]
        if enabled is None:
            enabled = not (await getattr(self.config.guild(ctx.guild).restrictions, key)())
        await getattr(self.config.guild(ctx.guild).restrictions, key).set(bool(enabled)); await ctx.tick()

    # ---- levelup message config
    @level.group(name="levelup")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def levelup_grp(self, ctx: redcommands.Context): ...

    @levelup_grp.command(name="enable")
    async def levelup_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).levelup.enabled())
        await self.config.guild(ctx.guild).levelup.enabled.set(bool(enabled)); await ctx.tick()

    @levelup_grp.command(name="channel")
    async def levelup_channel(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel]):
        await self.config.guild(ctx.guild).levelup.channel_id.set(channel.id if channel else None); await ctx.tick()

    @levelup_grp.command(name="template")
    async def levelup_template(self, ctx: redcommands.Context, *, text: str):
        await self.config.guild(ctx.guild).levelup.template.set(text[:500]); await ctx.tick()

    # ---- xp admin & migration
    @level.group(name="xp")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def xpgrp(self, ctx: redcommands.Context): ...

    @xpgrp.command(name="set")
    async def xp_set(self, ctx: redcommands.Context, member: discord.Member, amount: int):
        await self._set_xp(ctx.guild, member.id, amount); await ctx.tick()

    @xpgrp.command(name="add")
    async def xp_add(self, ctx: redcommands.Context, member: discord.Member, amount: int):
        old, new = await self._add_xp(ctx.guild, member, amount)
        await self.maybe_announce_levelup(ctx.guild, member, old, new); await ctx.tick()

    @xpgrp.command(name="exportcsv")
    async def xp_export(self, ctx: redcommands.Context):
        g = await self.config.guild(ctx.guild).all()
        buff = io.StringIO()
        w = csv.writer(buff)
        w.writerow(["user_id", "xp"])
        for uid, xp in g["xp"].items():
            w.writerow([uid, xp])
        buff.seek(0)
        await ctx.send(file=discord.File(fp=io.BytesIO(buff.getvalue().encode("utf-8")), filename=f"{ctx.guild.id}_xp_export.csv"))

    @xpgrp.command(name="importcsv")
    async def xp_import_csv(self, ctx: redcommands.Context, *, raw: str = ""):
        """
        Import total XP (e.g., from Arcane). Accepts:
        - Attached CSV with columns: user_id,xp
        - Pasted lines: `user_id,xp`
        """
        content = ""
        if ctx.message.attachments:
            try:
                content = (await ctx.message.attachments[0].read()).decode("utf-8", "ignore")
            except Exception:
                return await ctx.send("Couldn't read attachment.")
        else:
            content = raw

        parsed: List[Tuple[int, int]] = []
        for line in io.StringIO(content):
            line = line.strip()
            if not line or line.lower().startswith("user_id"):
                continue
            try:
                left, right = [p.strip() for p in line.split(",", 1)]
            except ValueError:
                continue
            uid = None
            if left.isdigit():
                uid = int(left)
            else:
                m = re.search(r"(\d{15,25})", left)
                if m:
                    uid = int(m.group(1))
            if uid is None:
                continue
            try:
                xp = int(float(right))
            except Exception:
                continue
            parsed.append((uid, max(0, xp)))

        if not parsed:
            return await ctx.send("No rows parsed.")
        data = await self.config.guild(ctx.guild).xp()
        for uid, xp in parsed:
            data[str(uid)] = xp
        await self.config.guild(ctx.guild).xp.set(data)
        await ctx.send(f"Imported **{len(parsed)}** user(s).")

async def setup(bot: Red) -> None:
    await bot.add_cog(LevelPlus(bot))
