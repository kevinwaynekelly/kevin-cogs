"""LevelPlus package entry point."""

from .cog import LevelPlus
from .constants import __red_end_user_data_statement__
from .levels import level_from_xp, level_thresholds

__all__ = ["LevelPlus", "__red_end_user_data_statement__", "level_from_xp", "level_thresholds"]


async def setup(bot):
    await bot.add_cog(LevelPlus(bot))
