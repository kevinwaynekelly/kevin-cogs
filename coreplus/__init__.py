"""CorePlus, an independently installable presentation layer for Red Core."""

from .cog import CorePlus
from .constants import __red_end_user_data_statement__

__all__ = ["CorePlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(CorePlus(bot))
