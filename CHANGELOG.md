# Changelog

## 2026-10-03: AudioPlus stream failure recovery

- Classify FFmpeg HTTP statuses, transient network errors, unsupported formats/protocols and unreadable local copies into fixed safe messages. Drain stderr continuously with a 16 KiB memory bound; reap children, join the owned drain thread and discard captured text on cleanup. Keep the independent IntroPlus decoder identical.
- Resolve a fresh provider stream once after a retryable decoder failure, preserving position, pause intent, queue and a single playback-start record. Bound recovery to one cache fallback plus one remote refresh; cancel recovery on skip/stop/unload and avoid refreshing direct URLs or unrelated/permanent failures.
- Allow configured HTTP proxy transport for remote music and intro FFmpeg playback/downloads without broadening local file protocols. Exercise real HTTP failures, headers, proxy transport, stderr floods, cleanup and retry state; external YouTube and Discord networking remain mocked.

## 2026-10-03: AudioPlus three-month short-song cache

- Automatically prepare audio-only local copies of known non-live provider songs strictly under five minutes after playback starts. The initial play keeps streaming; subsequent same-server plays skip stream extraction and read local audio. Preserve cached seek, pause, volume, normalization and IntroPlus overlays; retry a damaged copy from the provider once without duplicate start records.
- Keep complete copies across reloads/restarts in the persistent AudioPlus data directory, with fixed three-calendar-month UTC expiry, hourly/startup/access pruning, a 2 GiB/2,000-song bot-wide cap, 16 MiB file limits, two concurrent preparations and free-space reservations. Do not extend expiry on replay or evict unexpired copies to admit more songs. Daily YouTube checks bypass the cache.
- Add `[p]audiocache` and `/audiocache status`, administrator/Manage Server `clear` controls, diagnostic usage, safe notices and requester/server deletion/export hooks. Cache metadata excludes raw URLs, credentials and headers. Own/cancel download, validation and expiry tasks, including cancellation during process spawn.
- Cover real FFmpeg/FFprobe preparation, local decoding and restart reuse, exact duration/live boundaries, calendar expiry, coalescing, disk limits, corruption fallback, late-lookup invalidation, privacy cleanup and actual Red prefix/slash permission checks. Synchronize the independently installable IntroPlus resolver and PCM decoder.

## 2026-10-03: PresencePlus slash command hint

- Change the stock custom status to `Use /play or /search`, matching AudioPlus's real slash commands. Red's built-in help remains prefix-only.
- Migrate exact old stock entries in the existing `default` profile once on load, preserving custom text, other profiles, automation, availability and schedules. Save the migration version under the same global settings lock so later owner edits survive reloads.

## 2026-10-03: PresencePlus saved bot status

- Add independently installable PresencePlus with owner-only prefix controls and 24 slash actions, five activity types, availability, saved named profiles, rotation, dynamic placeholders and read-only previews. Loading starts disabled; valid configuration survives restarts.
- Add non-overlapping timezone-aware weekly schedules with overnight/DST handling and opt-in global listening status from one selected AudioPlus server. AudioPlus exposes only a read-only active title/listener snapshot; paused/stopped playback returns to the effective profile.
- Own/cancel the worker and pending writers, rate-limit/coalesce gateway updates, refresh after reconnects and restore the prior session presence when still owned. Bound records/text, validate before atomic Config commits and repeat owner/Red checks after writer waits.
- Document global visibility and data handling; validate all eleven cogs together at 401 slash actions and 85 roots. Tests use actual Red/Config/SDK behavior with mocked Discord gateway and voice transport.

## 2026-10-03: IntroPlus prepared local clips

- Download only each chosen intro segment in the background when a video, start or duration is saved; prepare existing choices on load. Play cached PCM files directly without YouTube resolution or FFmpeg startup on subsequent joins.
- Persist ready copies across reloads/restarts, bound the cache to 256 clips and 128 MiB, coalesce duplicate jobs, limit preparation to two concurrent jobs, and remove partial/orphan files. Show local-copy readiness and cache diagnostics through existing prefix/slash commands.
- Invalidate changed clips, remove cached audio with personal/server deletion and cancel owned subprocesses on interruption/unload, including cancellation during spawn. Preserve AudioPlus mixing, queues, pause and voice handoff. Verify real local FFmpeg downloads/replays; external YouTube and Discord voice transport remain mocked.

## 2026-10-03: IntroPlus personal voice entrance clips

- Add independently installable IntroPlus with personal YouTube videos, 0.5–30 second duration limits, start offsets, previews, manager assignment, cooldown/volume/channel controls and 16 slash actions.
- Reuse the native yt-dlp resolver, voice checks and extracted PCM decoder; enforce trimming in FFmpeg and decoded frame counts. Coordinate AudioPlus connections and hand off temporary intro voice sessions on music requests.
- Mix intros over same-channel active PCM music while preserving queue/progress, user pause and replacement sources. Bound pending joins and process lifetimes; cancel owned work on leave, clear, stop, unload and data deletion.
- Document the native backend's separation from Lavalink OAuth/remote-cipher settings, preserve credentials outside code, and check all ten cogs together at 377 slash actions and 84 roots. Discord/YouTube networking is mocked; local FFmpeg decoding is exercised.

