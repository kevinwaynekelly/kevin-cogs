# Red Config storage audit

Red's JSON backend names each cog's database `settings.json`. It contains every scope saved through the Config API, including `GLOBAL`, `GUILD`, `MEMBER` and `USER`. `register_member(...)` does not create a separate member-data file. Some cogs also store server-wide member maps under `GUILD`. Filtering out only `MEMBER` would therefore still expose XP, listening history and snapshots.

These records support features such as seen/activity, sticky roles, XP leaderboards, replay and restore. They need to survive restarts. The misleading filename comes from Red's driver. With the JSON backend, a Config write saves the cog's whole database; a large member database also increases write work. This audit reviews source behavior, not the size or contents of a particular live bot database.

## Findings across all fifteen cogs

Limits below are per server unless described otherwise. A count limit on a collection is not a lifetime limit on the number of users.

| Cog | Configuration | Other persistent records | Bounds and lifetime |
| --- | --- | --- | --- |
| CommunityPlus | Roles, welcome/goodbye, voice cleanup, tracking, summaries, onboarding, birthdays and native-event policy | Member seen/presence/counters, sticky roles, participation, birthday/rules choices; room ownership, role-menu messages, polls and events | Lifetime member totals and named games have no automatic count/age limit. Keep 35 dated participation buckets as activity is added. Up to 50 rooms, 20 polls and 20 events, 1,000 participants per record and 100 private reminders; old completed/nonrecurring records age out after 30 days. |
| LevelPlus | XP formula and sources, restrictions, announcements, reward definitions, challenges, streaks and monthly-season policy | XP/name maps, earned badges/goals/streaks, daily cap counters, calendar XP, current season, archives and pending announcements | Lifetime progress intentionally survives leave/rejoin. Calendar XP retains a 35-day window when new awards prune it. Five top-50 season archives, five pending top-three announcements and up to 25 active custom earned goals per user. |
| AudioPlus | Music/DJ/request policies, continuity settings and global daily-check schedule | Listening history, personal playlists/favorites, shared playlists, queue checkpoint, check results/cursors/recipient; unused legacy node credentials | History: 100 records, 512 KiB and 30 days. Checkpoint: 100 tracks and seven days. Up to ten personal playlists of 100 tracks and 100 favorites per user; ten shared playlists with bounded approved/pending tracks. Saved collections have no automatic age expiry. Short-song files/manifest live outside Config in `songs/`, bounded to 2 GiB/2,000 songs bot-wide for three calendar months. Session summaries and PCM buffers are memory-only. |
| LogPlus | Routing, event toggles, rate/style, retry, history and alert policies | Opt-in event text/history, incident cases, daily aggregate counts and notification recipient | History starts off: up to 1,000 records/2 MiB for 1–90 days. Up to 25 cases/1 MiB for 90 days, with ten notes and twenty log copies per case. Summaries keep a recent window of eight daily buckets as events are recorded. Retry queues and error windows are bounded memory. |
| OwoPlus | Transformation policies, channel scopes/styles, custom dictionaries, haiku/Undo switches | Per-user probabilities and opt-outs; explicit poem submissions, contests and votes | Poems/contests: 1 MiB combined for up to 90 days, 100 hall submissions, ten contests of 50 entries/500 voters. Personal overrides/opt-outs do not expire automatically. Original transformed text is only held in memory for two minutes, up to 50 Undo records. |
| SettingsHub | Theme, audit policy and automatic snapshot schedule | Configuration change history, saved policy snapshots and delivery cursor | Audit: 200 entries/512 KiB, default 30 days. Ten configuration snapshots, each subject to the 256 KiB settings bundle limit; no age expiry until replaced or deleted. Snapshot copies are excluded even though their payload contains policy. |
| BackupPlus | Automatic backup interval | Server structure snapshots, latest attempt/error and restore state | Five manual, three automatic and two safety copies; 2 MiB per snapshot and 21 MiB total state. Manual/safety records persist until deleted, rotated or server removal. Includes permission overwrites with member IDs; never chat history. This is a significant potential Config size contributor. |
| EmojiStealerPlus | Capture/reaction/notification policies and optional channel | Source-to-server emoji mapping and latest error | At most 1,000 mapping entries; no member/message history. Emoji images are on Discord rather than saved as local image files. |
| IntroPlus | Enable, volume, cooldown and optional channel restriction | One personal clip selection/timing per member | Choices are in `MEMBER`; removal/privacy hooks clear them. Prepared PCM lives separately in `clips/`, bounded to 256 clips/128 MiB bot-wide. Queue/cooldown/result records are bounded memory. |
| PresencePlus | Global profiles, status/rotation, schedules and optional music source | Only an internal default-hint migration marker in Config | Configuration capped at 64 KiB, ten profiles, twenty entries per profile and twenty schedules. Rotation/error/last-sent state is memory-only. No member history. |
| DashboardPlus | Global listener enabled/bind/port, displayed URL and explicit allowed hostnames | No stored credentials or duplicate member/history database; reads reviewed source records on demand | Up to 32 hashed codes and 32 sessions in memory. Five-minute single-use codes; sessions expire at eight hours or thirty idle minutes. Stop/unload/restart revokes them. Paginated dashboard views do not prune or change source records. |
| DownloaderPlus | Optional webhook enabled/bind/port policy and private signing secret | Configuring owner/channel, up to 256 replay delivery IDs/digests, pending flag and latest update result | Payloads are never retained. Native Downloader owns repository/package records. Secret, recipient IDs, delivery cursors, pending flag and results are excluded from settings-only exports; deleting the configuring owner disables and clears the webhook. |
| NotificationPlus | Global collection enabled flag | Sanitized operational alerts outside Config in `alerts/unraid.json` | At most 64 records/seven days, pruned on writes/status/load, with ten-minute repeat suppression. Includes server IDs and exception types/codes, without member IDs, names, messages, media URLs or secrets. Unraid delivery keeps a separate private host cursor. Disabling clears records. |
| CorePlus | No persisted Config settings | No persistent member or command records | Help controls hold requester/channel IDs and page selection in memory for up to three minutes, then clear on expiry, deletion or unload. Native Core data remains in Red's own database. |
| ExportPlus | No persisted Config settings | Temporary readable chat files in `exports/` | No Red `settings.json` database. Files expire within 24 hours and clear on unload; 256 MiB per export, 1 GiB storage bot-wide and sixteen retained jobs. They belong outside a configuration repository. |

