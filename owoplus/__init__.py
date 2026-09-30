"""OwoPlus package entry point."""

from .cog import OwoPlus
from .constants import __red_end_user_data_statement__

__all__ = ["OwoPlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(OwoPlus(bot))