## 2026-10-02: BackupPlus permission mask compatibility

- Fix backup creation rejecting server role permissions that contain reserved or newer bits absent from Discord.py's named flags. Keep bounded integer validation and existing restore authorization checks.
- Preserve raw role and channel overwrite masks through capture, JSON import/export and SDK restore requests, including uncached targets. Fail capture if the SDK's raw overwrite records are unavailable instead of silently discarding permission bits.
- Cover prefix/slash capture, JSON round trips and actual Discord.py create/edit serialization with mocked Discord networking.

## 2026-10-02: BackupPlus cancellation and recovery hardening

- Cancel restores during preparation as well as application, own private transfers by server, close their buffers on interruption, stop them on unload/server removal/user-data deletion, and cancel sibling discovery requests when an API fetch fails.
- Recheck administrator access after waiting for policy/deletion locks, report interrupted restores across reloads, preserve pending create markers, and keep the automatic scheduler alive after a transient storage failure.
- Verify Discord.py role color clearing payloads, recovered forum tag IDs, voice limits, same-server slash file imports and requester-bound confirmations using actual SDK/Red objects with mocked networking.

## 2026-10-02: BackupPlus server structure snapshots

- Add independently installable BackupPlus with 13 prefix/slash actions for bounded named server role/channel/permission snapshots, private JSON downloads/imports, exact restore previews and requester-bound confirmations.
- Preserve uncached member overwrites, restore supported channel settings and forum tags, protect managed/high roles, check hierarchy/permission/capacity prerequisites, and handle category permission propagation explicitly.
- Save a pre-restore snapshot, persist replacement IDs and uncertain create markers, stop on partial failures, and provide manual recovery bindings and cancellation. Keep automatic backups opt-in with bounded rotation and durable intervals.
- Add data hooks, same-server file validation, owned task cleanup and nine-cog command-tree/helper checks. Networking is mocked in regression tests; live Scarlet deployment is unverified.

## 2026-10-02: ExportPlus delivery and transcript hardening

- Put server/date/filter/completeness headers in every direct text file, redact unavailable channel/thread names, restrict local export directories, and prune expired exports when status is opened.
- Serialize private transfers, own/cancel retries on clear/unload, wait for compression before deletion, and reject stale job references. Keep exports privately retrievable after a Discord delivery failure.
- Pass 703 regressions on Python 3.10 and 3.11. Validate all eight cogs together at 348 slash actions across 82 roots. Cover channel converters, permission revocation, private retry, partial limits, expiry, user deletion, compressor/download cancellation and storage guards with real Red/filesystem code and mocked Discord boundaries.

## 2026-10-02: ExportPlus server chat exports

- Add independently installable ExportPlus with administrator prefix/slash exports for accessible server channels, voice/stage text chats, forums and active/archived threads.
- Stream chronological UTF-8 text and structured JSONL with an explicit completeness index, preserve message/reply/attachment metadata, filter dates/bots, and privately deliver bounded standalone ZIP volumes.
- Add requester-only progress/download/text/cancel/clear commands, preflight DMs, live access checks, bounded disk/jobs/deadlines, expiry, unload cleanup and author-only user-data exports.
- Validate real Red commands and filesystem/ZIP output with mocked Discord history/DM boundaries. Live Scarlet deployment remains unverified.

## 2026-10-02: Native event compatibility and occurrence bounds

- Treat missing Create Events flags in older Discord permission objects as failed prerequisites instead of crashing native/readiness commands. Creation uses Discord.py 2.4+; other Community features retain their compatibility.
- Refuse an additional active occurrence when five older mirrors remain unfinished, preserving all owned event IDs for later cleanup instead of exceeding or truncating the archive bound.
- Pass 683 regressions on Python 3.10/3.11 with the documented mocked Discord/provider boundaries.

## 2026-10-02: Native Discord events and support bundles

- Add opt-in owned Discord Events-tab mirrors, voice/external destinations, editable titles/times, automatic linking for new events, cancellation controls and gateway updates. Reconcile reloads/timeouts without duplicate creates, retain ongoing recurring occurrences until their end, and preserve separate local RSVPs/reminder consent.
- Add bounded support ZIP archives with whitelisted package/source/permission diagnostics, sixteen local readiness reports and transient same-server unexpected error types. Exclude member records, arguments, raw errors, paths, URLs, settings and log contents; keep failed audio diagnostics from blocking the report.
- Include EmojiStealerPlus capture policy in shared setup, backups, history and readiness while preserving copied emoji mappings. Keep all seven independently installable cogs within Discord's slash limits.
- Keep failed/timed-out music summaries from blocking voice cleanup and clear session controls when summary policy is restored off. Recheck queued emoji scope/reaction/permission changes and reuse known image aliases when emoji slots are full.
- Pass 681 regressions on Python 3.10 and 3.11, plus lint, formatting, syntax, metadata and whitespace checks. Validate 339 slash actions across 81 roots. Discord, CDN and media-provider boundaries are mocked; live Scarlet deployment remains unverified.