CommunityPlus's named-game dictionary and lifetime member records are allowed to grow as activity accumulates. Keeping them in Red's database is expected, and the filename does not imply they should be discarded or moved. The briefly introduced 100-game limit and reload pruning have been removed. If that version was already loaded and pruned an old catalog, restoring removed titles requires an earlier private database backup; removing the limit cannot recreate lost records.

`[p]community tracking false` stops new activity collection. It does not erase existing seen data, sticky roles or opted-in choices. Red user-data deletion/export hooks remain available; collection can resume after a deletion if tracking stays enabled. XP and saved collections are also intentional user data, rather than caches to erase during configuration cleanup.

## Dashboard data views

DashboardPlus's authenticated Data page reads selected records from each loaded suite cog, including member activity, XP, intro selections and listening history. It projects reviewed fields with current source-command checks and sends a bounded, searchable page to the owner. It does not copy raw Config databases, retain another member history, change source retention or remove old records. CorePlus and DownloaderPlus views show runtime and native inventory information without exposing private webhook settings; ExportPlus shows the requester's job metadata without chat files. The settings-only exporter below still excludes these records.

## Settings-only exports for a configuration repository

Use [scripts/red_settings_export.py](../scripts/red_settings_export.py) with Python 3.10 or newer. It uses only the standard library and does not require Red, Discord or any cog imports. Point `--source` at the directory containing `CommunityPlus/`, `LevelPlus/`, etc., using the parent of the actual cog data folders rather than Downloader's installed source folder. Missing/uninstalled cogs are skipped.

First inspect sizes without writing anything:

```sh
python3 scripts/red_settings_export.py --source /path/to/red/cogs
```

Then save the reviewed settings to a separate directory:

```sh
python3 scripts/red_settings_export.py \
  --source /path/to/red/cogs \
  --output /path/to/config-repo/kevin-cogs-settings.json
```

Substitute your persistent volume paths. The size report prints only cog names, byte counts and the number of skipped inactive namespaces. It never prints member records or setting values. Output is sorted and has no changing export timestamp, so unchanged policies do not create daily Git diffs.

The explicit nested allowlist covers all thirteen Config-using cogs and recognizes CorePlus and ExportPlus as having no Config policy. It includes configured roles/channels, welcome/rules/status text, custom style/goal definitions and schedules. It excludes all member/user scopes, XP/name maps, histories, queues, saved music, copied-emoji mappings, room ownership, posted-menu IDs, birthdays, personal opt-outs/overrides, temporary boosts, snapshots, audit records, runtime cursors/results, recipient user IDs and the legacy node password. Unknown fields and inactive namespaces are excluded by default. Text you intentionally put into a template/dictionary remains configuration; review that text before publishing an export.

Known fields with unexpected shapes, duplicate JSON keys, invalid server IDs, malformed JSON and source/output size violations abort the export. Source cog/file symlinks are refused. The source is read-only; output replacement is atomic, uses a private temporary file, and must be outside the source directory with a name other than `settings.json`. Failed validation or replacement preserves the previous export. Each input database is limited to 256 MiB and the output to 16 MiB. Export a stopped bot or a consistent filesystem snapshot when you need policies from several cogs to reflect exactly the same instant; individual live JSON replacements are atomic, but reads across cogs are not one transaction. This tool applies to the JSON backend; it does not read MongoDB.

The bundle identifies itself as `kevin-cogs/settings-only`, schema 1. **Do not replace Red's live `settings.json` with this bundle.** It is for reviewing/versioning configuration and excludes records needed for complete recovery. SettingsHub's existing `[p]settings backup` / restore workflow remains a separate validated, same-server policy backup for its six supported feature cogs. Keep complete bot/database/media backups privately when you want to recover XP, sticky roles, saved music, clips or structure snapshots.

Webhook signing secrets and failure outboxes belong with private application data and operational backups, never a settings repository.

`.gitignore` can ignore whole files/folders, never JSON fields. This repository now ignores raw `settings.json`, `songs/`, `clips/` and `exports/` to prevent accidental source commits. In a separate configuration repository, commit the generated `kevin-cogs-settings.json` and exclude raw databases/media there too. Adding an ignore does not untrack files already committed or remove old Git versions; replace those tracked copies in that repository after verifying the new export. Never delete the live files to reduce Git size.

## Maintenance

Review a new persistent field before extending the exporter allowlist. The tests compare reviewed policy/record scope against every cog's registered defaults and SettingsHub's six existing policy scopes, and exercise real Red JSON storage, large excluded member data, nested future fields/secrets, deterministic standalone exports, atomic failures and retained activity through reload/privacy hooks. A renamed file is not enough to establish a new boundary between policy and records.
