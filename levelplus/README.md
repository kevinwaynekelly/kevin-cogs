# LevelPlus

Server leveling for Red with message, reaction, voice, and slash-command XP; configurable level curves; leaderboards; level-up announcements; and XP import/export tools.

See the [repository README](../README.md) for installation. Load with `[p]load levelplus`. Replace `[p]` with your bot's prefix.

## Setup

XP collection starts as soon as the cog is loaded. To pause every XP source while configuring it:

```text
[p]level message enable false
[p]level reaction enable false
[p]level voice enable false
[p]level restrict toggles slashxp false
[p]level levelup channel #levels
[p]level diag
```

Enable the sources you want by repeating their commands with `true`. The `slashxp` setting controls the interaction listener independently of message XP.

Commands run in a server. Settings, XP migration/editing, aliases, and `testmsg`/`testup` require a Red admin or **Manage Server** permission. Status, help, diagnostics, member levels, leaderboards, and lookup are available to members unless restricted through Red's command permissions.

The bot needs **View Channel**, **Send Messages**, and **Embed Links** for commands and announcements, **Attach Files** for export, and **Read Message History** to fetch reaction target messages. **Add Reactions** is used by the diagnostic probe and command confirmations. Enable the **Message Content** intent for word-based XP, **Server Members** for reliable member resolution, and voice-state events for voice XP.

## Defaults

| Setting | Default behavior |
| --- | --- |
| Level curve | Linear, base `83.2`, increment `100.433`, multiplier `1.0`. |
| Maximum level | `0`, the uncapped setting. Level lookup extends beyond level 5,000 without constructing a threshold table. |
| Message XP | Enabled, `perword` mode, minimum `1`, maximum `1`, cooldown `60` seconds. This gives at most 1 XP per eligible message with words. |
| Reaction XP | Enabled, 25 XP, awards both the reactor and the message author, reactor cooldown `300` seconds. Self-reactions do not award author XP. |
| Voice XP | Enabled, 15 to 40 XP per eligible interval, cooldown `180` seconds, minimum 1 human member, anti-AFK disabled. Eligibility is checked every 20 seconds. |
| Restrictions | No excluded channels or roles. Thread, forum, text-in-voice, and slash-command flags are enabled. |
| Level-up announcements | Enabled. Uses the configured text channel, then the server's system channel as fallback. |

The default announcement is `{user.mention} has reached level **{user.level}**! GG!`. Templates support `{user.mention}`, `{user.name}`, `{user.level}`, and `{user.xp}` and are limited to 500 characters. Announcements are text only.

Changing a curve recalculates displayed levels from existing XP. The multiplier changes the XP required per level, not the XP earned from an event.

## Member commands

Square brackets indicate optional arguments.

| Command | Purpose |
| --- | --- |
| `[p]level` | Show current settings and tracked-user count. |
| `[p]level help` | Show the cog's command overview. |
| `[p]level diag` | Check settings, permissions, intents, and a reaction probe. |
| `[p]level show [@Member]` | Show XP and level. Defaults to yourself. |
| `[p]level leaderboard [top]` | Show the leaderboard. Default 10, range 1 to 50. |
| `[p]level lookup <name fragment\|mention\|ID>` | Find user IDs from current/cached users. |

## Admin commands

For boolean commands, omitting `true`/`false` toggles the setting.

