# Changelog

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