## 2026-10-02: Reward previews and music session summaries

- Add a read-only level/reward scenario command with optional formula, cap, milestone and stacking candidates, cached-member totals, bounded details and permission/hierarchy warnings. Share the exact reconciliation role plan and preserve custom earned rewards and removed-definition assignments.
- Add opt-in summaries after native voice departure with bounded transient track/requester records and decoded playback accounting across seek/rejoin segments. Add three-minute personal playlist saving with checked prefix/slash commands, a modal button, expiry, collection limits and user-data hooks.
- Extend shared component contexts with optional modal acknowledgement and source-message fallback; retain identical helpers and all existing command checks across seven independently installable packages.

## 2026-10-02: EmojiStealerPlus

- Add an independently installable cog that automatically captures external static and animated custom emoji from member messages, edits and reactions, enabled by default.
- Add checked prefix/slash capture controls, channel/thread scope, manual yoink and optional themed notices. Preserve messages and respect current Create Expressions permission and separate server emoji capacity.
- Bound CDN downloads and upload deadlines, queue/deduplicate captures and image fingerprints, track and cancel the owned worker/session, and document nonpersonal mapping storage and Discord/provider test boundaries.

## 2026-10-01: Configuration history and feature readiness

- Add selected server-settings change history with task-local caller attribution, real Config write/clear observation, before/after values, prefix/slash/component/restore coverage, and per-instance unload cleanup. Preserve successful settings writes if supplementary history fails.
- Bound retained history by age/count/bytes and value previews; enforce current setup visibility and identified-user export/deletion. Exclude global credentials, member/operational data and inherited background attribution.
- Add fourteen read-only readiness reports with optional candidate channels/roles, actual destination permissions, safe role hierarchy, intents, room category, scope, snapshot budget and native music prerequisites.
- Add matching slash controls and command-browser examples while preserving all independently installable cogs.

## 2026-10-01: Listening history and request limits

- Add server listening history with stable replay IDs, request attribution, 30-day/count/byte retention and user-data hooks. Record playback starts without duplicating seek/reconnect/recovery resumes; repeat playback records a fresh start.
- Add all-or-nothing duration and active-request limits under the player lock, covering search, playlists, favorites and replay with configured DJ, manager and owner exemptions. Defaults leave requests unrestricted.
- Add text/slash controls, original play permission checks, backup range validation and mocked Discord/media regression coverage.

## 2026-10-01: Integration and command discovery

- Apply server themes to live music and community card edits, PNG rank cards, daily playback/error DMs and log delivery retries. Keep base retry cards unchanged and reserve Unicode footer space within Discord limits.
- Add concrete command-browser examples and check disabled/permission-restricted shortcut sources. Include all six cogs and installed-source fingerprints in safe diagnostics. Keep large participation/poetry payloads out of routine policy caches and prevent preview styles leaking between channels.
- Correct reward-role backup validation to role ID → level and refresh live audio normalization after restore. Validate all six cogs' slash payload names, nesting, option counts and 77 roots; document 313 available slash actions.
- Pass all 583 regressions on Python 3.10 and 3.11, plus lint, formatting, syntax, metadata and whitespace checks. Discord transport/provider access is mocked; real local FFmpeg/native-library checks run, and live Scarlet deployment remains unverified.

## 2026-10-01: Custom styles, reversible transformations and haiku activities

- Add bounded named style dictionaries/decorations and accept custom names in existing channel styles and previews, preserving protected code, links, mentions and emoji.
- Add two-minute author-only Undo controls on separate bot cards with current permission checks, attachment-preserving text restoration, failure rollback and unload/privacy cleanup. Keep successful reposts when control delivery fails or unload starts after original deletion.
- Add explicitly submitted, moderator-approved haiku collections and timed contests with unique votes, deterministic winners, owned maintenance, retention/byte limits and user-data hooks.
- Validate haiku and incident storage budgets before writing. Red dictionary contexts otherwise save mutations even when their body raises a validation error.

## 2026-10-01: Moderation alerts and incident cases

- Add opt-in threshold/window alerts for joins, logical deletion events and actual permission edits, plus aggregate daily moderation summaries independent of delivery retries.
- Add private owner-configured grouped unexpected command error notifications with current-owner checks and sanitized signatures.
- Add staff incident cases with retained-log excerpts, notes, resolution, count/byte/age limits and identified-user export/deletion.

## 2026-10-01: Custom progression and monthly seasons

