"""Optional shared settings controls for independently installed Kevin's Cogs."""

from .cog import SettingsHub

__red_end_user_data_statement__ = "Stores server theme preferences and up to ten bounded configuration snapshots per server. Snapshots exclude member records, message histories, credentials and runtime cursors. Requester IDs and restore previews are kept in memory for up to three minutes. Files and messages posted to Discord remain managed there."


async def setup(bot):
    await bot.add_cog(SettingsHub(bot))
