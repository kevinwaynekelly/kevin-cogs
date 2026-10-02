"""BackupPlus, independently installable through Red Downloader."""

from .cog import BackupPlus
from .constants import __red_end_user_data_statement__

__all__ = ["BackupPlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(BackupPlus(bot))
