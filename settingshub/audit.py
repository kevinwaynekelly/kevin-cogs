"""Observe selected Config writes on these cog instances, with task-local actors."""

import asyncio
import io
import json
import re
import time
import uuid
import weakref
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass

import discord
from redbot.core import commands

from .presentation import clip
from .schema import EXCLUDED, FIELDS

AUDIT_DEFAULTS = {"enabled": True, "days": 30}
AUDIT_COUNT = 200
AUDIT_BYTES = 512 * 1024
VALUE_BYTES = 2048
MAX_CHANGES = 100
HUB_FIELDS = ("theme", "audit_policy", "snapshots")


@dataclass(frozen=True)
class Actor:
    guild_id: int
    user_id: int
    command: str
    task: object


def selected(name, data):
    data = deepcopy(data)
    excluded = EXCLUDED.get(name, ())
    if name == "SettingsHub":
        excluded = ("snapshots.records", "snapshots.last_at")
    for path in excluded:
        *parents, key = path.split(".")
        node = data
        for parent in parents:
            node = node.get(parent, {})
        node.pop(key, None)
    return data


def bounded_value(value):
    raw = json.dumps(value, ensure_ascii=False)
    encoded = raw.encode()
    if len(encoded) <= VALUE_BYTES:
        return value
    return {
        "preview": encoded[:VALUE_BYTES].decode(errors="ignore"),
        "truncated": True,
        "bytes": len(encoded),
    }


