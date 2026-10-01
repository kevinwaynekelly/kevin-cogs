# CommunityPlus

Community tools for Red, including first-time autoroles, role restoration after rejoining, welcome and goodbye embeds, solo voice disconnection, and member activity statistics.

See the [repository README](../README.md) for installation. Load with `[p]load communityplus`. Replace `[p]` with your bot's prefix.


Commands, confirmations, welcome/goodbye notices, and voice-timeout DMs use the [shared visual theme](../docs/PRESENTATION.md). Custom templates keep their text. Long results are paginated, and output falls back to text when embeds are unavailable.

## Setup

All `com` commands run in a server and require a Red admin or **Manage Server** permission. `restore` and `invites` additionally require the bot owner.

The cog starts with solo voice disconnection enabled. To disable it while configuring the other features:

```text
[p]com vcsolo disable
[p]com autorole set @Members
[p]com welcome channel #welcome
[p]com cya channel #goodbye
[p]com welcome preview
[p]com diag
```

Give the bot **View Channel**, **Send Messages**, and **Embed Links** in its command and announcement channels. CSV export needs **Attach Files**. Autoroles and sticky roles need **Manage Roles**, with the bot's highest role above every role it should assign. Solo voice disconnection needs **Move Members**.

Enable the **Server Members** intent for join/leave and role features, and the **Presence** intent for presence and game/activity statistics. Message and voice events must also be available to the bot. `[p]com diag` reports member/presence intents and role/voice permissions.

## Defaults

| Feature | Default behavior |
| --- | --- |
| Autorole | Enabled, with no role selected. Applied when a joining member has not previously been marked as seen. |
| Sticky roles | Enabled, with no ignored roles. Saves eligible role IDs when a member leaves and attempts to restore them on rejoin. |
| Welcome / goodbye | Enabled, with no channel selected, so no announcements are sent until configured. |
| Solo voice | Enabled. Disconnects the only human in a voice or stage channel after 900 seconds and attempts to DM them. Bots do not count as companions. |
| Seen and statistics | Enabled. Collects timestamps, message/voice counters, presence changes, and activity starts. |
| Compact embeds | Enabled. |

Managed/integration roles and `@everyone` are excluded from sticky restoration. Roles at or above the bot's highest role cannot be restored. Solo timers are rebuilt when the cog loads, keyed by server and member, and cancelled on unload or when disabled. Mute/deafen changes do not restart an existing solo deadline. Timers recheck the channel, companions, and enabled state before disconnecting.

## Commands

Square brackets indicate optional arguments. `enable` and `disable` are separate subcommands.

| Command | Purpose |
| --- | --- |
| `[p]com` | Show current settings. |
| `[p]com help` | Show the cog's command overview. Aliases: `commands`, `?`. |
| `[p]com diag` | Check relevant intents and permissions. |
| `[p]com autorole set @Role` | Select the first-time join role. |
| `[p]com autorole clear` | Clear the selected role. |
| `[p]com autorole enable` / `disable` / `show` | Enable, disable, or inspect autorole. |
| `[p]com sticky enable` / `disable` | Control role restoration. |
| `[p]com sticky ignore add @Role` / `remove @Role` / `list` | Manage roles excluded from restoration. |
| `[p]com sticky purge @Member` | Clear that member's saved sticky roles only. |
| `[p]com welcome enable` / `disable` | Control welcome announcements. |
| `[p]com welcome channel [#channel]` | Set the welcome channel, or omit it to clear the target. |
| `[p]com welcome message <text>` | Set the welcome template. |
| `[p]com welcome preview [@Member]` | Preview a welcome in the command channel. |
| `[p]com cya enable` / `disable` | Control goodbye announcements. |
| `[p]com cya channel [#channel]` | Set the goodbye channel, or omit it to clear the target. |
| `[p]com cya message <text>` | Set the goodbye template. |
| `[p]com cya preview [@Member]` | Preview a goodbye in the command channel. |
| `[p]com vcsolo enable` / `disable` | Control solo voice disconnection. |
| `[p]com vcsolo idle <seconds>` | Set the timeout, with a minimum of 60 seconds. |
| `[p]com seen [@Member]` | Show last-seen and presence information. |
| `[p]com seendetail [@Member]` | Show event timestamps, channels, and platform statuses. |
| `[p]com stats [@Member]` | Show counters and the top five recorded games. |
| `[p]com seenlist [limit]` | List recently seen current members. Default 25, range 1 to 100. |
| `[p]com seenlistcsv` | Export current members' last-seen and presence summary as CSV. |
| `[p]com embeds [true\|false]` | Inspect or set compact embeds. |

Welcome and goodbye templates support `{user}`, `{mention}`, `{server}`, `{count}`, `{created_at}`, and `{joined_at}`. An invalid template is sent unchanged.

### Bot owner commands

| Command | Behavior |
| --- | --- |
| `[p]com restore` | Creates or reuses a role named `Restored Admin` and assigns it to the invoking bot owner. A newly created role has **Administrator** permission. Discord must allow the bot to create and assign that role. |
| `[p]com invites` | DMs the bot owner an invite report for every server the bot belongs to. Creates one-use invites that expire after 24 hours where the bot has **Create Invite** permission. |

## Stored data and current limits

Red Config stores server settings and IDs for channels/roles, message templates, and per-member data keyed by server and user IDs. Member data includes the first-seen flag, sticky role IDs, event timestamps and channel IDs, presence/platform statuses, last-online/offline times, counters, and game names with launch counts. Ordinary message contents are not saved.

`seenlistcsv` exports a summary for current members, not every stored record. `sticky purge` clears only saved roles. Red's user-data export hook returns all stored records for that user across servers. Its deletion hook clears those records and cancels pending timers for the user. There is no command to toggle seen tracking or solo-disconnect DMs; their existing Config settings are respected. Statistics reflect events observed while the cog is running, not a historical Discord backfill. Presence duration tracks actual status changes rather than every activity update. Seen lists paginate and exports read member records in one batch.
