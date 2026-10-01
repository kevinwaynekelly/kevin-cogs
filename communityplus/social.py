"""Persistent bounded polls and opt-in event attendance/reminders."""

import asyncio
import uuid
from collections import Counter
from contextlib import suppress
from datetime import datetime

import discord
from redbot.core import commands

from .interactive import component_context, component_error
from .presentation import clip

SOCIAL_DEFAULTS = {"polls": {}, "events": {}}


def attendance_choice(record, uid, value):
    choices = record["rsvps"]
    capacity = record.get("capacity", 0)
    if (
        value == "yes"
        and capacity
        and sum(v == "yes" for key, v in choices.items() if key != uid) >= capacity
    ):
        value = "wait"
    choices[uid] = value
    promote_waitlist(record)


def promote_waitlist(record):
    choices = record["rsvps"]
    capacity = record.get("capacity", 0)
    if capacity:
        vacancies = max(0, capacity - sum(v == "yes" for v in choices.values()))
        for key, value in choices.items():
            if not vacancies:
                break
            if value == "wait":
                choices[key] = "yes"
                vacancies -= 1


MAX_RECORDS = 20
MAX_PARTICIPANTS = 1000
MAX_REMINDERS = 100
RETENTION = 30 * 86400


def event_time(value, now):
    try:
        if value.isdigit():
            stamp = int(value)
        else:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError
            stamp = int(date.timestamp())
        if not now + 60 <= stamp <= now + 180 * 86400:
            raise ValueError
        return stamp
    except (ValueError, OverflowError) as error:
        raise commands.BadArgument(
            "Use a future Unix timestamp or ISO date with an offset, such as 2026-10-03T19:00-05:00. Choose 1 minute to 180 days ahead."
        ) from error


def social_embed(cog, kind, key, record):
    title = "Poll" if kind == "polls" else "Event"
    embed = cog._presentation.embed(title, discord.utils.escape_markdown(record["title"]))
    embed.add_field(name="ID", value=key)
    embed.add_field(
        name="Status",
        value="Closed" if record["closed"] or record["at"] <= cog._now_ts() else "Open",
    )
    embed.add_field(
        name="Closes" if kind == "polls" else "Starts",
        value=f"<t:{record['at']}:F> · <t:{record['at']}:R>",
        inline=False,
    )
    if kind == "polls":
        totals = Counter(record["votes"].values())
        for index, option in enumerate(record["options"], 1):
            embed.add_field(
                name=clip(f"{index}. {option}", 100), value=f"{totals[index]} vote(s)", inline=False
            )
    else:
        totals = Counter(record["rsvps"].values())
        embed.add_field(
            name="Attendance",
            value=f"Going {totals['yes']} · Waitlist {totals['wait']} · Maybe {totals['maybe']} · Not going {totals['no']}",
            inline=False,
        )
        embed.add_field(
            name="Reminders",
            value="Opt in with `event remind <ID> true`. Reminders arrive privately before the start.",
            inline=False,
        )
    if kind == "events" and record.get("repeat_days"):
        embed.add_field(
            name="Schedule",
            value=f"Every {record['repeat_days']} days · Capacity {record.get('capacity') or 'unlimited'}",
            inline=False,
        )
    return embed


class SocialView(discord.ui.View):
    def __init__(self, cog, guild_id, kind, key, record):
        super().__init__(timeout=None)
        self.cog, self.guild_id, self.kind, self.key = cog, guild_id, kind, key
        options = (
            [
                discord.SelectOption(label=clip(text, 100), value=str(index))
                for index, text in enumerate(record["options"], 1)
            ]
            if kind == "polls"
            else [
                discord.SelectOption(label=label, value=value)
                for label, value in (("Going", "yes"), ("Maybe", "maybe"), ("Not going", "no"))
            ]
        )
        selector = discord.ui.Select(
            custom_id=f"community:{kind}:{guild_id}:{key}",
            placeholder="Cast your vote" if kind == "polls" else "Choose your attendance",
            options=options,
        )

        async def callback(interaction):
            try:
                if interaction.guild_id != guild_id or cog._closing:
                    raise commands.CheckFailure("This community panel expired.")
                path = "poll vote" if kind == "polls" else "event rsvp"
                ctx = await component_context(cog, interaction, path)
                value = int(selector.values[0]) if kind == "polls" else selector.values[0]
                await cog._social_choice(ctx, kind, key, value)
                await interaction.followup.send("Your choice was saved.", ephemeral=True)
            except commands.CommandError as error:
                await component_error(interaction, error)

        selector.callback = callback
        self.add_item(selector)

    async def on_error(self, interaction, error, item):
        await component_error(interaction, error)


