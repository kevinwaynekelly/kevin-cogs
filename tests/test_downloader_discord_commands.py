"""Discord webhook administration uses real Red permissions and private delivery."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from redbot.core import commands
from test_cog_hybrid import invoke_slash
from test_management_plus import core_runtime, download_runtime, red_command_runtime, sends

from downloaderplus import DownloaderPlus

__all__ = ["core_runtime", "download_runtime", "red_command_runtime"]
HOOK_ID = 117000000000000001
TOKEN = "discord-private-token"


@pytest.fixture
async def discord_controls(download_runtime, monkeypatch):
    bot, cog, source, repo, installed, member, invoke = download_runtime
    ctx = await invoke("!help")
    bot._connection._intents.message_content = True
    bot._connection._intents.guild_messages = True
    hook = SimpleNamespace(
        id=HOOK_ID,
        type=discord.WebhookType.incoming,
        guild_id=member.guild.id,
        channel_id=ctx.channel.id,
        token=TOKEN,
        url=f"https://discord.com/api/webhooks/{HOOK_ID}/{TOKEN}",
        user=bot.user,
        delete=AsyncMock(),
    )
    ctx.channel.create_webhook.return_value = hook
    ctx.channel.webhooks.return_value = [hook]
    monkeypatch.setattr(bot, "fetch_webhook", AsyncMock(return_value=hook))
    monkeypatch.setattr(
        bot, "get_channel", lambda cid: ctx.channel if cid == ctx.channel.id else None
    )
    monkeypatch.setattr(source, "_cog_update_logic", AsyncMock())
    yield SimpleNamespace(
        bot=bot, cog=cog, source=source, member=member, invoke=invoke, ctx=ctx, hook=hook
    )


@pytest.mark.parametrize("slash", [False, True])
async def test_discord_enable_existing_webhook_owner_only_and_disable(
    discord_controls, monkeypatch, slash
):
    r = discord_controls

    async def enable():
        return (
            await invoke_slash(
                r.bot, r.invoke, monkeypatch, "download discord enable", webhook_id=str(HOOK_ID)
            )
            if slash
            else await r.invoke(f"!download discord enable {HOOK_ID}")
        )

    assert (await enable()).command_failed
    r.bot.fetch_webhook.assert_not_awaited()
    r.bot.owner_ids.add(r.member.id)
    ctx = await enable()
    assert not ctx.command_failed, r.bot.on_command_error.call_args
    r.bot.fetch_webhook.assert_awaited_once_with(HOOK_ID)
    policy = await r.cog.config.discord_trigger()
    assert policy["enabled"] and policy["webhook_id"] == HOOK_ID
    assert policy["owner_id"] == r.member.id and policy["channel_id"] == ctx.channel.id
    assert r.cog._discord_trigger.worker is not None
    assert TOKEN not in json.dumps(await r.cog.config.all())
    assert TOKEN not in str(sends(ctx).call_args)
    r.member.send.assert_not_awaited()
    data = await r.cog.red_get_data_for_user(user_id=r.member.id)
    exported = json.loads(data["downloaderplus.json"].getvalue())
    assert exported["discord_trigger"]["owner_id"] == r.member.id
    assert TOKEN not in str(exported)
    assert "generation" not in exported["discord_trigger"]
    assert not (await r.invoke("!download discord disable")).command_failed
    after = await r.cog.config.discord_trigger()
    assert not after["enabled"] and not after["pending"]
    assert after["generation"] != policy["generation"]
    assert after["webhook_id"] == HOOK_ID
    assert r.cog._discord_trigger.worker is None
    r.hook.delete.assert_not_awaited()


@pytest.mark.parametrize(
    "change,value",
    [("type", discord.WebhookType.channel_follower), ("guild_id", 555), ("channel_id", 777)],
)
async def test_discord_rejects_wrong_webhook_type_guild_or_channel(discord_controls, change, value):
    r = discord_controls
    r.bot.owner_ids.add(r.member.id)
    setattr(r.hook, change, value)
    before = await r.cog.config.discord_trigger()
    ctx = await r.invoke(f"!download discord enable {HOOK_ID}")
    assert ctx.command_failed
    assert await r.cog.config.discord_trigger() == before
    assert r.cog._discord_trigger.worker is None


@pytest.mark.parametrize("identifier", ["garbage", "123.45", "-1"])
async def test_discord_rejects_non_snowflake_input_before_network(discord_controls, identifier):
    r = discord_controls
    r.bot.owner_ids.add(r.member.id)
    ctx = await r.invoke(f"!download discord enable {identifier}")
    assert ctx.command_failed
    r.bot.fetch_webhook.assert_not_awaited()


@pytest.mark.parametrize(
    "missing",
    ["view_channel", "send_messages", "manage_webhooks", "message_content", "guild_messages"],
)
async def test_discord_readiness_checks_prevent_partial_setup(discord_controls, missing):
    r = discord_controls
    r.bot.owner_ids.add(r.member.id)
    before = await r.cog.config.discord_trigger()
    if missing in {"message_content", "guild_messages"}:
        setattr(r.bot._connection._intents, missing, False)
    else:
        permissions = discord.Permissions.all()
        setattr(permissions, missing, False)
        r.ctx.channel.permissions_for.side_effect = None
        r.ctx.channel.permissions_for.return_value = permissions
    ctx = await r.invoke(f"!download discord enable {HOOK_ID}")
    assert ctx.command_failed
    r.ctx.channel.create_webhook.assert_not_awaited()
    assert await r.cog.config.discord_trigger() == before


async def test_discord_update_respects_native_group_disabling_and_owner_revocation(
    discord_controls,
):
    r = discord_controls
    r.bot.owner_ids.add(r.member.id)
    assert not (await r.invoke(f"!download discord enable {HOOK_ID}")).command_failed
    policy = await r.cog.config.discord_trigger()
    for command in (r.bot.get_command("cog update"), r.cog.discord_trigger):
        command.disable_in(r.member.guild)
        with pytest.raises(commands.DisabledCommand):
            await r.cog._discord_update(policy)
        command.enable_in(r.member.guild)
    r.bot.owner_ids.remove(r.member.id)
    with pytest.raises(commands.CheckFailure):
        await r.cog._discord_update(policy)
    r.source._cog_update_logic.assert_not_awaited()


async def test_discord_automated_update_syncs_and_uses_configured_owner(discord_controls):
    r = discord_controls
    r.bot.owner_ids.add(r.member.id)
    assert not (await r.invoke(f"!download discord enable {HOOK_ID}")).command_failed
    policy = await r.cog.config.discord_trigger()
    status, detail, finish = await r.cog._discord_update(policy)
    assert status == "complete"
    r.source._cog_update_logic.assert_awaited_once()
    context = r.source._cog_update_logic.call_args.args[0]
    assert context.author.id == r.member.id
    assert context.assume_yes
    assert r.source._cog_update_logic.call_args.kwargs["cogs"] == ()
    r.bot.tree.sync.assert_not_awaited()
    await finish()
    r.bot.tree.sync.assert_awaited_once()


async def test_discord_finish_uses_reloaded_cog_and_current_permissions(discord_controls):
    r = discord_controls
    r.bot.owner_ids.add(r.member.id)
    policy = {"owner_id": r.member.id, "channel_id": r.ctx.channel.id}
    status, detail, finish = await r.cog._discord_update(policy)
    await r.bot.remove_cog("DownloaderPlus")
    replacement = DownloaderPlus(r.bot)
    await r.bot.add_cog(replacement)
    replacement.discord_trigger.disable_in(r.member.guild)
    with pytest.raises(commands.DisabledCommand):
        await finish()
    r.bot.tree.sync.assert_not_awaited()


async def test_discord_privacy_deletion_preserves_another_owner_and_cancels_configuration(
    discord_controls,
):
    r = discord_controls
    r.bot.owner_ids.add(r.member.id)
    defaults = deepcopy(await r.cog.config.discord_trigger())
    assert not (await r.invoke(f"!download discord enable {HOOK_ID}")).command_failed
    before = await r.cog.config.discord_trigger()
    await r.cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=r.member.id + 1)
    assert await r.cog.config.discord_trigger() == before
    assert r.cog._discord_trigger.worker is not None
    await r.cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=r.member.id)
    assert await r.cog.config.discord_trigger() == defaults
    assert r.cog._discord_trigger.worker is None

    entered = asyncio.Event()

    async def blocked_fetch(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    r.bot.fetch_webhook.side_effect = blocked_fetch
    task = asyncio.create_task(r.cog.discord_enable.callback(r.cog, r.ctx, str(HOOK_ID)))
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(
        r.cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=r.member.id), 2
    )
    assert task.cancelled()
    r.hook.delete.assert_not_awaited()
    assert await r.cog.config.discord_trigger() == defaults
    assert not r.cog._privacy.operations and not r.cog._privacy.deleting
