# Repository instructions

This repository contains the standalone Aria Unraid bridge. Publish validated changes to
main when the user requests implementation and pushing. Preserve installed credentials,
persistent state, request deduplication, queue locks and recovery semantics. Never describe
mocked Docker/libvirt or local HTTP tests as a live Unraid deployment test.

Run `python -m ruff check .`, `python -m ruff format --check .`, `python -m pytest -q`,
PHP syntax checks for every PHP module, Bash syntax checks for every shell script, and
`git diff --check`. Keep tool manifests, documentation and CHANGELOG.md consistent.
Use Python 3.10 and 3.11. Keep job outcomes explicit when a provider response is uncertain.