- Add administrator-defined achievements with safe role and once-only XP rewards, bounded local-day streak bonuses and member views. Serialize earned rewards with existing XP updates and daily caps.
- Add automatic monthly closure/winner announcements with bounded archives while preserving lifetime XP, plus role-filtered calendar/lifetime boards.
- Extend configuration backup validation and user-data hooks for definitions, counters and pending summaries.

## 2026-10-01: Community rooms and participation tools

- Add bounded join-to-create voice rooms with owner admission/name/capacity controls, restart cleanup and a creation guard for delayed voice state updates.
- Add explicit rules acceptance with safe member roles and opt-in month/day birthday notices with tracked temporary roles and user-data hooks.
- Add capacity waitlists and recurring events that promote cancelled slots and advance missed dates without replaying old attendance or reminders.

## 2026-10-01: Audio continuity and shared playlists

- Add opt-in seven-day queue checkpoints, explicit DJ recovery, empty-room auto-pause/graceful departure and FFmpeg loudness normalization. Preserve the exhausted-queue ten-second departure and manual pause decisions.
- Add bounded collaborative server playlists with member proposals, DJ approval/rejection and requester-aware privacy hooks. Keep resolved stream URLs out of persisted records.
- Validate real FFmpeg decoding with normalization, Red command registration and mocked Discord recovery paths; live deployment remains unverified.

## 2026-10-01: Shared themes and configuration checkpoints

- Add server-wide semantic theme colors and footer branding through an optional runtime protocol; source cogs retain independent installation and default presentation.
- Add permission-aware command search, safe diagnostic downloads, and opt-in automatic snapshots with ten-record retention, unchanged-setting detection, comparisons, deletion and validated previewed restoration.
- Exercise Red command registration and Config locks with mocked Discord transport; live Scarlet deployment remains unverified.

## 2026-10-01: Incompatible native voice API repair

- Reject importable but incomplete native voice libraries before connecting, including the reported missing `davey.DAVE_PROTOCOL_VERSION`. Validate Discord's cached library references as well as current imports without reloading Discord classes or disabling DAVE encryption.
- Share API checks with fresh repair verification, let owner-only audiorepair reinstall incompatible packages, and report native API status separately from package versions. Keep working libraries and Downloader update requirements unchanged; require restart after an install attempt.
- Reproduce the missing protocol constant with Discord's import flags already enabled, invalid cached bindings, missing encryption/gateway methods, and a shadowing incomplete Downloader copy. Exercise the real Red play/repair command pipeline with mocked install/network boundaries and retain real native encryption/session regression checks.
- Validate 535 tests on Python 3.10/3.11, plus lint, formatting, syntax, metadata, and whitespace checks. Live Scarlet playback remains unverified.

## 2026-10-01: Playback check failure diagnostics

- Replace generic monitor/player failures with safe exception types, structured codes/names, and bounded traceback locations. Identify server setup, dependency checks, YouTube lookup, channel selection, voice connection, native playback, and cleanup stages without exposing arbitrary exception messages or signed stream URLs.
- Preserve the primary playback error when cleanup also fails and postpone active voice-library repair. Show the saved daily-check result in audiostatus after its temporary player closes; clear the previous result when moving the monitor to another server.
- Validate 527 tests on Python 3.10/3.11, plus lint, formatting, syntax, metadata, and whitespace checks. Test actual local FFmpeg/Opus probes with mocked Discord/provider boundaries; live Scarlet playback remains unverified.

## 2026-10-01: Native voice dependency repair

- Add bot-owner-only `[p]audiorepair` for unimportable PyNaCl/davey, installing binary wheels in the running Red interpreter rather than Downloader's update target. Keep working voice libraries and normal cog-update requirements unchanged.
- Verify fresh imports using Red's package search order, report import status independently from installed versions, and point playback errors to the repair command. Block new AudioPlus connections during repair and until Red restarts, including across cog reloads.
- Refuse repair during voice sessions/lookups, bound subprocess time/output, sanitize failure replies, and terminate owned children on timeout/unload, including cancellation during spawn. Document the Discord repair path and manual container setup.
- Validate 508 tests on Python 3.10/3.11, plus lint, formatting, syntax, metadata, and whitespace checks. Pip installation and Discord/provider transport remain mocked; fresh native import probes and subprocess cleanup run locally.

## 2026-10-01: Shared settings and discovery integration

- Add optional independently installable SettingsHub with a shared setup picker, local health report, configuration backups, and validated requester-bound same-server restore previews. Preserve member/operational data, replace custom maps, reject unsafe inputs, repeat current permissions, and roll back failed writes.
- Refresh command overviews, shared labels, guides, metadata, and CI syntax coverage for all discovery additions. Validate 236 slash actions and 485 tests on Python 3.10/3.11 with actual Red command/config APIs and mocked Discord/provider boundaries. Keep the original 209-command compatibility surface and five source cogs independently installable.

## 2026-10-01: Opt-in retained log history

