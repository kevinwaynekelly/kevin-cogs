"""Use real Red Config storage and command classes; mock Discord's network boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from redbot.core import Config
from redbot.core._drivers.json import JsonDriver


def make_guild(guild_id=123):
    guild = Mock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = f"Guild {guild_id}"
    guild.members = []
    guild.channels = []
    guild.voice_channels = []
    guild.stage_channels = []
    guild.afk_channel = None
    guild.system_channel = None
    guild.voice_client = None
    guild.emojis = []
    guild.emoji_limit = 50
    guild.me = SimpleNamespace(
        id=999,
        guild_permissions=discord.Permissions.all(),
        voice=None,
    )
    guild.get_member.side_effect = lambda uid: next((m for m in guild.members if m.id == uid), None)
    guild.get_channel.side_effect = lambda cid: next(
        (c for c in guild.channels if c.id == cid), None
    )
    guild.get_channel_or_thread.side_effect = guild.get_channel.side_effect
    guild.change_voice_state = AsyncMock()
    return guild


def make_channel(guild, channel_id=456, kind=discord.TextChannel):
    channel = Mock(spec=kind)
    channel.id = channel_id
    channel.guild = guild
    channel.name = f"channel-{channel_id}"
    channel.mention = f"<#{channel_id}>"
    channel.members = []
    if kind is discord.VoiceChannel:
        channel.user_limit = 0
    channel.parent_id = None
    channel.parent = None
    channel.permissions_for.return_value = discord.Permissions.all()
    channel.send = AsyncMock()
    channel.fetch_message = AsyncMock()
    channel.webhooks = AsyncMock(return_value=[])
    channel.create_webhook = AsyncMock()
    guild.channels.append(channel)
    if kind is discord.VoiceChannel:
        guild.voice_channels.append(channel)
    elif kind is discord.StageChannel:
        guild.stage_channels.append(channel)
    return channel


def make_member(guild, user_id=123456789012345678, *, bot=False, name="Kevin"):
    member = Mock(spec=discord.Member)
    member.id = user_id
    member.guild = guild
    member.bot = bot
    member.display_name = name
    member.name = name
    member.global_name = None
    member.discriminator = "0"
    member.mention = f"<@{user_id}>"
    member.roles = []
    member.voice = None
    member.status = discord.Status.online
    member.desktop_status = member.mobile_status = member.web_status = discord.Status.online
    member.activities = []
    member.display_avatar = SimpleNamespace(url="https://example.invalid/avatar.png")
    member.move_to = AsyncMock()
    member.send = AsyncMock()
    member.add_roles = AsyncMock()
    guild.members.append(member)
    return member


def make_message(member, channel, *, content="hello world", attachments=None):
    message = Mock(spec=discord.Message)
    message.id = 987654321012345678
    message.created_at = discord.utils.utcnow()
    message.edited_at = None
    message.guild = member.guild
    message.author = member
    message.channel = channel
    message.content = content
    message.webhook_id = None
    message.embeds = []
    message.attachments = attachments or []
    message.delete = AsyncMock()
    return message


def make_context(guild, channel=None, author=None):
    return SimpleNamespace(
        guild=guild,
        channel=channel,
        author=author,
        clean_prefix="!",
        message=SimpleNamespace(attachments=[]),
        send=AsyncMock(),
        tick=AsyncMock(),
        send_help=AsyncMock(),
    )


def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Denied")


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    def get_conf(cog, identifier, force_registration=False):
        name = type(cog).__name__
        driver = JsonDriver(name, str(identifier), data_path_override=tmp_path / name)
        return Config(name, f"{identifier}:{tmp_path}", driver, force_registration)

    monkeypatch.setattr(Config, "get_conf", staticmethod(get_conf))


@pytest.fixture
def guild():
    return make_guild()


@pytest.fixture
def bot(guild):
    bot = SimpleNamespace(
        guilds=[guild],
        users=[],
        cached_messages=[],
        user=SimpleNamespace(id=999),
        is_owner=AsyncMock(return_value=False),
        cog_disabled_in_guild=AsyncMock(return_value=False),
        get_valid_prefixes=AsyncMock(return_value=["!"]),
        wait_until_red_ready=AsyncMock(),
    )
    bot.get_guild = lambda gid: next((g for g in bot.guilds if g.id == gid), None)
    return bot
