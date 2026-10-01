# Changelog

Changes recorded here start with the repository's documentation and metadata pass. Earlier implementation history is available in Git commits.

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
