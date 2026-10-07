# Contributing

Use Python 3.10 or 3.11, PHP CLI with SimpleXML/POSIX, Bash, Git, jq and OpenSSL.
Install development dependencies with `python -m pip install -r requirements-dev.txt`.
Run the checks described in AGENTS.md. Native process supervision tests run on Linux with
matching process namespaces; restricted test environments skip those cases explicitly.

Code stays under tools/aria_bridge so existing Unraid installation paths remain compatible.
Runtime Python uses the standard library. Docker builds install the pinned tunnel client.
Test fixtures fake Docker/libvirt and exercise local HTTP providers rather than real accounts.
