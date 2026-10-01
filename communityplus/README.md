# CommunityPlus

Community tools for Red, including first-time autoroles, role restoration after rejoining, welcome and goodbye embeds, solo voice disconnection, and member activity statistics.

See the [repository README](../README.md) for installation. Load with `[p]load communityplus`. Replace `[p]` with your bot's prefix.


Commands, confirmations, welcome/goodbye notices, and voice-timeout DMs use the [shared visual theme](../docs/PRESENTATION.md). Custom templates keep their text. Long results are paginated, and output falls back to text when embeds are unavailable.

## Setup

All `community` commands and direct activity shortcuts run in a server and require a Red admin or **Manage Server** permission. The direct `roles` command and posted role pickers are available to members. `restore` and `invites` additionally require the bot owner.

The cog starts with solo voice disconnection enabled. To disable it while configuring the other features:

```text
[p]community vcsolo disable
[p]community autorole set @Members
[p]community welcome channel #welcome
[p]community cya channel #goodbye
[p]community welcome preview
[p]community diag
```

Give the bot **View Channel**, **Send Messages**, and **Embed Links** in its command and announcement channels. CSV export needs **Attach Files**. Autoroles and sticky roles need **Manage Roles**, with the bot's highest role above every role it should assign. Solo voice disconnection needs **Move Members**.

Enable the **Server Members** intent for join/leave and role features, and the **Presence** intent for presence and game/activity statistics. Message and voice events must also be available to the bot. `[p]community diag` reports member/presence intents and role/voice permissions.

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

## Direct and slash commands

| Direct text command | Slash command | Purpose |
| --- | --- | --- |
| `[p]seen [@Member]` | `/seen` | Last-seen and presence information. |
| `[p]seendetail [@Member]` | `/seendetail` | Detailed event times, channels, and presence. |
| `[p]activity [@Member]` | `/activity` | Counters and top games. Text alias: `stats`. |
| `[p]seenlist [limit]` | `/seenlist` | Recently seen members, default 25 and maximum 100. |

These shortcuts retain the permissions and disabled state of their grouped versions. The renamed root is `community`; the old `com` name is removed. Installation and reload still use `communityplus`.

Enable the 51 slash actions once as the bot owner:

```text
[p]slash enablecog communityplus
[p]slash sync
```

Use `/community status` for settings. Autorole, sticky-role enable/disable/purge, welcome/goodbye, solo voice, diagnostics, activity, CSV export, and embed settings have slash versions. For example, `/community autorole set` accepts a role, `/community welcome preview` accepts an optional member, and `/community vcsolo idle` accepts seconds. Slash requests are acknowledged before settings I/O.

`community sticky ignore ...` remains text-only because its nesting exceeds Discord's slash limit. Owner recovery and invite commands also remain text-only. Slash groups have no bare-group action; use the offered subcommands. After updates, reload `communityplus` and run `slash sync` again. Custom Red rules referencing `com ...` need to be reapplied under `community ...`.

## Role menus, voice time, and weekly summaries

These additions have slash equivalents and use the same theme. `roles` is a member command; other commands below require administrator access.

| Command | Purpose |
| --- | --- |
| `[p]roles` | Open a personal self-service role picker. |
| `[p]voicehours [@Member]` | Show recorded lifetime voice hours. |
| `[p]community rolemenu add @Role` / `remove @Role` / `list` | Offer up to 25 safe self-service roles. Removing an offer leaves assignments alone. |
| `[p]community rolemenu post #channel` | Post a persistent picker, with at most ten saved menus per server. |
| `[p]community rolemenu unpost <message_id>` | Forget a posted menu and attempt to disable its controls. |
| `[p]community tracking <enabled>` | Start or stop activity collection without deleting previous records. |
| `[p]community vcsolo exemptchannel <channel> [enabled=True]` | Exempt a voice or Stage channel, or remove the exemption with `False`. |
| `[p]community vcsolo exemptrole @Role [enabled=True]` | Exempt members with that role. |
| `[p]community vcsolo warning <seconds>` | Send an optional warning before timeout. `0` disables it, maximum `3600`. |
| `[p]community vcsolo notify <enabled>` | Control warning and timeout DMs. |
| `[p]community summary show` | Show the latest seven calendar days of participation, including today. |
| `[p]community summary channel [#channel]` | Set or clear the weekly digest channel. |
| `[p]community summary enable <enabled>` | Enable or disable scheduled digests, disabled by default. |
| `[p]community summary schedule [weekday=0] [hour=9] [zone=America/Chicago]` | Choose Monday=0 through Sunday=6, local hour 0..23, and an installed IANA timezone. |
| `[p]community setup` | Open channel/role pickers and tracking/solo-cleanup toggles. |

Self-service roles must be unmanaged, below the bot, and free of server management or moderation permissions. Each click checks the actual member, current Red command permissions, disabled state, and the current safe-role list. Selection only changes configured safe roles. Posted menus resume after reload, and configuration changes refresh their options when Discord allows editing. A role that becomes privileged is rejected even by an old menu. Personal pickers and setup panels expire after three minutes; posted pickers remain registered until removed or the cog unloads.

