"""Build an SVG theme preview from actual cog payloads, using mocked network objects."""

from __future__ import annotations

import asyncio
import random
import re
import tempfile
import textwrap
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from redbot.core import Config
from redbot.core._drivers.json import JsonDriver

from audioplus import AudioPlus
from audioplus.player import GuildPlayer
from audioplus.resolver import Track
from communityplus import CommunityPlus
from levelplus import LevelPlus
from logplus import LogPlus
from owoplus import OwoPlus


async def samples(directory):
    def get_conf(cog, identifier, force_registration=False):
        name = type(cog).__name__
        driver = JsonDriver(name, str(identifier), data_path_override=directory / name)
        return Config(name, str(identifier), driver, force_registration)

    guild = Mock(spec=discord.Guild)
    guild.id, guild.name, guild.member_count = 123, "The Lounge", 42
    guild.me = SimpleNamespace(guild_permissions=discord.Permissions.all())
    guild.get_role.return_value = None
    channel = Mock(spec=discord.TextChannel)
    channel.id, channel.mention = 456, "#lounge"
    channel.permissions_for.return_value = discord.Permissions.all()
    guild.get_channel.return_value = channel
    member = SimpleNamespace(
        id=1234,
        mention="@Kevin",
        display_name="Kevin",
        display_avatar=SimpleNamespace(url="https://example.invalid/avatar.png"),
    )
    bot = SimpleNamespace(is_owner=AsyncMock(return_value=False))
    ctx = SimpleNamespace(
        guild=guild,
        channel=channel,
        author=member,
        clean_prefix="!",
        command=None,
        send=AsyncMock(),
    )
    cards = []

    async def capture(coroutine):
        ctx.send.reset_mock()
        await coroutine
        cards.extend(call.kwargs["embed"] for call in ctx.send.await_args_list)

    with patch.object(Config, "get_conf", staticmethod(get_conf)):
        audio = AudioPlus(bot)
        voice = Mock(spec=discord.VoiceClient)
        voice.guild = guild
        voice.is_paused.return_value = False
        voice.is_playing.return_value = True
        player = GuildPlayer(voice, audio._resolver, AsyncMock())
        player.volume = 70
        player.source = SimpleNamespace(position=98000)
        player.current = Track(
            "https://example.invalid/track", "Midnight City", "M83", 242000, "youtube"
        )
        guild.voice_client = voice
        audio._players[guild.id] = player
        await capture(AudioPlus.audio_nowplaying.callback(audio, ctx))

        level = LevelPlus(bot)
        await level.config.guild(guild).xp.set({str(member.id): 150})
        await capture(LevelPlus.show.callback(level, ctx))

        community = CommunityPlus(bot)
        await community.config.guild(guild).welcome.channel_id.set(channel.id)
        await capture(CommunityPlus.com.callback(community, ctx))

        logging = LogPlus(bot)
        await logging.config.guild(guild).style.compact.set(False)
        event = await logging._E(
            guild,
            "Message deleted",
            "A message was removed from #lounge.",
            etype="message_deleted",
            footer="Message ID 987654321",
        )
        event.add_field(name="Author", value="@Kevin")
        event.add_field(name="Channel", value="#lounge")
        await capture(logging._reply(ctx, embed=event))

        owo = OwoPlus(bot)
        await owo.config.guild(guild).one_in.set(1)
        random.seed(42)
        await capture(
            OwoPlus.owoplus_preview.callback(owo, ctx, text="Hello friend, welcome to the server!")
        )
        ctx.command = SimpleNamespace(qualified_name="community welcome channel")
        await capture(community._presentation.confirm(ctx))
    return cards


def plain(text):
    return re.sub(r"[*`\\]", "", re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or ""))


def render(cards):
    """Approximate Discord's embed layout, keeping actual text, colors, and sections."""
    width, margin, gap, card_width = 1100, 36, 24, 502
    columns = [145, 145]
    elements = []

    def text(x, y, value, size=15, color="#DBDEE1", weight="400"):
        return f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}">{escape(value)}</text>'

    for index, embed in enumerate(cards):
        column = index % 2
        x, top = margin + column * (card_width + gap), columns[column]
        cursor = top + 35
        content = [text(x + 22, cursor, embed.title, 19, "#F2F3F5", "700")]
        cursor += 28
        for line in (embed.description or "").splitlines():
            for wrapped in textwrap.wrap(plain(line), width=51) or [""]:
                content.append(text(x + 22, cursor, wrapped))
                cursor += 23
        if embed.description:
            cursor += 8
        row = []

        def fields(values, start):
            bottom = start
            for cell, field in enumerate(values):
                left = x + 22 + cell * ((card_width - 44) // len(values))
                y = start
                content.append(text(left, y, plain(field.name), 15, "#F2F3F5", "700"))
                y += 23
                for line in field.value.splitlines():
                    for wrapped in textwrap.wrap(plain(line), width=51 // len(values)) or [""]:
                        content.append(text(left, y, wrapped))
                        y += 22
                bottom = max(bottom, y)
            return bottom + 12

        for field in embed.fields:
            if field.inline:
                row.append(field)
                if len(row) == 2:
                    cursor = fields(row, cursor)
                    row = []
            else:
                if row:
                    cursor = fields(row, cursor)
                    row = []
                cursor = fields([field], cursor)
        if row:
            cursor = fields(row, cursor)
        for line in textwrap.wrap(embed.footer.text, width=61):
            content.append(text(x + 22, cursor + 3, line, 12, "#B5BAC1"))
            cursor += 18
        height = cursor - top + 19
        elements.append(
            f'<rect x="{x}" y="{top}" width="{card_width}" height="{height}" rx="8" fill="#2B2D31"/>'
        )
        elements.append(
            f'<path d="M{x + 4} {top + 8} V{top + height - 8}" stroke="#{embed.color.value:06X}" stroke-width="5" stroke-linecap="round"/>'
        )
        elements.extend(content)
        columns[column] = top + height + gap
    height = max(columns) + 38
    header = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<title>Kevin’s Cogs: unified Discord theme</title>",
        f'<rect width="{width}" height="{height}" fill="#1E1F22"/>',
        '<g font-family="DejaVu Sans, sans-serif">',
        text(margin, 53, "KEVIN'S COGS", 28, "#F2F3F5", "700"),
        text(
            margin,
            87,
            "One theme across music, community, levels, logs, and transformations.",
            16,
            "#B5BAC1",
        ),
        text(
            margin,
            112,
            "Sample data from actual payloads. Discord controls final rendering.",
            12,
            "#B5BAC1",
        ),
    ]
    return "\n".join([*header, *elements, "</g></svg>"]) + "\n"


def main():
    with tempfile.TemporaryDirectory() as directory:
        cards = asyncio.run(samples(Path(directory)))
    path = Path(__file__).resolve().parents[1] / "docs" / "presentation-preview.svg"
    path.parent.mkdir(exist_ok=True)
    path.write_text(render(cards))
    print(path)


if __name__ == "__main__":
    main()
