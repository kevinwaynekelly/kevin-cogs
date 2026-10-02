# BackupPlus

Named backups of server roles, channels and permission overwrites, with private restore previews and prefix/slash controls. Each package is independently installable through Red Downloader.

## Install

```text
[p]repo update kevin
[p]cog install kevin backupplus
[p]load backupplus
[p]slash enablecog backupplus
[p]slash sync
```

Use your repository alias if it is not `kevin`. `[p]` is the bot prefix, such as `!`. Enabling and syncing slash commands requires the bot owner.

All BackupPlus commands require the **server owner or Discord Administrator**, including when Red's owner/admin permissions would otherwise allow a command. The bot needs **Manage Roles** and **Manage Channels** for capture and restore, permission to apply the saved permission bits, and a role above any editable roles. Downloads and detailed previews arrive only by DM. Slash acknowledgements are private.

## First backup and restore

```text
[p]backup create baseline
[p]backup download baseline
[p]backup preview baseline
```

Read the privately delivered complete JSON plan, then run the restore command with its confirmation code:

```text
[p]backup restore baseline <code-from-preview>
```

The code belongs to the requesting administrator and server and expires after ten minutes. A changed snapshot, recovery mapping, server structure, bot role membership, Community status or bitrate limit invalidates it. Restoration saves a fresh `before-...` snapshot first. Restore that snapshot through another preview to reverse supported structural changes; objects created by a restore remain because this cog never deletes Discord objects.

## Commands

| Prefix command | Slash command | Purpose |
| --- | --- | --- |
| `[p]backup` or `[p]backup status` | `/backup status` | Snapshot count, automatic interval and latest restore/error status. Text alias: `backup progress`. |
| `[p]backup help` | `/backup help` | Command help. |
| `[p]backup create <name>` | `/backup create` | Capture a manual snapshot. |
| `[p]backup list` | `/backup list` | List names, times, types and object counts. |
| `[p]backup show <name>` | `/backup show` | DM scope, omissions and recovery status. |
| `[p]backup download <name>` | `/backup download` | DM a portable JSON snapshot using recovered IDs. |
| `[p]backup import <name>` with a JSON attachment | `/backup import` with `attachment` | Validate and retain a same-server snapshot without changing Discord. |
| `[p]backup preview <name>` | `/backup preview` | DM exact before/after changes, blockers, warnings and confirmation code. |
| `[p]backup restore <name> <token>` | `/backup restore` | Apply a fresh, reviewed plan. |
| `[p]backup delete <name>` | `/backup delete` | Remove one stored snapshot. |
| `[p]backup auto [hours=0]` | `/backup auto` | Set a 6–168 hour interval; zero disables. |
| `[p]backup bind <name> <role-or-channel> <source_id> <target_id>` | `/backup bind` | Manually associate a snapshot object with a matching existing server object after an uncertain create. |
| `[p]backup cancel` | `/backup cancel` | Stop a running restore; completed changes remain. |

Names use 1–32 lowercase letters, numbers, underscores or hyphens. `auto-` and `before-` names are reserved. An existing manual name is never overwritten; delete it explicitly or choose a new one.

## Snapshot scope

- Roles: IDs, names, permission bits, primary/secondary/tertiary colors, hoisting, mentionability and relative order.
- Channels: categories, text/announcement, voice, Stage, forums and media forums; IDs, names, positions, category relationships, NSFW and role/member permission overwrites, including uncached member targets.
- Text/forums: topics, slowmode, default thread archive duration and thread slowmode.
- Voice/Stage: bitrate, user limit, region and video quality mode.
- Forums: tags, default reaction, sort order, layout and required-tag setting. Existing tag IDs are reused where possible.

Managed roles, the bot's highest role and roles above it are protected and left unchanged; differences are shown as warnings. Editable backed-up roles retain their relative order within the available current role slots below the bot. Absolute positions around unrelated or managed roles can differ. Announcement/forum restoration requires Discord Community and voice bitrates must fit the server's current boost limit.

Snapshots exclude chat messages/history, thread/post contents, member role assignments, role icons, emoji/sticker/soundboard images, webhooks/tokens, bans, integrations, bot credentials, cog settings and server-wide settings. Unsupported channel types are explicitly listed as omissions. Use **ExportPlus** for chats and **SettingsHub** for selected cog policy backups. A recreated role receives a new ID and has no members until assigned separately; other cogs' saved role/channel references are not rewritten.

## Failure handling

Restoration uses original IDs and saved replacement mappings, never name matching. New channels are created with their complete overwrites in the creation request. Missing managed overwrite roles, permission loss, unsupported channel changes, capacity limits or loss of bot management access block a restore. Category changes that would silently change a synced channel outside the snapshot also block it; backed-up synced children have their explicit overwrites reapplied after their category changes.

Discord changes happen sequentially and cannot form a single transaction. The first failed operation stops the restore and records a partial result. Successfully created IDs are saved immediately. A timeout, cancellation or server error during creation leaves an uncertain marker instead of automatically creating another object on retry. Inspect Discord, then use `backup bind` to point that source ID to the existing object and generate a new preview. If the request definitively failed with a Discord client error, its uncertainty marker is cleared. Automatic snapshots do not attempt restores.

If an uncertain request created no object, inspect Discord first, then download that snapshot and import it under a new name to explicitly reset uncertainty. Downloads substitute all confirmed replacement IDs so re-importing preserves successful recovery work.

Each API operation has a 20-second deadline and a restore has a 30-minute deadline. Current administrator, Red command/parent/cog checks and bot permissions are repeated during restore. Cancel covers preparation and application; unload, server removal and user-data deletion also stop owned private transfers and close file buffers. Completed changes and recovery state remain after cancellation/unload. A restore still marked running after a restart is reported as interrupted; it never resumes automatically.

## Automatic backups and retention

```text
[p]backup auto 24
```

Automatic backups default **off**. The first runs after the configured interval; subsequent attempts use a durable last-attempt cursor, including across reloads. A failed attempt records a safe error type and waits until the next interval. The maintenance task checks once per minute, skips disabled cogs, and stops on unload.

Each server retains at most **five manual**, **three automatic**, and **two pre-restore** snapshots. Only automatic/safety snapshots rotate, oldest first; a snapshot being restored is protected from rotation. Each snapshot is limited to **2 MiB**, 250 roles, 500 channels and 10,000 overwrites; total server state is limited to **21 MiB**. Over-limit captures/imports leave existing records intact. Leaving a server clears its snapshots. Stored backups persist through normal reloads.

## Data and verification

Snapshots include member IDs appearing in permission overwrites. Red user-data exports return only that member's overwrite rows. User-data deletion cancels restores and removes whole snapshots containing that member so an incomplete permission set is never restored. Already downloaded files and Discord objects remain with their recipients/server.

Tests use actual Red command parsing, slash preparation, Config and Discord.py objects, with Discord networking mocked. They do not validate a live Scarlet deployment. Try capture, private preview and restoration in a test server before relying on a backup for recovery.
