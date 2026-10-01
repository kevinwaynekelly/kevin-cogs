# AudioPlus

Music search, playback, queues, and Discord voice control inside Red. AudioPlus uses **yt-dlp**, **FFmpeg**, and **Discord.py native voice with DAVE encryption support**. A Lavalink server, Wavelink, Java, and a separate YouTube plugin are no longer required.

`[p]` means your bot's command prefix. With `!`, `[p]audio play roar` becomes `!audio play roar`. Replies use the shared Kevin's Cogs theme and fall back to text when embeds are unavailable.

## Requirements

- Red **3.5.24 or newer**, with a Discord.py version that supports DAVE voice. Development checks use Red 3.5.24 and Discord.py 2.7.1 on Python 3.10/3.11.
- Python packages declared in `info.json`: `yt-dlp[default]>=2026.8.19`, `PyNaCl>=1.5.0,<1.6`, and `davey>=0.1.6`. The yt-dlp default extra includes its matching `yt-dlp-ejs` challenge solver.
- The **FFmpeg executable** and **libopus** in the Red container or host. Installing a Python package named ffmpeg does not install the executable.
- A supported JavaScript runtime in Red's `PATH`, preferably **Deno 2.3+** or **Node.js 22+**, for full YouTube extraction. AudioPlus enables detected Deno, Node, and QuickJS runtimes in yt-dlp.
- Network access from the **Red container** to media providers and Discord voice, including UDP. Playback no longer uses the Lavalink container's network connection.

Downloader installs the Python dependencies; it cannot install container system packages. Red imports Discord.py before exposing Downloader's package folder. AudioPlus initializes those optional voice imports before diagnostics or playback, without reloading Discord.py or replacing its classes. **Restart Red when upgrading voice libraries that are already loaded.** Package versions listed in diagnostics describe installed files; the separate **Discord voice** status reports whether native playback prerequisites can actually load.

AudioPlus can load with missing system dependencies so its help and diagnostics remain available. It uses the `audio` command group, which conflicts with Red's bundled Audio cog. Unload the bundled cog before loading AudioPlus:

```text
[p]unload audio
[p]cog install kevin-cogs audioplus
```

Restart Red after dependency installation, then run:

```text
[p]load audioplus
[p]audio pingnode
```

## Upgrading from the Lavalink backend

Update AudioPlus from this repository, then restart Red:

```text
[p]cog update False audioplus
```

Install the container dependencies below before testing playback. If Downloader reports a dependency installation failure, resolve it before proceeding. The bot owner can reinstall Python packages with:

```text
[p]pipinstall yt-dlp[default]>=2026.8.19 PyNaCl>=1.5.0,<1.6 davey>=0.1.6
```

Restart Red after that command. When dependencies are already installed, `[p]cog update True audioplus` can update and reload the cog directly.

Saved Config identifiers and defaults remain compatible. The old host, port, password, TLS flag, and resume timeout stay saved for rollback but are **ignored by native playback**. In-memory queues reset on reload or restart, as before. Existing commands, aliases, arguments, and permission checks remain registered. `audio repeat` is new.

The old node commands remain available with documented new behavior:

| Command | Native backend behavior |
| --- | --- |
| `[p]audio pingnode` | Shows local FFmpeg, yt-dlp, EJS, voice libraries, JavaScript runtime, player state, and latest failure. No node request is sent. |
| `[p]audio connectnode` | Owner-only local dependency check. No Lavalink connection is opened. |
| `[p]audio shownode` | Owner-only display of preserved legacy settings, with the password hidden. |
| `[p]audio setnode <host> <port> <password> [secure]` | Owner-only update of legacy settings for rollback. Does not reconnect or change playback. Use a private channel because the command contains a password. |

## Container setup

Install the dependencies **inside the Red image**, not only on Unraid or in Lavalink. For a Debian/Ubuntu-based Red container, FFmpeg and Opus can be installed as root with:

```sh
apt-get update
apt-get install -y --no-install-recommends ffmpeg libopus0
```

Install Node.js **22 or newer** or Deno **2.3 or newer** using its official distribution. An older Debian Node package may not meet that requirement. Confirm the executable is available to the user running Red:

```sh
ffmpeg -version
node --version
```

