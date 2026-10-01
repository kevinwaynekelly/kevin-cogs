"""Scalar settings must share section locks with compound feature updates."""

import asyncio

import pytest
from conftest import make_context

from audioplus import AudioPlus
from levelplus import LevelPlus
from owoplus import OwoPlus


@pytest.mark.parametrize("cls", [AudioPlus, LevelPlus, OwoPlus])
async def test_scalar_setting_waits_for_compound_writer_and_preserves_both_updates(cls, bot, guild):
    cog = cls(bot)
    ctx = make_context(guild)
    if cls is AudioPlus:
        section, key, value = cog.config.guild(guild).music, "panel", False
        update, options = cog.audioset_panel.callback, {"enabled": False}
        extra_key, extra_value = "dj_role", 789
    elif cls is LevelPlus:
        section, key, value = cog.config.guild(guild).xp_features, "min_words", 7
        update, options = cog.guard_minwords.callback, {"count": 7}
        extra_key, extra_value = (
            "boosts",
            [{"factor": 2, "role": None, "channel": None, "expires": 1e12}],
        )
    else:
        section, key, value = cog.config.guild(guild).features, "intensity", 2
        update, options = cog.owo_intensity.callback, {"value": 2}
        extra_key, extra_value = "optouts", {"123456789012345678": True}
    async with section() as data:
        data[extra_key] = extra_value
        task = asyncio.create_task(update(cog, ctx, **options))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 0.03)
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    await task
    result = await section()
    assert result[key] == value
    assert result[extra_key] == extra_value
