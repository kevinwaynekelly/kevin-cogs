"""DownloaderPlus, independently installable beside Red Downloader."""

from .cog import DownloaderPlus
from .constants import __red_end_user_data_statement__

__all__ = ["DownloaderPlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(DownloaderPlus(bot))
