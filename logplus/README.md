# LogPlus

Discord event logs for messages, reactions, server changes, members, voice activity, and bot commands. Events are sent as timestamped embeds, with audit-log attribution when available.

[Repository setup and installation](../README.md)


Commands and event notices use the [shared visual theme](../docs/PRESENTATION.md). Event timestamps, attribution, IDs, and the compact-header setting are retained. Long log fields are paginated, and output falls back to text when embeds are unavailable.

## Setup

`[p]` means your bot's command prefix. After installing the cog:

```text
[p]load logplus
[p]log setchannel #server-logs
[p]log
```

Commands require Red administrator access or the **Manage Server** permission and can only run in a server. The bot needs **View Channel**, **Send Messages**, and **Embed Links** in each log destination. Grant **View Audit Log** for attribution. Message text requires the Message Content intent, and member events require the Server Members intent to be enabled for the bot. Presence logging also needs the Presence intent.

No destination is configured initially. Event switches default to on, compact styling defaults to on, and the duplicate-suppression window defaults to 2 seconds. Selected repeated reactions, voice states, presence changes, and other-bot messages use this window. Different reaction users have separate suppression keys.

## Direct and slash commands

| Direct text command | Slash command | Purpose |
| --- | --- | --- |
| `[p]logstatus` | `/logstatus` | Logging settings and switches. |
| `[p]logchannel [#channel]` | `/logchannel` | Show the destination, or set it to the supplied channel. |
| `[p]lograte [seconds]` | `/lograte` | Show or set duplicate suppression. |

All retain Red administrator or **Manage Server** checks and honor the grouped command's permission/disabled state. The renamed root is `log`; `logplus` is now only the package name for installation and reload.

Enable the 30 slash actions once as the bot owner:

```text
[p]slash enablecog logplus
[p]slash sync
```

Use `/log status`, `/log setchannel`, `/log route set`, `/log style preview`, or `/log diag`. `/log event` offers autocomplete for all 45 event switches. Select an event such as `message.delete`, `voice.join`, or `sched.create` and a boolean to turn it on/off. Omit the boolean to inspect the current switch without changing it. This command also works as `[p]log event voice.join false`.

The existing `log toggle ...` paths remain text-only because of Discord's nesting limit. `/log event` covers those switches and scheduled events. After updates, reload `logplus` and run `slash sync` again. Custom Red rules referencing `logplus ...` need to be reapplied under `log ...`.

## Category routing, exemptions, and delivery recovery

All commands below have slash equivalents and retain the root administrator checks.

| Command | Purpose |
| --- | --- |
| `[p]log route category <category> [#channel]` | Set a category destination, or omit the channel to clear it. |
| `[p]log route categories` | List category routes. |
| `[p]log ignore add <channel> [scope=all]` | Exempt a channel and its threads from message/server events. |
| `[p]log ignore remove <channel> [scope=all]` | Remove an exemption. |
| `[p]log ignore list` | List message/server exclusions. |
| `[p]log delivery [retry]` | Inspect delivery counters, queue depth, and the latest safe error, or enable/disable retries. |
| `[p]log setup` | Open a destination picker and message/member/voice/server group toggles. |

Categories are `message`, `reactions`, `server`, `invites`, `member`, `voice`, `sched`, and `commands`. Source-channel overrides have priority, followed by a thread-parent override, category routing, and the default channel. An explicitly selected missing destination discards the event instead of silently sending to another channel. Exemption scopes are `message`, `server`, or `all`; `all` means both of those scopes, preserving the existing exemption semantics. It does not suppress voice, member, command, invite, or reaction events. Settings and setup writes use their existing section locks.

Failed Discord sends retry three times after 2, 4, and 8 seconds. Only unacknowledged pages or text chunks remain queued, preserving the original event timestamp. Each retry checks the current routes, exemptions, event switch, and cog disabled state. There is no audit fetch during retry. Missing destinations, disabled events, expired records, and exhausted attempts are discarded. A Discord response lost after accepting a send can still cause a duplicate on retry.

Queues stay in memory, with at most 100 events/2 MiB of serialized payload per server and 1000 events/8 MiB across the cog. Events expire after five minutes, and each send has a 15-second timeout. Disabling retries, removing the server, or unloading cancels owned workers and clears pending records. Delivery counters reset on reload. The additive `features` Config section preserves all original settings and routes through Red's merged defaults.

## Grouped text commands

| Command | Purpose |
| --- | --- |
| `[p]log` | Show settings and event switches. |
| `[p]log help` | Show the built-in command summary. |
| `[p]log channel` | Show the default destination. |
| `[p]log setchannel #channel` | Set the default destination. |
| `[p]log clearchannel` | Clear the default destination. Existing routing overrides remain active. |
| `[p]log route set #source #destination` | Route events with that source channel to a different destination. |
| `[p]log route clear #source` | Remove a source-channel override. |
| `[p]log route list` | List routing overrides. |
| `[p]log rate [seconds]` | Show or set the suppression window. Use `0` to disable suppression. |
| `[p]log style compact [on\|off]` | Show or set event-title emojis. |
| `[p]log style preview` | Send three sample event embeds. |
| `[p]log diag` | Read settings, routing, and audit-log permission without changing live switches. |

