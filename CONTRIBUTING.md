# Contributing

Keep changes focused and describe the behavior being changed and how it was checked. Commit useful checkpoints as you go, especially during a pass across multiple cogs. The repository owner prefers publishing validated maintenance changes directly to `main`. Use a working branch when isolation helps, then merge its completed pull request after checks pass.

## Layout

Each cog is independently installable through Red Downloader:

- `__init__.py`: asynchronous `setup(bot)` entry point and public cog export.
- `cog.py`: commands, event listeners, and lifecycle management.
- `constants.py`: persistent defaults and presentation constants.
- `info.json` and `README.md`: Downloader metadata and the user guide.
- `events.py`, where present: Red's per-server disable check for listeners.

AudioPlus isolates subprocess media resolution in `resolver.py`, native queue/playback in `player.py`, system dependency checks in `backend.py`, and daily scheduling/private alerts in `watchdog.py`. Audio/Level/Community/Owo feature policies live in their own `features.py` modules. Community participation and persistent role pickers remain separate from its event commands. LogPlus isolates bounded delivery in `delivery.py` and permission formatting in `diffs.py`. LevelPlus isolates threshold calculations in `levels.py`. OwoPlus isolates syllable counting and haiku detection in `haiku.py`. PresencePlus isolates bounded profile/template/schedule validation in `profiles.py` and owned gateway rotation/restoration in `controller.py`. Its bot-wide configuration uses one global section lock and explicit owner rechecks; selected-server music is a read-only AudioPlus protocol. Keep cog modules self-contained; Downloader can install one cog without the others.

AudioPlus's `dependencies.py` owns explicit owner-triggered native voice repair. Keep package specs fixed, install with the bot's interpreter in isolated mode, verify with Red's actual import precedence, bound output/time, and clean up children on timeout/cancellation, including during spawn. `voice_libraries.py` shares the native API checks between the running bot and the fresh verification process. Validate the APIs actually used by the pinned Discord.py and its cached library bindings before opening voice; a successful import or installed distribution version is insufficient. Keep compatible libraries usable without reloading Discord modules or disabling DAVE. Never install packages from playback or ordinary updates. Prevent repair during voice connections/lookups, require restart after attempted changes, and preserve that in-memory requirement across cog reloads. Automated repair tests mock pip installation and exercise real native API probes/process cleanup; they do not modify a live bot environment.

Each of the thirteen packages vendors the same `presentation.py` helper. Edit the AudioPlus copy and sync it to the other twelve; tests enforce identical copies, including EmojiStealerPlus and optional SettingsHub. Use the presentation helper for bot-owned messages and retain webhook/user content semantics. See [the visual design](docs/PRESENTATION.md), including the command to regenerate its sample preview.

All thirteen cogs also vendor identical `command_support.py` and `interactive.py` helpers. Component contexts use the clicking member and check the full current command path; setup panels are bound to the original requester and server. Slash invocations check the full prefix permission path before deferring. Direct shortcuts check the original grouped command and its disabled state before reusing a callback. Modal-opening components check before acknowledging and repeat checks on submission, using the original control message when Discord omits a modal message. Preserve context and permission state even when a parent check fails. Legacy branches too deep for slash are attached after Cog command copying; keep them out of the application-command tree. Tests load all thirteen cogs beside Red Core, exercise messages and slash preparation/conversion/hooks, and check removal/reload.

Config identifiers, cog class names, and defaults preserve existing saved settings. Use a migration for changes to their schema. Named additive sections use Red's merged defaults without rewriting existing records; document their defaults, start of collection, limits, and data hooks. Scalar and compound writes to an added section must share that section's lock. Protect read/modify/write operations with the same Config lock used by related writers. CommunityPlus activity updates use the member's whole-record lock; LevelPlus XP and aliases use their respective field locks.