class CommunitySocial:
    async def _social_record(self, guild, kind, key):
        record = await self.config.guild(guild).social.get_raw(kind, key, default=None)
        if not record:
            raise commands.BadArgument("Unknown ID. Use poll or event to list saved entries.")
        return record

    async def _social_refresh(self, guild, kind, key):
        async with self._social_locks[(guild.id, kind, key)]:
            await self._social_refresh_unlocked(guild, kind, key)

    async def _social_refresh_unlocked(self, guild, kind, key):
        record = await self._social_record(guild, kind, key)
        channel = guild.get_channel_or_thread(record["channel"])
        if channel is None or not record["message"]:
            return
        closed = record["closed"] or record["at"] <= self._now_ts()
        token = (guild.id, kind, key)
        old = self._social_views.pop(token, None)
        if old:
            old.stop()
        view = None if closed else SocialView(self, guild.id, kind, key, record)
        if view:
            self._social_views[token] = view
        try:
            message = await asyncio.wait_for(channel.fetch_message(record["message"]), 10)
            embed = social_embed(self, kind, key, record)
            embed = self._presentation.apply_theme(embed, bot=self.bot, guild=guild)
            use_embeds = (
                record.get("use_embeds", True) and channel.permissions_for(guild.me).embed_links
            )
            text = clip(
                f"**{embed.title}**\n{embed.description}\n"
                + "\n".join(f"**{field.name}** · {field.value}" for field in embed.fields)
                + f"\n{embed.footer.text}",
                2000,
            )
            await asyncio.wait_for(
                message.edit(
                    content=None if use_embeds else text,
                    embed=embed if use_embeds else None,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                10,
            )
        except (discord.HTTPException, asyncio.TimeoutError):
            # Choices are saved even if the old announcement cannot be refreshed.
            pass

    async def _restore_social(self, guild):
        for kind, rows in (await self.config.guild(guild).social()).items():
            for key, record in rows.items():
                if not record["closed"] and record["at"] > self._now_ts() and record["message"]:
                    view = SocialView(self, guild.id, kind, key, record)
                    self._social_views[(guild.id, kind, key)] = view
                    self.bot.add_view(view, message_id=record["message"])

    async def _social_create(self, ctx, kind, record):
        if self._closing:
            raise commands.CommandError("Community is unloading.")
        key = uuid.uuid4().hex[:12]
        record.update(creator=ctx.author.id, channel=ctx.channel.id, message=None, closed=False)
        requested = getattr(ctx, "embed_requested", None)
        record["use_embeds"] = await requested() if requested else True
        group = self.config.guild(ctx.guild).social
        async with group() as data:
            rows = data[kind]
            if (
                sum(not item["closed"] and item["at"] > self._now_ts() for item in rows.values())
                >= 10
            ):
                raise commands.BadArgument(
                    "Up to ten polls or ten events can be open. Close one first."
                )
            for stale in sorted(rows, key=lambda rid: rows[rid]["at"]):
                if len(rows) < MAX_RECORDS:
                    break
                if rows[stale]["closed"] or rows[stale]["at"] <= self._now_ts():
                    rows.pop(stale)
                    view = self._social_views.pop((ctx.guild.id, kind, stale), None)
                    if view:
                        view.stop()
                    self._social_locks.pop((ctx.guild.id, kind, stale), None)
            rows[key] = record
        view = SocialView(self, ctx.guild.id, kind, key, record)
        try:
            message = await self._reply(ctx, embed=social_embed(self, kind, key, record), view=view)
        except BaseException:
            view.stop()
            async with group() as data:
                data[kind].pop(key, None)
            raise
        async with group() as data:
            if key in data[kind]:
                data[kind][key]["message"] = message.id
        if self._closing:
            view.stop()
        else:
            self._social_views[(ctx.guild.id, kind, key)] = view

    async def _social_choice(self, ctx, kind, key, value):
        if self._closing or await self.bot.cog_disabled_in_guild(self, ctx.guild):
            raise commands.CheckFailure("Community is disabled here.")
        group = self.config.guild(ctx.guild).social
        async with group() as data:
            record = data[kind].get(key)
            if not record or record["closed"] or record["at"] <= self._now_ts():
                raise commands.BadArgument("This entry closed or no longer exists.")
            if kind == "polls" and not 1 <= value <= len(record["options"]):
                raise commands.BadArgument("Choose a listed option number.")
            if kind == "events" and value not in {"yes", "maybe", "no"}:
                raise commands.BadArgument("Choose yes, maybe, or no.")
            choices = record["votes" if kind == "polls" else "rsvps"]
            uid = str(ctx.author.id)
            if uid not in choices and len(choices) >= MAX_PARTICIPANTS:
                raise commands.BadArgument("This entry reached its 1,000-participant limit.")
            if kind == "events":
                attendance_choice(record, uid, value)
            else:
                choices[uid] = value
        await self._social_refresh(ctx.guild, kind, key)

    async def _social_close(self, ctx, kind, key):
        await self._social_record(ctx.guild, kind, key)
        async with self.config.guild(ctx.guild).social() as data:
            if key not in data[kind]:
                raise commands.BadArgument("This entry no longer exists.")
            data[kind][key]["closed"] = True
        await self._social_refresh(ctx.guild, kind, key)
        await self._presentation.confirm(ctx)

    async def _social_list(self, ctx, kind, key):
        if key:
            return await self._reply(
                ctx,
                embed=social_embed(
                    self, kind, key, await self._social_record(ctx.guild, kind, key)
                ),
            )
        rows = await self.config.guild(ctx.guild).social.get_attr(kind)()
        lines = [
            f"`{rid}` · {discord.utils.escape_markdown(row['title'])} · <t:{row['at']}:R> · {'Closed' if row['closed'] or row['at'] <= self._now_ts() else 'Open'}"
            for rid, row in sorted(rows.items(), key=lambda item: item[1]["at"], reverse=True)
        ]
        await self._reply(
            ctx,
            "\n".join(lines) or "No saved entries.",
            title="Polls" if kind == "polls" else "Events",
        )

    @commands.hybrid_group(name="poll", invoke_without_command=True, fallback="list")
    @commands.guild_only()
    async def poll(self, ctx, poll_id: str = ""):
        """Vote in community polls and view results."""
        await self._social_list(ctx, "polls", poll_id)

    @poll.command(name="create")
    @commands.admin_or_permissions(manage_guild=True)
    async def poll_create(self, ctx, question: str, options: str, hours: int = 24):
        """Post a poll with pipe-separated options and a closing time."""
        choices = [text.strip() for text in options.split("|")]
        if (
            not question.strip()
            or len(question) > 300
            or not 2 <= len(choices) <= 10
            or any(not value or len(value) > 80 for value in choices)
            or len(set(value.casefold() for value in choices)) != len(choices)
            or not 1 <= hours <= 168
        ):
            raise commands.BadArgument(
                "Use a question up to 300 characters, 2 to 10 unique options up to 80 characters separated by |, and 1 to 168 hours."
            )
        await self._social_create(
            ctx,
            "polls",
            {
                "title": question.strip(),
                "options": choices,
                "at": self._now_ts() + hours * 3600,
                "votes": {},
            },
        )

    @poll.command(name="vote")
    async def poll_vote(self, ctx, poll_id: str, choice: int):
        """Cast or change your one vote using a listed option number."""
        await self._social_choice(ctx, "polls", poll_id, choice)
        await self._presentation.confirm(ctx)

    @poll.command(name="close")
    @commands.admin_or_permissions(manage_guild=True)
    async def poll_close(self, ctx, poll_id: str):
        """Close a poll and retain its final results."""
        await self._social_close(ctx, "polls", poll_id)

    @commands.hybrid_group(name="event", invoke_without_command=True, fallback="list")
    @commands.guild_only()
    async def event(self, ctx, event_id: str = ""):
        """View community events and manage your attendance."""
        await self._social_list(ctx, "events", event_id)

    @event.command(name="create")
    @commands.admin_or_permissions(manage_guild=True)
    async def event_create(self, ctx, title: str, when: str, reminder_minutes: int = 15):
        """Post an event using a date with a timezone offset."""
        if not title.strip() or len(title) > 200 or not 1 <= reminder_minutes <= 1440:
            raise commands.BadArgument(
                "Use a title up to 200 characters and a reminder from 1 to 1,440 minutes before the start."
            )
        at = event_time(when, self._now_ts())
        await self._social_create(
            ctx,
            "events",
            {
                "title": title.strip(),
                "at": at,
                "rsvps": {},
                "remind": {},
                "notified": [],
                "reminder_at": at - reminder_minutes * 60,
                "announced": False,
            },
        )

    @event.command(name="policy")
    @commands.admin_or_permissions(manage_guild=True)
    async def event_policy(self, ctx, event_id: str, capacity: int = 0, repeat_days: int = 0):
        """Set capacity and a recurring interval in days; zero disables either."""
        if not 0 <= capacity <= 1000 or not 0 <= repeat_days <= 365:
            raise commands.BadArgument("Choose capacity 0 to 1000 and repeat days 0 to 365.")
        async with self.config.guild(ctx.guild).social() as data:
            record = data["events"].get(event_id)
            if not record or record["closed"] or record["at"] <= self._now_ts():
                raise commands.BadArgument("Choose an open future event.")
            if capacity and sum(v == "yes" for v in record["rsvps"].values()) > capacity:
                raise commands.BadArgument("Capacity cannot be smaller than confirmed attendance.")
            record.update(capacity=capacity, repeat_days=repeat_days)
            for uid, value in list(record["rsvps"].items()):
                if value == "wait":
                    attendance_choice(record, uid, "yes")
        await self._social_refresh(ctx.guild, "events", event_id)
        await self._reply(
            ctx,
            "Event capacity and recurring schedule saved. Attendance and reminder opt-ins reset for each occurrence.",
        )

    @event.command(name="rsvp")
    async def event_rsvp(self, ctx, event_id: str, attendance: str):
        """Set attendance to yes, maybe, or no."""
        await self._social_choice(ctx, "events", event_id, attendance.lower())
        record = await self._social_record(ctx.guild, "events", event_id)
        choice = record["rsvps"].get(str(ctx.author.id), attendance.lower())
        await self._reply(
            ctx,
            "Your attendance: "
            + {"yes": "Going", "wait": "Waitlisted", "maybe": "Maybe", "no": "Not going"}[choice],
        )

    @event.command(name="remind")
    async def event_remind(self, ctx, event_id: str, enabled: bool = True):
        """Opt in or out of a private reminder for one future event."""
        await self._social_record(ctx.guild, "events", event_id)
        async with self.config.guild(ctx.guild).social() as data:
            record = data["events"].get(event_id)
            if record is None:
                raise commands.BadArgument("This event no longer exists.")
            if record["closed"] or record["at"] <= self._now_ts():
                raise commands.BadArgument("This event already closed.")
            uid = str(ctx.author.id)
            if enabled:
                if uid not in record["remind"] and len(record["remind"]) >= MAX_REMINDERS:
                    raise commands.BadArgument("This event reached its reminder limit.")
                record["remind"][uid] = True
            else:
                record["remind"].pop(uid, None)
        await self._reply(
            ctx,
            "Private reminder enabled." if enabled else "Private reminder disabled.",
            tone="success",
        )

    @event.command(name="cancel")
    @commands.admin_or_permissions(manage_guild=True)
    async def event_cancel(self, ctx, event_id: str):
        """Cancel an event and its pending reminders."""
        await self._social_close(ctx, "events", event_id)

    async def _social_tick(self, guild):
        if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
            return
        group = self.config.guild(guild).social
        for kind, rows in (await group()).items():
            for key, record in rows.items():
                if record["at"] < self._now_ts() - RETENTION and (
                    record["closed"] or not record.get("repeat_days")
                ):
                    await self._social_refresh(guild, kind, key)
                    async with group() as data:
                        data[kind].pop(key, None)
                    view = self._social_views.pop((guild.id, kind, key), None)
                    if view:
                        view.stop()
                    self._social_locks.pop((guild.id, kind, key), None)
                    continue
                if not record["closed"] and record["at"] <= self._now_ts():
                    async with group() as data:
                        current = data[kind].get(key)
                        if current and not current["closed"] and current["at"] <= self._now_ts():
                            step = current.get("repeat_days", 0) * 86400 if kind == "events" else 0
                            if step:
                                delay = current["at"] - current["reminder_at"]
                                current["at"] += step * (
                                    (self._now_ts() - current["at"]) // step + 1
                                )
                                current.update(
                                    rsvps={},
                                    remind={},
                                    notified=[],
                                    announced=False,
                                    reminder_at=current["at"] - delay,
                                )
                            else:
                                current["closed"] = True
                    await self._social_refresh(guild, kind, key)
                elif (
                    kind == "events"
                    and not record["closed"]
                    and record["reminder_at"] <= self._now_ts()
                ):
                    await self._event_reminders(guild, key)

    async def _event_reminders(self, guild, key):
        group = self.config.guild(guild).social
        record = await self._social_record(guild, "events", key)
        channel = guild.get_channel_or_thread(record["channel"])
        if not record["announced"] and channel:
            with suppress(discord.HTTPException, asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._presentation.send(
                        channel, embed=social_embed(self, "events", key, record)
                    ),
                    10,
                )
                async with group() as data:
                    if key in data["events"]:
                        data["events"][key]["announced"] = True
        slots = asyncio.Semaphore(5)

        async def notify(uid):
            async with slots:
                await send_reminder(uid)

        async def send_reminder(uid):
            # Read again before each delivery so cancel/opt-out/deletion take effect.
            current = await self._social_record(guild, "events", key)
            if (
                self._closing
                or current["closed"]
                or self._now_ts() >= current["at"]
                or await self.bot.cog_disabled_in_guild(self, guild)
            ):
                return
            if uid not in current["remind"] or uid in current["notified"]:
                return
            member = guild.get_member(int(uid))
            if member:
                with suppress(discord.HTTPException, asyncio.TimeoutError):
                    await asyncio.wait_for(
                        member.send(
                            embed=social_embed(self, "events", key, current),
                            allowed_mentions=discord.AllowedMentions.none(),
                        ),
                        3,
                    )
                    async with group() as data:
                        if key in data["events"] and uid in data["events"][key]["remind"]:
                            data["events"][key]["notified"].append(uid)

        await asyncio.gather(
            *(notify(uid) for uid in record["remind"] if uid not in record["notified"])
        )

    async def _social_user_data(self, user_id, *, delete=False):
        exported, uid = {}, str(user_id)
        for gid in await self.config.all_guilds():
            group = self.config.guild_from_id(gid).social
            async with group() as data:
                for kind, rows in data.items():
                    for key, row in rows.items():
                        fields = ("votes",) if kind == "polls" else ("rsvps", "remind")
                        personal = {field: row[field][uid] for field in fields if uid in row[field]}
                        if row["creator"] == user_id:
                            personal["created"] = True
                        if kind == "events" and uid in row["notified"]:
                            personal["notified"] = True
                        if personal:
                            exported.setdefault(str(gid), {})[f"{kind}:{key}"] = personal
                        if delete:
                            for field in fields:
                                row[field].pop(uid, None)
                            if row["creator"] == user_id:
                                row["creator"] = None
                            if kind == "events":
                                promote_waitlist(row)
                                row["notified"] = [
                                    value for value in row["notified"] if value != uid
                                ]
        return exported