Voice time counts connected humans, including muted members and Stage listeners. Active sessions checkpoint every minute and on moves, leaves, reports, and clean unload. A crash can lose the interval since the last checkpoint, and time while Red is offline or tracking is disabled is not reconstructed. Daily message/voice totals retain at most 35 dated buckets per member, with lifetime voice seconds stored separately. Local midnight and daylight-saving boundaries use the digest timezone. Changing that timezone affects future buckets and does not rewrite previous dates. Turning tracking off leaves welcome, roles, and solo cleanup active.

Solo exemptions are checked again at the warning and timeout. Warnings are capped below the idle duration and do not extend its deadline. Changing solo settings rebuilds the timers; mute, deafen, and camera changes keep the current deadline. DMs need the member's DM permissions and can fail without preventing cleanup.

Weekly digests include the last seven completed calendar days, total messages/voice hours, active members, and separate top-five lists for current members. Enabling or rescheduling waits until the next weekly boundary. A saved weekly cursor prevents ordinary reloads from repeating delivered summaries; after downtime, at most the current due week is sent. Failed sends retry on the next maintenance cycle. Like other Discord sends, a process crash immediately after delivery but before saving its cursor can repeat that send. Mention notifications are suppressed.

The additive `features` guild section and `participation` member section merge through Red defaults. Existing settings, sticky roles, counters, and timestamps are preserved. The new daily counters start with this update and do not infer history from lifetime counts.

## Grouped text commands

Square brackets indicate optional arguments. `enable` and `disable` are separate subcommands.

| Command | Purpose |
| --- | --- |
| `[p]community` | Show current settings. |
| `[p]community help` | Show the cog's command overview. Aliases: `commands`, `?`. |
| `[p]community diag` | Check relevant intents and permissions. |
| `[p]community autorole set @Role` | Select the first-time join role. |
| `[p]community autorole clear` | Clear the selected role. |
| `[p]community autorole enable` / `disable` / `show` | Enable, disable, or inspect autorole. |
| `[p]community sticky enable` / `disable` | Control role restoration. |
| `[p]community sticky ignore add @Role` / `remove @Role` / `list` | Manage roles excluded from restoration. |
| `[p]community sticky purge @Member` | Clear that member's saved sticky roles only. |
| `[p]community welcome enable` / `disable` | Control welcome announcements. |
| `[p]community welcome channel [#channel]` | Set the welcome channel, or omit it to clear the target. |
| `[p]community welcome message <text>` | Set the welcome template. |
| `[p]community welcome preview [@Member]` | Preview a welcome in the command channel. |
| `[p]community cya enable` / `disable` | Control goodbye announcements. |
| `[p]community cya channel [#channel]` | Set the goodbye channel, or omit it to clear the target. |
| `[p]community cya message <text>` | Set the goodbye template. |
| `[p]community cya preview [@Member]` | Preview a goodbye in the command channel. |
| `[p]community vcsolo enable` / `disable` | Control solo voice disconnection. |
| `[p]community vcsolo idle <seconds>` | Set the timeout, with a minimum of 60 seconds. |
| `[p]community seen [@Member]` | Show last-seen and presence information. |
| `[p]community seendetail [@Member]` | Show event timestamps, channels, and platform statuses. |
| `[p]community stats [@Member]` | Show counters and the top five recorded games. |
| `[p]community seenlist [limit]` | List recently seen current members. Default 25, range 1 to 100. |
| `[p]community seenlistcsv` | Export current members' last-seen and presence summary as CSV. |
| `[p]community embeds [true\|false]` | Inspect or set compact embeds. |

Welcome and goodbye templates support `{user}`, `{mention}`, `{server}`, `{count}`, `{created_at}`, and `{joined_at}`. An invalid template is sent unchanged.

### Bot owner commands

| Command | Behavior |
| --- | --- |
| `[p]community restore` | Creates or reuses a role named `Restored Admin` and assigns it to the invoking bot owner. A newly created role has **Administrator** permission. Discord must allow the bot to create and assign that role. |
| `[p]community invites` | DMs the bot owner an invite report for every server the bot belongs to. Creates one-use invites that expire after 24 hours where the bot has **Create Invite** permission. |

## Stored data and current limits

Red Config stores server settings and IDs for channels/roles, message templates, posted self-role menu IDs, solo exemptions, and digest schedules/cursors. Per-member data includes the first-seen flag, sticky role IDs, event timestamps and channel IDs, presence/platform statuses, last-online/offline times, counters, game names with launch counts, lifetime voice seconds, and up to 35 daily message/voice buckets. Ordinary message contents are not saved.

`seenlistcsv` exports a summary for current members, not every stored record. `sticky purge` clears only saved roles. Red's user-data export hook returns all stored records for that user across servers. Its deletion hook clears those records and cancels pending timers and the current voice accounting interval for the user. Future observed activity can create new records. Use `community tracking` and `community vcsolo notify` to control collection and DMs. Statistics reflect events observed while the cog is running, without historical Discord backfill. Presence duration tracks actual status changes rather than every activity update. Seen lists paginate and exports read member records in one batch.
