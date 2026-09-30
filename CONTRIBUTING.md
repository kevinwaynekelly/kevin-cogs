# Contributing

Keep changes focused on one cog or one shared repository concern. Describe the behavior being changed and how it was checked.

## Layout

Each cog is a package with three files:

- `__init__.py`: implementation and the asynchronous `setup(bot)` entry point.
- `info.json`: Red Downloader metadata, required Python packages, and the end-user data statement.
- `README.md`: setup, commands, permissions, defaults, and stored data.

Repository metadata lives in the root `info.json`. See [Red's publishing guide](https://docs.discord.red/en/stable/guide_publish_cogs.html) for the supported fields. Required pip packages belong in the cog's `requirements` list; optional packages should be documented separately.

## Local checks

From the repository root, run these checks with your Red environment's Python:

```sh
python -m compileall -q audioplus communityplus levelplus logplus owoplus
python -m json.tool info.json
python -m json.tool audioplus/info.json
python -m json.tool communityplus/info.json
python -m json.tool levelplus/info.json
python -m json.tool logplus/info.json
python -m json.tool owoplus/info.json
git diff --check
```

These check Python syntax, JSON syntax, and whitespace. They do not exercise Red, Discord events, or Lavalink. The repository does not currently include an automated behavior test suite.

For implementation changes, load the affected cog in a development Red instance and exercise the changed commands or events. Check persistent settings after reloading. For AudioPlus, also use a reachable Lavalink v4 node. Report which runtime versions and checks were used.

## Documentation and metadata

- Match command names and argument order to their decorators and signatures, including nested groups.
- Document defaults that take effect as soon as a cog loads.
- Update data statements whenever persistent records change. Distinguish Config storage from content posted to Discord.
- Keep root and cog guides consistent; use `[p]` for the bot prefix.
- Record meaningful changes in [CHANGELOG.md](CHANGELOG.md).

For bug reports, include the cog, command or event, expected behavior, actual behavior, relevant logs, and runtime versions. Remove bot tokens and node passwords from logs before posting.
