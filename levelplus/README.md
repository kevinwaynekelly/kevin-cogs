# LevelPlus

## Reward previews

`[p]level rewards preview` shows which cached members would currently gain or lose milestone reward roles. `/level rewards preview` also accepts optional proposed `curve`, `multiplier`, linear `base`/`increment`, `max_level`, reward `role`/`threshold`, `stack`, and a specific `member`. Omitted fields retain their current values. For example, select `base:200` to preview a slower linear progression, or `role:@Veteran threshold:10` to preview a reward definition. Both candidate role and threshold must be supplied together; threshold zero stops managing that role and preserves existing assignments, matching reward removal.

The command requires the existing reward administrator checks and changes no settings, XP or Discord roles. It uses the same role plan as actual reconciliation, respects safe role hierarchy, stacking and tie-breaking, and protects overlapping custom earned role rewards. Reports show current/proposed levels, additions/removals, unavailable/unsafe roles, and missing Manage Roles permission. Totals cover up to 10000 cached human members, while detailed rows are capped at 100; select one member in larger servers. Reliable full membership requires the Members intent. Results reflect the current cache/XP snapshot and do not guarantee later Discord updates will succeed. Candidate numeric values must be finite and in command ranges.

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

The bot needs **View Channel** and **Send Messages** for commands and announcements. Grant **Embed Links** for the themed cards; output falls back to text without it. Export needs **Attach Files**, and fetching reaction target messages needs **Read Message History**. **Add Reactions** is used by the diagnostic probe. Enable the **Message Content** intent for word-based XP, **Server Members** for reliable member resolution, and voice-state events for voice XP.

Commands, confirmations, and level-up notices use the [shared visual theme](../docs/PRESENTATION.md). Member cards show level, XP, an avatar, and progress to the next level. Custom announcement templates retain their text and mention behavior. Long lists are paginated.

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

The default announcement is `{user.mention} has reached level **{user.level}**! GG!`. Templates support `{user.mention}`, `{user.name}`, `{user.level}`, and `{user.xp}` and are limited to 500 characters. The template remains the message text, paired with a themed level/XP card so mentions keep their original behavior.

Changing a curve recalculates displayed levels from existing XP. The multiplier changes the XP required per level, not the XP earned from an event.

## Direct and slash commands

| Direct text command | Slash command | Purpose |
| --- | --- | --- |
| `[p]rank [@Member]` | `/rank` | Level, total XP, and progress. |
| `[p]leaderboard [top]` | `/leaderboard` | Highest XP totals, default 10 and maximum 50. Text alias: `lb`. |
| `[p]levellookup <query>` | `/levellookup` | Find IDs by mention, numeric ID, or name. |

These are member commands. Administrator settings stay under `level`, with the existing checks. Direct shortcuts also honor Red permission and disabled-command rules on the original grouped command.

Enable slash actions once as the bot owner:

```text
[p]slash enablecog levelplus
[p]slash sync
```

Use `/level status`, `/level show`, or the direct member commands. Slash settings cover formulas, message/reaction/voice XP, channel-type restrictions, level-up announcements, member XP editing, imports/exports, names, and diagnostics. For example, `/level message enable` accepts an optional boolean and `/level xp add` accepts a member and amount. Administrator checks apply to slash settings too.

Calibration uses lowercase slash options: `/level formula calibrate level1:1 xp1:100 level2:2 xp2:250` fits thresholds of 100 total XP at level 1 and 250 total XP at level 2, preserving the current multiplier. The text command remains `[p]level formula calibrate 1 100 2 250`.

`level formula linear ...`, `level restrict nochannels ...`, and `level restrict noroles ...` remain text-only because their nesting exceeds Discord's limit. `level xp setid`, `level xp removeid`, `level name setid`, and `level name get` also remain text-only to preserve exact 64-bit user IDs without slash integer rounding. `levellookup` accepts an ID as text. CSV imports through slash use the `raw` text option; prefix imports still support message attachments. After updates, reload `levelplus` and run `slash sync` again.

## Grouped member commands

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

## Rewards, calendar rankings, boosts, and farming controls

New settings and member reports have slash counterparts. Existing lifetime XP and aliases are preserved. Added Config sections use merged defaults on upgrade. Calendar tracking starts enabled from installation/update, with no historical backfill; farming controls start disabled.

