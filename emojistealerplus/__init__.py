"""EmojiStealerPlus, independently installable through Red Downloader."""

from .cog import EmojiStealerPlus
from .constants import __red_end_user_data_statement__

__all__ = ["EmojiStealerPlus", "__red_end_user_data_statement__"]


async def setup(bot):
    await bot.add_cog(EmojiStealerPlus(bot))
