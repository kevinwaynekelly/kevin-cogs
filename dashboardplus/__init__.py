"""DashboardPlus, a private local web interface for Red."""

from .cog import DashboardPlus
from .constants import __red_end_user_data_statement__

__all__ = ["DashboardPlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(DashboardPlus(bot))
