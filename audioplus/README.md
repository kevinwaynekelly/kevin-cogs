# AudioPlus

Lavalink v4 music playback through Wavelink 3.x, with queue controls and diagnostics for voice connection problems.

[Repository installation](../README.md#install)


Command replies use the [shared visual theme](../docs/PRESENTATION.md), with a sectioned command overview, playback cards, and matching status colors. Grant **Embed Links** to show the cards; replies respect Red's embed preference and fall back to text when embeds are unavailable.

## Requirements and first load

- Red 3.5.0 or newer.
- Wavelink `>=3.4.1,<4.0.0` and aiohttp `>=3.8`, installed by Downloader from `info.json`.
- A running Lavalink v4 node with sources configured for the tracks you want to play.
- **View Channel**, **Send Messages**, **Connect**, and **Speak** for the bot. Stage channels may also require a moderator to approve speaking.

AudioPlus uses the `audio` command group. If Red's bundled Audio cog is loaded, unload it before loading this cog:

```text
[p]unload audio
```

Loading creates a reusable HTTP session and makes the configuration commands available without requiring a reachable node. On a fresh install, the connection defaults are:

| Setting | Default |
| --- | --- |
| Host | `127.0.0.1` |
| Port | `2333` |
| Password | `youshallnotpass` |
| TLS | Off |
| Resume timeout | 60 seconds |

In a container, `127.0.0.1` refers to the bot's own container. Configure a reachable node after loading the cog; playback also attempts a connection when needed.

After loading, the bot owner can save node settings and connect:

```text
[p]audio setnode lavalink.example.com 2333 "your-node-password" false
[p]audio connectnode
[p]audio shownode
[p]audio pingnode
```

Use `true` for the final argument when the node uses HTTPS. Node settings persist in Red Config and are used for later connections. Run `setnode` in a private server channel since the command includes the password. The command group is server-only, so these commands cannot be run in DMs.

`setnode` saves settings and immediately attempts a fresh connection. If it fails, the settings remain saved and the command reports the failure. A fresh node connection disconnects AudioPlus players; queue music again after changing nodes.

## Play music

Join a voice channel, then:

```text
[p]audio join
[p]audio play your search terms
[p]audio queue
[p]audio np
```

Plain search terms use `ytsearch:`. Explicit search prefixes are preserved, so you can choose another enabled source:

```text
[p]audio play scsearch:artist and song
[p]audio play ytmsearch:artist and song
```

URL playback and search availability depend on the Lavalink node's source configuration. A search or URL adds tracks to the queue and starts playback when the player is idle, clearing an old idle pause flag. Adding tracks while the current track is paused preserves that pause. If the node rejects the initial playback request, the track stays at the front of the queue for a later retry.

Finding a track and accepting a playback request do not prove that the source can stream it. Asynchronous playback failures are reported in the most recent AudioPlus request channel and recorded temporarily for `[p]audio pingnode`. Stuck tracks receive one skip request; Wavelink advances the queue after the resulting track-end event.

## Commands

All commands below use your bot prefix in place of `[p]` and run in a server.

| Command | Purpose |
| --- | --- |
| `[p]audio join` | Join or move to your voice channel. Aliases: `connect`, `summon`. |
| `[p]audio leave` | Disconnect. Aliases: `dc`, `disconnect`. |
| `[p]audio play <query or URL>` | Search for or queue audio. Alias: `p`. |
| `[p]audio skip` | Skip the current track; Wavelink advances the queued tracks. Aliases: `next`, `s`. |
| `[p]audio stop` | Stop playback and clear the queue. |
| `[p]audio pause` / `[p]audio resume` | Pause or resume playback. |
| `[p]audio volume` | Show the current volume. |
| `[p]audio volume <value>` | Set volume; values are clamped to 0 through 1000. Alias: `vol`. |
| `[p]audio np` | Show the current track. Alias: `nowplaying`. |
| `[p]audio queue` | Show up to ten queued tracks. Alias: `q`. |
| `[p]audio shuffle` | Shuffle queued tracks. |
| `[p]audio pingnode` | Show node connectivity, sources, Lavalink/Lavaplayer/plugin versions, statistics, and the latest playback failure. |
| `[p]audio playerstate` | Inspect the Lavalink REST player state. |
| `[p]audio debugvc` | Show Discord voice flags and player status. |
| `[p]audio speak` | Try to unsuppress the bot or request to speak on a Stage channel. |
| `[p]audio undeafen` | Try to clear self-deafen without replacing the player. |
| `[p]audio fixvoice` | Try Stage speaking and self-deafen recovery. |
| `[p]audio rejoin` | Reconnect to the current voice channel, restoring the track, position, volume, pause state, and queue when successful. |
| `[p]audio tone` | Queue a direct SoundHelix MP3 test track to help diagnose source/voice issues. |

Owner-only commands:

| Command | Purpose |
| --- | --- |
| `[p]audio setnode <host> <port> <password> [secure]` | Save node settings and reconnect; `secure` defaults to `false`. |
| `[p]audio shownode` | Show the configured host, port, and TLS setting without the password. |
| `[p]audio connectnode` | Attempt a fresh connection using the saved settings. |

Use `[p]help audio` or `[p]help audio <subcommand>` for Red's generated help. Playback commands have no dedicated DJ/admin check in the current implementation.

## Troubleshooting

1. Check the node with `[p]audio pingnode` and reconnect with `[p]audio connectnode` as the owner. A connected node confirms the bot can reach Lavalink; source playback and Discord voice still need separate checks.
2. Check **Connect/Speak** permissions and inspect `[p]audio debugvc` and `[p]audio playerstate`.
3. Try `[p]audio fixvoice` or `[p]audio rejoin` if voice state is stuck.
4. Run `[p]audio stop` to clear the existing queue, then `[p]audio tone`. This plays a direct MP3 rather than a YouTube search. The node must have its HTTP source enabled and be able to reach the MP3 URL. If it also fails, inspect Lavalink's logs and network access to the source and Discord voice, including UDP egress.

### No supported audio streams

`No supported audio streams available` is a source playback error inside Lavalink. The source may find a track's metadata and still fail to obtain a playable stream. A cog reload or a successful node connection does not establish that the source is working.

If the MP3 test works but YouTube fails, inspect the Lavalink startup logs and `application.yml`. Use the maintained [youtube-source plugin](https://github.com/lavalink-devs/youtube-source#plugin), disable Lavalink's built-in YouTube source when using that plugin, and check the installed plugin version and configured clients against its documentation. Some client failures require authentication or changes to signature deciphering; choose those changes from the actual node logs rather than replacing unrelated settings.

If SoundCloud appears in `[p]audio pingnode`, `[p]audio play scsearch:artist and song` can test it independently. AudioPlus does not silently substitute another provider for a requested track.

For a playback report, include `[p]audio pingnode`, `[p]audio playerstate`, the failed query or URL, and Lavalink's matching log entry. Include a redacted `application.yml` when troubleshooting source configuration; remove node passwords, OAuth refresh tokens, and other credentials.

## Data

Red Config stores global node settings, including the password. AudioPlus does not persist listening histories or saved playlists in its own Config. The active player and queue are maintained in memory and on Lavalink.

The most recent request context and playback failure are held in memory per server and cleared when leaving voice, reconnecting the node, unloading the cog, or removing the server. A successful track start clears the previous failure.

AudioPlus owns one node in Wavelink's shared pool. Reconnecting or unloading closes that node and its own HTTP session, preserving other cogs' nodes. Players use partial autoplay to progress through queued tracks without adding recommendations. Diagnostic REST calls use AudioPlus's own Lavalink session and have timeouts. Live Discord voice and Lavalink playback still need a deployment smoke test.

## References

- [Wavelink 3.4.1 documentation](https://wavelink.readthedocs.io/en/v3.4.1/)
- [Lavalink documentation](https://lavalink.dev/)
