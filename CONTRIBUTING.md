# Contributing

Keep changes focused and describe the behavior being changed and how it was checked. Commit useful checkpoints to a working branch as you go, especially during a pass across multiple cogs. Publish the completed change in a pull request.

## Layout

Each cog is independently installable through Red Downloader:

- `__init__.py`: asynchronous `setup(bot)` entry point and public cog export.
- `cog.py`: commands, event listeners, and lifecycle management.
- `constants.py`: persistent defaults and presentation constants.
- `info.json` and `README.md`: Downloader metadata and the user guide.
- `events.py`, where present: Red's per-server disable check for listeners.

AudioPlus isolates subprocess media resolution in `resolver.py`, native queue/playback in `player.py`, and system dependency checks in `backend.py`. LevelPlus isolates threshold calculations in `levels.py`. OwoPlus isolates syllable counting and haiku detection in `haiku.py`. Keep cog modules self-contained; Downloader can install one cog without the others.

Each cog vendors the same `presentation.py` helper. Edit the AudioPlus copy and sync it to the other four; tests enforce identical copies. Use the presentation helper for bot-owned messages and retain webhook/user content semantics. See [the visual design](docs/PRESENTATION.md), including the command to regenerate its sample preview.

Config identifiers, cog class names, and defaults preserve existing saved settings. Use a migration for changes to their schema. Protect read/modify/write operations with the same Config lock used by related writers. CommunityPlus activity updates use the member's whole-record lock; LevelPlus XP and aliases use their respective field locks.

## Local checks

Use Python 3.10 or 3.11, supported by the pinned Red test version:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q audioplus communityplus levelplus logplus owoplus
.venv/bin/python -c "import json, pathlib; [json.loads(p.read_text()) for p in [pathlib.Path('info.json'), *pathlib.Path('.').glob('*/info.json')]]"
git diff --check
```

Install FFmpeg and libopus in the test environment. Node.js 22+ or Deno 2.3+ is needed for full YouTube extraction. The tests use actual Red Config with temporary JSON storage and actual command classes. Discord networking and external media providers are mocked. Local HTTP audio tests run actual yt-dlp subprocesses and FFmpeg decoders, including process cancellation checks. They cover concurrent updates, data hooks, level boundaries, imports, timers, webhook rollback, event registration, routing, and Discord size limits.

`tests/compatibility.json` captures the 209-command surface, Config identifiers, and defaults from commit `32592217b5b341f4327473d6772625d9f9bcc75f`. Changes to that fixture should represent an intentional compatibility change. CI runs the suite with Python 3.10/3.11 using Red 3.5.24, its pinned Discord.py 2.7.1, yt-dlp and EJS, PyNaCl, davey, FFmpeg, libopus, and Node.js 22. The native migration retains all baseline commands and Config defaults; legacy node commands have documented new behavior and `audio repeat` is added.

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
- Declare required pip packages in the cog's `requirements`; document optional packages separately.
- Keep root and cog guides consistent, use `[p]` for the bot prefix, and record meaningful changes in [CHANGELOG.md](CHANGELOG.md).

For bug reports, include the cog, command or event, expected and actual behavior, relevant logs, and runtime versions. Remove bot tokens, cookies, passwords, and signed media URLs from logs before posting.
