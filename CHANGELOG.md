# Changelog

Changes recorded here start with the repository's documentation and metadata pass. Earlier implementation history is available in Git commits.

## 2026-09-30: refactor and reliability pass

### Changed

- Split every cog into a small entry point, implementation, and constants while preserving Config identifiers, defaults, commands, and permissions.
- Use cached settings and bounded transient caches; event handlers respect Red's per-server cog disable setting.
- Calculate level thresholds with cumulative decimal formulas and logarithmic lookup, removing the implicit 5,000-level ceiling and exponential overflow.
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
