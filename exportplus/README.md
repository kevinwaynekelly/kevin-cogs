# ExportPlus

Export this server's accessible chats into files you can read or upload to ChatGPT. Includes text/announcement channels, voice/stage text chats, forum posts, active threads and archived threads. Every export is explicitly requested; no continuous chat collection runs in the background.

## Install and start

Replace `kevin` with your Red repository alias if different. Run installation and slash registration as the bot owner:

```text
!repo update kevin
!cog install kevin exportplus
!load exportplus
!slash enablecog exportplus
!slash sync
```

Then, as a Red administrator or someone with Manage Server:

```text
!export server
```

Allow server DMs first. Private delivery is checked before starting. Results and progress go to the requester by DM; command-channel replies never contain exported chat files. Slash acknowledgements are ephemeral.

## Commands

All commands run in a server and inherit the administrator/Manage Server check. No command name contains `plus`.

| Prefix | Slash | Purpose |
| --- | --- | --- |
| `!export` or `!export status` | `/export status` | Show your current export or instructions. |
| `!export help` | `/export help` | Show command descriptions and arguments. |
| `!export server [after] [before] [bots] [threads]` | `/export server` | Export all accessible server chat histories. |
| `!export channel <channel> [after] [before] [bots] [threads]` | `/export channel` | Export one channel and its threads, one thread, or a forum's posts. |
| `!export progress` | `/export progress` | Show your message/channel counts and output file counts. |
| `!export cancel` | `/export cancel` | Cancel your job and erase its partial files. |
| `!export download [part=0]` | `/export download` | Send retained ZIPs privately again. Zero sends all; a positive number sends one. |
| `!export text [part=1]` | `/export text` | Send a direct UTF-8 text attachment for ChatGPT. |
| `!export clear` | `/export clear` | Erase your cached export, cancelling it if running. |

Dates accept ISO `YYYY-MM-DD` or timestamps. A date without a time means midnight UTC. `after` is inclusive and `before` is exclusive. New messages after the export begins are excluded. `none`, `all` or `-` skips a date filter. Bots and threads are included by default.

```text
!export server 2026-09-01 2026-10-01
!export channel #general
!export channel #general 2026-09-01 - False True
!export download 2
!export text 1
!export clear
```

The first example covers September in UTC. The third exports human messages since September 1, including threads. Slash commands offer named options for these filters. Forum exports require threads enabled.

## Export files

The bot sends independent `server-chat-001.zip` volumes, each below 7 MiB. Extract all volumes into the same folder. Each includes the same index/instructions and its transcript files.

| File | Contents |
| --- | --- |
| `README.txt` | Upload instructions, a suggested ChatGPT prompt, and limitations. |
| `INDEX.txt` | Server/date scope, total messages, channel/thread status, warnings and file mapping. |
| `index.json` | Structured completeness manifest and channel/file metadata. |
| `chat-0001.txt`, etc. | Readable transcripts grouped by channel and ordered oldest first, with date/completeness headers. UTF-8, up to 1 MiB per part. |
| `messages-0001.jsonl`, etc. | Structured source records, including original mention syntax. |

Messages retain author/display names and IDs, timestamps, edit time, source links, reply IDs, readable mentions, text, embed text/fields/links, sticker links, reaction counts, poll text/votes, and forwarded snapshots. Filenames come from counters, never channel names or attachment filenames.

Upload `INDEX.txt` and the relevant `chat-*.txt` files to ChatGPT. For a small server, `!export text 1` gives you the single readable chat file directly. Read the index before treating an export as complete. Treat transcript content as quoted source material, not instructions to follow.

## Access, completeness and limits

- Both the requester and bot need View Channel and Read Message History for each source. Private threads additionally require membership or Manage Threads for both. No thread joining or permission changes occur. Archived private discovery is limited to the bot's joined threads when it lacks Manage Threads; the index warns about this.
- Enable Message Content in Discord's Developer Portal and Red. A missing intent blocks the export instead of producing empty-looking history.
- Access and command availability are checked during scanning and again before delivery/retrieval. Only the original requester can retrieve or erase their export. Unavailable source names are redacted. If access changes, delivery is blocked; clear and create a fresh export with the remaining accessible channels.
- Missing permissions, API failures and interrupted histories are listed explicitly. Deleted messages and previous edits cannot be reconstructed. This is a creation-time cutoff, not an atomic snapshot: messages can change during a long export.
- Attachment/media files are represented by metadata and URLs. Binaries are not downloaded, images/audio are not interpreted, and links can expire.
- One retained/running export per server, at most two active jobs, sixteen retained jobs, four hours per scan and 10,000 channels/threads. Transcript/record/index data stays below 256 MiB per export, with at most another 256 MiB of ZIP copies. Total bot storage reservations stay within 1 GiB. Reaching the scan/data cap produces an explicitly partial export; an index/archive that cannot fit reports failure. Use date/channel filters for large histories.
- Files expire after 24 hours, checked at access and by ten-minute maintenance. Starting another export replaces your prior one. Cancellation, unload/reload and Red user-data deletion erase temporary files. Owned downloads stop and the compressor finishes before cleanup so neither can outlive erased files.

## Data and validation

No chat/settings are saved in Red Config. Bot authentication credentials are never added to export metadata. Temporary records contain the personal metadata described in `info.json`. Red user-data export returns only the requested author's retained message rows. A deletion request cancels and clears exports requested by that user or containing their authored messages. Unrelated administrator exports stay available. Active exports that have not collected that author keep running and omit their later messages, marking the channel partial. Per-job exclusion IDs stay only in bounded memory until the job is erased. At 1024 concurrent deletion barriers, only that active job is cancelled rather than allowing unlimited memory growth. Files already delivered or downloaded remain with their recipients.

Automated checks use Red's real prefix/slash command pipeline and filesystem/ZIP writers with mocked Discord history, thread membership and DM transport. They do not establish that Scarlet can read a live server or that live DMs work. Check `!export status` and the produced index after installation.