AudioPlus's opt-in `watchdog` section uses Red's merged defaults on upgrade. Watchdog settings/results and legacy node setters share the global Config lock. Keep the persistent daily cursor and pending failure alert across reloads, bound probes, cancel owned tasks on settings changes/unload, and postpone existing voice connections. Recipient export/deletion hooks must exclude legacy credentials. Tests cover DST/local schedules, private delivery retries, and a silent probe using real FFmpeg, Discord's audio thread, and Opus with mocked voice transport.

Playback error helpers in `failures.py` identify probe stages and preserve safe exception fields and bounded code locations. Never display/log arbitrary native/provider exception messages or raw HTTP bodies. Keep primary errors/cancellation when cleanup also fails. Show saved check results only in their monitored server and clear them when that server changes.

AudioPlus also adds guild music preferences and bounded member playlists/favorites through merged defaults. Collection writers and data-deletion hooks share their field locks. Components build a fresh checked context for the clicking member, including Red permission and disabled-command rules. Cancel owned player-panel tasks and close views on disconnect/unload. The compatibility test permits these named additive sections while retaining the original snapshot.

AudioPlus's `cache.py` warms server-scoped short-song copies after playback starts, with full extraction duration/live checks, calendar-month expiry, bounded disk admission and hashed source metadata outside Config. Preserve completed copies on reload, never extend retention on replay or evict valid copies to admit another song, and cancel owned FFmpeg/FFprobe preparations and hourly pruning on unload. Keep normal streaming available when preparation fails, reject incomplete/oversized copies, and prevent pre-clear lookups from repopulating erased entries. Cached decoder failure retries remote playback once without another start record; keep accounting, seek, normalization and IntroPlus overlays consistent. Daily probes must bypass cached audio. Test actual local downloads/decoding, expiry, restart reuse, privacy/server deletion, spawn cancellation and prefix/slash permission paths. Keep the independently vendored IntroPlus resolver/decoder copies synchronized.

## Local checks

