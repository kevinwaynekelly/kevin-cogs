"""CommunityPlus package entry point."""

from .cog import CommunityPlus
from .constants import __red_end_user_data_statement__

__all__ = ["CommunityPlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(CommunityPlus(bot))
