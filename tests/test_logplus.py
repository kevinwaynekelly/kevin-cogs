from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import make_channel, make_context, make_member

from logplus import LogPlus


def units(text):
    return len((text or "").encode("utf-16-le")) // 2


@pytest.mark.parametrize("text", ["x", "😀"])
def test_embed_limits_include_footer_author_and_total(text):
    embed = discord.Embed(title=text * 1000, description=text * 10000)
    embed.set_footer(text=text * 5000)
    embed.set_author(name=text * 1000)
    for _ in range(30):
        embed.add_field(name=text * 400, value=text * 4000)
    fitted = LogPlus._fit_embed(embed)
    assert units(fitted.title) <= 256
    assert units(fitted.description) <= 4096
    assert units(fitted.footer.text) <= 2048
    assert units(fitted.author.name) <= 256
    assert len(fitted.fields) <= 25
    total = sum(
        units(t) for t in [fitted.title, fitted.description, fitted.footer.text, fitted.author.name]
    )
    for field in fitted.fields:
        assert 0 < units(field.name) <= 256
        assert 0 < units(field.value) <= 1024
        total += units(field.name) + units(field.value)
    assert total <= 6000


def test_first_event_at_uptime_zero_and_bounded_rate_cache(bot, monkeypatch):
    cog = LogPlus(bot)
    monkeypatch.setattr("logplus.cog.time.monotonic", lambda: 0)
    assert not cog._should_suppress("first", 2)
    assert cog._should_suppress("first", 2)
    for i in range(10020):
        cog._should_suppress(f"event:{i}", 2)
    assert len(cog._last_event_at) == 10000


async def test_diagnostic_does_not_mutate_live_settings(bot, guild):
    cog = LogPlus(bot)
    await cog.config.guild(guild).message.edit.set(False)
    before = await cog.config.guild(guild).all()
    await LogPlus.diag.callback(cog, make_context(guild))
    assert await cog.config.guild(guild).all() == before


async def test_two_reactors_both_logged_but_duplicate_is_suppressed(bot, guild):
    cog = LogPlus(bot)
    first = make_member(guild)
    second = make_member(guild, first.id + 1)
    channel = make_channel(guild)
    cog._send = AsyncMock()
    payload = SimpleNamespace(
        guild_id=guild.id, channel_id=channel.id, message_id=999, emoji="✅", user_id=first.id
    )
    await cog.on_raw_reaction_add(payload)
    payload.user_id = second.id
    await cog.on_raw_reaction_add(payload)
    await cog.on_raw_reaction_add(payload)
    assert cog._send.await_count == 2


async def test_audit_fetch_is_shared_and_skipped_without_destination(bot, guild):
    cog = LogPlus(bot)
    guild.audit_logs = Mock()
    assert await cog._audit_actor(guild, discord.AuditLogAction.role_create, 999) is None
    guild.audit_logs.assert_not_called()
    await cog.config.guild(guild).log_channel.set(456)
    await cog.cog_after_invoke(make_context(guild))
    entry = SimpleNamespace(
        action=discord.AuditLogAction.role_create,
        created_at=discord.utils.utcnow(),
        target=SimpleNamespace(id=999),
        user=SimpleNamespace(id=111),
    )

    async def audit_entries():
        yield entry

    guild.audit_logs.side_effect = lambda **kwargs: audit_entries()
    assert await cog._audit_actor(guild, discord.AuditLogAction.role_create, 999)
    assert await cog._audit_actor(guild, discord.AuditLogAction.role_create, 999)
    guild.audit_logs.assert_called_once()
    entry.created_at -= timedelta(seconds=120)
    assert await cog._audit_actor(guild, discord.AuditLogAction.role_create, 999) is None


async def test_thread_routes_and_parent_exemptions(bot, guild):
    cog = LogPlus(bot)
    parent = make_channel(guild)
    thread = make_channel(guild, parent.id + 1, discord.Thread)
    thread.parent_id = parent.id
    destination = make_channel(guild, parent.id + 2)
    await cog.config.guild(guild).overrides.set({str(parent.id): destination.id})
    await cog.config.guild(guild).server.exempt_channels.set([parent.id])
    assert await cog._log_channel(guild, thread.id) is destination
    assert await cog._is_exempt(guild, thread.id, "server")


async def test_scheduled_thread_handlers_are_registered_and_dispatch(bot, guild):
    cog = LogPlus(bot)
    listeners = dict(cog.get_listeners())
    for name in [
        "on_scheduled_event_create",
        "on_scheduled_event_update",
        "on_scheduled_event_delete",
        "on_scheduled_event_user_add",
        "on_scheduled_event_user_remove",
        "on_thread_create",
        "on_thread_delete",
        "on_thread_update",
    ]:
        assert name in listeners
    cog._audit_actor_recent = AsyncMock(return_value=None)
    cog._send = AsyncMock()
    event = SimpleNamespace(guild=guild, id=987, name="Event", creator=None)
    await listeners["on_scheduled_event_create"](event)
    cog._send.assert_awaited_once()
    bot.cog_disabled_in_guild.return_value = True
    await listeners["on_scheduled_event_create"](event)
    assert cog._send.await_count == 1


def test_corrected_command_names_keep_old_aliases(bot):
    cog = LogPlus(bot)
    root = next(c for c in cog.get_commands() if c.name == "log")
    toggles = root.get_command("toggle")
    assert toggles.get_command("commands") is toggles.get_command("commands_")
    server = toggles.get_command("server")
    assert server.get_command("threadupdate") is server.get_command("thredupdate")


async def test_concurrent_routes_and_toggles_preserve_each_update(bot, guild):
    import asyncio

    cog = LogPlus(bot)
    sources = [make_channel(guild, 1000 + i) for i in range(10)]
    destination = make_channel(guild, 2000)
    ctx = make_context(guild)
    await asyncio.gather(
        *(LogPlus.route_set.callback(cog, ctx, source, destination) for source in sources)
    )
    assert await cog.config.guild(guild).overrides() == {
        str(source.id): destination.id for source in sources
    }
    await asyncio.gather(cog._flip(ctx, "message", "edit"), cog._flip(ctx, "message", "edit"))
    assert await cog.config.guild(guild).message.edit() is True
