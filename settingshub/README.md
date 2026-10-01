# SettingsHub

An optional shared dashboard for AudioPlus, CommunityPlus, LevelPlus, LogPlus, and OwoPlus. Every original cog remains independently installable; this cog requires no other package from the repository and discovers whichever cogs are loaded.

## Install and use

Use your Red repository name in place of `kevin-cogs`:

```text
[p]cog install kevin-cogs settingshub
[p]load settingshub
[p]slash enablecog settingshub
[p]slash sync
```

Maintenance commands require Red administrator access or Manage Server and run in a server. `commandbrowser` is available to members and only shows commands they can currently run. The bot needs Send Messages; Embed Links enables themed cards, and Attach Files is required for backups. `[p]` means your bot prefix.

| Text command | Slash command | Behavior |
| --- | --- | --- |
| `[p]settings` | `/settings panel` | Open a requester-bound cog picker that delegates to each cog's existing setup panel. |
| `[p]settings health` | `/settings health` | Inspect load/disable status, log delivery counters, local music packages/executables, and current-channel permissions. |
| `[p]settings backup` | `/settings backup` | Export eligible loaded cogs' selected server settings to JSON. |
| `[p]settings restore` with an attached JSON backup | `/settings restore file:<attachment>` | Validate and preview a same-server restore, then apply with the requester-bound button. |

Controls last three minutes and repeat the current command, member, server, and cog checks on every click. Unloaded/reloaded cogs require a fresh restore preview. The shared theme respects Red's embed preference and text fallback. Health reports detected prerequisites; it does not test a live Discord voice connection, YouTube extraction, or runtime version compatibility. Use `audiostatus` and the opt-in audio monitor for playback checks.

## Backup scope and restore

Backups contain music/continuity policies; community roles/welcome/solo/tracking/digest/room/onboarding/birthday policies; level formulas, source/exclusion/reward/farming/challenge/custom-goal/streak/monthly-season policies; log routing, switches, history/delivery/alert policies; and transformation dictionaries/channel/custom styles and Undo policy. Only loaded source cogs the caller can currently configure are included. A backup can contain a subset of the five source cogs; each included cog must have its complete supported settings shape. Hub themes, change history/policy and its own snapshot schedule are outside this source-cog backup scope.

Backups exclude global audio credentials/watchdog schedules and recipients, queue recovery payloads, listening history, personal/shared playlists/favorites, probability overrides/opt-outs, XP/display names/earned goal/streak progress, room ownership, birthday/acceptance/participation/sticky member records, active polls/events, incident cases/log text/digest cursors, owner alert recipient IDs, haiku submissions/contests/votes, registered role-menu messages, dated rankings, and temporary XP boosts. These records are preserved during restore. New definition/policy sections use merged defaults; use a fresh backup after updating to include the current supported shape.

Files are capped at 256 KiB. Restore rejects other server IDs, duplicate JSON keys, nonfinite values, unknown settings, invalid types/ranges/timezones, unavailable roles/channels, and unsafe automatic/self-service grant roles. Removed server IDs must be corrected or cleared before restoring. The JSON is validated before preview and again after acquiring settings locks. Refresh failures are reported separately after configuration is saved. Dynamic dictionaries are replaced; omitted operational/member fields stay intact. Restore uses the added sections' existing locks, saves prior values, and rolls back applied settings if a write fails. If storage also prevents rollback, the command reports affected settings for recovery. Legacy scalar setters retain their normal last-write behavior.

Restore clears settings caches, updates live audio fair-queue/autoplay/normalization preferences, refreshes community solo timers/role menus, and prunes log history to its restored policy. Existing Discord roles/messages and queued tracks are retained; reward-role reconciliation follows normal subsequent awards or the level sync command. Backups and previews are posted in the invoking channel, so choose an appropriate administrator channel.

## Stored data

SettingsHub persists server themes and up to ten configuration snapshots, each capped at 256 KiB. Snapshots contain the same selected settings as manual backups and exclude personal records. Requester/server IDs and restore previews are held in memory for up to three minutes and discarded on timeout/unload or a user-data deletion request. Its bounded configuration history retains actor IDs and selected setting values, with identified-user export/deletion as described below. Dashboard messages and backup files posted to Discord remain managed there. The five source cogs retain their own documented data hooks.

## Themes and maintenance

