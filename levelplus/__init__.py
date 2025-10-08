# path: cogs/levelplus/__init__.py
from __future__ import annotations

import csv
import io
import random
import re
import time
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands, tasks
from redbot.core import commands as redcommands
from redbot.core.bot import Red
from redbot.core.config import Config
from redbot.core.utils.chat_formatting import box

__red_end_user_data_statement__ = (
    "This cog stores per-guild leveling settings, per-user XP totals, and a last-known display name. "
    "Data persists across leaves/joins to preserve user progress. Admins may export or erase specific users via commands."
)

__author__ = "Code Copilot"
__version__ = "0.3.3"

DEFAULTS_GUILD = {
    "curve": "linear",
    "multiplier": 1.0,
    "max_level": 0,
    "linear": {"base": 83.2, "inc": 100.433},  # Arcane-like
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
    "xp": {},      # {user_id(str): int}
    "names": {},   # {user_id(str): alias}
}

WORD_RE = re.compile(r"\b\w+\b", re.UNICODE)


def level_thresholds(curve: str, mult: float, max_level: int, linear_base: float, linear_inc: float) -> List[int]:
    curve = (curve or "linear").lower()
    cap = max(1, max_level) if max_level > 0 else 200
    out: List[int] = [0]
    total: float = 0.0
    for lvl in range(1, cap + 1):
        if curve == "constant":
            need = 100.0 * mult
        elif curve == "exponential":
            need = (100.0 * (1.25 ** (lvl - 1))) * mult
        else:
            need = (linear_base + linear_inc * (lvl - 1)) * mult
        total += max(0.0, need)
        out.append(int(round(total)))
    return out


def level_from_xp(xp: int, curve: str, mult: float, max_level: int, base: float, inc: float) -> int:
    th = level_thresholds(curve, mult, max_level, base, inc)
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


