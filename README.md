# Aria GPT Bridge

Manage an Unraid server through an outbound MCP tunnel. Aria provides container/template
management, root-authorized commands, scripts and files, application APIs, diagnostics,
virtual machines, deployment recovery and durable host-local automation.

- [Installation and tunnel setup](tools/aria_bridge/README.md)
- [Management and recovery](tools/aria_bridge/MANAGEMENT.md)
- [Feature guide and tool catalog](tools/aria_bridge/FEATURES.md)
- [Migration from kevin-cogs](tools/aria_bridge/STANDALONE.md)

## Existing installation

The one-time migration preserves credentials and appdata and retains the original checkout.
Follow the migration guide rather than changing the old checkout's Git origin in place.
The source layout stays under tools/aria_bridge for compatible host paths.

After installation, refresh the Aria plugin to discover the expanded tool catalog.
Daily bridge updates run on the host after an initial 24-hour delay and only install an
exact main revision that passed both CI jobs. Application profiles require your local
credential references. Optional host programs are detected, not silently installed.

## Development

Python 3.10/3.11, PHP CLI with SimpleXML/POSIX, Bash, jq, Git and OpenSSL.
See CONTRIBUTING.md. Automated tests use fake native services and local HTTP providers;
they do not replace verification on the target Unraid host.

Originally developed in kevinwaynekelly/kevin-cogs; separated as its own application.
