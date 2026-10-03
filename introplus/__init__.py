"""IntroPlus, independently installable through Red Downloader."""

from .cog import IntroPlus
from .constants import __red_end_user_data_statement__

__all__ = ["IntroPlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(IntroPlus(bot))