- `[p]theme color <info|success|warning|error> <#RRGGBB>`, `[p]theme footer <text>`, and `[p]theme reset` customize all loaded Kevin cogs in this server. The footer is limited to 80 characters. Themes return to defaults while SettingsHub is unloaded and reappear when loaded.
- `[p]commandbrowser <search>` finds up to 25 available commands with descriptions, usage and concrete examples for common actions. Other actions link to their detailed help. Searches are case-insensitive and repeat current parent, shortcut-source and cog permission checks. Availability reflects Red checks; operational conditions such as a current voice session still apply when invoked.
- `[p]settings diagnostics` downloads package/native API diagnostics, current channel permissions, loaded cog identifiers, installed-source SHA-256 fingerprints and the latest configured playback check. Fingerprints identify files on disk, so reload after updates to run them. It excludes full settings, secrets, signed URLs and message history.
- `[p]snapshots auto true 6` enables automatic snapshots every six hours; `false` disables them. Intervals range from 1 to 168 hours, disabled by default. Unchanged settings are skipped.
- `[p]snapshots create`, `[p]snapshots`, `[p]snapshots diff <id>`, `[p]snapshots restore <id>` and `[p]snapshots delete <id|all>` manage checkpoints. Automatic capture excludes disabled cogs. Restore repeats current source permissions and live role/channel validation, then requires the same requester-bound preview button as manual restore. Slash equivalents are available.

The hub adds 22 slash actions. Live music cards, refreshed poll/event cards and retried logs use the current server theme. Automatic snapshots start disabled, retain up to ten records and skip unchanged settings.

## Configuration change history

| Text command | Slash command | Behavior |
| --- | --- | --- |
| `[p]settings history [page]` | `/settings history list` | Browse ten retained writes per page, with actor, timestamp, command and stable change ID. |
| `[p]settings history show <id>` | `/settings history show` | Show each changed setting's previous and new values. |
| `[p]settings history enabled true 30` | `/settings history enabled` | Collect changes with 1 to 90 days of retention; default enabled, 30 days. Use `false` to pause without erasing existing records. |
| `[p]settings history clear` | `/settings history clear` | Erase records for source cogs you can currently configure. |
| `[p]settings history export` | `/settings history export` | Download accessible history as JSON; requires Attach Files. |

Collection begins after this update while SettingsHub is loaded and enabled in the server. Update and reload all six cogs so their command/component helpers can identify the actor. Prefix and hybrid/slash writes, guided setup buttons, and confirmed backup/snapshot restores are attributed to the actual caller. An instance-local observer checks writes and clears to these cogs' Red Config drivers; unload restores the original methods. Source cogs remain independently installable. Concurrent writes are serialized during observation. Background jobs do not inherit attribution from the command that started them, and unchanged/failed writes create no entry. A successful source write remains saved if supplementary history storage fails.

History covers the explicit selected server-settings scope used by backups, plus Hub themes, history policy and snapshot enable/interval settings. Global audio/node credentials/watchdog settings, personal preferences/records, XP balances, listening history, active queues, snapshot contents and runtime cursors are excluded. External edits and commands from older, unreloaded cog copies are not reconstructed. Restores record each actual setting write, including rollback writes if a later stage fails. History is informational; it does not itself offer an undo button. Snapshot/backup restores retain their existing validation and preview controls.

Retain at most 200 entries and 512 KiB per server. Each entry has at most 100 changed paths and 128 KiB. Values exceeding 2048 bytes use explicitly marked previews, and oversized entries report omitted paths. Startup/hourly, read and export pruning enforce the age policy, including inactive servers. Additive `audit_policy` and `configuration_history` defaults preserve existing data. Administrator commands repeat current Hub checks; source records are visible only while that cog is loaded and its setup command is accessible. User-data hooks export or delete whole records identifying that user as actor or in selected values. Discord-posted reports/files remain managed there.

## Feature readiness checks

`[p]settings ready [feature] [text_channel] [voice_channel] [role]`, or `/settings ready`, checks current configuration. Optional candidate arguments override the relevant destination or role for the report only. Discord slash fields make candidates easier to select. For example:

```text
[p]settings ready
[p]settings ready playback
[p]settings ready playback "Music Lounge"
[p]settings ready welcome #welcome
[p]settings ready autorole @Members
[p]settings ready voicerooms "Create a Room"
```

Feature choices are `playback`, `welcome`, `autorole`, `roles`, `onboarding`, `voicerooms`, `birthdays`, `messagexp`, `voicexp`, `levelrewards`, `logging`, `logalerts`, `transformations` and `snapshots`; omit the choice for all available features. Each source repeats its actual setup permission/disabled checks. Reports use the common theme and distinguish required failures from optional notes, including text fallback and best-effort audit attribution.

Checks use the exact selected/configured channels and their effective permissions, channel capacity, room category, current role hierarchy and automatic grant policy, required local intents, transformation scope, snapshot size, and AudioPlus's native API/FFmpeg/Opus/package/runtime diagnostics. These are local prerequisite checks, not a live provider, storage or Discord transport test. They never connect/play, create rooms, grant roles or enable features. Run them before saving/enabling a feature and again after changing roles or channel overwrites. The separate opt-in `audiocheck now` still performs the bounded live playback probe.
