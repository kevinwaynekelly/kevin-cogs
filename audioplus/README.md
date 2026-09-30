# AudioPlus

Lavalink v4 music playback through Wavelink 3.x, with queue controls and diagnostics for voice connection problems.

[Repository installation](../README.md#install)

## Requirements and first load

- Red 3.5.0 or newer.
- Wavelink `>=3.4.1,<4.0.0` and aiohttp `>=3.8`, installed by Downloader from `info.json`.
- A running Lavalink v4 node with sources configured for the tracks you want to play.
- **View Channel**, **Send Messages**, **Connect**, and **Speak** for the bot. Stage channels may also require a moderator to approve speaking.

AudioPlus uses the `audio` command group. If Red's bundled Audio cog is loaded, unload it before loading this cog:

```text
[p]unload audio
```

The current implementation connects to Lavalink while the cog loads. On a fresh install, the connection defaults are:

| Setting | Default |
| --- | --- |
| Host | `127.0.0.1` |
| Port | `2333` |
| Password | `youshallnotpass` |
| TLS | Off |
| Resume timeout | 60 seconds |

Make that node reachable before the first load. In a container, `127.0.0.1` refers to the bot's own container. If the initial connection fails, loading can fail before the configuration commands become available.

After a successful load, the bot owner can save a different node and explicitly reconnect:

```text
[p]audio setnode lavalink.example.com 2333 "your-node-password" false
[p]audio connectnode
[p]audio shownode
[p]audio pingnode
```

Use `true` for the final argument when the node uses HTTPS. Node settings persist in Red Config and are used on later loads. Run `setnode` in a private server channel since the command includes the password. The command group is server-only, so these commands cannot be run in DMs.

`setnode` saves settings, but an already connected node can remain active until `connectnode` is used.

## Play music

Join a voice channel, then:

```text
[p]audio join
[p]audio play your search terms
[p]audio queue
[p]audio np
```

Search terms use `ytsearch:`. URL playback and search availability depend on the Lavalink node's source configuration. A search or URL adds tracks to the queue and starts playback when the player is idle.

## Commands

All commands below use your bot prefix in place of `[p]` and run in a server.

| Command | Purpose |
| --- | --- |
| `[p]audio join` | Join or move to your voice channel. Aliases: `connect`, `summon`. |
| `[p]audio leave` | Disconnect. Aliases: `dc`, `disconnect`. |
| `[p]audio play <query or URL>` | Search for or queue audio. Alias: `p`. |
| `[p]audio skip` | Skip the current track and try to start the next queued track. Aliases: `next`, `s`. |
| `[p]audio stop` | Stop playback and clear the queue. |
| `[p]audio pause` / `[p]audio resume` | Pause or resume playback. |
| `[p]audio volume` | Show the current volume. |
| `[p]audio volume <value>` | Set volume; values are clamped to 0 through 1000. Alias: `vol`. |
| `[p]audio np` | Show the current track. Alias: `nowplaying`. |
| `[p]audio queue` | Show up to ten queued tracks. Alias: `q`. |
| `[p]audio shuffle` | Shuffle queued tracks. |
| `[p]audio pingnode` | Show node connectivity and available version/statistics information. |
| `[p]audio playerstate` | Inspect the Lavalink REST player state. |
| `[p]audio debugvc` | Show Discord voice flags and player status. |
| `[p]audio speak` | Try to unsuppress the bot or request to speak on a Stage channel. |
| `[p]audio undeafen` | Try to clear self-deafen, reconnecting if needed. |
| `[p]audio fixvoice` | Try Stage speaking and self-deafen recovery. |
| `[p]audio rejoin` | Disconnect and reconnect to the current voice channel. |
| `[p]audio tone` | Queue a direct SoundHelix MP3 test track to help diagnose source/voice issues. |

Owner-only commands:

| Command | Purpose |
| --- | --- |
| `[p]audio setnode <host> <port> <password> [secure]` | Save node settings; `secure` defaults to `false`. |
| `[p]audio shownode` | Show the configured host, port, and TLS setting without the password. |
| `[p]audio connectnode` | Attempt a fresh connection using the saved settings. |

Use `[p]help audio` or `[p]help audio <subcommand>` for Red's generated help. Playback commands have no dedicated DJ/admin check in the current implementation.

## Troubleshooting

1. Check the node with `[p]audio pingnode` and reconnect with `[p]audio connectnode` as the owner.
2. Check **Connect/Speak** permissions and inspect `[p]audio debugvc` and `[p]audio playerstate`.
3. Try `[p]audio fixvoice` or `[p]audio rejoin` if voice state is stuck.
4. Try `[p]audio tone`. This plays a direct MP3 rather than a YouTube search. If it also fails, inspect Lavalink's logs and network access to Discord voice, including UDP egress.

## Data

Red Config stores global node settings, including the password. AudioPlus does not persist listening histories or saved playlists in its own Config. The active player and queue are maintained in memory and on Lavalink.

The cog uses Wavelink's shared pool and closes that pool when unloaded. Its track-end listener logs the event; it does not explicitly advance the queue. Automatic progression depends on the active player behavior and should be verified on your deployment.

## References

- [Wavelink 3.4.1 documentation](https://wavelink.readthedocs.io/en/v3.4.1/)
- [Lavalink documentation](https://lavalink.dev/)
