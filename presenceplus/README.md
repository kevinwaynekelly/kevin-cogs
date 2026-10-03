# PresencePlus

Saved bot status profiles, rotating messages, weekly schedules and optional AudioPlus music presence. Each control is available through `!presence` and `/presence`; all controls require **bot ownership**, including previews. Server administrators do not receive access to global bot presence.

`[p]` means your bot's prefix, for example `!`. Activity text is visible across every server Scarlet is in. Pick one server as the source before enabling music status.

## Install and start

```text
[p]repo update kevin-cogs
[p]cog install kevin-cogs presenceplus
[p]load presenceplus
[p]slash enablecog presenceplus
[p]slash sync
[p]presence set custom Use /play or /search
[p]presence add watching {servers} servers
[p]presence add playing Music for {members} members
[p]presence interval 300
```

Use your existing repository alias if it differs from `kevin-cogs`, such as `kevin`. Requires Red 3.5.24+ and Python 3.10/3.11, with no additional dependencies. Loading the cog starts with automation **disabled** and preserves your current status. `set`, `add`, `profile use` and enabling music also enable automation. Other edits leave the enabled flag as it was.

The stock custom status is `Use /play or /search`, referring to AudioPlus's slash commands. On the first update/reload, existing `default` profile entries that exactly match the old stock `Use !help for commands` are replaced under the configuration lock. Custom text, other named profiles, availability, automation and schedules are preserved. An internal migration version prevents later owner edits from being rewritten. Red's built-in help remains the prefix command `[p]help`.

`[p]presence` / `/presence show` shows the editable base profile, the effective scheduled profile, music source, rendered preview and latest safe error type. Changes reach Discord within 15 seconds. Gateway failures retry on subsequent checks.

## Commands

Slash actions mirror the following prefix commands. Prefix-only `[p]presence show` is an alias for the root status screen; slash uses `/presence show`.

| Command | Behavior |
| --- | --- |
| `[p]presence` | Show configuration, effective preview and latest result. |
| `[p]presence help` | Show commands and available placeholders. |
| `[p]presence set <activity> <text>` | Replace the base profile's messages with one entry and enable automation. |
| `[p]presence add <activity> <text>` | Append a rotating entry and enable automation. |
| `[p]presence list` | List base-profile entries and their numbers. |
| `[p]presence remove <index>` | Remove one entry using its one-based list number. |
| `[p]presence clear` | Clear the base profile's activity messages, retaining availability. |
| `[p]presence interval <seconds>` | Set the global rotation interval, 60-86400 seconds; default 300. |
| `[p]presence status <availability>` | Set base-profile availability: online, idle, dnd or invisible. |
| `[p]presence enable <true/false>` | Enable saved automation or restore the previous presence when still owned. |
| `[p]presence timezone <zone>` | Set an available IANA timezone; default America/Chicago. |
| `[p]presence preview` | Render the effective profile without sending or advancing anything. |
| `[p]presence next` | Advance the effective profile's rotation; music can still override it. |
| `[p]presence music <true/false> [server_id]` | Use active AudioPlus playback from one selected server. |
| `[p]presence musictext <text>` | Set the listening override template; default `{song}`. |
| `[p]presence profile list` | List saved profiles, availability and message counts. |
| `[p]presence profile create <name>` | Copy the base profile to a new name without selecting it. |
| `[p]presence profile use <name>` | Select the editable base profile and enable automation. |
| `[p]presence profile delete <name>` | Remove an unselected profile with no schedule references. |
| `[p]presence schedule list` | List named windows and the configured timezone. |
| `[p]presence schedule add <rule> <profile> <start> <end> [days]` | Add a non-overlapping weekly window using HH:MM times. |
| `[p]presence schedule remove <rule>` | Remove one named window. |
| `[p]presence schedule clear` | Remove all windows and use the base profile. |
| `[p]presence reset [confirm]` | Preview clearing everything; explicitly supply `true` to reset and disable. |

Edits change the selected **base profile**. A scheduled profile or active music may currently take priority over its display. Select a profile with `profile use` before editing its messages or availability. An empty profile clears the activity while keeping its availability. Reset preserves no custom profiles, rules or music choices.

## Activities and templates

Activities are `custom`, `playing`, `listening`, `watching` and `competing`. Discord chooses their visual label; for example `listening` with `{song}` produces “Listening to …”. Text must be one line of 1-128 UTF-16 units. Emoji can use two units. Rendered dynamic text is safely truncated to that limit.

