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

Backups contain music/continuity policies; community roles/welcome/solo/tracking/digest/room/onboarding/birthday policies; level formulas, source/exclusion/reward/farming/challenge/custom-goal/streak/monthly-season policies; log routing, switches, history/delivery/alert policies; and transformation dictionaries/channel/custom styles and Undo policy. Only loaded source cogs the caller can currently configure are included. A backup can contain a subset of the five source cogs; each included cog must have its complete supported settings shape. Hub themes and its own snapshot schedule are outside this source-cog backup scope.

Backups exclude global audio credentials/watchdog schedules and recipients, queue recovery payloads, listening history, personal/shared playlists/favorites, probability overrides/opt-outs, XP/display names/earned goal/streak progress, room ownership, birthday/acceptance/participation/sticky member records, active polls/events, incident cases/log text/digest cursors, owner alert recipient IDs, haiku submissions/contests/votes, registered role-menu messages, dated rankings, and temporary XP boosts. These records are preserved during restore. New definition/policy sections use merged defaults; use a fresh backup after updating to include the current supported shape.

Files are capped at 256 KiB. Restore rejects other server IDs, duplicate JSON keys, nonfinite values, unknown settings, invalid types/ranges/timezones, unavailable roles/channels, and unsafe automatic/self-service grant roles. Removed server IDs must be corrected or cleared before restoring. The JSON is validated before preview and again after acquiring settings locks. Refresh failures are reported separately after configuration is saved. Dynamic dictionaries are replaced; omitted operational/member fields stay intact. Restore uses the added sections' existing locks, saves prior values, and rolls back applied settings if a write fails. If storage also prevents rollback, the command reports affected settings for recovery. Legacy scalar setters retain their normal last-write behavior.

Restore clears settings caches, updates live audio fair-queue/autoplay/normalization preferences, refreshes community solo timers/role menus, and prunes log history to its restored policy. Existing Discord roles/messages and queued tracks are retained; reward-role reconciliation follows normal subsequent awards or the level sync command. Backups and previews are posted in the invoking channel, so choose an appropriate administrator channel.

## Stored data

SettingsHub persists server themes and up to ten configuration snapshots, each capped at 256 KiB. Snapshots contain the same selected settings as manual backups and exclude personal records. Requester/server IDs and restore previews are held in memory for up to three minutes and discarded on timeout/unload or a user-data deletion request. It exports no additional personal records. Dashboard messages and backup files posted to Discord remain managed there. The five source cogs retain their own documented data hooks.

## Themes and maintenance

- `[p]theme color <info|success|warning|error> <#RRGGBB>`, `[p]theme footer <text>`, and `[p]theme reset` customize all loaded Kevin cogs in this server. The footer is limited to 80 characters. Themes return to defaults while SettingsHub is unloaded and reappear when loaded.
- `[p]commandbrowser <search>` finds up to 25 available commands with descriptions, usage and concrete examples for common actions. Other actions link to their detailed help. Searches are case-insensitive and repeat current parent, shortcut-source and cog permission checks. Availability reflects Red checks; operational conditions such as a current voice session still apply when invoked.
- `[p]settings diagnostics` downloads package/native API diagnostics, current channel permissions, loaded cog identifiers, installed-source SHA-256 fingerprints and the latest configured playback check. Fingerprints identify files on disk, so reload after updates to run them. It excludes full settings, secrets, signed URLs and message history.
- `[p]snapshots auto true 6` enables automatic snapshots every six hours; `false` disables them. Intervals range from 1 to 168 hours, disabled by default. Unchanged settings are skipped.
- `[p]snapshots create`, `[p]snapshots`, `[p]snapshots diff <id>`, `[p]snapshots restore <id>` and `[p]snapshots delete <id|all>` manage checkpoints. Automatic capture excludes disabled cogs. Restore repeats current source permissions and live role/channel validation, then requires the same requester-bound preview button as manual restore. Slash equivalents are available.

The hub adds 16 slash actions. Live music cards, refreshed poll/event cards and retried logs use the current server theme. Automatic snapshots start disabled, retain up to ten records and skip unchanged settings.