Each toggle flips the current value. For example, `[p]log toggle message edit` switches edit logging on or off.

| Command group | Available toggle commands |
| --- | --- |
| `[p]log toggle message` | `edit`, `delete`, `bulk`, `pins` |
| `[p]log toggle reactions` | `add`, `remove`, `clear` |
| `[p]log toggle server` | `channelcreate`, `channeldelete`, `channelupdate`, `rolecreate`, `roledelete`, `roleupdate`, `serverupdate`, `emojiupdate`, `stickerupdate`, `integrationsupdate`, `webhooksupdate`, `threadcreate`, `threaddelete`, `threadupdate` |
| `[p]log toggle invites` | `create`, `delete` |
| `[p]log toggle member` | `join`, `leave`, `roles`, `nick`, `ban`, `unban`, `timeout`, `presence` |
| `[p]log toggle voice` | `join`, `move`, `leave`, `mute`, `deaf`, `video`, `stream` |
| `[p]log toggle commands` | `thisbot`, `otherbots` |

`commands_` and `thredupdate` remain aliases for compatibility. Thread and presence listeners honor their switches. Scheduled-event listeners use Discord's dispatched `on_scheduled_event_*` names and respect their Config switches, now available through `log event sched.<switch> [true|false]` and `/log event`.

## Behavior and limitations

- Source-specific routing applies when an event supplies a source channel. All recognized events support category routes, including member and voice events without a source.
- Cached edits/deletes retain author and content details, with deleted text limited to 1,024 characters and each before/after edit field limited to 1,000 characters. Raw events also cover messages outside the cache. Uncached edits show the new content when supplied, with the previous text explicitly unavailable. Uncached deletes show message/channel IDs with author and content unavailable. Raw handlers skip cached payloads so the same event is not logged twice. Bulk deletions log a count. Unknown-author raw events in configured destinations and known own log messages are skipped to avoid logging the logger.
- Channel updates show names/topics, NSFW, slowmode, voice settings, and per-target overwrite changes with Allow/Deny/Inherit states. Role updates show names, color, display settings, position, and allowed/removed permissions. Server updates show changed visible settings. Unchanged updates are skipped. Permission labels use current Discord names.
- The other-bot logger detects messages authored by other bots that begin with certain prefix-like characters. It does not observe or verify another bot's command execution.
- Audit attribution is best effort. Recent entries are shared through a one-second per-server cache, matched by action and target, and discarded for attribution after the lookback window. No audit request is made without permission or configured destinations. Missing entries or event timing can still result in an unknown actor.
- Embed titles, descriptions, fields, footer/author text, and total size are clipped to Discord limits. Mentions are suppressed. Delivery failures are counted and retried within the stated limits without breaking event listeners.
- Event listeners respect Red's per-server cog disable setting.
- Clearing the default channel does not remove overrides. Clear source and category routes too if you want all destinations removed.

## Stored data

Red Config stores server settings, event switches, destination channel IDs, source/category routing overrides, channel exemption lists, retry preferences, and style/rate preferences. Message text is persisted in Config only when optional local history is enabled. Logs posted to Discord can contain user IDs, names, invite codes, message contents, and event details, and remain in the destination channels until removed there.

Pending retry records temporarily contain the same event details and message contents in memory. User-data export/deletion hooks return or remove pending and retained records containing that user's Discord ID and clear cached audit entries on deletion. Optional Config history holds bounded event records identifying members; posted Discord logs are managed in Discord.

## Retained history, timelines, and exports

History starts disabled. `[p]log history enabled true` begins collecting one bounded local record for each enabled, non-exempt event accepted for an available log destination. Collection happens once before delivery; retries do not create extra records. Existing Discord messages are not imported. Disabling collection keeps retained records until they expire. `[p]log history retention <days>` chooses 1 to 90 days (default 7) and prunes immediately; background cleanup also runs at startup and hourly, including inactive servers. Count and size limits keep the latest 1000 events and at most 2 MiB per server, so busy servers can retain less than their chosen duration.

`[p]timeline <member> [days]` lists the latest 25 matching events. `[p]logsearch <query> [category] [days]` searches retained text. `[p]logexport [json|csv] [member] [category] [days] [query]` exports up to 1000 matches; exports need Attach Files and CSV values are protected against spreadsheet formula execution. Use `all` for every category. These administrator commands also respect the original log root's permission and disabled rules and have matching slash actions. `[p]log history clear yes` erases retained local events.

Records store event time/type/category, source channel ID, Discord IDs appearing in event details, and bounded titles/descriptions/fields. Those fields can include edited/deleted message text, member names, invite links, and audit actors. Timelines include only records that identify the member by ID, so unidentified raw events cannot be attributed. Red data hooks export/delete complete retained records identifying the requester, alongside pending delivery data; Discord-posted logs remain there. New `history_settings` and `history_records` sections use merged defaults and preserve existing settings. Normal routing reads exclude retained records from the settings cache.