| Placeholder | Value |
| --- | --- |
| `{servers}` | Number of servers currently cached by Red. |
| `{members}` | Sum of each server's member count, including bots and members shared between servers. |
| `{uptime}` | Red's uptime in days, hours and minutes. |
| `{song}` | Active song from the opted-in music server, or `Nothing playing`. |
| `{listeners}` | Human listeners in that song's voice channel who are not deafened; otherwise 0. |

Use `{{` and `}}` for literal braces. Other placeholders, attribute/index access, conversions and format specifications are rejected. Member counts depend on Discord's cache. Song fields only read the opted-in server, never other servers' tracks.

## Saved profiles and schedules

```text
[p]presence profile create night
[p]presence profile use night
[p]presence set watching The moon
[p]presence status idle
[p]presence profile use default
[p]presence timezone America/Chicago
[p]presence schedule add nighttime night 22:00 07:00 all
[p]presence schedule list
```

Times use 24-hour `HH:MM`. Days are `all` or comma-separated names: `mon,tue,wed,thu,fri,sat,sun`. The start is inclusive; the end is exclusive. Overnight rules use their **starting day**, so a Friday 22:00-07:00 window includes Saturday morning. Weekly windows cannot overlap, including Sunday rollover. Adjacent windows are allowed. Equal start/end is rejected; use the base profile for an all-day status.

Schedules follow local wall time, including daylight-saving transitions. A repeated DST hour keeps the same matching profile; a skipped hour is skipped. The worker checks every 15 seconds. Outside matching windows it uses the base profile. Profile changes/reloads start rotation at the first message; runtime cursors are not persisted.

Limits: ten profiles, twenty messages per profile, twenty named rules and 64 KiB of saved configuration. Names use 1-24 lowercase letters, numbers, `-` or `_`, starting with a letter or number. Profile creation copies messages and availability; rotation interval and timezone are global.

## AudioPlus song status

Update/reload AudioPlus first. Run the music command in the source server, or specify its numeric ID from another server or a DM:

```text
[p]cog update audioplus
[p]reload audioplus
[p]presence music true
[p]presence musictext {song} · {listeners} listeners
[p]presence preview
```

While AudioPlus actively plays, the cog sets a listening activity using this template and the effective profile's availability. Preparing, paused, stopped and disconnected players return to the normal profile. Disabling/unloading AudioPlus or disabling it in the source server also removes the override. The integration only reads an active player snapshot; it does not join voice, queue tracks or run a YouTube check. Intro overlays preserve the underlying music title.

Use `[p]presence music false` to stop the override and keep profile automation. The source server's title becomes visible in **all** Scarlet's servers when enabled. Requests, requester IDs and song URLs are not included.

## Lifecycle and troubleshooting

Saved profiles, selection, interval, timezone, rules and music source survive reload/restart. One owned worker coalesces changes, sends only changed presence, refreshes after gateway reconnects and spaces gateway attempts at least 15 seconds apart. Calls time out after ten seconds; `presence` shows only the latest exception type. Global Config writers share one lock, recheck owner access after waiting and commit validated settings atomically.

Disable automation before using Red's built-in `[p]set status` controls or another presence cog. While enabled, future rotation/schedule/music changes can overwrite manual status. Disabling or unloading restores the presence captured when this session first took control, provided another tool has not already changed it. Restoration follows the same gateway spacing, so unloading may wait up to 15 seconds plus the ten-second gateway timeout. On process restart, the former transient presence is unavailable; saved automation resumes if enabled.

If a song never appears, check `presence`, `presence preview`, the selected server ID and whether the updated AudioPlus is loaded and actually playing. Missing/malformed saved settings can be cleared with `presence reset true`. Commands use the shared themed presentation, respect embed preferences and suppress mentions. Slash replies are private after successful permission checks; prefix replies use the channel where you run them. Red's core permission errors remain controlled by Red.

## Data and validation

Global settings contain owner-supplied profile/template text, availability, timing, timezone, rules and optional music server ID. Avoid private personal information in status text. Dynamic song/listener/member information is read from memory and not stored. There are no member records or listening histories; Red user-data hooks return no per-user records. `presence reset true` clears all global configuration.

Regression tests exercise actual Red prefix/slash checks, Config persistence, atomic writers, schedules/DST, activity serialization, throttling, reconnect refresh and task cleanup. Discord gateway transport and music voice networking are mocked. No live Scarlet deployment test is claimed.

API references: [Red's built-in status commands](https://docs.discord.red/en/stable/cog_guides/core.html#set-status), [Discord presence updates](https://docs.discord.com/developers/events/gateway-events#update-presence) and [activity types](https://docs.discord.com/developers/events/gateway-events#activity-object).
