"""Theme consistency, real command integration, and Discord delivery boundaries."""

import importlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from conftest import make_channel, make_context, make_member

from audioplus import AudioPlus
from audioplus.presentation import COLORS, Presentation, units
from communityplus import CommunityPlus
from levelplus import LevelPlus
from logplus import LogPlus
from owoplus import OwoPlus

COGS = [
    (AudioPlus, "audio"),
    (CommunityPlus, "com"),
    (LevelPlus, "level"),
    (LogPlus, "logplus"),
    (OwoPlus, "owoplus"),
]


def assert_limits(embed):
    assert units(embed.title) <= 256
    assert units(embed.description) <= 4096
    assert units(embed.footer.text) <= 2048
    assert units(embed.author.name) <= 256
    assert len(embed.fields) <= 25
    total = sum(
        units(text)
        for text in (embed.title, embed.description, embed.footer.text, embed.author.name)
    )
    for field in embed.fields:
        assert 0 < units(field.name) <= 256
        assert 0 < units(field.value) <= 1024
        total += units(field.name) + units(field.value)
    assert total <= 6000


def test_vendored_themes_match_and_import_independently():
    sources = []
    for cls, _ in COGS:
        module = importlib.import_module(f"{cls.__module__.split('.')[0]}.presentation")
        sources.append(Path(module.__file__).read_text())
    assert len(set(sources)) == 1


@pytest.mark.parametrize("cls,root", COGS)
async def test_root_commands_use_same_theme(bot, guild, cls, root):
    cog = cls(bot)
    ctx = make_context(guild)
    ctx.command = SimpleNamespace(qualified_name=root)
    ctx.clean_prefix = "?"
    await getattr(cls, root).callback(cog, ctx)
    for call in ctx.send.await_args_list:
        embed = call.kwargs["embed"]
        assert embed.title.startswith(f"{cls.__name__} · ")
        assert embed.color.value == COLORS["info"]
        assert "Kevin's Cogs" in embed.footer.text
        assert "[p]" not in embed.footer.text
        assert "?" in embed.footer.text
        assert_limits(embed)


async def test_long_unicode_output_is_lossless_and_files_are_sent_once(guild):
    theme = Presentation("TestPlus", "test")
    ctx = make_context(guild)
    description = "line 😀 " * 2000
    field_value = "🌸 long field\n" * 500
    embed = theme.embed("Details", description)
    embed.set_author(name="😀" * 300)
    for index in range(30):
        embed.add_field(name=f"Field {index}", value=field_value if index == 0 else str(index))
    attachment = discord.File(io.BytesIO(b"export"), filename="export.txt")
    await theme.send(ctx, embed=embed, file=attachment)
    pages = [call.kwargs["embed"] for call in ctx.send.await_args_list]
    assert len(pages) > 1
    assert "".join(page.description or "" for page in pages) == description
    values = [field.value for page in pages for field in page.fields]
    assert "".join(values[:-29]) == field_value
    assert values[-29:] == [str(i) for i in range(1, 30)]
    assert sum("file" in call.kwargs for call in ctx.send.await_args_list) == 1
    for index, page in enumerate(pages, 1):
        assert f"Page {index}/{len(pages)}" in page.footer.text
        assert_limits(page)


@pytest.mark.parametrize("preference", [False, True])
async def test_plain_text_fallback_retains_content_and_attachment(guild, preference):
    channel = make_channel(guild)
    channel.permissions_for.return_value.embed_links = preference
    ctx = make_context(guild, channel)
    ctx.embed_requested = AsyncMock(return_value=False)
    text = "😀 @everyone " * 700
    attachment = discord.File(io.BytesIO(b"export"), filename="export.txt")
    await Presentation("TestPlus", "test").send(ctx, text, file=attachment)
    assert ctx.send.await_count > 1
    output = "".join(call.args[0] for call in ctx.send.await_args_list)
    # Each themed page has a header/footer; all original words remain present.
    assert output.count("😀") == 700
    assert output.count("@everyone") == 700
    for call in ctx.send.await_args_list:
        assert units(call.args[0]) <= 2000
        assert not call.kwargs["allowed_mentions"].everyone
    assert sum("file" in call.kwargs for call in ctx.send.await_args_list) == 1


async def test_confirmation_has_success_color_and_command_heading(guild):
    ctx = make_context(guild)
    ctx.command = SimpleNamespace(qualified_name="com autorole enable")
    await Presentation("CommunityPlus", "com").confirm(ctx)
    embed = ctx.send.await_args.kwargs["embed"]
    assert embed.title == "CommunityPlus · Autorole Enable"
    assert embed.description == "Settings saved."
    assert embed.color.value == COLORS["success"]


async def test_event_delivery_uses_theme_and_retains_metadata(bot, guild):
    cog = LogPlus(bot)
    channel = make_channel(guild)
    await cog.config.guild(guild).log_channel.set(channel.id)
    embed = await cog._E(guild, "Member joined", "Welcome", etype="member_join", footer="ID 123")
    embed.set_thumbnail(url="https://example.invalid/avatar.png")
    await cog._send(guild, embed)
    output = channel.send.await_args.kwargs["embed"]
    assert output.title.startswith("LogPlus · ")
    assert output.footer.text == "Kevin's Cogs · ID 123"
    assert output.thumbnail.url == embed.thumbnail.url
    assert output.timestamp == embed.timestamp


async def test_custom_levelup_template_keeps_text_and_mentions(bot, guild):
    cog = LevelPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    await cog.config.guild(guild).levelup.channel_id.set(channel.id)
    await cog.config.guild(guild).levelup.template.set("{user.mention} reached **{user.level}**!")
    await cog.maybe_announce_levelup(guild, member, 0, 2)
    output = channel.send.await_args.kwargs
    assert output["embed"].description == f"{member.mention} reached **2**!"
    assert output["allowed_mentions"] is None


async def test_long_owo_preview_paginates_instead_of_rejecting(bot, guild):
    cog = OwoPlus(bot)
    ctx = make_context(guild)
    ctx.command = SimpleNamespace(qualified_name="owoplus preview")
    text = "a long preview 😀 " * 400
    cog._render_message_mode = lambda *args, **kwargs: text
    await OwoPlus.owoplus_preview.callback(cog, ctx, text=text)
    assert ctx.send.await_count > 1
    values = [
        field.value for call in ctx.send.await_args_list for field in call.kwargs["embed"].fields
    ]
    assert "".join(values) == text
    for call in ctx.send.await_args_list:
        assert_limits(call.kwargs["embed"])
