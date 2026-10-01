import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from conftest import forbidden, make_channel, make_member, make_message

from owoplus import OwoPlus, haiku


@pytest.mark.parametrize("text", ["a" * 7000, "😀" * 3000, "  one\n\n two " * 600])
def test_chunks_preserve_text_and_respect_discord_limit(bot, text):
    chunks = OwoPlus(bot)._chunk_message(text)
    assert "".join(chunks) == text
    assert all(0 < len(chunk.encode("utf-16-le")) // 2 <= 2000 for chunk in chunks)


async def test_attachment_failure_keeps_original(bot, guild):
    cog = OwoPlus(bot)
    member = make_member(guild)
    channel = make_channel(guild)
    attachment = SimpleNamespace(to_file=AsyncMock(side_effect=forbidden()))
    message = make_message(member, channel, attachments=[attachment])
    cog._ensure_webhook = AsyncMock()
    assert not await cog._repost(message, "new content")
    message.delete.assert_not_awaited()
    cog._ensure_webhook.assert_not_awaited()


async def test_all_attachments_sent_before_original_is_deleted(bot, guild):
    cog = OwoPlus(bot)
    channel = make_channel(guild)
    files = [discord.File(io.BytesIO(b"file"), filename=f"{i}.txt") for i in range(10)]
    attachments = [SimpleNamespace(to_file=AsyncMock(return_value=f)) for f in files]
    message = make_message(make_member(guild), channel, attachments=attachments)
    hook = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())))
    cog._ensure_webhook = AsyncMock(return_value=hook)

    async def delete_original():
        assert hook.send.await_count == 2

    message.delete.side_effect = delete_original
    assert await cog._repost(message, "x" * 2100)
    assert hook.send.await_args_list[0].kwargs["files"] == files
    assert "files" not in hook.send.await_args_list[1].kwargs
    assert all(f.fp.closed for f in files)


@pytest.mark.parametrize("failure", ["second_chunk", "delete", "cancel"])
async def test_partial_repost_rolls_back(bot, guild, failure):
    cog = OwoPlus(bot)
    message = make_message(make_member(guild), make_channel(guild))
    first = SimpleNamespace(delete=AsyncMock())
    second = SimpleNamespace(delete=AsyncMock())
    send = AsyncMock(return_value=first)
    if failure == "delete":
        send.side_effect = [first, second]
        message.delete.side_effect = forbidden()
    else:
        send.side_effect = [first, asyncio.CancelledError() if failure == "cancel" else forbidden()]
    cog._ensure_webhook = AsyncMock(return_value=SimpleNamespace(send=send))
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await cog._repost(message, "x" * 2100)
    else:
        assert not await cog._repost(message, "x" * 2100)
    first.delete.assert_awaited_once()
    if failure == "delete":
        second.delete.assert_awaited_once()
    else:
        message.delete.assert_not_awaited()


async def test_webhook_uses_only_owned_hook_and_concurrent_calls_create_once(bot, guild):
    cog = OwoPlus(bot)
    channel = make_channel(guild)
    unrelated = SimpleNamespace(name="Other", user=bot.user, token="token")
    channel.webhooks.return_value = [unrelated]
    owned = SimpleNamespace(name="OwoPlus", user=bot.user, token="token")
    channel.create_webhook.return_value = owned
    results = await asyncio.gather(*(cog._ensure_webhook(channel) for _ in range(20)))
    assert all(hook is owned for hook in results)
    channel.create_webhook.assert_awaited_once()
    assert len(cog._webhook_locks) == 0


async def test_forum_thread_uses_parent_webhook(bot, guild):
    cog = OwoPlus(bot)
    forum = make_channel(guild, kind=discord.ForumChannel)
    thread = make_channel(guild, forum.id + 1, discord.Thread)
    thread.parent, thread.parent_id = forum, forum.id
    hook = SimpleNamespace(name="OwoPlus", user=bot.user, token="token")
    forum.webhooks.return_value = [hook]
    assert await cog._ensure_webhook(thread) is hook
    forum.create_webhook.assert_not_awaited()


async def test_renderer_preserves_code_and_handles_haiku_consistently(bot):
    cog = OwoPlus(bot)
    raw = "hello `return real_value`\n```python\nprint('hello')\n```"
    rendered = await cog._render_async(raw, "full", False)
    assert "`return real_value`" in rendered
    assert "```python\nprint('hello')\n```" in rendered
    poem = "the sun is so bright\nthe sky is so blue and clear\nwe go home at night"
    assert haiku.Haiku.detect_breaks(poem) is not None
    assert await cog._render_async(poem, "none", True) == cog._render_message_mode(
        poem, "none", use_haiku=True
    )


def test_optional_backend_failure_falls_back(monkeypatch):
    monkeypatch.setattr(haiku, "G2p", Mock(side_effect=RuntimeError("missing model")))
    engine = haiku._SyllableEngine()
    assert engine.count("beautiful") == 3


async def test_user_overrides_apply_and_delete_exports(bot, guild):
    cog = OwoPlus(bot)
    member = make_member(guild)
    await cog.config.guild(guild).user_probs.set({str(member.id): 1})
    settings = await cog._settings(guild)
    assert cog._choose_mode(member, "hello", settings) == "full"
    assert "owoplus.json" in await cog.red_get_data_for_user(user_id=member.id)
    await cog.red_delete_data_for_user(requester="user", user_id=member.id)
    assert await cog.red_get_data_for_user(user_id=member.id) == {}
    assert not (await cog._settings(guild))["user_probs"]


async def test_embeds_and_missing_delete_permission_leave_original(bot, guild):
    cog = OwoPlus(bot)
    channel = make_channel(guild)
    message = make_message(make_member(guild), channel)
    message.embeds = [discord.Embed(title="Keep this")]
    assert not await cog._repost(message, "output")
    message.embeds = []
    channel.permissions_for.return_value = discord.Permissions.none()
    assert not await cog._repost(message, "output")
    message.delete.assert_not_awaited()
