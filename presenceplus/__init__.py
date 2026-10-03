"""PresencePlus, independently installable through Red Downloader."""

from .cog import PresencePlus
from .constants import __red_end_user_data_statement__

__all__ = ["PresencePlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(PresencePlus(bot))