- Add administrator member timelines, text/category/date searches, filtered JSON/CSV exports, and slash equivalents behind original log permissions. Capture one logical accepted event before delivery without duplicating retries.
- Start collection disabled, bound records by age/count/bytes, prune on retention changes and hourly even for inactive servers, cancel owned maintenance on unload, and export/delete retained user records. Exclude history payloads from normal routing caches.

## 2026-10-01: Channel styles and automatic expiry

- Add inherited Owo, pirate, and robot channel styles, permanent or expiring overrides, administrator slash controls, and a manual member stylize preview. Preserve code, links, mentions, emoji, opt-outs, and haiku priority.
- Recheck expiry after rendering and stop late transformations on unload. Merge bounded style defaults without changing legacy settings or storing message contents.

## 2026-10-01: Achievements, challenges, and rank cards

- Add seven earned badges, configurable optional weekly goals, member status commands, and administrator controls with slash equivalents.
- Serialize rewards with XP updates, respect caps/boost/source policies, avoid reward recursion, preserve unpaid earned rewards across reloads and week changes, and export/delete added member records.
- Add locally rendered themed PNG rank cards with original show permissions, bounded rendering, and a declared Pillow dependency. Preserve the existing text rank command and independent cog installation.


## 2026-10-01: Community polls and events

- Add persistent member poll voting and event attendance selectors, administrator creation/closing, offset-aware event times, optional private reminders, and matching slash actions.
- Bound active/retained records and participant counts; close/prune expired records, persist successful reminder delivery, recover selectors after reload, and stop owned deliveries on unload. Export/delete personal votes, RSVPs, reminder settings, and creator attribution.


## 2026-10-01: Music discovery and fair queues

- Add a requester-bound search-results picker with fresh permission checks and matching slash commands. Selection joins voice only after choosing a track.
- Add optional round-robin requester queues and artist-based autoplay, both disabled by default. Preserve requester order through reconnects, exclude recent sources, bound lookups, and invalidate late suggestions on stop/disconnect/unload.


## 2026-10-01: Community reload timer guard

- Prevent late voice events and settings callbacks from creating solo timers after the cog unloads. Mark the instance closed before cancelling owned tasks and guard every timer creation.
- Reproduce the late-event leak, verify the fix, and run the full 400-test suite on Python 3.10/3.11 with lint, formatting, syntax, metadata, and whitespace checks.

## 2026-10-01: Feature integration and final validation

- Unify new control labels and command overviews, add guided setup to every cog, bind panels to their requester/server, and keep presentation/interaction/permission helpers identical for independent installs. Refresh the actual-payload visual preview for native audio.
- Tighten self-service/reward role policies against message/thread/event/voice moderation, mass mentions, and private audit/insight permissions. Share section locks between scalar controls and compound policy/collection updates.
- Validate all 202 slash actions beside Red Core, root/options/depth/name limits, member versus administrator prefix/slash permissions, original 209-command compatibility, queue shutdown races, and data hooks. Run 399 regression checks on Python 3.10 and 3.11, plus lint, formatting, syntax, metadata, whitespace, and rendered preview checks. Update the repository overview, individual guides, development instructions, and Downloader metadata.

## 2026-10-01: Log feature expansion

- Add channel overwrite Allow/Deny/Inherit diffs, role permission/display changes, visible server-setting changes, and raw uncached message edit/delete coverage without duplicating cached handlers or inventing missing text/authors.
- Add category destinations behind source/thread routes, administrator exemption controls, guided setup, and slash equivalents. Merge additive defaults without changing original switches or routes.
- Add bounded memory delivery queues, three delayed retries of only unsent parts, current routing/disable/exemption checks, safe error counters, user-data cleanup, and worker cancellation. Validate partial embed/text failures, route precedence, queue/expiry limits, permission-only changes, and raw-event coverage with mocked Discord transport.

## 2026-10-01: Community feature expansion

- Add safe self-service role menus with persistent message registration, bounded offers/menus, current clicker checks, and refreshed options. Keep unrelated and no-longer-offered roles unchanged.
- Add solo channel/role exemptions, optional warnings without extending deadlines, DM and tracking controls, voice duration checkpoints, 35 daily participation buckets, and opt-in weekly digests with saved cursors.
- Add direct/slash roles and voicehours plus guided setup. Merge additive guild/member defaults, preserve existing settings and counters, and include duration/rollup data in user-data hooks. Validate role escalation prevention, reloads, DST date boundaries, timer cancellation, tracking switches, summary failures, and original command compatibility.

## 2026-10-01: Owo feature expansion

- Add all-channel/allowlist scopes, exclusions with thread inheritance, persistent personal opt-outs, member owoify/haiku commands, and requester-bound guided setup. Manual transformations leave source messages alone.
- Add custom whole-word replacements, optional keyword triggers, fixed or automatic intensity, bounded per-member repost cooldowns, and server-local syllable corrections. Preserve code and original messages when replacement fails.
- Merge additive feature defaults without changing existing settings; export/delete opt-outs and probability overrides. Validate scope precedence, concurrent failure reservations, cross-server dictionaries, pronunciation overrides, and original command compatibility.