Use Python 3.10 or 3.11, supported by the pinned Red test version:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q audioplus communityplus levelplus logplus owoplus settingshub emojistealerplus exportplus backupplus introplus presenceplus coreplus downloaderplus
.venv/bin/python -c "import json, pathlib; [json.loads(p.read_text()) for p in [pathlib.Path('info.json'), *pathlib.Path('.').glob('*/info.json')]]"
git diff --check
```

Install FFmpeg and libopus in the test environment. Node.js 22+ or Deno 2.3+ is needed for full YouTube extraction. The tests use actual Red Config with temporary JSON storage and actual command classes. Hybrid command tests register with Red's command tree and exercise slash option conversion, callbacks, and response deferral. Discord synchronization/networking and external media providers are mocked. Local HTTP audio tests run actual yt-dlp subprocesses and FFmpeg decoders, including process cancellation checks. They cover concurrent updates, data hooks, level boundaries, imports, timers, webhook rollback, event registration, routing, and Discord size limits.

`tests/compatibility.json` captures the 209-command surface, Config identifiers, and defaults from commit `32592217b5b341f4327473d6772625d9f9bcc75f`. Changes to that fixture should represent an intentional compatibility change. The compatibility test maps the intentional public renames `com` → `community`, `logplus` → `log`, and `owoplus` → `owo` and explicitly validates additive watchdog/music collections, Level rewards/calendar policies, Community feature/participation sections, Log routes/retries, and Owo feature defaults; it still checks all original subcommand arguments, aliases, permission decorators, Config identifiers, and legacy defaults. CI runs the suite with Python 3.10/3.11 using Red 3.5.24, its pinned Discord.py 2.7.1, yt-dlp and EJS, PyNaCl, davey, FFmpeg, libopus, and Node.js 22. The native migration retains all baseline commands and Config defaults; legacy node commands have documented new behavior and `audio repeat` is added.

Run the reproducible level-calculation benchmark from the repository root:

```sh
.venv/bin/python -m scripts.benchmark_levels
```

It compares the old 5,000-threshold lookup with the new cumulative-formula lookup. Results reflect local calculation time and allocation, not overall bot latency. A sample run with Python 3.11 measured 600 lookups in 0.421 seconds versus 0.00122 seconds, and peak allocations of 202,000 versus 2,879 bytes. Timing varies by machine and cache state.

Before deployment, load changed cogs in a development Red instance, check commands and settings after reload, and exercise real Discord events. AudioPlus additionally needs its local native dependencies, accessible media providers, and a real Discord voice connection. The optional container Dockerfile is not built by the regression suite. Automated tests do not replace that live smoke test.

## Documentation and metadata

Repository metadata lives in root `info.json`. See [Red's publishing guide](https://docs.discord.red/en/stable/guide_publish_cogs.html) for supported fields.

- Match command names, arguments, and aliases to the implementation.
- Document defaults that take effect when a cog loads and behavior changes on upgrade.
- Update data statements and Red data hooks whenever persistent records change. Distinguish Config records from content posted to Discord.
- Declare cog-managed pip packages in the cog's `requirements`; document optional packages separately. AudioPlus treats PyNaCl and davey as required bot-level voice prerequisites, installed once in Red's Python environment. Keep them out of Downloader's per-update requirement reinstall and retain explicit setup instructions and runtime import checks.
- Keep root and cog guides consistent, use `[p]` for the bot prefix, and record meaningful changes in [CHANGELOG.md](CHANGELOG.md).

For bug reports, include the cog, command or event, expected and actual behavior, relevant logs, and runtime versions. Remove bot tokens, cookies, passwords, and signed media URLs from logs before posting.

## Shared settings maintenance

The [storage audit](docs/CONFIG_STORAGE.md) inventories every cog's policy and records. `scripts/red_settings_export.py` provides a standalone, explicitly allowlisted JSON-backend export for configuration repositories. Keep its nested scope aligned with registered defaults and SettingsHub's reviewed policy fields; new fields must be classified as configuration or records. Never copy arbitrary dictionaries or user scopes, emit credential/history/recipient data, or overwrite the live database. Keep exports stable across runs and validate failures before an atomic output replacement.

SettingsHub discovers source cogs at runtime and imports none of their modules. Keep its explicit backup scope in `settingshub/schema.py` aligned with source defaults. Exclude member data, global credentials, operational cursors, registered message IDs, and temporary XP boosts. Validate the actual current setup path and role/channel policies before a restore, hold the existing added-section locks, preserve excluded nested fields, replace dynamic maps, and roll back failed writes. Test prefix/slash permission failures, requester/server boundaries, reloads, invalid files, and member data preservation. Health checks report detected prerequisites, not verified live provider/voice access.

Keep the vendored presentation, command and component helpers identical across all thirteen packages. Server theme overrides must cover direct live edits and log retries as well as normal replies. Keep the unthemed retry card so a failed delivery can adopt a later theme without losing its semantic tone. Check combined source and hub slash payloads against Discord's root/option/nesting limits; usage examples should follow actual command parameters.

Red Config's mutable dictionary context saves changed values even if its body raises an exception. For byte-budget or transaction validation, hold the section's existing lock, read a detached dictionary, validate it and explicitly save only after success. Regression tests should prove failed haiku/incident writes leave stored records unchanged. New tasks belong to their cog and must stop on unload; new user records need bounded retention and export/deletion hooks. The new continuity, community tools, progression, incident and Owo fun modules keep their policy settings separate from member/operational records so backups can preserve those records.

SettingsHub's optional audit observer wraps only these loaded cog instances' Config driver `set`/`clear` methods and restores them on unload. Its actor context belongs to the exact invoking task and stores a weak task reference; inherited background tasks must not claim the command's author. Begin/reset actor scopes in the identical vendored command hooks and checked setup/restore component wrappers. Restrict comparisons to selected server-policy fields and exclude secrets, personal/operational records and cursors. Failed/unchanged writes create no entry, and audit storage failures must not undo successful source writes. Exercise real Red prefix/slash/component pipelines, concurrent scalar writes, clear/default semantics, privacy cleanup and unload/reload.

Readiness is a local dry run with candidate overrides. Keep checks aligned with each source's actual channel, role and intent requirements, including optional text fallback and best-effort audit attribution. Reuse AudioPlus's diagnostic runtime protocol without importing another cog package. Do not enable features, grant roles or open voice from readiness checks.

Native event mirrors use CommunityPlus's existing social record locks and owned maintenance task. Edit only events created by the bot with the matching mirror marker; recover completed creates from Discord after timeouts rather than duplicating them. Keep current and next recurring occurrences separate, preserve local RSVP/reminder consent, and avoid recreating manually deleted native events. Test Discord API arguments, gateway echoes, status transitions, cancellation retries and reloads using mocked transport.

EmojiStealerPlus downloads only the constructed Discord CDN emoji endpoint, with redirects disabled, time/streamed-byte limits and image signature checks. Serialize capture and policy writes; keep queue/mapping bounds and separate static/animated capacity. Never delete a server emoji to make room.

Support bundles are built from a whitelist, not log files or Config dumps. Export no raw errors, command arguments, signed URLs, paths or member records. Keep the transient exception-type buffer bounded by count/age, server-isolated and cleared on unload. Reward previews share the actual safe role plan and change no XP/settings/roles. Music session accounting uses decoded frames across seek/rejoin segments; session saving repeats current playlist checks and expires after three minutes.

ExportPlus streams official bot history into bounded UTF-8/JSONL files and privately delivers independent ZIPs. Preserve requester/bot history and private-thread membership checks, snapshot/date bounds, explicit partial manifests, archive/disk reservations, owned worker/compressor cleanup and user-data hooks. Mock API boundaries but use real filesystem/ZIP output and Red prefix/slash checks. Exporter chat data stays outside Config/backups.

BackupPlus stores bounded same-server structure snapshots separately from SettingsHub policy backups. Preserve all overwrite targets, review category inheritance effects, require real Discord Administrator/server ownership and fresh requester-bound previews, and persist replacement IDs before continuing. Unknown create outcomes must block automatic retries until manually bound. Own/cancel restore and private delivery work, keep automatic retention separate from manual/safety copies, and remove whole snapshots for user-data deletion instead of silently dropping permission targets.

IntroPlus reuses independently vendored `resolver.py`, `voice_libraries.py` and the shared `source.py` PCM decoder. Keep source/native checks consistent with AudioPlus. Use its public connection-lock/handoff protocol and preserve music queues, positions, pause decisions and replacement sources during overlays. Bound clip frames and FFmpeg duration, serialize per-server joins, cancel owned jobs/processes, and use member Config field locks for every clip writer and privacy hook. Intros and member clips are outside SettingsHub policy backups. Never store provider credentials or raw signed streams.

IntroPlus prepares selected segments in `cache.py` using owned FFmpeg subprocesses, then reads local 48 kHz stereo PCM without a decoder on playback. Keep the cache bounded by bytes/items, validate/prune ready copies against Config on load, cancel partial downloads on unload or invalidation, and remove audio with member/server/privacy deletion. Schedule preparation and clip invalidation under the existing member clip lock; never hold that lock for a full download. Preserve ready copies on reload/restart, coalesce duplicate preparation, and keep live audio handles usable through LRU eviction. Test real local segment downloads and network-free file playback alongside mocked YouTube/Discord transport.

CorePlus uses Red's removable before-invoke hook to style only native management Context replies and the supported help-formatter interface. Keep original commands intact and use their actual parser/invocation/error pipeline for new controls. Remove the renamed `/help` application definition explicitly on unload, restore only an owned formatter and recheck requester/channel/current command/source permissions on help navigation. Do not intercept licensing or user-data rights. Management overlays create no Config databases or new package engines.

CorePlus and DownloaderPlus vendor identical `management.py` bridges. Use native command conversion, checks and invoke hooks for every operation; retain the repository installation agreement and installed-package converters. No new repository engine or Config namespace is introduced.
