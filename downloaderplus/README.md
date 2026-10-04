# DownloaderPlus

The shared theme and owner-only prefix/slash controls for bundled Red Downloader. Red keeps its repository copies, installed-package records, dependency installer, converters, installation agreement and reload behavior.

## Install

```text
[p]load downloader
[p]cog install kevin-cogs downloaderplus
[p]load downloaderplus
[p]slash enablecog downloaderplus
[p]slash sync
```

Use your configured repository name if it differs from `kevin-cogs`. Requires Red 3.5.24+ and Python 3.10/3.11, with no additional packages. Native Downloader must be loaded for operations; DownloaderPlus can load first and reports an unavailable source clearly. Optional CorePlus combines native Downloader commands with the DownloaderPlus help category.

## Commands

Every new control requires bot ownership. Server administrator permission alone is insufficient. Root commands have a slash fallback: `[p]download` is `/download status`, and `[p]download repos` is `/download repos list`.

| Text | Slash | Purpose |
| --- | --- | --- |
| `[p]download` | `/download status` | Repository, installed-cog and pinned counts. |
| `[p]download help` | `/download help` | Command help. |
| `[p]download repos` | `/download repos list` | List configured repositories. |
| `[p]download repos add <name> <url> [branch]` | `/download repos add` | Add a repository through Red's installation agreement. |
| `[p]download repos remove <names>` | `/download repos remove` | Remove space-separated repositories. |
| `[p]download repos info <name>` | `/download repos info` | Repository information. |
| `[p]download repos update [names]` | `/download repos update` | Refresh repository copies, all if omitted. |
| `[p]download available <repo>` | `/download available` | Packages available in a repository. |
| `[p]download installed` | `/download installed` | Installed packages, repository names and pinned state. |
| `[p]download info <repo> <package>` | `/download info` | Package metadata. |
| `[p]download install <repo> <packages>` | `/download install` | Install space-separated packages; load afterward. |
| `[p]download uninstall <packages>` | `/download uninstall` | Native uninstall and unload handling. |
| `[p]download update [reload] [packages]` | `/download update` | Update selected packages or all unpinned packages. Reload defaults to true. |
| `[p]download checkupdates` | `/download checkupdates` | Check without installing updates. |
| `[p]download pin <packages>` | `/download pin` | Exclude selected installed packages from normal updates. |
| `[p]download unpin <packages>` | `/download unpin` | Enable normal updates for them. |
| `[p]download pinned` | `/download pinned` | Show pinned packages. |
| `[p]download version <repo> <revision> [reload] [packages]` | `/download version` | Update installed packages from a particular revision. |
| `[p]download find <command>` | `/download find` | Find a command's installed package and repository. |

For example, `[p]download update True audioplus communityplus` updates and reloads those installed packages. `[p]download version kevin-cogs origin/main True audioplus` uses that repository's main revision. Pinning, compatibility/dependency failures and revision errors are reported by the original engine. Review repository code before accepting Red's native installation agreement; DownloaderPlus does not automatically accept it.

## Native behavior

Existing `[p]repo`, `[p]cog`, `[p]findcog` and `[p]pipinstall` replies sent through their Context use the shared theme when this addon is enabled. Their commands, checks and arguments remain unchanged. New controls invoke the original parser, full command/parent/cog checks, hooks, cooldowns and error handlers. Disabling a native source command also blocks its new control. The new public `download` group is owner-only, including its `find` shortcut; native `findcog` retains its existing access policy.

An update's reload option follows Red's native behavior. Repository-copy updates alone do not install changed cog files. Installation does not load a package automatically. Unloading DownloaderPlus removes only its commands and removable styling hook; bundled Downloader and its database remain intact.

## Data and validation

DownloaderPlus creates no Config database, repository copies, persistent member records or command history. Red Downloader remains responsible for its existing data. The user-data export hook returns no additional records.

Regression tests use real Red parsing and repository/installed-package conversion, with network, package installation and Discord transport mocked. They check prefix/slash ownership, source disabling, native hook invocation, update arguments and unload cleanup; they do not install packages on a live bot.
