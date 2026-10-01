"""Bounded custom styles, reversible reposts and opt-in haiku activities."""

import asyncio
import json
import logging
import re
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from typing import Optional

import discord
from redbot.core import commands

from .features import STYLE_WORDS, valid_word
from .haiku import _detect_haiku_breaks, _normalize_for_haiku, _reflow_text_as_haiku
from .interactive import component_context, component_error

log = logging.getLogger(__name__)
FUN_DEFAULTS = {"undo": True}
POETRY_DEFAULTS = {"hall": {}, "contests": {}}
POETRY_LIMIT = 1024 * 1024
RETENTION = 90 * 86400


def style_name(name):
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", name):
        raise commands.BadArgument(
            "Use a style name of 1 to 24 letters, digits, dashes or underscores."
        )
    return name


def single_line(value, maximum):
    if len(value) > maximum or any(ord(char) < 32 for char in value):
        raise commands.BadArgument(f"Use one line of at most {maximum} characters.")
    return value


def style_words(raw):
    if len(raw) > 8192:
        raise commands.BadArgument("Keep the replacement JSON under 8192 characters.")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate word")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, TypeError):
        raise commands.BadArgument('Use a JSON object such as {"hello":"greetings"}.') from None
    if not isinstance(value, dict) or len(value) > 50:
        raise commands.BadArgument("Use at most 50 word replacements per style.")
    result = {}
    for key, replacement in value.items():
        key = valid_word(key)
        if key in result or not isinstance(replacement, str) or not replacement:
            raise commands.BadArgument("Each word must have one nonempty text replacement.")
        result[key] = single_line(replacement, 60)
    return result


def poetry_size(data):
    if len(json.dumps(data, ensure_ascii=False).encode()) > POETRY_LIMIT:
        raise commands.BadArgument(
            "The haiku collection is full. Remove older entries or contests."
        )


def close_contest(contest):
    """Freeze a deterministic winner: votes, then earliest submission, then ID."""
    counts = Counter(contest["votes"].values())
    entries = contest["entries"]
    contest["winner"] = (
        min(entries, key=lambda key: (-counts[key], entries[key]["at"], key)) if entries else None
    )
    contest["closed"] = True


class UndoView(discord.ui.View):
    """Retain text briefly and restore webhook copies without reuploading files."""

    def __init__(self, cog, original, hook, posted):
        super().__init__(timeout=120)
        self.cog, self.original_id = cog, original.id
        self.owner_id, self.guild_id, self.channel_id = (
            original.author.id,
            original.guild.id,
            original.channel.id,
        )
        self.channel, self.author, self.hook = original.channel, original.author, hook
        self.raw, self.posted = original.content, list(posted)
        self.expires = time.monotonic() + 120
        self.message = None
        self.lock = asyncio.Lock()
        self.cog._views.add(self)

    @discord.ui.button(label="Undo transformation", style=discord.ButtonStyle.secondary)
    async def undo_button(self, interaction, button):
        if (
            interaction.guild is None
            or interaction.guild.id != self.guild_id
            or interaction.channel_id != self.channel_id
        ):
            raise commands.CheckFailure("This Undo belongs to another channel.")
        ctx = await component_context(self.cog, interaction, "owoundo", owner_id=self.owner_id)
        await self.restore(ctx)
        await interaction.followup.send("Original text restored.", ephemeral=True)

    async def on_error(self, interaction, error, item):
        await component_error(interaction, error)

    async def _finish(self):
        self.stop()
        self.cog._views.discard(self)
        if self.cog._undos.get(self.original_id) is self:
            self.cog._undos.pop(self.original_id, None)
        self.raw = ""
        self.author = self.hook = self.channel = None
        self.posted.clear()
        if self.message:
            try:
                await asyncio.wait_for(self.message.edit(view=None), 10)
            except (discord.HTTPException, asyncio.TimeoutError):
                pass
            self.message = None

    async def on_timeout(self):
        async with self.lock:
            await self._finish()

    async def restore(self, ctx):
        async with self.lock:
            if (
                ctx.author.id != self.owner_id
                or ctx.guild.id != self.guild_id
                or ctx.channel.id != self.channel_id
            ):
                raise commands.CheckFailure(
                    "Only the original author can undo this message in its channel."
                )
            if self.cog._closing or not self.raw or time.monotonic() >= self.expires:
                await self._finish()
                raise commands.BadArgument(
                    "This Undo expired. Original text is retained for two minutes."
                )
            if await self.cog.bot.cog_disabled_in_guild(self.cog, ctx.guild):
                raise commands.CheckFailure("OwoPlus is disabled here.")
            parts = self.cog._chunk_message(self.raw)
            edited, added = [], []
            try:
                for index, part in enumerate(parts):
                    if index < len(self.posted):
                        posted = self.posted[index]
                        previous = getattr(posted, "content", None)
                        await asyncio.wait_for(
                            posted.edit(
                                content=part, allowed_mentions=discord.AllowedMentions.none()
                            ),
                            10,
                        )
                        edited.append((posted, previous))
                    else:
                        added.append(
                            await asyncio.wait_for(
                                self.cog._send_via_webhook(
                                    self.hook,
                                    channel=self.channel,
                                    author=self.author,
                                    content=part,
                                    files=None,
                                    wait=True,
                                ),
                                10,
                            )
                        )
            except (
                discord.HTTPException,
                OSError,
                asyncio.TimeoutError,
                asyncio.CancelledError,
            ) as error:
                # Retain the transformed copies if restoring the full original fails.
                for posted, previous in edited:
                    if isinstance(previous, str):
                        try:
                            await asyncio.wait_for(
                                posted.edit(
                                    content=previous,
                                    allowed_mentions=discord.AllowedMentions.none(),
                                ),
                                10,
                            )
                        except (discord.HTTPException, asyncio.TimeoutError):
                            log.warning("Could not roll back a partial Undo", exc_info=True)
                for posted in added:
                    try:
                        await asyncio.wait_for(posted.delete(), 10)
                    except (discord.HTTPException, asyncio.TimeoutError):
                        pass
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise commands.BadArgument(
                    "Undo could not restore every part. You can retry before it expires."
                ) from error
            extras = self.posted[len(parts) :]
            for posted in extras:
                try:
                    await asyncio.wait_for(posted.delete(), 10)
                except (discord.HTTPException, asyncio.TimeoutError):
                    log.warning(
                        "Undo restored the text but could not remove an extra transformed part"
                    )
            await self._finish()


