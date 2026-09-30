"""LogPlus package entry point."""

from .cog import LogPlus
from .constants import __red_end_user_data_statement__

__all__ = ["LogPlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(LogPlus(bot))
