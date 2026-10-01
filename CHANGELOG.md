# Changelog

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
