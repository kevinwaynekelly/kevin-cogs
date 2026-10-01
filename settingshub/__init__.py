"""Optional shared settings controls for independently installed Kevin's Cogs."""

from .cog import SettingsHub

__red_end_user_data_statement__ = (
    "This cog does not persist records. Requester/server IDs and restore previews are held "
    "in memory for up to three minutes. Backups export selected server settings from loaded "
    "cogs, excluding member records, message histories, credentials, and runtime cursors. "
    "Files and dashboard messages posted to Discord remain managed there."
)


async def setup(bot):
    await bot.add_cog(SettingsHub(bot))