For Deno, run these commands as root in the Red container's console:

```sh
apt-get update
apt-get install -y --no-install-recommends curl unzip libopus0
curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- --yes --no-modify-path
deno --version
```

This places Deno in `/usr/local/bin`, rather than root's private home directory, so Red can find it. Only one supported JavaScript runtime is needed. Node.js and QuickJS may still show `missing` when Deno is available. Run `[p]audio pingnode` again, then test `[p]audio tone` and a YouTube search. These console changes survive a container restart but can disappear when the container is recreated.

The included [Dockerfile](Dockerfile) builds a persistent image for **PhasecoreX's Debian-based Red image family**, carrying FFmpeg, libopus, and Node.js 22. It inherits the existing image's entrypoint, `/data` volume, and bot startup command.

From a checkout of this repository on your Docker host:

```sh
docker build -f audioplus/Dockerfile --build-arg RED_IMAGE=phasecorex/red-discordbot:core -t kevin-red-native .
```

If you already use a different PhasecoreX tag, pass that same tag as `RED_IMAGE`. In Unraid, use `kevin-red-native` as the Red container's Repository image, keeping its existing `/data` mapping, environment variables, and other settings. Do not replace your appdata mapping. This Dockerfile is an image recipe; automated checks do not build or deploy it to your server.

For another base-image family, add FFmpeg, libopus, and a supported JavaScript runtime using that image's package manager. AudioPlus's `[p]audio pingnode` reports what Red can actually use.

## Playback

Join a voice channel, then try:

```text
[p]audio play roar
[p]audio play https://www.youtube.com/watch?v=VIDEO_ID
[p]audio play https://www.youtube.com/playlist?list=PLAYLIST_ID
[p]audio play scsearch:artist and song
[p]audio np
[p]audio queue
```

Plain text and `ytsearch:` search YouTube and queue one result. `ytmsearch:` is retained for compatibility and maps to yt-dlp's regular YouTube search. `scsearch:` searches SoundCloud. HTTP/HTTPS media and provider URLs are accepted. YouTube playlist URLs queue up to 100 accessible entries. Other providers supported by yt-dlp can work through their URLs, subject to their access requirements. Lavalink plugin prefixes such as `spsearch:` are rejected with an explanation rather than silently searching another provider.

Play and tone confirmations show the selected track's title, artist/uploader, duration when available, and a clickable source link. Playlist confirmations preview the first five entries and report how many additional tracks were queued. `[p]audio np` shows the current title and source link with playback progress. These replies use the shared theme and retain track details when embeds are disabled; they do not expose resolved, signed playback URLs.

Each guild has an independent in-memory player. The queue holds at most 100 upcoming tracks. A paused current track stays paused when additional tracks are queued. Provider stream URLs are resolved immediately before playback to avoid using links that expired while waiting in the queue. Direct media URLs are used as supplied. yt-dlp runs in bounded subprocesses outside Red's event loop, with two concurrent lookups and a 45-second extraction timeout.

Natural completion advances once. Skip cancels the current lookup or playback before advancing. Failed tracks are reported in the latest request channel and the player tries the next queued track. A failed or skipped track is not repeated. Stop clears upcoming tracks and cancels the active playback operation. Rejoin refreshes the stream and restores position, pause state, volume, and the queue for seekable audio. Live streams may restart at their live edge. If reconnection fails, tracks remain available in memory for a later `[p]audio join`.

## Commands

All playback and voice commands are server commands. The bot needs Connect and Speak in the target voice channel and may need Stage moderator approval to speak. Ordinary controls retain their existing permission checks; the three legacy setup commands remain bot-owner-only.

