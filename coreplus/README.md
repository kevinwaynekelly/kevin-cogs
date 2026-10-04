# CorePlus

The shared theme for Red's bot-management commands, a category-based help menu and selected slash controls. CorePlus installs beside bundled Core and CogManagerUI; Red continues to handle management operations and saved settings.

## Install

```text
[p]cog install kevin-cogs coreplus
[p]load coreplus
[p]slash enablecog coreplus
[p]slash sync
```

Use your actual repository name if it differs from `kevin-cogs`. Requires Red 3.5.24+ and Python 3.10/3.11, with no extra packages. Unload any other custom-help cog before loading CorePlus. Red allows only one replacement help formatter; CorePlus fails cleanly if another formatter owns help.

## Help

`[p]help`, `[p]helpme` and `/help` open one themed category card. Choose AudioPlus, CommunityPlus or any other available category, then use Previous/Next to browse its commands. `[p]help play` and `/help query:play` show full descriptions, text arguments, aliases and any slash counterpart. Categories reflect all installed cogs, including Red's native commands. Core and CogManagerUI appear together as CorePlus. With DownloaderPlus loaded, native Downloader commands appear in its category too.

The menu repeats current Red command, parent, cog and native-source checks. Disabled/unavailable commands are omitted; owner-only operations are shown only to bot owners. Explicitly requesting a hidden command can show its details if its checks pass. Red's show-hidden and show-aliases settings are respected; permission filtering is always active, even if native help verification was disabled. Ordinary Red checks can depend on the current channel or operational state, so a visible command can still require a voice connection or valid arguments when executed.

Controls belong to the requester, stay in the original channel, recheck permissions on every click and expire after three minutes. No long help dump is posted automatically. There is a text fallback when embeds are disabled or unavailable, and the optional SettingsHub theme applies. CorePlus's category menu replaces native reaction/menu/automatic-DM help delivery, tagline, delete-delay and page-count presentation settings. Standard help returns on unload or `[p]helpset resetformatter`.

## Commands

| Text | Slash | Access and purpose |
| --- | --- | --- |
| `[p]core` | `/core status` | Bot versions, gateway latency, uptime and loaded-cog count. |
| `[p]core help` | `/core help` | Bot-management category. |
| `[p]core info` | `/core info` | Red's information response. |
| `[p]core uptime` | `/core uptime` | Red's uptime response. |
| `[p]core invite` | `/core invite` | Invitation, subject to Red's invite policy. |
| `[p]core cogs` | `/core cogs` | Owner: loaded and available packages. |
| `[p]core load <packages>` | `/core load` | Owner: load space-separated installed packages. |
| `[p]core unload <packages>` | `/core unload` | Owner: unload packages without uninstalling files. |
| `[p]core reload <packages>` | `/core reload` | Owner: reload space-separated packages. |
| `[p]core slash` | `/core slash list` | Owner: enabled and disabled application commands. |
| `[p]core slash enable <packages>` | `/core slash enable` | Owner: enable slash definitions for packages. |
| `[p]core slash disable <packages>` | `/core slash disable` | Owner: disable their slash definitions. |
| `[p]core slash sync` | `/core slash sync` | Owner: publish changes with Red's original sync cooldown. |

CorePlus registers 14 slash actions across two roots, `/help` and `/core`. `[p]help` remains Red's original prefix command; `[p]helpme` is the hybrid prefix counterpart of `/help`, preventing a name collision.

## Native behavior and lifecycle

Existing `[p]load`, `[p]reload`, `[p]unload`, `[p]uptime`, `[p]cogs` and other Core/CogManagerUI replies sent through the invocation's Context gain the shared theme. Original command objects, names, aliases, checks, converters, hooks, cooldowns and error handlers remain in place. New management controls invoke the original command pipeline, including native parent checks and package parsing. Disabling an original command also blocks its new control. Replies sent directly to a user or through another native destination are not intercepted. Licensing and `mydata` responses are kept intact.

Unload removes the registered reply hook, `/help`, controls and the owned help formatter without displacing another formatter installed after a native reset. Existing native commands remain usable. Disabling CorePlus in a server restores native help/replies there; unloading is the bot-wide way to turn it off. There is no automatic install, update, slash synchronization or package execution.

## Data and validation

No Config database, command history or persistent member records are created. Views hold the requester's ID/channel/selection in memory until timeout or unload. Red privacy deletion closes that user's views; the export hook returns no stored data. Red Core remains responsible for its own saved data and settings.

Tests run real Red command parsing, hybrid conversion, permission checks, native hooks/cooldowns, source disabling and unload/reload with mocked Discord transport. They do not install packages on a live bot or verify Discord synchronization.
