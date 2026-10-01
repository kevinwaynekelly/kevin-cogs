"""Opt-in, bounded local event history and administrator report helpers."""

import asyncio
import csv
import io
import json
import logging
import time

from redbot.core import commands

from .delivery import CATEGORIES

log = logging.getLogger(__name__)
HISTORY_DEFAULTS = {"enabled": False, "days": 7}
MAX_RECORDS = 1000
MAX_BYTES = 2 * 1024 * 1024


def history_text(record):
    return "\n".join(
        [
            record["title"],
            record["description"],
            *(f"{field['name']}\n{field['value']}" for field in record["fields"]),
        ]
    )


def prune_history(records, days, now):
    cutoff = now - days * 86400
    records[:] = [record for record in records if record["time"] >= cutoff][-MAX_RECORDS:]
    sizes = [len(json.dumps(record, ensure_ascii=False).encode()) for record in records]
    total = sum(sizes)
    remove = 0
    while total > MAX_BYTES and remove < len(sizes):
        total -= sizes[remove]
        remove += 1
    del records[:remove]


def export_history(records, format):
    if format == "json":
        return io.BytesIO(json.dumps(records, ensure_ascii=False, indent=2).encode())
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["timestamp", "event", "category", "source_channel", "member_ids", "details"])
    for record in records:
        # Neutralize spreadsheet formulas even after leading whitespace/control characters.
        values = [
            record["time"],
            record["event"],
            record["category"],
            record["source"],
            " ".join(map(str, record["users"])),
            history_text(record),
        ]
        writer.writerow(
            [
                "'" + value
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                else value
                for value in values
            ]
        )
    return io.BytesIO(stream.getvalue().encode("utf-8-sig"))


class LogHistory:
    async def _save_history(self, guild, embed, pending):
        group = self.config.guild(guild)
        async with group.history_records() as records:
            policy = await group.history_settings()
            if (
                self._closing
                or not policy["enabled"]
                or await self._delivery_route(guild, pending) is None
            ):
                return
            now = time.time()
            # Bound each logical record independently of the number of delivery pages.
            remaining = 4000
            fields = []
            for field in embed.fields[:25]:
                value = field.value[: min(remaining, 1000)]
                if not value:
                    break
                fields.append({"name": field.name[:100], "value": value})
                remaining -= len(value)
            records.append(
                {
                    "time": now,
                    "event": pending.event_type,
                    "category": pending.category,
                    "source": pending.source,
                    "users": sorted(pending.users),
                    "title": (embed.title or "Event")[:200],
                    "description": (embed.description or "")[:2000],
                    "fields": fields,
                }
            )
            prune_history(records, policy["days"], now)

    async def _history_query(
        self, guild, *, query="", member_id=None, category="all", days=7, limit=100
    ):
        if category not in {"all", *CATEGORIES}:
            raise commands.BadArgument(
                "Choose all or a valid log category: " + ", ".join(CATEGORIES)
            )
        if not 1 <= days <= 90 or not 1 <= limit <= MAX_RECORDS or len(query) > 200:
            raise commands.BadArgument(
                "Use 1 to 90 days, 1 to 1000 results, and at most 200 search characters."
            )
        now = time.time()
        group = self.config.guild(guild)
        async with group.history_records() as records:
            policy = await group.history_settings()
            prune_history(records, policy["days"], now)
            result = [
                record
                for record in reversed(records)
                if record["time"] >= now - days * 86400
                and (member_id is None or member_id in record["users"])
                and (category == "all" or record["category"] == category)
                and query.casefold() in history_text(record).casefold()
            ]
        return result[:limit]

    async def _history_report(self, ctx, records):
        if not records:
            return await self._reply(
                ctx, "No matching retained events. History starts collecting when enabled."
            )
        lines = [
            f"<t:{int(record['time'])}:f> · **{record['event'] or 'event'}**\n{history_text(record)[:500]}"
            for record in records
        ]
        await self._reply(ctx, "\n\n".join(lines), title="Log history")

    async def _set_history_policy(self, guild, key, value):
        group = self.config.guild(guild)
        async with group.history_records() as records:
            await group.history_settings.get_attr(key).set(value)
            policy = await group.history_settings()
            prune_history(records, policy["days"], time.time())
        self._settings_cache.pop(guild.id, None)

    async def _history_tick(self):
        # Includes inactive or left servers; only reads policy for nonempty history.
        data = await self.config.all_guilds()
        for gid, settings in data.items():
            if self._closing:
                return
            if settings.get("history_records"):
                group = self.config.guild_from_id(gid)
                async with group.history_records() as records:
                    policy = await group.history_settings()
                    prune_history(records, policy["days"], time.time())

    async def _history_maintenance(self):
        await self.bot.wait_until_red_ready()
        while not self._closing:
            try:
                await self._history_tick()
                await self._prune_incidents()
            except Exception:
                log.exception("Could not prune expired local log history")
            await asyncio.sleep(3600)

    async def _history_user_data(self, user_id, *, delete=False):
        result = {}
        for gid, data in (await self.config.all_guilds()).items():
            if not data.get("history_records"):
                continue
            group = self.config.guild_from_id(gid)
            async with group.history_records() as records:
                policy = await group.history_settings()
                prune_history(records, policy["days"], time.time())
                found = [record for record in records if user_id in record["users"]]
                if found:
                    result[str(gid)] = found
                if delete:
                    records[:] = [record for record in records if user_id not in record["users"]]
        return result
