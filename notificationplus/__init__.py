"""NotificationPlus, independently installable through Red Downloader."""

from .cog import NotificationPlus
from .constants import __red_end_user_data_statement__

__all__ = ["NotificationPlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(NotificationPlus(bot))