| Command | Purpose |
| --- | --- |
| `[p]audio` | Show the command overview. |
| `[p]audio play <query>` | Search or queue music; alias `p`. |
| `[p]audio join` | Join or move to your voice channel; aliases `connect`, `summon`. |
| `[p]audio leave` | Disconnect and clear the queue; aliases `dc`, `disconnect`. |
| `[p]audio skip` | Skip current playback or lookup; aliases `next`, `s`. |
| `[p]audio stop` | Stop playback and clear the queue. |
| `[p]audio pause` | Pause the current track. |
| `[p]audio resume` | Resume paused playback. |
| `[p]audio volume [value]` | Show/set volume, clamped to 0 through 1000%; alias `vol`. Values above 100% can clip. |
| `[p]audio np` | Show current track and progress; alias `nowplaying`. |
| `[p]audio queue` | Show the next ten tracks; alias `q`. |
| `[p]audio shuffle` | Shuffle upcoming tracks. |
| `[p]audio repeat [off|track|queue]` | Show/set repeat mode; default off. |
| `[p]audio pingnode` | Check local dependencies and latest playback failure. |
| `[p]audio playerstate` | Inspect the native player's state. |
| `[p]audio debugvc` | Inspect Discord voice flags and local playback state. |
| `[p]audio tone` | Queue a public direct MP3 to test playback independently of YouTube. It still requires internet access to the test URL. |
| `[p]audio speak` | Try to unsuppress/request speaking access on a Stage channel. |
| `[p]audio undeafen` | Try to clear self-mute/self-deafen. |
| `[p]audio fixvoice` | Attempt Stage speaking and voice-flag recovery. |
| `[p]audio rejoin` | Reconnect and restore seekable playback and queue state. |

The four legacy node commands are described in the upgrade table above. Use `[p]help audio <command>` for native Red command help.

## Troubleshooting

1. Run `[p]audio pingnode`. Install missing packages/binaries in the Red environment. AudioPlus loads newly available Downloader voice dependencies automatically; restart Red after upgrading a library already loaded in the process. If a native library cannot import, diagnostics identify PyNaCl or davey separately. These checks verify dependencies, not live provider access or voice delivery.
2. Run `[p]audio stop`, then `[p]audio tone`. If it fails, inspect Red's voice permissions, UDP egress, FFmpeg/Opus availability, and access to the MP3 source.
3. If direct audio works but YouTube fails, update yt-dlp and its matching EJS package, verify Deno/Node meets the required version, and retry a public track. Some provider requests can require authentication or be denied by a provider even with current extraction software. This cog does not automatically collect browser cookies or bypass authentication.
4. Test SoundCloud independently with `[p]audio play scsearch:artist and song`. SoundCloud access is independent of YouTube access.
5. Playback errors appear in the request channel and remain in local diagnostics until the next successful track start. Dependency installation, lookup, voice connection, and decoder failures are reported separately.

The bot owner can update extraction packages without a cog source change:

```text
[p]pipinstall yt-dlp[default]
```

For a breakage already fixed in yt-dlp's nightly channel, the owner can use `[p]pipinstall --pre yt-dlp[default]`, following yt-dlp's release guidance. Red's Downloader passes pip arguments through. No automatic package upgrades run while playing music.

For bug reports, include Red/Discord.py versions, `[p]audio pingnode`, `[p]audio playerstate`, the public query/URL, and the matching Red error. Remove tokens, cookies, passwords, and signed stream URLs before sharing logs.

## Stored data and lifecycle

Only legacy global node settings remain in Red Config, including their old password. Native playback ignores them. The cog does not persist listening histories, playlists, user profiles, audio files, or yt-dlp disk caches. Track metadata, command contexts, errors, queues, volume, and repeat settings stay in memory. The data hooks therefore have no per-user Config records to export or delete.

Unload closes only AudioPlus's players and cancels owned lookups/decoders. Other cogs' voice connections are left alone. Removing a guild also closes its player. Each cog remains independently installable through Downloader.

## Development and references

Regression tests cover Red Config/command compatibility, queue races, paused playback, stale callbacks, repeat, reconnect recovery, provider errors, process cancellation, and real yt-dlp/FFmpeg against a local HTTP audio fixture. The native Discord audio thread and Opus encoding are exercised against local audio too. Discord voice networking and external YouTube/SoundCloud behavior are mocked. A successful test suite does not establish live playback on your server.

- [yt-dlp documentation](https://github.com/yt-dlp/yt-dlp)
- [yt-dlp JavaScript runtime setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
- [Discord.py voice example](https://github.com/Rapptz/discord.py/blob/master/examples/basic_voice.py)
- [PhasecoreX Red image](https://github.com/PhasecoreX/docker-red-discordbot)