| Command | Purpose |
| --- | --- |
| `[p]level rewards` | Show milestone roles and stacking mode. |
| `[p]level rewards add @Role <threshold>` / `remove @Role` | Configure up to 100 reward roles at levels 1 through 100000. |
| `[p]level rewards stack <true\|false>` | Keep every currently qualified reward, or only the highest. |
| `[p]level rewards sync [@Member]` | Reconcile the selected member, default yourself. |
| `[p]periodboard [week\|month\|season] [top]` | Show earned XP for this calendar week, month, or current season; default week/top 10, maximum 50. |
| `[p]level season start <name>` | Archive the current season's top 50 and start a new season without resetting lifetime XP. |
| `[p]level season history` | List the five retained archives. |
| `[p]level season enable <true\|false>` | Enable/pause calendar and seasonal collection. |
| `[p]level season timezone <IANA zone>` | Set calendar boundaries, default America/Chicago. Weeks start Monday; months start on the first. |
| `[p]level boost [factor] [minutes] [@Role] [#channel]` | List boosts, or create a 1–10× earned-XP boost for 1–43200 minutes. Factor 1 clears all boosts. Optional role/channel restrict its scope. |
| `[p]level guard repeat <seconds>` | Suppress repeated normalized messages within 0–86400 seconds; 0 disables. |
| `[p]level guard reactions <true\|false>` | Allow one award per reactor/message in a rolling 24-hour window. |
| `[p]level guard dailycap <XP>` | Cap earned XP per member/local day, 0–1000000000; 0 disables. |
| `[p]level guard minwords <count>` | Require 0–100 words for message XP. |
| `[p]level setup` | Open a three-minute guided panel with announcement-channel and feature pickers. |

Administrator checks protect changes and role synchronization. `periodboard` and season history are member reports. Role rewards require Manage Roles and eligible unmanaged roles below the bot; management/moderation roles, voice moderators, mass mentions, and private audit/insight permissions cannot be configured. Rewards refresh on earned awards, explicit XP setting, member rejoins/role changes, and manual sync, even when announcements are disabled. Curve changes and bulk imports/resets take effect at the next reconciliation. Removing a reward setting leaves existing assignments alone. Highest-only mode removes other currently managed rewards; lowering XP can remove unqualified rewards.

Boosts affect earned event XP, unlike the original threshold multiplier. The highest matching boost applies, up to five active boosts, and expired boosts stop applying without a restart. Daily caps cover message/reaction/voice/slash XP together, persist across reloads, and apply after boosting. Administrative additions/imports bypass earned-XP policies and do not increase calendar/season totals. Message/reaction duplicate caches are bounded in memory and reset on reload; daily caps do not reset on reload. Short rejected messages do not consume message cooldowns.

Calendar records retain 35 daily buckets and the current season. Archives retain five seasons' top 50 only. Install a timezone database or `tzdata` if the container lacks America/Chicago; collection falls back to UTC when the configured zone is unavailable. New `/periodboard`, `/level rewards ...`, `/level season ...`, `/level boost`, `/level guard ...`, and `/level setup` are included among this cog's 74 slash actions. Reload and `slash sync` after updating.

## Stored data and current limits

Red Config stores settings per server, XP totals keyed by user ID, and saved display names or aliases. XP survives a member leaving and rejoining. Message contents are inspected to count words but are not persisted. Message, reaction, and voice cooldown timestamps are kept in memory and reset on reload.

Added records include reward role IDs/thresholds, boost scopes/expiry, farming preferences, dated earned-XP totals, current/archived season rankings, and current-day cap counters keyed by member ID. User-data hooks include and delete these new per-user records across servers. Repeated-message hashes and reactor/message IDs are held only in bounded memory, without storing message text.

`exportcsv` exports users with XP rows; aliases without XP rows are not included. Individual XP removal does not delete aliases. `clear yes` clears both maps for the server. Red's user-data export hook includes the user's XP and saved aliases across servers. Its deletion hook clears both and removes their in-memory cooldowns.

Current behavior to account for:

- Only application-command interactions award slash XP. They share the message cooldown and do not require message XP to be enabled.
- Channel and role exclusions apply across XP sources. Thread-parent exclusions, forum threads, and text-in-voice restrictions are enforced. The text-in-voice flag controls messages/reactions/interactions, not voice participation.
- One reaction-event cooldown limits both reactor and author awards. Bot authors and self-reaction author awards are excluded. If a message fetch fails, eligible reactor XP can still be awarded.
- Voice XP considers ordinary voice channels, not stage channels. Empty ticks do not write XP; eligible awards use one batch update per server.
- Thresholds use cumulative decimal formulas with half-even rounding. This fixes exponential overflow and float accumulation drift. A few exact half-XP boundaries can differ by one XP from the old float calculation, including default linear thresholds at levels 2,201 and 3,000. Existing XP totals remain unchanged.
- A capped zero-cost curve reaches its cap immediately. An uncapped zero-cost curve reports level 0 because it has no finite highest level.
- Event listeners and voice ticks respect Red's per-server cog disable setting. Cooldowns reset on reload and have bounded memory usage.