| Command | Purpose |
| --- | --- |
| `[p]level formula curve <linear\|exponential\|constant>` | Select a level curve. |
| `[p]level formula multiplier <value>` | Set the threshold multiplier, range 0.1 to 10.0. |
| `[p]level formula maxlevel <level>` | Set a level cap. `0` selects uncapped mode. |
| `[p]level formula linear base <value>` / `inc <value>` | Set nonnegative linear coefficients. |
| `[p]level formula preset arcane` | Apply the default Arcane-like linear coefficients. |
| `[p]level formula calibrate <L1> <XP1> <L2> <XP2>` | Fit a linear curve to two nonnegative cumulative XP thresholds, preserving the current multiplier. Rejects invalid or unrepresentable fits without changing settings. |
| `[p]level message enable [true\|false]` | Control message XP. |
| `[p]level message mode <perword\|random\|none>` | Select word-based XP, a random amount, or no message award. |
| `[p]level message min <value>` / `max <value>` | Set random bounds, or the XP-per-word value and per-message cap in `perword` mode. |
| `[p]level message cooldown <seconds>` | Set the cooldown, range 0 to 3,600 seconds. |
| `[p]level reaction enable [true\|false]` | Control reaction XP. |
| `[p]level reaction awards <both\|author\|reactor\|none>` | Choose award recipients. |
| `[p]level reaction min <value>` / `max <value>` | Set the nonnegative random award range. |
| `[p]level reaction cooldown <seconds>` | Set the reactor cooldown, range 0 to 3,600 seconds. |
| `[p]level voice enable [true\|false]` | Control voice XP. |
| `[p]level voice range <min> <max>` | Set the nonnegative random award range. |
| `[p]level voice cooldown <seconds>` | Set the interval, range 15 to 3,600 seconds. |
| `[p]level voice minmembers <count>` | Require 1 to 99 human channel members. |
| `[p]level voice antiafk [true\|false]` | Skip AFK, muted, or deafened members when enabled. |
| `[p]level restrict nochannels add #channel` / `remove #channel` / `list` / `clear` | Manage excluded text channels. |
| `[p]level restrict noroles add @Role` / `remove @Role` / `list` / `clear` | Manage excluded roles. |
| `[p]level restrict toggles <threadxp\|forumxp\|textvoicexp\|slashxp> [true\|false]` | Control message/reaction/slash-command eligibility for these channel types. |
| `[p]level levelup enable [true\|false]` | Control announcements. |
| `[p]level levelup channel [#channel]` | Set the announcement channel, or omit it to clear the target. |
| `[p]level levelup template <text>` | Set the announcement template. |
| `[p]level testmsg [@Member]` | Send a test announcement without changing XP. |
| `[p]level testup [@Member] [levels]` | Award real XP to advance the member's level. Default one level. |

### XP and aliases

| Command | Purpose |
| --- | --- |
| `[p]level xp set @Member <amount>` / `setid <ID> <amount>` | Set an XP total, clamped to zero or greater. |
| `[p]level xp add @Member <amount>` | Add positive XP and announce any resulting level increase. |
| `[p]level xp remove @Member` / `removeid <ID>` | Remove the XP row only. The saved name/alias remains. |
| `[p]level xp purgebots` | Remove XP rows for bots currently in the server. |
| `[p]level xp clear yes` | Erase all XP and saved aliases for this server. |
| `[p]level xp exportcsv` | Export XP rows with `user_id,xp,alias` columns. |
| `[p]level xp importcsv [CSV text]` | Import the first UTF-8 attachment, or pasted CSV text. Matching IDs are overwritten. |
| `[p]level xp importlines <lines>` | Import `identifier,xp` lines using an ID, mention, or resolvable name. Matching IDs are overwritten. |
| `[p]level name set @Member <alias>` / `setid <ID> <alias>` | Save an alias, up to 100 characters. |
| `[p]level name get <ID>` | Show a saved alias. |

CSV import supports quoted commas and line breaks, UTF-8 with or without a byte-order mark, and attachments up to 8 MB. XP is parsed as an integer to preserve large totals. Malformed quoting rejects the import before any writes; invalid IDs or XP rows are skipped. `importlines` also accepts quoted identifiers and skips ambiguous names. Export before an import or reset if you need to retain the existing totals. New activity preserves saved aliases.

## Stored data and current limits

Red Config stores settings per server, XP totals keyed by user ID, and saved display names or aliases. XP survives a member leaving and rejoining. Message contents are inspected to count words but are not persisted. Message, reaction, and voice cooldown timestamps are kept in memory and reset on reload.

`exportcsv` exports users with XP rows; aliases without XP rows are not included. Individual XP removal does not delete aliases. `clear yes` clears both maps for the server. Red's user-data export hook includes the user's XP and saved aliases across servers. Its deletion hook clears both and removes their in-memory cooldowns.

Current behavior to account for:

- Only application-command interactions award slash XP. They share the message cooldown and do not require message XP to be enabled.
- Channel and role exclusions apply across XP sources. Thread-parent exclusions, forum threads, and text-in-voice restrictions are enforced. The text-in-voice flag controls messages/reactions/interactions, not voice participation.
- One reaction-event cooldown limits both reactor and author awards. Bot authors and self-reaction author awards are excluded. If a message fetch fails, eligible reactor XP can still be awarded.
- Voice XP considers ordinary voice channels, not stage channels. Empty ticks do not write XP; eligible awards use one batch update per server.
- Thresholds use cumulative decimal formulas with half-even rounding. This fixes exponential overflow and float accumulation drift. A few exact half-XP boundaries can differ by one XP from the old float calculation, including default linear thresholds at levels 2,201 and 3,000. Existing XP totals remain unchanged.
- A capped zero-cost curve reaches its cap immediately. An uncapped zero-cost curve reports level 0 because it has no finite highest level.
- Event listeners and voice ticks respect Red's per-server cog disable setting. Cooldowns reset on reload and have bounded memory usage.
