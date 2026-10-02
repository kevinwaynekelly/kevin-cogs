"""ExportPlus, independently installable through Red Downloader."""

from .cog import ExportPlus
from .constants import __red_end_user_data_statement__

__all__ = ["ExportPlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(ExportPlus(bot))
