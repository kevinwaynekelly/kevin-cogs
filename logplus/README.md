# LogPlus

Discord event logs for messages, reactions, server changes, members, voice activity, and bot commands. Events are sent as timestamped embeds, with audit-log attribution when available.

[Repository setup and installation](../README.md)

## Setup

`[p]` means your bot's command prefix. After installing the cog:

```text
[p]load logplus
[p]logplus setchannel #server-logs
[p]logplus
```

Commands require Red administrator access or the **Manage Server** permission and can only run in a server. The bot needs **View Channel**, **Send Messages**, and **Embed Links** in each log destination. Grant **View Audit Log** for attribution. Message text requires the Message Content intent, and member events require the Server Members intent to be enabled for the bot. Presence logging also needs the Presence intent.

No destination is configured initially. Event switches default to on, compact styling defaults to on, and the duplicate-suppression window defaults to 2 seconds. Selected repeated reactions, voice states, presence changes, and other-bot messages use this window. Different reaction users have separate suppression keys.

## Commands

| Command | Purpose |
| --- | --- |
| `[p]logplus` | Show settings and event switches. |
| `[p]logplus help` | Show the built-in command summary. |
| `[p]logplus channel` | Show the default destination. |
| `[p]logplus setchannel #channel` | Set the default destination. |
| `[p]logplus clearchannel` | Clear the default destination. Existing routing overrides remain active. |
| `[p]logplus route set #source #destination` | Route events with that source channel to a different destination. |
| `[p]logplus route clear #source` | Remove a source-channel override. |
| `[p]logplus route list` | List routing overrides. |
| `[p]logplus rate [seconds]` | Show or set the suppression window. Use `0` to disable suppression. |
| `[p]logplus style compact [on\|off]` | Show or set event-title emojis. |
| `[p]logplus style preview` | Send three sample event embeds. |
| `[p]logplus diag` | Read settings, routing, and audit-log permission without changing live switches. |

Each toggle flips the current value. For example, `[p]logplus toggle message edit` switches edit logging on or off.

| Command group | Available toggle commands |
| --- | --- |
| `[p]logplus toggle message` | `edit`, `delete`, `bulk`, `pins` |
| `[p]logplus toggle reactions` | `add`, `remove`, `clear` |
| `[p]logplus toggle server` | `channelcreate`, `channeldelete`, `channelupdate`, `rolecreate`, `roledelete`, `roleupdate`, `serverupdate`, `emojiupdate`, `stickerupdate`, `integrationsupdate`, `webhooksupdate`, `threadcreate`, `threaddelete`, `threadupdate` |
| `[p]logplus toggle invites` | `create`, `delete` |
| `[p]logplus toggle member` | `join`, `leave`, `roles`, `nick`, `ban`, `unban`, `timeout`, `presence` |
| `[p]logplus toggle voice` | `join`, `move`, `leave`, `mute`, `deaf`, `video`, `stream` |
| `[p]logplus toggle commands` | `thisbot`, `otherbots` |

`commands_` and `thredupdate` remain aliases for compatibility. Thread and presence listeners now honor their switches. Scheduled-event listeners use Discord's dispatched `on_scheduled_event_*` names and respect the existing scheduled-event Config switches; there are no scheduled-event toggle commands.

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
