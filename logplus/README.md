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

Enable the 16 slash actions once as the bot owner:

```text
[p]slash enablecog logplus
[p]slash sync
```

Use `/log status`, `/log setchannel`, `/log route set`, `/log style preview`, or `/log diag`. `/log event` offers autocomplete for all 45 event switches. Select an event such as `message.delete`, `voice.join`, or `sched.create` and a boolean to turn it on/off. Omit the boolean to inspect the current switch without changing it. This command also works as `[p]log event voice.join false`.

The existing `log toggle ...` paths remain text-only because of Discord's nesting limit. `/log event` covers those switches and scheduled events. After updates, reload `logplus` and run `slash sync` again. Custom Red rules referencing `logplus ...` need to be reapplied under `log ...`.

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

- Source-specific routing applies only when the event passes a source channel. Member, role, server, and voice logs use the default destination.
- Single-message edit and delete events depend on Discord's message cache. Deleted text is limited to 1,024 characters, and each before/after edit field is limited to 1,000 characters. Bulk deletions log a count rather than message contents.
- Channel-update logs cover name, topic, and NSFW changes. They do not provide a full permission diff.
- The other-bot logger detects messages authored by other bots that begin with certain prefix-like characters. It does not observe or verify another bot's command execution.
- Audit attribution is best effort. Recent entries are shared through a one-second per-server cache, matched by action and target, and discarded for attribution after the lookback window. No audit request is made without permission or configured destinations. Missing entries or event timing can still result in an unknown actor.
- Embed titles, descriptions, fields, footer/author text, and total size are clipped to Discord limits. Mentions are suppressed. Delivery failures are logged without breaking event listeners.
- Event listeners respect Red's per-server cog disable setting.
- Clearing the default channel does not remove overrides. Clear individual routes too if you want all destinations removed.

## Stored data

Red Config stores server settings, event switches, destination channel IDs, routing overrides, channel exemption lists, and style/rate preferences. It does not persist message text in Config. Logs posted to Discord can contain user IDs, names, invite codes, message contents, and event details, and remain in the destination channels until removed there.