## 2026-10-01: Level feature expansion

- Add milestone reward roles with stacking/highest-only policies, hierarchy checks, current-level reconciliation, and synchronization independent of announcement settings.
- Track earned XP in 35 daily buckets and the current season, expose weekly/monthly/season leaderboards, and retain five top-50 season archives without resetting lifetime XP.
- Add scoped expiring earned-XP boosts, optional repeated-message/reaction farming limits, minimum word counts, persistent shared daily caps, and guided setup with matching slash actions.
- Preserve legacy defaults and XP through additive merged sections, maintain common XP locks across event sources and voice batches, and export/delete added member records. Validate time boundaries, caps across reloads/concurrent sources, scoped boosts, archives, rewards, and existing command compatibility.

## 2026-10-01: Audio feature expansion

- Add automatically updating now-playing panels with checked Discord buttons and embed/text fallback. Cancel panel tasks/views on disconnect or unload and keep the ten-second idle deadline.
- Add direct/slash seek, queue remove/move, private saved playlists and favorites, and administrator audioset controls/setup. Bound saved collections and include them in user-data export/deletion.
- Add opt-in DJ controls and listener vote skipping, retain open controls by default, count unique current listeners, and protect active players from unauthorized moves. Preserve legacy command arguments and settings with additive guild defaults.
- Validate resumed seeking, queue ordering, collection isolation/deletion, votes, panel updates, native command registration, and existing regression coverage. Live Discord/provider boundaries remain mocked.

## 2026-10-01: daily AudioPlus playback checks

- Add owner-only `audiocheck` setup/status, immediate test, disable, schedule, and video commands. Default to 09:00 America/Chicago with DST handling and DM the owner who enables checking in the test server. Verify setup DM delivery before enabling.
- Probe a public YouTube video through the existing resolver, FFmpeg/native player, and Discord audio thread for three silent seconds. Disconnect and clean up afterward. Use automatic voice-channel selection or a configured ordinary channel, respect guild disable settings, and postpone busy/foreign voice connections or retained queues for 15 minutes.
- Persist one daily cursor, the latest safe result, and pending DM delivery. Stay quiet on success, send failure alerts privately, retry undelivered alerts every 15 minutes, and cancel owned tasks/probes on disable, settings changes, or unload. Catch up once after downtime without replaying missed days.
- Add an opt-in global `watchdog` Config section through Red's merged defaults, preserving all legacy settings and defaults. Implement recipient data export/deletion without exposing the legacy node password or signed streams. Update guides and Downloader data statements.
- Exercise local/DST schedules, reload persistence, duplicate prevention, busy connections, blocked DMs, command permissions, cancellation, decoder failures, and real FFmpeg/Discord audio-thread/Opus playback against local HTTP audio. External YouTube access and Discord networking remain mocked.

## 2026-10-01: LevelPlus slash synchronization fix

- Give `level formula calibrate` the valid lowercase slash options `level1`, `xp1`, `level2`, and `xp2`, with descriptions. Capitalized option names previously caused Discord to reject the entire slash synchronization request with HTTP 400. Preserve the text command arguments, permission checks, calibration behavior, and saved settings.
- Validate serialized command, subcommand, parameter, and localized names across all five cogs. Reproduce the rejected names before the fix and exercise renamed slash options and unchanged text calibration through Red's real command pipeline, including permission denials. Discord networking remains mocked.

## 2026-10-01: direct commands and slash groups across the cogs

- Rename public `com` to `community`, `logplus` to `log`, and `owoplus` to `owo`, without old-name aliases. Keep `level`, cog package/class names, Config identifiers, defaults, XP, and member records. Document reapplying custom Red rules that used old command paths.
- Add direct `rank`, `leaderboard` (`lb`), `levellookup`, `seen`, `seendetail`, `activity` (`stats`), `seenlist`, `logstatus`, `logchannel`, and `lograte`, with slash counterparts and readable help.
- Add 109 slash actions across CommunityPlus, LevelPlus, LogPlus, and OwoPlus, including status panels, welcome/role settings, XP sources/editing/imports, log routing, and transformations. Keep deeper prefix branches and exact user-ID arguments available as text commands.
- Add `log event` with slash autocomplete for 45 event switches, including scheduled events. Omitted booleans inspect settings; supplied booleans set them using the same section lock as existing toggles.
- Check every parent permission and disabled state for slash requests. Direct shortcuts also check the original grouped command, preserving Red permission rules. Defer authorized slash requests before settings I/O and restore context state on all failure paths.
- Exercise all-cog registration beside Red Core, slash payload limits, converted options, regular/admin/owner permissions, disabled commands, original-path rules, nested prefix commands, and unload/reload with mocked Discord transport. Update guides, metadata, and native help examples.