class LevelPlus(redcommands.Cog):
    """Arcane-style leveling with messages/reactions/voice/slash XP, leaderboards, CSV import, tests, calibration, and migration helpers."""

    def __init__(self, bot: Red) -> None:
        self.bot: Red = bot
        self.config: Config = Config.get_conf(self, identifier=0x1EAF01, force_registration=True)
        self.config.register_guild(**DEFAULTS_GUILD)

        self._last_msg: Dict[Tuple[int, int], float] = {}
        self._last_rxn: Dict[Tuple[int, int], float] = {}
        self._last_voice: Dict[Tuple[int, int], float] = {}

        self.voice_tick.start()

    def cog_unload(self) -> None:
        self.voice_tick.cancel()

    def format_help_for_context(self, ctx: redcommands.Context) -> str:
        base = super().format_help_for_context(ctx)
        return f"{base}\nCog Version: {__version__} • Author: {__author__}"

    # ---------- helpers ----------
    async def _g(self, guild: discord.Guild):
        return await self.config.guild(guild).all()

    async def _lin(self, guild: discord.Guild):
        lin = await self.config.guild(guild).linear()
        return float(lin.get("base", 83.2)), float(lin.get("inc", 100.433))

    async def _remember_name(self, guild: discord.Guild, member: discord.Member) -> None:
        # why: preserve leaderboard names for leavers
        try:
            names = await self.config.guild(guild).names()
            dn = member.display_name
            if names.get(str(member.id)) != dn:
                names[str(member.id)] = dn
                await self.config.guild(guild).names.set(names)
        except Exception:
            pass

    async def _get_xp(self, guild: discord.Guild, user_id: int) -> int:
        data = await self.config.guild(guild).xp()
        return int(data.get(str(user_id), 0))

    async def _set_xp(self, guild: discord.Guild, user_id: int, value: int) -> None:
        data = await self.config.guild(guild).xp()
        data[str(user_id)] = int(max(0, value))
        await self.config.guild(guild).xp.set(data)

    async def _add_xp(self, guild: discord.Guild, user: discord.abc.User, amount: int) -> Tuple[int, int]:
        if amount <= 0:
            g = await self._g(guild); base, inc = await self._lin(guild)
            lvl = level_from_xp(await self._get_xp(guild, user.id), g["curve"], float(g["multiplier"]),
                                int(g["max_level"]), base, inc)
            return lvl, lvl
        gconf = await self._g(guild)
        data = gconf["xp"]
        old_xp = int(data.get(str(user.id), 0))
        new_xp = old_xp + int(amount)
        data[str(user.id)] = new_xp
        await self.config.guild(guild).xp.set(data)
        base, inc = await self._lin(guild)
        old_lvl = level_from_xp(old_xp, gconf["curve"], float(gconf["multiplier"]), int(gconf["max_level"]), base, inc)
        new_lvl = level_from_xp(new_xp, gconf["curve"], float(gconf["multiplier"]), int(gconf["max_level"]), base, inc)
        return old_lvl, new_lvl

    async def current_level(self, guild: discord.Guild, user_id: int) -> int:
        gconf = await self._g(guild)
        base, inc = await self._lin(guild)
        xp = int(gconf["xp"].get(str(user_id), 0))
        return level_from_xp(xp, gconf["curve"], float(gconf["multiplier"]), int(gconf["max_level"]), base, inc)

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

    # ---------- listeners ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        guild = message.guild
        member = message.author
        await self._remember_name(guild, member)

        gconf = await self._g(guild)
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

    @commands.Cog.listener())
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not guild:
            return
        gconf = await self._g(guild)
        if not gconf["reaction"]["enabled"]:
            return
        channel = guild.get_channel(payload.channel_id)
        if isinstance(channel, discord.abc.GuildChannel):
            if channel.id in set(gconf["restrictions"]["no_channels"]):
                return

        rx_min = int(gconf["reaction"]["min"])
        rx_max = max(int(gconf["reaction"]["max"]), rx_min)
        value = random.randint(rx_min, rx_max)

        reactor = guild.get_member(payload.user_id)
        if reactor and reactor.bot:
            reactor = None
        if reactor:
            await self._remember_name(guild, reactor)
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
                if msg and msg.author and not msg.author.bot and isinstance(msg.author, discord.Member):
                    author = msg.author
                    await self._remember_name(guild, author)
        except Exception:
            author = None

        awards = (gconf["reaction"]["awards"] or "both").lower()
        targets: List[discord.Member] = []
        if awards in ("both", "reactor") and reactor:
            targets.append(reactor)
        if awards in ("both", "author") and author and author.id != payload.user_id:
            targets.append(author)

        for m in targets:
            role_ids = {r.id for r in getattr(m, "roles", [])}
            if role_ids.intersection(set(gconf["restrictions"]["no_roles"])):
                continue
            old, new = await self._add_xp(guild, m, value)
            await self.maybe_announce_levelup(guild, m, old, new)

    @tasks.loop(seconds=20.0)
    async def voice_tick(self):
        await self.bot.wait_until_red_ready()
        for guild in list(self.bot.guilds):
            try:
                gconf = await self._g(guild)
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
                    await self._remember_name(guild, m)
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

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if not interaction.guild or interaction.user.bot:
            return
        guild = interaction.guild
        gconf = await self._g(guild)
        if not gconf["restrictions"]["slash_command_xp"]:
            return
        key = (guild.id, interaction.user.id)
        cd = int(gconf["message"]["cooldown"])
        if cd and time.time() - self._last_msg.get(key, 0.0) < cd:
            return
        self._last_msg[key] = time.time()
        val = max(1, int(gconf["message"]["min"]))
        old, new = await self._add_xp(guild, interaction.user, val)  # type: ignore[arg-type]
        if isinstance(interaction.user, discord.Member):
            await self.maybe_announce_levelup(guild, interaction.user, old, new)

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
        await ctx.send(box("\n".join(lines), lang="ini"))

    @level.command(name="help")
    async def level_help(self, ctx: redcommands.Context):
        p = ctx.clean_prefix
        desc = (
            f"**View** `{p}level`, `{p}level show`, `{p}level leaderboard`, `{p}level diag`\n"
            f"**Formula** `{p}level formula preset arcane` • `{p}level formula calibrate <L1> <XP1> <L2> <XP2>`\n"
            f"**Message** `{p}level message enable|mode|min|max|cooldown`\n"
            f"**Reaction** `{p}level reaction enable|awards|min|max|cooldown`\n"
            f"**Voice** `{p}level voice enable|range|cooldown|minmembers|antiafk`\n"
            f"**Restrict** `{p}level restrict nochannels|noroles|toggles`\n"
            f"**LevelUp** `{p}level levelup enable|channel|template`\n"
            f"**Import/Export** `{p}level xp exportcsv` • `{p}level xp importcsv` • `{p}level xp importlines`\n"
            f"**Leavers** `{p}level lookup <name>` • `{p}level xp setid <id> <xp>` • `{p}level name setid <id> <alias>`\n"
            f"**Tests** `{p}level testmsg` • `{p}level testup`\n"
            f"*Bots are ignored everywhere.*"
        )
        try:
            await ctx.send(embed=discord.Embed(title="LevelPlus — Help", description=desc, color=discord.Color.blurple()))
        except discord.Forbidden:
            await ctx.send(box(desc, lang="ini"))

    @level.command()
    async def diag(self, ctx: redcommands.Context):
        g = await self._g(ctx.guild); base, inc = await self._lin(ctx.guild)
        ch = ctx.channel
        perms = ch.permissions_for(ctx.guild.me) if isinstance(ch, (discord.TextChannel, discord.Thread)) else None  # type: ignore
        intents = self.bot.intents
        probe = "skip"
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try:
                m = await ctx.send("LevelPlus diag probe…"); await m.add_reaction("✅"); probe = "OK"; await m.delete()
            except Exception as e:
                probe = f"FAIL:{type(e).__name__}"
        lines = [
            f"curve={g['curve']} mult={g['multiplier']} maxlvl={g['max_level']} linear(base={base:.3f}, inc={inc:.3f})",
            f"perms(send={getattr(perms,'send_messages',None)} embed={getattr(perms,'embed_links',None)} add_rxn={getattr(perms,'add_reactions',None)} read_hist={getattr(perms,'read_message_history',None)})",
            f"intents(voice_states={intents.voice_states} message_content={intents.message_content})",
            f"probe={probe}",
            "store_rw=OK" if isinstance(g["xp"], dict) else "store_rw=FAIL",
        ]
        await ctx.send(box("\n".join(lines), lang="ini"))

    # ---- test/view
    @level.command(name="testmsg")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def level_testmsg(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
        m = member or ctx.author
        conf = await self.config.guild(ctx.guild).levelup()
        if not conf["enabled"]:
            return await ctx.send("Level-up messages are disabled.")
        ch: Optional[discord.TextChannel] = None
        cid = conf.get("channel_id")
        if cid:
            ch = ctx.guild.get_channel(cid) if isinstance(cid, int) else ctx.guild.get_channel(int(cid))
            if not isinstance(ch, discord.TextChannel):
                ch = None
        if not ch:
            ch = ctx.channel
        g = await self._g(ctx.guild)
        cur = await self.current_level(ctx.guild, m.id)
        next_level = cur + 1 if (g["max_level"] == 0 or cur < g["max_level"]) else cur
        u = type("U", (), {"mention": m.mention, "name": m.display_name, "level": next_level, "xp": await self._get_xp(ctx.guild, m.id)})()
        try:
            msg = conf.get("template", "{user.mention} has reached level **{user.level}**!").format(user=u)
        except Exception:
            msg = f"{m.mention} has reached level **{next_level}**!"
        await ch.send(f"[TEST] {msg}"); await ctx.tick()

    @level.command(name="testup")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def level_testup(self, ctx: redcommands.Context, member: Optional[discord.Member] = None, levels: int = 1):
        m = member or ctx.author
        levels = max(1, levels)
        g = await self._g(ctx.guild); base, inc = await self._lin(ctx.guild)
        cur_xp = await self._get_xp(ctx.guild, m.id)
        cur_lvl = level_from_xp(cur_xp, g["curve"], float(g["multiplier"]), int(g["max_level"]), base, inc)
        target = min(cur_lvl + levels, int(g["max_level"]) if g["max_level"] > 0 else cur_lvl + levels)
        th = level_thresholds(g["curve"], float(g["multiplier"]), max(target, cur_lvl + 1), base, inc)
        needed = max(0, th[target] - cur_xp + 1)
        old, new = await self._add_xp(ctx.guild, m, needed)
        if isinstance(m, discord.Member):
            await self.maybe_announce_levelup(ctx.guild, m, old, new)
        await ctx.send(f"Gave {m.mention} **{needed}** XP (L{old} → L{new}).")

    @level.command()
    async def show(self, ctx: redcommands.Context, member: Optional[discord.Member] = None):
        m = member or ctx.author
        xp = await self._get_xp(ctx.guild, m.id)
        g = await self._g(ctx.guild); base, inc = await self._lin(ctx.guild)
        lvl = level_from_xp(xp, g["curve"], float(g["multiplier"]), int(g["max_level"]), base, inc)
        await ctx.send(f"{m.mention} — XP: **{xp}**, Level: **{lvl}**")

    @level.command()
    async def leaderboard(self, ctx: redcommands.Context, top: int = 10):
        g = await self._g(ctx.guild); base, inc = await self._lin(ctx.guild)
        names = g.get("names", {})
        items = sorted(((int(uid), int(xp)) for uid, xp in g["xp"].items()), key=lambda t: t[1], reverse=True)[:max(1, min(50, top))]
        if not items:
            return await ctx.send("No XP yet.")
        lines = []
        for i, (uid, xp) in enumerate(items, start=1):
            m = ctx.guild.get_member(uid)
            lvl = level_from_xp(xp, g["curve"], float(g["multiplier"]), int(g["max_level"]), base, inc)
            name = (m.display_name if m else names.get(str(uid))) or str(uid)
            lines.append(f"{i:>2}. {name} — L{lvl} ({xp} xp)")
        await ctx.send(box("\n".join(lines), lang="ini"))

    # ---- formula
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

    @formula.group(name="linear")
    async def formula_linear(self, ctx: redcommands.Context): ...

    @formula_linear.command(name="base")
    async def formula_linear_base(self, ctx: redcommands.Context, value: float):
        await self.config.guild(ctx.guild).linear.base.set(float(max(0.0, value))); await ctx.tick()

    @formula_linear.command(name="inc")
    async def formula_linear_inc(self, ctx: redcommands.Context, value: float):
        await self.config.guild(ctx.guild).linear.inc.set(float(max(0.0, value))); await ctx.tick()

    @formula.command(name="preset")
    async def formula_preset(self, ctx: redcommands.Context, which: str):
        which = which.lower()
        if which != "arcane":
            return await ctx.send("Only `arcane` preset is available.")
        await self.config.guild(ctx.guild).linear.set({"base": 83.2, "inc": 100.433})
        await self.config.guild(ctx.guild).curve.set("linear")
        await ctx.send("Set curve to **linear** with Arcane-like preset (base=83.2, inc=100.433).")

    @formula.command(name="calibrate")
    async def formula_calibrate(self, ctx: redcommands.Context, L1: int, XP1: int, L2: int, XP2: int):
        if L1 <= 0 or L2 <= 0 or L1 == L2:
            return await ctx.send("Levels must be positive and different.")
        a1 = 2.0 * XP1 / L1
        a2 = 2.0 * XP2 / L2
        d = (a1 - a2) / (L1 - L2)
        b = (a1 - (L1 - 1) * d) / 2.0
        if d < 0 or b < 0:
            return await ctx.send("Calibration failed (negative base/inc). Check inputs.")
        await self.config.guild(ctx.guild).linear.set({"base": float(b), "inc": float(d)})
        await self.config.guild(ctx.guild).curve.set("linear")
        await ctx.send(f"Calibrated linear curve: base=**{b:.3f}**, inc=**{d:.3f}**")

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
        if channel.id in data:
            return await ctx.send("Already set.")
        data.append(channel.id)
        await self.config.guild(ctx.guild).restrictions.no_channels.set(data)
        await ctx.tick()

    @res_noch.command(name="remove")
    async def res_noch_remove(self, ctx: redcommands.Context, channel: discord.TextChannel):
        data = await self.config.guild(ctx.guild).restrictions.no_channels()
        if channel.id not in data:
            return await ctx.send("Not present.")
        data = [c for c in data if c != channel.id]
        await self.config.guild(ctx.guild).restrictions.no_channels.set(data)
        await ctx.tick()

    @res_noch.command(name="list")
    async def res_noch_list(self, ctx: redcommands.Context):
        data = await self.config.guild(ctx.guild).restrictions.no_channels()
        await ctx.send("No-XP channels: " + (", ".join(f"<#{c}>" for c in data) if data else "none"))

    @res_noch.command(name="clear")
    async def res_noch_clear(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).restrictions.no_channels.set([])
        await ctx.tick()

    @restrict.group(name="noroles")
    async def res_noroles(self, ctx: redcommands.Context): ...

    @res_noroles.command(name="add")
    async def res_noroles_add(self, ctx: redcommands.Context, role: discord.Role):
        data = await self.config.guild(ctx.guild).restrictions.no_roles()
        if role.id in data:
            return await ctx.send("Already set.")
        data.append(role.id)
        await self.config.guild(ctx.guild).restrictions.no_roles.set(data)
        await ctx.tick()

    @res_noroles.command(name="remove")
    async def res_noroles_remove(self, ctx: redcommands.Context, role: discord.Role):
        data = await self.config.guild(ctx.guild).restrictions.no_roles()
        if role.id not in data:
            return await ctx.send("Not present.")
        data = [r for r in data if r != role.id]
        await self.config.guild(ctx.guild).restrictions.no_roles.set(data)
        await ctx.tick()

    @res_noroles.command(name="list")
    async def res_noroles_list(self, ctx: redcommands.Context):
        data = await self.config.guild(ctx.guild).restrictions.no_roles()
        roles = [ctx.guild.get_role(r).mention for r in data if ctx.guild.get_role(r)] or ["none"]
        await ctx.send("No-XP roles: " + ", ".join(roles))

    @res_noroles.command(name="clear")
    async def res_noroles_clear(self, ctx: redcommands.Context):
        await self.config.guild(ctx.guild).restrictions.no_roles.set([])
        await ctx.tick()

    @restrict.command(name="toggles")
    async def restrict_toggles(self, ctx: redcommands.Context, feature: str, enabled: Optional[bool] = None):
        feature = feature.lower()
        if feature not in {"threadxp", "forumxp", "textvoicexp", "slashxp"}:
            return await ctx.send("feature: threadxp|forumxp|textvoicexp|slashxp")
        key = {"threadxp": "thread_xp", "forumxp": "forum_xp", "textvoicexp": "text_in_voice_xp", "slashxp": "slash_command_xp"}[feature]
        if enabled is None:
            enabled = not (await getattr(self.config.guild(ctx.guild).restrictions, key)())
        await getattr(self.config.guild(ctx.guild).restrictions, key).set(bool(enabled))
        await ctx.tick()

    # ---- levelup config
    @level.group(name="levelup")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def levelup_grp(self, ctx: redcommands.Context): ...

    @levelup_grp.command(name="enable")
    async def levelup_enable(self, ctx: redcommands.Context, enabled: Optional[bool] = None):
        if enabled is None:
            enabled = not (await self.config.guild(ctx.guild).levelup.enabled())
        await self.config.guild(ctx.guild).levelup.enabled.set(bool(enabled))
        await ctx.tick()

    @levelup_grp.command(name="channel")
    async def levelup_channel(self, ctx: redcommands.Context, channel: Optional[discord.TextChannel]):
        await self.config.guild(ctx.guild).levelup.channel_id.set(channel.id if channel else None)
        await ctx.tick()

    @levelup_grp.command(name="template")
    async def levelup_template(self, ctx: redcommands.Context, *, text: str):
        await self.config.guild(ctx.guild).levelup.template.set(text[:500])
        await ctx.tick()

    # ---- XP admin & migration
    @level.group(name="xp")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def xpgrp(self, ctx: redcommands.Context): ...

    @xpgrp.command(name="set")
    async def xp_set(self, ctx: redcommands.Context, member: discord.Member, amount: int):
        await self._remember_name(ctx.guild, member)
        await self._set_xp(ctx.guild, member.id, amount); await ctx.tick()

    @xpgrp.command(name="setid")
    async def xp_setid(self, ctx: redcommands.Context, user_id: int, amount: int):
        await self._set_xp(ctx.guild, user_id, amount); await ctx.tick()

    @xpgrp.command(name="add")
    async def xp_add(self, ctx: redcommands.Context, member: discord.Member, amount: int):
        await self._remember_name(ctx.guild, member)
        old, new = await self._add_xp(ctx.guild, member, amount)
        await self.maybe_announce_levelup(ctx.guild, member, old, new); await ctx.tick()

    @xpgrp.command(name="exportcsv")
    async def xp_export(self, ctx: redcommands.Context):
        g = await self._g(ctx.guild)
        buff = io.StringIO(); w = csv.writer(buff)
        w.writerow(["user_id", "xp", "alias"])
        for uid, xp in g["xp"].items():
            alias = g.get("names", {}).get(uid, "")
            w.writerow([uid, xp, alias])
        buff.seek(0)
        await ctx.send(file=discord.File(fp=io.BytesIO(buff.getvalue().encode("utf-8")), filename=f"{ctx.guild.id}_xp_export.csv"))

    @xpgrp.command(name="importcsv")
    async def xp_import_csv(self, ctx: redcommands.Context, *, raw: str = ""):
        content = ""
        if ctx.message.attachments:
            try:
                content = (await ctx.message.attachments[0].read()).decode("utf-8", "ignore")
            except Exception:
                return await ctx.send("Couldn't read attachment.")
        else:
            content = raw
        parsed: List[Tuple[int, int, Optional[str]]] = []
        for line in io.StringIO(content):
            line = line.strip()
            if not line or line.lower().startswith("user_id"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            m = re.search(r"(\d{15,25})", parts[0])
            if not m:
                continue
            uid = int(m.group(1))
            try:
                xp = int(float(parts[1]))
            except Exception:
                continue
            alias = parts[2] if len(parts) >= 3 and parts[2] else None
            parsed.append((uid, max(0, xp), alias))
        if not parsed:
            return await ctx.send("No rows parsed.")
        xpmap = await self.config.guild(ctx.guild).xp(); names = await self.config.guild(ctx.guild).names()
        for uid, xp, alias in parsed:
            xpmap[str(uid)] = xp
            if alias:
                names[str(uid)] = alias[:100]
        await self.config.guild(ctx.guild).xp.set(xpmap)
        await self.config.guild(ctx.guild).names.set(names)
        await ctx.send(f"Imported **{len(parsed)}** user(s).")

    @xpgrp.command(name="importlines")
    async def xp_import_lines(self, ctx: redcommands.Context, *, lines: str):
        """
        Paste lines: `identifier,xp`
        identifier = <id> | <@mention> | name#1234 | display/global name
        """
        ok, skip = 0, 0
        xpmap = await self.config.guild(ctx.guild).xp()
        names_map = await self.config.guild(ctx.guild).names()

        def resolve_id(identifier: str) -> Optional[int]:
            identifier = identifier.strip()
            m = re.search(r"(\d{15,25})", identifier)
            if m:
                return int(m.group(1))
            # name#1234
            if "#" in identifier:
                name, discrim = identifier.rsplit("#", 1)
                for u in self.bot.users:
                    if not getattr(u, "bot", False) and u.name == name and getattr(u, "discriminator", None) == discrim:
                        return u.id
            # by display/global/name in guild first
            for mbr in ctx.guild.members:
                if mbr.bot:
                    continue
                if identifier.lower() in {mbr.display_name.lower(), mbr.name.lower(), (mbr.global_name or "").lower()}:
                    return mbr.id
            # global cache
            for u in self.bot.users:
                if getattr(u, "bot", False):
                    continue
                if identifier.lower() in {u.name.lower(), (getattr(u, "global_name", "") or "").lower()}:
                    return u.id
            return None

        for raw in io.StringIO(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                ident, xp_str = [p.strip() for p in raw.split(",", 1)]
            except ValueError:
                skip += 1; continue
            uid = resolve_id(ident)
            if uid is None:
                skip += 1; continue
            try:
                xp = int(float(xp_str))
            except Exception:
                skip += 1; continue
            xpmap[str(uid)] = max(0, xp)
            mbr = ctx.guild.get_member(uid)
            if mbr:
                names_map[str(uid)] = mbr.display_name
            else:
                names_map.setdefault(str(uid), ident[:100])
            ok += 1

        await self.config.guild(ctx.guild).xp.set(xpmap)
        await self.config.guild(ctx.guild).names.set(names_map)
        await ctx.send(f"Imported **{ok}** row(s), skipped **{skip}**.")

    # ---- lookup & aliases
    @level.command(name="lookup")
    async def level_lookup(self, ctx: redcommands.Context, *, query: str):
        q = query.lower()
        candidates: Dict[int, str] = {}
        for m in ctx.guild.members:
            if m.bot:
                continue
            names = [m.display_name, m.name, getattr(m, "global_name", None)]
            if any(n and q in n.lower() for n in names):
                candidates[m.id] = m.display_name
        for u in self.bot.users:
            if getattr(u, "bot", False):
                continue
            names = [u.name, getattr(u, "global_name", None)]
            if any(n and q in n.lower() for n in names):
                candidates[u.id] = getattr(u, "global_name", None) or u.name
        if not candidates:
            return await ctx.send("No matches.")
        lines = [f"{i:>2}. {name} — `{uid}`" for i, (uid, name) in enumerate(list(candidates.items())[:20], start=1)]
        await ctx.send(box("\n".join(lines), lang="ini"))

    @level.group(name="name")
    @redcommands.admin_or_permissions(manage_guild=True)
    async def namegrp(self, ctx: redcommands.Context): ...

    @namegrp.command(name="set")
    async def name_set(self, ctx: redcommands.Context, member: discord.Member, *, alias: str):
        names = await self.config.guild(ctx.guild).names()
        names[str(member.id)] = alias[:100]
        await self.config.guild(ctx.guild).names.set(names); await ctx.tick()

    @namegrp.command(name="setid")
    async def name_setid(self, ctx: redcommands.Context, user_id: int, *, alias: str):
        names = await self.config.guild(ctx.guild).names()
        names[str(user_id)] = alias[:100]
        await self.config.guild(ctx.guild).names.set(names); await ctx.tick()

    @namegrp.command(name="get")
    async def name_get(self, ctx: redcommands.Context, user_id: int):
        names = await self.config.guild(ctx.guild).names()
        alias = names.get(str(user_id), "none")
        await ctx.send(f"`{user_id}` → {alias}")


async def setup(bot: Red) -> None:
    await bot.add_cog(LevelPlus(bot))