class FunCommands:
    @asynccontextmanager
    async def _poetry_edit(self, guild_id):
        group = self.config.guild_from_id(guild_id).poetry
        async with group.get_lock():
            data = await group()
            yield data
            poetry_size(data)
            await group.set(data)

    @commands.hybrid_group(name="customstyle", autohelp=False, fallback="list")
    @commands.guild_only()
    async def customstyle(self, ctx):
        """List this server's custom transformation styles."""
        data = (await self.config.guild(ctx.guild).features())["custom_styles"]
        await self._reply(
            ctx,
            "\n".join(
                f"**{key}** · {len(value['words'])} word replacements"
                for key, value in sorted(data.items())
            )
            or "No custom styles. An administrator can use customstyle create.",
            title="Custom styles",
        )

    @customstyle.command(name="create")
    @commands.admin_or_permissions(manage_guild=True)
    async def customstyle_create(self, ctx, name: str, *, replacements: str):
        """Create a named style from a JSON word dictionary."""
        name, words = style_name(name), style_words(replacements)
        if name in STYLE_WORDS:
            raise commands.BadArgument("Built-in style names are reserved.")
        async with self.config.guild(ctx.guild).features() as data:
            styles = data["custom_styles"]
            if name in styles:
                raise commands.BadArgument(
                    "That style exists. Delete it before replacing its dictionary."
                )
            if len(styles) >= 10:
                raise commands.BadArgument("Keep at most 10 custom styles.")
            styles[name] = {"words": words, "prefix": "", "suffix": "", "uppercase": False}
        self._settings_cache.pop(ctx.guild.id, None)
        await self._reply(
            ctx,
            f"Created **{name}**. Preview with stylize or apply with owo style set.",
            tone="success",
        )

    @customstyle.command(name="decorate")
    @commands.admin_or_permissions(manage_guild=True)
    async def customstyle_decorate(
        self, ctx, name: str, prefix: str, suffix: str, uppercase: bool = False
    ):
        """Set a style's prefix, suffix and uppercase option."""
        name = style_name(name)
        prefix, suffix = single_line(prefix, 80), single_line(suffix, 80)
        async with self.config.guild(ctx.guild).features() as data:
            if name not in data["custom_styles"]:
                raise commands.BadArgument("Choose an existing custom style.")
            data["custom_styles"][name].update(prefix=prefix, suffix=suffix, uppercase=uppercase)
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @customstyle.command(name="delete")
    @commands.admin_or_permissions(manage_guild=True)
    async def customstyle_delete(self, ctx, name: str):
        """Delete a custom style and its channel overrides."""
        name = style_name(name)
        async with self.config.guild(ctx.guild).features() as data:
            if not data["custom_styles"].pop(name, None):
                raise commands.BadArgument("Choose an existing custom style.")
            data["channel_styles"] = {
                cid: value
                for cid, value in data["channel_styles"].items()
                if value["style"] != name
            }
        self._settings_cache.pop(ctx.guild.id, None)
        await self._presentation.confirm(ctx)

    @commands.hybrid_command(name="owoundoset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def owoundoset(self, ctx, enabled: bool):
        """Toggle two-minute Undo controls for future reposts."""
        group = self.config.guild(ctx.guild).fun_settings
        async with group.get_lock():
            await group.undo.set(enabled)
        await self._presentation.confirm(ctx)

    @commands.hybrid_command(name="owoundo")
    @commands.guild_only()
    async def owoundo(self, ctx, message_id: Optional[str] = None):
        """Undo your latest transformation in this channel."""
        view = next(
            (
                view
                for view in reversed(tuple(self._undos.values()))
                if view.owner_id == ctx.author.id
                and view.guild_id == ctx.guild.id
                and view.channel_id == ctx.channel.id
                and (message_id is None or str(view.original_id) == message_id)
            ),
            None,
        )
        if view is None:
            raise commands.BadArgument(
                "No active Undo for you in this channel. Controls expire after two minutes."
            )
        await view.restore(ctx)
        await self._reply(
            ctx,
            "Original text restored. Copied attachments remain on the webhook message.",
            title="Undo",
            tone="success",
        )

    async def _attach_undo(self, original, hook, posted):
        if self._closing or not posted or not callable(getattr(posted[0], "edit", None)):
            return
        if not await self.config.guild(original.guild).fun_settings.undo() or self._closing:
            return
        if len(original.content.encode("utf-16-le")) // 2 > 4000:
            return
        while len(self._undos) >= 50:
            await next(iter(self._undos.values())).on_timeout()
        view = UndoView(self, original, hook, posted)
        self._undos[original.id] = view
        try:
            # Incoming webhooks cannot carry bot components. Use a separate bot card.
            view.message = await asyncio.wait_for(
                self._presentation.send(
                    original.channel,
                    f"<@{original.author.id}> can restore the original text for two minutes.\nOriginal message ID: `{original.id}`",
                    title="Undo",
                    view=view,
                ),
                10,
            )
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            await view.on_timeout()
            log.debug("Could not send an Undo control; successful repost retained", exc_info=True)
        except asyncio.CancelledError:
            await view.on_timeout()
            raise

    async def _poetry_manager(self, ctx):
        if ctx.author.guild_permissions.manage_guild or await self.bot.is_owner(ctx.author):
            return True
        return bool(
            callable(getattr(self.bot, "is_admin", None)) and await self.bot.is_admin(ctx.author)
        )

    async def _poem_text(self, guild, text):
        if not text.strip() or len(text) > 300:
            raise commands.BadArgument("Submit a haiku of at most 300 characters.")
        syllables = (await self.config.guild(guild).features())["syllables"]

        def detect():
            normalized = _normalize_for_haiku(text)
            cuts = _detect_haiku_breaks(normalized, syllables)
            return _reflow_text_as_haiku(normalized, cuts) if cuts else None

        async with self._render_gate:
            poem = await asyncio.to_thread(detect)
        if poem is None:
            raise commands.BadArgument(
                "No English 5-7-5 haiku was detected. Use owo poem diag or syllable corrections."
            )
        return poem

    @commands.hybrid_group(name="haikuhall", autohelp=False, fallback="list")
    @commands.guild_only()
    async def haikuhall(self, ctx, entry_id: Optional[str] = None):
        """Browse the approved server haiku collection."""
        data = (await self.config.guild(ctx.guild).poetry())["hall"]
        manager = await self._poetry_manager(ctx)
        if entry_id:
            entry = data.get(entry_id)
            if not entry or (
                not entry["approved"] and not manager and entry["author"] != ctx.author.id
            ):
                raise commands.BadArgument("Choose a visible haiku ID from haikuhall.")
            await self._reply(
                ctx,
                f"{entry['text']}\n\nAuthor: <@{entry['author']}> · {'Approved' if entry['approved'] else 'Pending approval'}",
                title=f"Haiku {entry_id}",
            )
            return
        lines = [
            f"`{key}` · <@{entry['author']}> · {entry['text'].splitlines()[0]}"
            for key, entry in list(data.items())[-20:]
            if entry["approved"]
        ]
        await self._reply(
            ctx,
            "\n".join(lines) or "No approved haiku yet. Use haikuhall submit.",
            title="Haiku hall",
        )

    @haikuhall.command(name="submit")
    @commands.cooldown(1, 15, commands.BucketType.member)
    async def haikuhall_submit(self, ctx, *, text: str):
        """Submit your own haiku for moderator approval."""
        poem, key = await self._poem_text(ctx.guild, text), uuid.uuid4().hex[:12]
        async with self._poetry_edit(ctx.guild.id) as data:
            hall = data["hall"]
            if (
                len(hall) >= 100
                or sum(
                    entry["author"] == ctx.author.id and not entry["approved"]
                    for entry in hall.values()
                )
                >= 3
            ):
                raise commands.BadArgument(
                    "The hall holds 100 haiku, with at most three pending per author."
                )
            hall[key] = {
                "text": poem,
                "author": ctx.author.id,
                "at": time.time(),
                "approved": False,
                "approver": None,
            }
            poetry_size(data)
        await self._reply(
            ctx,
            f"Submitted `{key}` for approval. Submissions expire after 90 days.",
            tone="success",
        )

    @haikuhall.command(name="review")
    @commands.admin_or_permissions(manage_guild=True)
    async def haikuhall_review(self, ctx):
        """Show pending haiku for approval."""
        hall = (await self.config.guild(ctx.guild).poetry())["hall"]
        lines = [
            f"**{key}** · <@{entry['author']}>\n{entry['text']}"
            for key, entry in hall.items()
            if not entry["approved"]
        ]
        await self._reply(
            ctx, "\n\n".join(lines) or "No pending submissions.", title="Haiku review"
        )

    @haikuhall.command(name="approve")
    @commands.admin_or_permissions(manage_guild=True)
    async def haikuhall_approve(self, ctx, entry_id: str, approved: bool = True):
        """Approve a haiku or reject and remove its submission."""
        async with self._poetry_edit(ctx.guild.id) as data:
            entry = data["hall"].get(entry_id)
            if not entry:
                raise commands.BadArgument("Choose a haiku ID from haikuhall review.")
            if approved:
                entry.update(approved=True, approver=ctx.author.id)
            else:
                data["hall"].pop(entry_id)
        await self._presentation.confirm(ctx)

    @haikuhall.command(name="remove")
    async def haikuhall_remove(self, ctx, entry_id: str):
        """Remove your haiku; moderators can remove any entry."""
        manager = await self._poetry_manager(ctx)
        async with self._poetry_edit(ctx.guild.id) as data:
            entry = data["hall"].get(entry_id)
            if not entry or (entry["author"] != ctx.author.id and not manager):
                raise commands.BadArgument("Choose your own haiku ID or ask a moderator.")
            data["hall"].pop(entry_id)
        await self._presentation.confirm(ctx)

    @commands.hybrid_group(name="haikucontest", autohelp=False, fallback="list")
    @commands.guild_only()
    async def haikucontest(self, ctx, contest_id: Optional[str] = None):
        """Browse haiku contests, entries and vote totals."""
        contests = (await self.config.guild(ctx.guild).poetry())["contests"]
        if not contest_id:
            lines = []
            for key, entry in contests.items():
                state = "Closed" if entry["closed"] else f"Ends <t:{int(entry['ends'])}:R>"
                lines.append(f"`{key}` · {entry['title']} · {state}")
            await self._reply(
                ctx,
                "\n".join(lines) or "No contests. Ask a moderator to create one.",
                title="Haiku contests",
            )
            return
        contest = contests.get(contest_id)
        if not contest:
            raise commands.BadArgument("Choose a contest ID from haikucontest.")
        counts = Counter(contest["votes"].values())
        lines = [
            f"**{key}** · <@{entry['author']}> · {counts[key]} votes\n{entry['text']}"
            for key, entry in contest["entries"].items()
        ]
        state = (
            f"Winner: `{contest['winner'] or 'No entries'}`"
            if contest["closed"]
            else f"Ends <t:{int(contest['ends'])}:R>. Use haikucontest vote {contest_id} <entry_id>."
        )
        await self._reply(
            ctx, state + "\n\n" + ("\n\n".join(lines) or "No entries yet."), title=contest["title"]
        )

    @haikucontest.command(name="create")
    @commands.admin_or_permissions(manage_guild=True)
    async def haikucontest_create(self, ctx, hours: int, *, title: str):
        """Create a contest lasting from one hour to seven days."""
        title = single_line(title.strip(), 100)
        if not title or not 1 <= hours <= 168:
            raise commands.BadArgument("Use a title and a duration of 1 to 168 hours.")
        key = uuid.uuid4().hex[:12]
        async with self._poetry_edit(ctx.guild.id) as data:
            if len(data["contests"]) >= 10:
                raise commands.BadArgument("Keep at most 10 contests. Delete an older one first.")
            data["contests"][key] = {
                "title": title,
                "creator": ctx.author.id,
                "channel": ctx.channel.id,
                "at": time.time(),
                "ends": time.time() + hours * 3600,
                "closed": False,
                "entries": {},
                "votes": {},
                "winner": None,
                "announced": False,
            }
            poetry_size(data)
        await self._reply(
            ctx, f"Created `{key}`. Enter with haikucontest submit {key} <haiku>.", tone="success"
        )

    @haikucontest.command(name="submit")
    async def haikucontest_submit(self, ctx, contest_id: str, *, text: str):
        """Enter your own haiku once in an open contest."""
        poem, key = await self._poem_text(ctx.guild, text), uuid.uuid4().hex[:12]
        async with self._poetry_edit(ctx.guild.id) as data:
            contest = data["contests"].get(contest_id)
            if not contest or contest["closed"] or time.time() >= contest["ends"]:
                raise commands.BadArgument("Choose an open contest before its deadline.")
            entries = contest["entries"]
            if len(entries) >= 50 or any(
                entry["author"] == ctx.author.id for entry in entries.values()
            ):
                raise commands.BadArgument(
                    "Each author gets one entry, with at most 50 entries per contest."
                )
            entries[key] = {"text": poem, "author": ctx.author.id, "at": time.time()}
            poetry_size(data)
        await self._reply(ctx, f"Entered `{key}` in contest `{contest_id}`.", tone="success")

    @haikucontest.command(name="vote")
    async def haikucontest_vote(self, ctx, contest_id: str, entry_id: str):
        """Cast or change your vote; authors cannot vote for themselves."""
        async with self._poetry_edit(ctx.guild.id) as data:
            contest = data["contests"].get(contest_id)
            if not contest or contest["closed"] or time.time() >= contest["ends"]:
                raise commands.BadArgument("Choose an open contest before its deadline.")
            entry = contest["entries"].get(entry_id)
            if not entry or entry["author"] == ctx.author.id:
                raise commands.BadArgument("Choose another author's contest entry.")
            votes = contest["votes"]
            if str(ctx.author.id) not in votes and len(votes) >= 500:
                raise commands.BadArgument("This contest has reached its 500-voter limit.")
            votes[str(ctx.author.id)] = entry_id
            poetry_size(data)
        await self._presentation.confirm(ctx)

    @haikucontest.command(name="withdraw")
    async def haikucontest_withdraw(self, ctx, contest_id: str):
        """Withdraw your entry and its votes before the deadline."""
        async with self._poetry_edit(ctx.guild.id) as data:
            contest = data["contests"].get(contest_id)
            if not contest or contest["closed"] or time.time() >= contest["ends"]:
                raise commands.BadArgument("Choose an open contest before its deadline.")
            removed = {
                key for key, entry in contest["entries"].items() if entry["author"] == ctx.author.id
            }
            for key in removed:
                contest["entries"].pop(key)
            contest["votes"] = {
                uid: key for uid, key in contest["votes"].items() if key not in removed
            }
        await self._presentation.confirm(ctx)

    @haikucontest.command(name="close")
    @commands.admin_or_permissions(manage_guild=True)
    async def haikucontest_close(self, ctx, contest_id: str):
        """Close voting early and choose the contest winner."""
        async with self._poetry_edit(ctx.guild.id) as data:
            contest = data["contests"].get(contest_id)
            if not contest:
                raise commands.BadArgument("Choose a contest ID from haikucontest.")
            if not contest["closed"]:
                close_contest(contest)
        await self._poetry_tick(ctx.guild)
        await self._presentation.confirm(ctx)

    @haikucontest.command(name="delete")
    @commands.admin_or_permissions(manage_guild=True)
    async def haikucontest_delete(self, ctx, contest_id: str):
        """Delete a contest and its stored submissions and votes."""
        async with self._poetry_edit(ctx.guild.id) as data:
            if not data["contests"].pop(contest_id, None):
                raise commands.BadArgument("Choose a contest ID from haikucontest.")
        await self._presentation.confirm(ctx)

    async def _poetry_tick(self, guild, *, now=None):
        if self._closing or await self.bot.cog_disabled_in_guild(self, guild):
            return
        now = time.time() if now is None else now
        async with self._poetry_edit(guild.id) as data:
            for contest in data["contests"].values():
                if not contest["closed"] and now >= contest["ends"]:
                    close_contest(contest)
                if not contest["closed"] or contest["announced"]:
                    continue
                channel = guild.get_channel_or_thread(contest["channel"])
                if channel is None:
                    continue
                winner = contest["entries"].get(contest["winner"])
                text = (
                    f"Winner: <@{winner['author']}>\n\n{winner['text']}\n\nVotes: {Counter(contest['votes'].values())[contest['winner']]}"
                    if winner
                    else "The contest closed without a remaining entry."
                )
                try:
                    await asyncio.wait_for(
                        self._presentation.send(
                            channel, text, title=f"{contest['title']} · Winner"
                        ),
                        10,
                    )
                except (discord.HTTPException, OSError, asyncio.TimeoutError):
                    continue
                contest["announced"] = True

    async def _prune_poetry(self, *, now=None):
        now = time.time() if now is None else now
        for guild_id in await self.config.all_guilds():
            async with self._poetry_edit(guild_id) as data:
                for section in ("hall", "contests"):
                    data[section] = {
                        key: entry
                        for key, entry in data[section].items()
                        if now - entry["at"] < RETENTION
                    }

    async def _fun_loop(self):
        await self.bot.wait_until_red_ready()
        last_prune = 0
        while not self._closing:
            try:
                now = time.time()
                if now - last_prune >= 3600:
                    await self._prune_poetry(now=now)
                    last_prune = now
                for guild in self.bot.guilds:
                    await self._poetry_tick(guild, now=now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Haiku activity maintenance failed")
            await asyncio.sleep(60)

    async def _delete_fun_user(self, user_id):
        for view in tuple(self._undos.values()):
            if view.owner_id == user_id:
                await view.on_timeout()
        for guild_id in await self.config.all_guilds():
            async with self._poetry_edit(guild_id) as data:
                data["hall"] = {
                    key: entry for key, entry in data["hall"].items() if entry["author"] != user_id
                }
                for entry in data["hall"].values():
                    if entry["approver"] == user_id:
                        entry["approver"] = None
                for contest in data["contests"].values():
                    if contest["creator"] == user_id:
                        contest["creator"] = None
                    removed = {
                        key
                        for key, entry in contest["entries"].items()
                        if entry["author"] == user_id
                    }
                    for key in removed:
                        contest["entries"].pop(key)
                    contest["votes"] = {
                        uid: key
                        for uid, key in contest["votes"].items()
                        if uid != str(user_id) and key not in removed
                    }
                    if contest["winner"] in removed:
                        if contest["announced"]:
                            contest["winner"] = None
                        else:
                            close_contest(contest)

    async def _fun_user_data(self, user_id):
        result = {}
        for gid, conf in (await self.config.all_guilds()).items():
            data = conf.get("poetry", POETRY_DEFAULTS)
            hall = {
                key: entry
                for key, entry in data["hall"].items()
                if user_id in (entry["author"], entry["approver"])
            }
            contests = {}
            for key, contest in data["contests"].items():
                entries = {
                    eid: entry
                    for eid, entry in contest["entries"].items()
                    if entry["author"] == user_id
                }
                vote = contest["votes"].get(str(user_id))
                if entries or vote or contest["creator"] == user_id:
                    contests[key] = {
                        "title": contest["title"],
                        "created_by_you": contest["creator"] == user_id,
                        "entries": entries,
                        "vote": vote,
                    }
            if hall or contests:
                result[str(gid)] = {"hall": hall, "contests": contests, "active_undo": {}}
        # A default-enabled Undo need not have any corresponding persisted guild record.
        for view in self._undos.values():
            if view.owner_id == user_id and time.monotonic() < view.expires:
                record = result.setdefault(
                    str(view.guild_id), {"hall": {}, "contests": {}, "active_undo": {}}
                )
                record["active_undo"][str(view.original_id)] = {
                    "channel": view.channel_id,
                    "text": view.raw,
                }
        return result
