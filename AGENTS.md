# Repository instructions

Commit and push validated checkpoints as you go. The repository owner requests direct commits to `main` by default and authorizes automatic merges of completed pull requests after checks pass. Use a working branch when isolation helps, then merge validated work without asking for another merge confirmation. During a long refactor, save completed implementation and test stages before starting the next stage. Do not leave the entire pass unpublished until the final response.

Preserve existing cog class names, Config identifiers, saved defaults, command arguments, aliases, and permission checks unless a change is intentional and documented. Changes to saved data schemas need a migration. Each cog must remain independently installable through Red Downloader.

Use consistent Config locks for concurrent writers. Track long-lived tasks, cancel them on unload, and preserve original Discord messages if a replacement fails.

Run `python -m ruff check .`, `python -m ruff format --check .`, `python -m pytest -q`, and `git diff --check` for implementation changes. Use the supported Python and dependency versions in CONTRIBUTING.md. Automated tests use mocked Discord/Lavalink boundaries; report that limitation and do not claim a live deployment test.

Keep cog guides, Downloader metadata, data statements, and CHANGELOG.md consistent with implemented behavior. Publish completed, validated changes to `main`. If using a working branch, open a pull request and merge it automatically once checks pass.