Changes recorded here start with the repository's documentation and metadata pass. Earlier implementation history is available in Git commits.

## 2026-10-01: AudioPlus automatic voice selection and idle disconnect

- Let direct, slash, and legacy play commands work when the requester is not in voice. Select the available ordinary voice channel with the most people, excluding bots and the AFK channel, and respect channel access and member limits. Keep the requester's own voice channel preferred when present.
- Disconnect after 10 seconds with an empty queue and no active track or lookup. New songs and play/tone searches cancel the timer. Paused tracks, stream preparation, and repeating playback keep the connection active; empty or failed searches release their reservation and allow a fresh idle countdown.
- Keep timers per guild, recheck ownership/activity at expiry, cancel them on disconnect/unload, and reconnect normally for the next play command. Preserve all command names, arguments, checks, and saved Config defaults.
- Exercise timer expiry/cancellation, concurrent searches, voice-disconnect events, channel choice, permissions, and reconnects with real player/command lifetimes and mocked Discord transport. Live server behavior still needs a deployment check.

## 2026-10-01: AudioPlus updates with existing voice libraries

- Treat PyNaCl and davey as required bot-level voice prerequisites instead of reinstalling them into Downloader's package folder on every cog update. Downloader still installs yt-dlp and its matching EJS package. Existing usable voice libraries, including copies installed by Downloader, remain supported.
- Document one-time installation into Red's Python environment, the verified PhasecoreX `/data/venv` path, and recovery from a requirements failure that prevents updated cog files from being copied. Missing or unimportable voice libraries still block playback and diagnostics explain what to install.
- Check Red's actual requirement/update/copy flow with a failing native-package installer and verify that a media-package installation failure still stops an update. No changes to Red's installer or to a running user's container are made by this update.

## 2026-10-01: AudioPlus command loading checks

- Exercise prefix messages through Red's actual context parser, permission requirements, and command handler for owners and ordinary members. Check query errors, aliases, converted volume arguments, playback controls, and registration after removing and replacing the cog.
- Document explicit reloading when Downloader reports that installed files are already current, plus `help play` and repository/path checks for silent unrecognized commands. Prefix controls do not depend on slash enablement.
- Discord/media transport remains mocked. These checks verify current source behavior and do not identify which version or configuration is running on a remote bot.

## 2026-10-01: AudioPlus direct and slash commands

- Add 20 direct playback, voice, and diagnostic controls with matching slash commands, including `play`, `skip`, `pause`, `np`, and `queue`. Both interfaces use the existing player and themed track confirmations.
- Acknowledge slash requests before voice connections or media lookups. Offer repeat modes as slash choices and keep optional volume/repeat arguments.
- Preserve legacy `audio` commands, aliases, Config, and checks. Use `disconnect` for music so Red's core `leave` command keeps its existing behavior. Keep legacy owner-only node settings out of slash commands.
- Update the overview, footers, diagnostics, guides, and Downloader metadata for direct controls. Document Red's owner-only `slash enablecog audioplus` and `slash sync` setup.
- Check registration and removal with a real Red command tree, plus prefix/slash callbacks and response timing with mocked Discord transport.

## 2026-10-01: AudioPlus track confirmations

- Identify queued tracks with title, artist/uploader, duration, and a clickable source link instead of only reporting a track count. Preview the first five playlist entries and preserve details in plain-text replies.
- Give direct-audio tests the same track confirmation and add source links to the current-track display. Resolved, signed playback URLs remain private.

## 2026-10-01: AudioPlus voice initialization

- Load PyNaCl and davey installed in Red Downloader's private package folder after Discord.py's first import. Initialize the existing voice client, state, and gateway bindings without reloading Discord.py or replacing its classes.
- Report actual Discord voice readiness separately from installed package versions and identify a native library that genuinely cannot import.
- Add system-wide Deno installation commands for the Red container and explain that only one supported JavaScript runtime is needed.
- Exercise the startup ordering failure in isolated processes with real NaCl packet encryption, DAVE key generation, gateway processing, and missing-library failures. Live Discord networking remains outside these regression checks.

## 2026-10-01: AudioPlus native playback

- Replace Wavelink/Lavalink playback with per-guild native Discord voice, yt-dlp media resolution, and FFmpeg decoding inside Red. No Java node or YouTube plugin is needed.
- Resolve streams at track start, preserve pause/volume/queue state, advance once from Discord audio-thread callbacks, and cancel lookups and decoders on skip, stop, leave, or unload.
- Add repeat modes, bounded queues/playlists, lookup timeouts, useful provider/decoder failures, and reconnect recovery that retains tracks after a failed rejoin.
- Preserve all original command names, aliases, arguments, checks, Config identifiers, and defaults. Legacy node settings remain stored for rollback and are unused; node commands now report local status or manage preserved settings. `ytmsearch:` maps to regular YouTube search and unsupported Lavalink plugin prefixes report a clear error.
- Require Red 3.5.24+, yt-dlp with EJS, PyNaCl, davey, FFmpeg, libopus, and a supported JavaScript runtime. Restart Red after voice dependency installation. Include a PhasecoreX container image recipe and upgrade instructions.
- Replace Lavalink regression boundaries with native player tests, real yt-dlp/FFmpeg local media checks, and process cleanup coverage. CI uses Python 3.10/3.11. Live YouTube and Discord voice still require a deployment smoke test.