## Achievements, weekly challenges, and rank cards

`[p]achievements [@member]` shows seven earned badges: first earned XP, 1,000/10,000 earned XP, and levels 5/10/25/50. New badge tracking is enabled by default; `[p]level badges false` pauses it without revoking earned badges. Collection starts with eligible XP awards after this upgrade. Existing high-level members qualify for level badges on their next eligible award. Administrative XP edits/imports do not advance earned totals or challenges.

`[p]challenges [@member]` shows four weekly goals. Collection defaults to disabled; administrators use `[p]level challenges enabled true` and `[p]level challenges goal message|reaction|voice|xp <target> <reward>`. Default goals are 50 qualifying message awards for 100 XP, 20 qualifying reaction awards for 50 XP, 10 qualifying voice intervals for 100 XP, and 1,000 earned XP for 100 XP. Targets allow 1 to 100,000 and rewards 0 to 10,000 XP. Weeks begin Monday in the existing XP timezone. A voice interval means an eligible voice XP award, not a fixed minute.

Only positive XP surviving source restrictions, farming protections, and the shared daily cap advances goals. Each goal pays once per member per week. Rewards are not boosted and do not advance their own goals; they count toward lifetime/calendar/season XP and the daily cap. Unpaid portions remain pending across reloads and week changes and are paid on a later eligible award when cap room is available. Disabling challenge collection retains already earned pending rewards. Changing a completed goal does not pay it a second time that week.

`[p]rankcard [@member]` renders a local 900×340 PNG with level, server rank, next-level progress, total XP, and earned achievement count, using the common indigo/mint theme. It requires Attach Files and the cog's `Pillow>=10.4,<13` dependency, respects the original `level show` permission/disabled rules, and keeps the existing text rank command. Rendering is bounded to two tasks and never fetches external avatars or fonts. All new member commands and administrator controls have slash equivalents.

Additive `milestone_settings` and `milestones` sections preserve existing XP/settings. Member records store earned XP, badge IDs/times, one current week's counters/completions, and pending earned rewards. Data hooks export/delete them. XP-only removal/reset commands retain earned badges and challenge records; Red data deletion clears all personal records. Generated rank cards remain in memory until sent and are not saved by the cog.

## Custom progression and automatic seasons

- `[p]achievement create <name> <metric> <target> [reward] [role]` defines one of 25 server goals. Metrics: `xp`, `message`, `reaction`, `voice`, `level`, `streak`. Targets range from 1 to 1 billion; XP rewards from 0 to 10000. Role rewards must be unmanaged, below the bot and without moderation/management permissions; grants recheck safety. Members use `achievement [member]` alongside the existing fixed `achievements` badges. Managers use `achievement delete <name>`; granted rewards remain. Recreation gets a new goal identity. Counts start while custom goals or streaks are enabled, use qualifying awards and exclude bonus XP. Voice counts qualifying award intervals.
- `[p]streakset true 10 100` enables a daily bonus that grows by 10 XP per consecutive local active day, capped at 100 XP. One bonus per day; skipped days reset the streak. The multiplier stops growing after ten days. Daily step is limited to 100 XP and the maximum to 1000 XP. `[p]streak [member]` shows progress. Existing source/exclusion policies and daily XP caps apply; unpaid earned rewards remain pending, capped at 250000 XP. Goals and streaks are disabled/empty by default and award no extra XP until configured.
- `[p]monthlyseason true [channel]` archives the current seasonal rankings and starts automatic calendar seasons using the XP timezone. Month changes archive up to 50 leaders, announce the top three if a channel is set, and start a fresh season. Five archives are retained; lifetime XP and the existing calendar boards stay intact. Period tracking must be enabled. Missed months produce one closure for the accumulated season, without replaying empty months. `false` stops automation. Manual season starts still work between monthly boundaries.
- `[p]roleboard <role> [all|week|month|season] [top]` ranks up to 25 current cached human members of a role. Removed or unavailable members are excluded; reliable membership needs the Members intent.

All additions have slash equivalents and use existing Config namespaces, award serialization, daily caps and user-data deletion/export hooks.