def changes(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            result.extend(changes(before.get(key), after.get(key), f"{path}.{key}".strip(".")))
        return result
    if before == after:
        return []
    return [{"path": path, "before": bounded_value(before), "after": bounded_value(after)}]


def retained(records, days, now):
    records = [r for r in records if now - r.get("at", 0) <= days * 86400][-AUDIT_COUNT:]
    while records and len(json.dumps(records, ensure_ascii=False).encode()) > AUDIT_BYTES:
        records.pop(0)
    return records


def identifies(record, user_id):
    return bool(re.search(rf"(?<!\d){user_id}(?!\d)", json.dumps(record, ensure_ascii=False)))


class AuditCommands:
    def _init_audit(self):
        self._audit_actor = ContextVar(f"settings-audit-{id(self)}", default=None)
        self._observed_drivers = {}
        self._audit_locks = {}
        self._next_audit_prune = 0

    async def begin_configuration_action(self, ctx):
        if (
            self._closing
            or ctx.guild is None
            or self.bot.get_cog("SettingsHub") is not self
            or await self.bot.cog_disabled_in_guild(self, ctx.guild)
        ):
            return None
        for cog in [self, *self._loaded().values()]:
            self._observe_config(cog)
        return self._audit_actor.set(
            Actor(
                ctx.guild.id,
                ctx.author.id,
                ctx.command.qualified_name,
                weakref.ref(asyncio.current_task()),
            )
        )

    def end_configuration_action(self, token):
        if token is not None:
            self._audit_actor.reset(token)

    def _observe_config(self, cog):
        driver = cog.config._driver
        if driver in self._observed_drivers:
            if self._observed_drivers[driver][0] is cog:
                return
            self._unobserve_config(driver)
        methods = {}
        for name in ("set", "clear"):
            original = getattr(driver, name)
            previous = driver.__dict__.get(name)

            async def observe(identifier_data, *args, _original=original, **kwargs):
                return await self._audit_write(cog, _original, identifier_data, *args, **kwargs)

            setattr(driver, name, observe)
            methods[name] = (original, observe, previous)
        self._observed_drivers[driver] = (cog, methods)

    def _unobserve_config(self, driver):
        entry = self._observed_drivers.pop(driver, None)
        if entry:
            for name, (_, wrapper, previous) in entry[1].items():
                if getattr(driver, name) is wrapper:
                    if previous is None:
                        delattr(driver, name)
                    else:
                        setattr(driver, name, previous)

    def _close_audit(self):
        for driver in tuple(self._observed_drivers):
            self._unobserve_config(driver)
        self._audit_locks.clear()

    async def _audit_write(self, cog, original, identifier, *args, **kwargs):
        name = cog.qualified_name
        fields = HUB_FIELDS if cog is self else FIELDS.get(name, ())
        if identifier.category != "GUILD" or len(identifier.primary_key) != 1:
            return await original(identifier, *args, **kwargs)
        relevant = (
            tuple(key for key in fields if key == identifier.identifiers[0])
            if identifier.identifiers
            else fields
        )
        if not relevant:
            return await original(identifier, *args, **kwargs)
        gid = int(identifier.primary_key[0])
        lock = self._audit_locks.setdefault((name, gid), asyncio.Lock())
        async with lock:
            actor = self._audit_actor.get()
            watching = (
                actor is not None
                and actor.task() is asyncio.current_task()
                and actor.guild_id == gid
                and not self._closing
                and self.bot.get_cog("SettingsHub") is self
            )
            if not watching:
                return await original(identifier, *args, **kwargs)
            guild = self.bot.get_guild(gid)
            if guild is not None and await self.bot.cog_disabled_in_guild(self, guild):
                return await original(identifier, *args, **kwargs)
            group = cog.config.guild_from_id(gid)
            try:
                policy = await self.config.guild_from_id(gid).audit_policy()
                before = selected(name, {key: await group.get_attr(key)() for key in relevant})
            except Exception:
                self._maintenance_log.exception("Could not capture settings before a write")
                return await original(identifier, *args, **kwargs)
            if not policy["enabled"]:
                return await original(identifier, *args, **kwargs)
            # The source write runs exactly once. A failure creates no audit entry.
            result = await original(identifier, *args, **kwargs)
            try:
                after = selected(name, {key: await group.get_attr(key)() for key in relevant})
                delta = changes(before, after)
                if delta:
                    await self._append_audit(gid, actor, name, delta)
            except Exception:
                # History storage is supplementary; retain a successful source write.
                self._maintenance_log.exception("Could not retain a settings change")
            return result

    async def _append_audit(self, guild_id, actor, name, delta):
        group = self.config.guild_from_id(guild_id)
        policy = await group.audit_policy()
        record = {
            "id": uuid.uuid4().hex[:12],
            "at": int(time.time()),
            "actor": actor.user_id,
            "cog": name,
            "command": actor.command,
            "changes": delta[:MAX_CHANGES],
            "omitted": max(0, len(delta) - MAX_CHANGES),
        }
        while len(json.dumps(record, ensure_ascii=False).encode()) > AUDIT_BYTES // 4:
            record["changes"].pop()
            record["omitted"] += 1
        section = group.configuration_history
        async with section.get_lock():
            await section.set(retained([*await section(), record], policy["days"], time.time()))

    async def _audit_records(self, guild_id):
        group = self.config.guild_from_id(guild_id)
        policy = await group.audit_policy()
        section = group.configuration_history
        async with section.get_lock():
            records = await section()
            recent = retained(records, policy["days"], time.time())
            if recent != records:
                await section.set(recent)
            return recent

    async def _audit_tick(self):
        if time.monotonic() < self._next_audit_prune:
            return
        self._next_audit_prune = time.monotonic() + 3600
        for gid in await self.config.all_guilds():
            await self._audit_records(gid)

    async def _visible_audit(self, ctx):
        visible = {"SettingsHub"}
        for name, cog in self._loaded().items():
            try:
                await self._source_check(ctx, cog)
            except (commands.CheckFailure, commands.DisabledCommand):
                continue
            visible.add(name)
        return [r for r in await self._audit_records(ctx.guild.id) if r["cog"] in visible]

    async def _show_configuration_history(self, ctx, page):
        records = list(reversed(await self._visible_audit(ctx)))
        pages = max(1, (len(records) + 9) // 10)
        if not 1 <= page <= pages:
            raise commands.BadArgument(f"Choose a history page from 1 to {pages}.")
        lines = [
            f"`{r['id']}` · **{r['cog'].removesuffix('Plus')}** · <@{r['actor']}> · <t:{r['at']}:R>\n"
            f"`{r['command']}` · " + ", ".join(clip(c["path"], 100) for c in r["changes"][:5])
            for r in records[(page - 1) * 10 : page * 10]
        ]
        await self._reply(
            ctx,
            "\n\n".join(lines) or "No recorded configuration changes yet.",
            title=f"Configuration history · {page}/{pages}",
        )

    async def _show_configuration_change(self, ctx, identifier):
        record = next(
            (r for r in await self._visible_audit(ctx) if r["id"] == identifier.lower()), None
        )
        if not record:
            raise commands.BadArgument("Choose an available ID shown by settings history.")
        lines = [f"<@{record['actor']}> · `{record['command']}` · <t:{record['at']}:f>"]
        for change in record["changes"]:
            before = discord.utils.escape_markdown(json.dumps(change["before"], ensure_ascii=False))
            after = discord.utils.escape_markdown(json.dumps(change["after"], ensure_ascii=False))
            lines.append(f"**{change['path']}**\nBefore: {before}\nAfter: {after}")
        if record["omitted"]:
            lines.append(
                f"{record['omitted']} additional paths exceeded this entry's change/byte limit."
            )
        await self._reply(ctx, "\n\n".join(lines), title="Configuration change")

    async def _export_configuration_history(self, ctx):
        records = await self._visible_audit(ctx)
        await self._reply(
            ctx,
            "Retained configuration changes attached. Oversized values include bounded previews.",
            file=discord.File(
                io.BytesIO(json.dumps(records, indent=2).encode()),
                filename="configuration-history.json",
            ),
        )

    async def _audit_user_data(self, user_id, *, delete=False):
        result = {}
        for gid in await self.config.all_guilds():
            await self._audit_records(gid)
            section = self.config.guild_from_id(gid).configuration_history
            async with section.get_lock():
                records = await section()
                personal = [r for r in records if identifies(r, user_id)]
                if personal:
                    result[str(gid)] = personal
                    if delete:
                        await section.set([r for r in records if not identifies(r, user_id)])
        return result

    @commands.Cog.listener()
    async def on_cog_add(self, cog):
        if not self._closing and (cog is self or cog.qualified_name in FIELDS):
            self._observe_config(cog)

    @commands.Cog.listener()
    async def on_cog_remove(self, cog):
        if hasattr(cog, "config"):
            driver = cog.config._driver
            entry = self._observed_drivers.get(driver)
            if entry and entry[0] is cog:
                self._unobserve_config(driver)
