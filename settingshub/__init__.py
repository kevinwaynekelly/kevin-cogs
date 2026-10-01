"""Optional shared settings controls for independently installed Kevin's Cogs."""

from .cog import SettingsHub

__red_end_user_data_statement__ = "Stores server theme preferences and up to ten bounded configuration snapshots per server. Snapshots exclude member records, message histories, credentials and runtime cursors. Requester IDs and restore previews are kept in memory for up to three minutes. Files and messages posted to Discord remain managed there. Configuration history starts enabled and retains actor IDs, canonical command names, timestamps and selected old/new server setting values for 30 days by default, at most 200 records/512 KiB. Oversized values are marked previews. It excludes global credentials, personal/operational data and background attribution. User-data hooks export/delete whole retained history records identifying the user. Readiness checks change no settings."


async def setup(bot):
    await bot.add_cog(SettingsHub(bot))