## 2026-10-01: AudioPlus playback fixes

- Preserve explicit source searches such as `scsearch:` instead of rewriting them as YouTube queries; reject empty searches before connecting to voice.
- Start an idle player's queued track unpaused, preserve a paused current track, and retain queue order when a playback request is rejected or cancelled.
- Report asynchronous track failures in the request channel, recover stuck tracks with a single skip request, and keep Wavelink responsible for queue advancement.
- Show source managers, installed plugin versions, and the latest playback failure in node diagnostics. Read voice connectivity from Lavalink's nested player state and require a loaded track before reporting playback.
- Add regression coverage with real Wavelink players and a local Lavalink HTTP test server. Live YouTube playback still depends on the Lavalink node's source configuration.

## 2026-10-01: unified Discord presentation

- Give all five cogs a shared indigo theme, consistent headers and footers, readable settings, and matching success/warning/error colors.
- Send command confirmations as themed messages and add a sectioned AudioPlus command overview.
- Paginate long descriptions, lists, and preview fields within Discord's UTF-16 limits; send export attachments only once.
- Respect Red's embed preference and provide readable text when Embed Links is unavailable.
- Theme event notices and DMs while preserving custom template text, level-up mention behavior, and OwoPlus webhook content.
- Keep each cog independently installable with its own copy of the theme helper, checked for consistency by tests.
- Add descriptions to all 209 commands and groups so Red's native help menus show meaningful summaries and detailed command help.

## 2026-09-30: refactor and reliability pass

### Changed

- Split every cog into a small entry point, implementation, and constants while preserving Config identifiers, defaults, commands, and permissions.
- Use cached settings and bounded transient caches; event handlers respect Red's per-server cog disable setting.
- Calculate level thresholds with cumulative decimal formulas and logarithmic lookup, removing the implicit 5,000-level ceiling and exponential overflow.
- Preserve the configured multiplier during linear calibration and reject negative, overflowing, or imprecise fits before changing saved settings.
- Read XP per user, update it under consistent locks, batch voice awards, and use top-N selection for leaderboards and seen lists.
- Parse real quoted CSV, preserve integer XP precision and custom aliases, and reject ambiguous name imports.
- Scope solo voice timers to server/member, preserve existing deadlines, recheck eligibility, and cancel timers on disable/unload.
- Load AudioPlus without requiring a node, reuse an owned HTTP session, reconnect only its node, restore playback state on voice rejoin, and let Wavelink advance queued tracks.
- Render OwoPlus outside the event loop, reuse owned webhooks, preserve all supported attachments, and roll back partial reposts when sending or original deletion fails.
- Cache recent audit entries, correctly normalize Discord audit-action enums, enforce embed limits, and keep logging diagnostics read-only.
- Activate thread/presence and scheduled-event logging; retain `commands_` and `thredupdate` as aliases for corrected names.
- Implement Red user-data export/deletion for CommunityPlus, LevelPlus, and OwoPlus.

### Added

- Regression tests with real Red Config storage and command classes, including all 209 existing commands and saved Config defaults.
- Ruff checks, a Python/Wavelink compatibility CI matrix, contributor test instructions, and a reproducible level benchmark.

### Upgrade notes

- Existing XP and settings remain in their original Config namespaces. Decimal half-even thresholds can differ from old floating-point totals by one XP at exact rounding boundaries; default linear levels 2,201 and 3,000 are examples.
- Reaction cooldowns now cover author awards as well as reactor awards. Channel/role and thread/forum restrictions apply consistently across XP sources; component interactions no longer award slash XP.
- Scheduled, thread, and presence logs now receive their actual Discord events. Their existing enabled defaults apply once a destination is configured.
- Node configuration reconnects immediately and disconnects AudioPlus players. Queue tracks again after changing nodes.
- User-data hooks clear Config records, not already-posted Discord log/webhook messages. Live Discord/Lavalink behavior still requires a deployment smoke test.

## 2026-09-30: documentation and metadata

### Added

- Repository README with cog overview, installation/update instructions, defaults, and data-storage details.
- Setup and command guides for AudioPlus, CommunityPlus, LevelPlus, LogPlus, and OwoPlus.
- Repository and cog `info.json` files for Red Downloader, including AudioPlus dependencies and cog data statements.
- Contributor guidance and a Python-focused `.gitignore`.

Cog implementations are unchanged in this pass.
