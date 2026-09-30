"""AudioPlus package entry point."""

from .cog import AudioPlus
from .constants import __red_end_user_data_statement__

__all__ = ["AudioPlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(AudioPlus(bot))
