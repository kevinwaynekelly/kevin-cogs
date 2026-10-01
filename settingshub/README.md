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

All commands require Red administrator access or Manage Server and run in a server. The bot needs Send Messages; Embed Links enables themed cards, and Attach Files is required for backups. `[p]` means your bot prefix.

| Text command | Slash command | Behavior |
| --- | --- | --- |
| `[p]settings` | `/settings panel` | Open a requester-bound cog picker that delegates to each cog's existing setup panel. |
| `[p]settings health` | `/settings health` | Inspect load/disable status, log delivery counters, local music packages/executables, and current-channel permissions. |
| `[p]settings backup` | `/settings backup` | Export eligible loaded cogs' selected server settings to JSON. |
| `[p]settings restore` with an attached JSON backup | `/settings restore file:<attachment>` | Validate and preview a same-server restore, then apply with the requester-bound button. |

Controls last three minutes and repeat the current command, member, server, and cog checks on every click. Unloaded/reloaded cogs require a fresh restore preview. The shared theme respects Red's embed preference and text fallback. Health reports detected prerequisites; it does not test a live Discord voice connection, YouTube extraction, or runtime version compatibility. Use `audiostatus` and the opt-in audio monitor for playback checks.

## Backup scope and restore

Backups contain guild music preferences; community roles/welcome/solo/tracking/digest settings; level formulas, XP source/exclusion policies, reward roles, farming and challenge settings; log routing, switches, delivery/history policies; and transformation settings/dictionaries/channel styles. Only loaded cogs the caller can currently configure are included. A backup can contain a subset of the five cogs; each included cog must have its complete supported settings shape.

Backups exclude global audio credentials/watchdog schedules and recipients, member probability overrides/opt-outs, XP/display names/achievements/challenge progress, playlists/favorites, participation/sticky member records, active poll/event records, retained log text, posted role-menu registrations, digest cursors, dated rankings, and temporary XP boosts. These records are preserved during restore. This is a configuration backup, not a full bot data backup.

Files are capped at 256 KiB. Restore rejects other server IDs, duplicate JSON keys, nonfinite values, unknown settings, invalid types/ranges/timezones, unavailable roles/channels, and unsafe automatic/self-service grant roles. Removed server IDs must be corrected or cleared before restoring. The JSON is validated before preview and again after acquiring settings locks. Refresh failures are reported separately after configuration is saved. Dynamic dictionaries are replaced; omitted operational/member fields stay intact. Restore uses the added sections' existing locks, saves prior values, and rolls back applied settings if a write fails. If storage also prevents rollback, the command reports affected settings for recovery. Legacy scalar setters retain their normal last-write behavior.

Restore clears settings caches, updates live audio fair-queue/autoplay preferences, refreshes community solo timers/role menus, and prunes log history to its restored policy. Existing Discord roles/messages and queued tracks are retained; reward-role reconciliation follows normal subsequent awards or the level sync command. Backups and previews are posted in the invoking channel, so choose an appropriate administrator channel.

## Stored data

SettingsHub has no persistent Config records. Requester/server IDs and restore previews are held in memory for up to three minutes and discarded on timeout/unload or a user-data deletion request. It exports no additional personal records. Dashboard messages and backup files posted to Discord remain managed there. The five source cogs retain their own documented data hooks.
