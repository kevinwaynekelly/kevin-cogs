# AudioPlus

Music search, playback, queues, and Discord voice control inside Red. AudioPlus uses **yt-dlp**, **FFmpeg**, and **Discord.py native voice with DAVE encryption support**. A Lavalink server, Wavelink, Java, and a separate YouTube plugin are no longer required.

`[p]` means your bot's command prefix. With `!`, `[p]play roar` becomes `!play roar`. Playback and voice controls also have slash commands, including `/play`, `/skip`, and `/queue`. Replies use the shared Kevin's Cogs theme and fall back to text when embeds are unavailable. Existing `[p]audio ...` commands remain available.

## Requirements

- Red **3.5.24 or newer**, with a Discord.py version that supports DAVE voice. Development checks use Red 3.5.24 and Discord.py 2.7.1 on Python 3.10/3.11.
- Downloader-managed Python package: `yt-dlp[default]>=2026.8.19`, declared in `info.json`. The default extra includes its matching `yt-dlp-ejs` challenge solver.
- Required native voice libraries: `PyNaCl>=1.5.0,<1.6` and `davey>=0.1.6`, installed once in **Red's Python environment** using the instructions below. Working copies already installed by Downloader remain supported. Cog updates do not reinstall these libraries.
- The **FFmpeg executable** and **libopus** in the Red container or host. Installing a Python package named ffmpeg does not install the executable.
- A supported JavaScript runtime in Red's `PATH`, preferably **Deno 2.3+** or **Node.js 22+**, for full YouTube extraction. AudioPlus enables detected Deno, Node, and QuickJS runtimes in yt-dlp.
- Network access from the **Red container** to media providers and Discord voice, including UDP. Playback no longer uses the Lavalink container's network connection.

Downloader installs the media extractor; it cannot install container system packages. Native voice libraries belong to the bot environment and are set up separately so updating command files does not overwrite working native packages. Red imports Discord.py before exposing Downloader's package folder. AudioPlus also initializes voice imports found there before diagnostics or playback, without reloading Discord.py or replacing its classes. **Restart Red after changing voice libraries.** Package versions listed in diagnostics describe installed files; the separate **Discord voice** status reports whether native playback prerequisites can actually load.

AudioPlus can load with missing system dependencies so its help and diagnostics remain available. It uses the `audio` command group, which conflicts with Red's bundled Audio cog. Unload the bundled cog before loading AudioPlus:

```text
[p]unload audio
[p]cog install kevin-cogs audioplus
```

Install missing voice libraries and container dependencies using the setup below, restart Red after changing voice libraries, then run:

```text
[p]load audioplus
[p]audiostatus
```

## Enable slash commands

After loading AudioPlus, run these commands as the bot owner:

```text
[p]slash enablecog audioplus
[p]slash sync
```

Use the lowercase module name `audioplus`. Red keeps application commands disabled until the owner enables them. Then choose `/play` and enter a song name or URL in its `query` option. All 20 direct controls in the command table below have matching slash commands. Prefix aliases such as `[p]p` do not create additional slash names.

When updating an installation that already enabled AudioPlus slash commands, run `[p]slash sync` after reloading to publish command changes. If commands still do not appear, use Red's `[p]invite` to ensure the bot was invited with application-command access.

## Upgrading from the Lavalink backend

Update AudioPlus from this repository, then restart Red:

```text
[p]cog update False audioplus
```

Install the voice libraries and container dependencies below before testing playback. The bot owner can update media-extraction packages with:

```text
[p]pipinstall yt-dlp[default]>=2026.8.19
```

When voice dependencies are already usable, `[p]cog update True audioplus` can update and reload the cog directly. No voice-library reinstall or bot restart is required for a source-only update.

### If Downloader fails to install PyNaCl or davey during an update

Red aborts an update before copying cog files when any declared requirement fails, even if an existing version of that package already works for playback. Current AudioPlus keeps working voice libraries and declares only the media extractor for Downloader to manage.

If native playback already works, leave those libraries in place and install the current AudioPlus revision. For a repository added to Red under the name `kevin`, run as the bot owner:

```text
[p]cog updatetoversion True kevin origin/main audioplus
[p]reload audioplus
[p]help play
[p]audiostatus
[p]slash enablecog audioplus
[p]slash sync
```

Use your actual Red repository name if it differs. If this still reports PyNaCl or davey as failed **requirements**, Downloader is reading older repository metadata; check `[p]repo info kevin` for this repository and its `main` branch. If diagnostics report a missing or unimportable voice library, follow the one-time setup below. For other requirement failures, keep the matching pip `ERROR` lines from the Red container log; Downloader's short failure reply does not contain the underlying reason.

### If new prefix commands do not respond

As the bot owner, update the installed files, explicitly reload the cog, and verify that Red recognizes `play`:

```text
[p]cog update True audioplus
[p]reload audioplus
[p]help play
```

Downloader only offers or performs its automatic reload when an update is installed during that command. If it reports that the cog is already up to date, an older copy can still be active in memory; the explicit `reload` handles that case. Red normally stays silent for unrecognized commands when fuzzy help is disabled.

`[p]help play` should show AudioPlus's song/URL query argument. If reloading fails, keep the full reload reply and matching console error. If `play` remains missing after a successful reload, check `[p]repo info kevin-cogs` for the repository URL and `main` branch, plus `[p]paths` for another copy of AudioPlus. Use your actual repository name if it differs. Updating repository files alone does not update an installed cog. Prefix commands do not require slash enablement or synchronization.

Saved Config identifiers and defaults remain compatible. The old host, port, password, TLS flag, and resume timeout stay saved for rollback but are **ignored by native playback**. In-memory queues reset on reload or restart, as before. Existing commands, aliases, arguments, and permission checks remain registered. Direct controls and slash commands are additional entry points to the same player. Red treats direct controls as new command names, so custom command permission rules on legacy `[p]audio ...` commands should also be applied to the corresponding direct controls.

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

### One-time native voice library setup

Skip this step if `[p]audiostatus` reports usable Discord voice and playback already works. PyNaCl and davey remain required; a source update simply leaves their installed copies alone.

[PhasecoreX's image](https://github.com/PhasecoreX/docker-red-discordbot#extending-this-image) runs Red in `/data/venv`. Run the following in that container as the user running Red, matching its `PUID`/`PGID`:

```sh
/data/venv/bin/python -m pip install --only-binary=:all: 'PyNaCl>=1.5.0,<1.6' 'davey>=0.1.6'
```

Alternatively, run it from the Docker host with `docker exec`. This example assumes a container named `red-discordbot` and UID/GID `1000:1000`; substitute your container name and configured user IDs:

```sh
docker exec --user 1000:1000 red-discordbot /data/venv/bin/python -m pip install --only-binary=:all: 'PyNaCl>=1.5.0,<1.6' 'davey>=0.1.6'
```

Using the bot's interpreter installs into its own environment instead of Downloader's shared target folder. Binary wheels avoid compiling Rust/C dependencies in the running container. The command does not force an upgrade of versions already satisfying the requirements. If no compatible wheel is available for your platform, retain the pip error and use a supported Python/container architecture or build the libraries in your image's build environment.

Restart Red, then run `[p]audiostatus` and `[p]tone`. Do not delete Downloader's existing libraries or install packages into the host's unrelated Python environment. For other images or a non-container installation, substitute the interpreter used to launch Red. The PhasecoreX environment persists on `/data`; repeat setup if an image upgrade recreates that environment for a different Python version.

### FFmpeg, Opus, and JavaScript runtime

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

This places Deno in `/usr/local/bin`, rather than root's private home directory, so Red can find it. Only one supported JavaScript runtime is needed. Node.js and QuickJS may still show `missing` when Deno is available. Run `[p]audiostatus` again, then test `[p]tone` and a YouTube search. These console changes survive a container restart but can disappear when the container is recreated.

The included [Dockerfile](Dockerfile) builds a persistent image for **PhasecoreX's Debian-based Red image family**, carrying FFmpeg, libopus, and Node.js 22. It inherits the existing image's entrypoint, `/data` volume, and bot startup command. Install voice libraries once in the running bot environment as described above; the `/data` volume is created or mounted at runtime and cannot be populated by an image-build pip command.

From a checkout of this repository on your Docker host:

```sh
docker build -f audioplus/Dockerfile --build-arg RED_IMAGE=phasecorex/red-discordbot:core -t kevin-red-native .
```

If you already use a different PhasecoreX tag, pass that same tag as `RED_IMAGE`. In Unraid, use `kevin-red-native` as the Red container's Repository image, keeping its existing `/data` mapping, environment variables, and other settings. Do not replace your appdata mapping. This Dockerfile is an image recipe; automated checks do not build or deploy it to your server.

For another base-image family, add FFmpeg, libopus, and a supported JavaScript runtime using that image's package manager. AudioPlus's `[p]audiostatus` reports what Red can actually use.

## Playback

Run a play command in your server. AudioPlus joins your voice channel when you are in one. If you are not in voice, it chooses the available voice channel with the most people, excluding bots from the count. It skips the AFK channel, channels where it lacks View Channel/Connect/Speak, and full channels unless it has permission to bypass the member limit. Ties, including empty channels, follow the server's channel order. If no channel is available, it explains the permissions or capacity needed. Automatic selection uses ordinary voice channels; joining your existing Stage channel still uses the Stage-speaking checks.

Try:

```text
[p]play roar
[p]play https://www.youtube.com/watch?v=VIDEO_ID
[p]play https://www.youtube.com/playlist?list=PLAYLIST_ID
[p]play scsearch:artist and song
[p]np
[p]queue
```

The same controls work as `/play`, `/np`, and `/queue`. Slash requests are acknowledged before voice connection or media lookup so those operations can finish without exceeding Discord's initial response deadline.

Plain text and `ytsearch:` search YouTube and queue one result. `ytmsearch:` is retained for compatibility and maps to yt-dlp's regular YouTube search. `scsearch:` searches SoundCloud. HTTP/HTTPS media and provider URLs are accepted. YouTube playlist URLs queue up to 100 accessible entries. Other providers supported by yt-dlp can work through their URLs, subject to their access requirements. Lavalink plugin prefixes such as `spsearch:` are rejected with an explanation rather than silently searching another provider.

Play and tone confirmations show the selected track's title, artist/uploader, duration when available, and a clickable source link. Playlist confirmations preview the first five entries and report how many additional tracks were queued. `[p]np` and `/np` show the current title and source link with playback progress. These replies use the shared theme and retain track details when embeds are disabled; they do not expose resolved, signed playback URLs.

Each guild has an independent in-memory player. The queue holds at most 100 upcoming tracks. A paused current track stays paused when additional tracks are queued. Provider stream URLs are resolved immediately before playback to avoid using links that expired while waiting in the queue. Direct media URLs are used as supplied. yt-dlp runs in bounded subprocesses outside Red's event loop, with two concurrent lookups and a 45-second extraction timeout.

Natural completion advances once. Skip cancels the current lookup or playback before advancing. Failed tracks are reported in the latest request channel and the player tries the next queued track. A failed or skipped track is not repeated. Stop clears upcoming tracks and cancels the active playback operation. Rejoin refreshes the stream and restores position, pause state, volume, and the queue for seekable audio. Live streams may restart at their live edge. If reconnection fails, tracks remain available in memory for a later `[p]join` or `/join`.

**AudioPlus disconnects automatically after the queue has been idle for 10 seconds.** Adding a song or starting a new play/tone search cancels the countdown. A failed, cancelled, or empty search starts a fresh countdown once all pending searches finish. Paused tracks, active stream preparation, and repeating playback keep the connection active. Stop, skipping the last track, and exhausting failed tracks also leave after the queue becomes idle. Each server has its own timer, and disconnect/reload/unload cancels it. The next play command connects again using the same channel-selection rules.

## Commands

All playback and voice commands are server commands. The bot needs Connect and Speak in the target voice channel and may need Stage moderator approval to speak. Ordinary controls retain their existing permission checks; the three legacy setup commands remain bot-owner-only.

| Prefix command | Slash command | Purpose |
| --- | --- | --- |
| `[p]play <query>` | `/play` | Search or queue music in your voice channel, or the available channel with the most people; prefix alias `p`. |
| `[p]join` | `/join` | Join or move to your voice channel; prefix aliases `connect`, `summon`. |
| `[p]disconnect` | `/disconnect` | Disconnect and clear the queue; prefix alias `dc`. |
| `[p]skip` | `/skip` | Skip current playback or lookup; prefix aliases `next`, `s`. |
| `[p]stop` | `/stop` | Stop playback and clear the queue. |
| `[p]pause` | `/pause` | Pause the current track. |
| `[p]resume` | `/resume` | Resume paused playback. |
| `[p]volume [value]` | `/volume` | Show/set volume, clamped to 0 through 1000%; prefix alias `vol`. Values above 100% can clip. |
| `[p]np` | `/np` | Show current track and progress; prefix alias `nowplaying`. |
| `[p]queue` | `/queue` | Show the next ten tracks; prefix alias `q`. |
| `[p]shuffle` | `/shuffle` | Shuffle upcoming tracks. |
| `[p]repeat [off\|track\|queue]` | `/repeat` | Show/set repeat mode; default off. Slash offers the three modes as choices. |
| `[p]audiostatus` | `/audiostatus` | Check local dependencies and latest playback failure; prefix alias `pingnode`. |
| `[p]playerstate` | `/playerstate` | Inspect the native player's state. |
| `[p]debugvc` | `/debugvc` | Inspect Discord voice flags and local playback state. |
| `[p]tone` | `/tone` | Queue a public direct MP3 to test playback independently of YouTube. It still requires internet access to the test URL. |
| `[p]speak` | `/speak` | Try to unsuppress/request speaking access on a Stage channel. |
| `[p]undeafen` | `/undeafen` | Try to clear self-mute/self-deafen. |
| `[p]fixvoice` | `/fixvoice` | Attempt Stage speaking and voice-flag recovery. |
| `[p]rejoin` | `/rejoin` | Reconnect and restore seekable playback and queue state. |

Use `[p]audio` for the themed overview or `[p]help AudioPlus` for Red's full command help. Legacy `[p]audio ...` commands retain their names and aliases, including `[p]audio leave` and `[p]audio pingnode`. The four legacy node commands are described in the upgrade table above. Music disconnection uses `[p]disconnect`; Red's core `[p]leave` command retains its server-leaving behavior.

## Player panels, queue tools, and saved music

New music controls have matching slash commands. Automatic now-playing panels are enabled by default and follow queue transitions, with Pause/Resume, Skip, Queue, and Stop buttons. Progress refreshes every 15 seconds while connected. Controls repeat current Red permission and disabled-command checks for the clicking member. Panels respect embed preferences, close on idle disconnect/reload, and never display resolved stream URLs.

| Command | Purpose |
| --- | --- |
| `[p]seek <seconds or 1:23>` | Seek within a playing track with a known duration; preserve pause and upcoming tracks. Live/unknown-duration streams cannot seek. |
| `[p]remove <position>` | Remove an upcoming queue entry. |
| `[p]move <source> <destination>` | Reorder upcoming entries. |
| `[p]playlist` | List your saved playlists in this server. |
| `[p]playlist save <name>` | Save the current track and queue, up to 100 tracks. Replaces that named playlist. |
| `[p]playlist play <name>` / `delete <name>` | Queue or remove your own playlist. Maximum ten playlists per member/server. |
| `[p]favorite` | List your favorites in this server. |
| `[p]favorite add [query]` | Save the current track, or the first result of a search. |
| `[p]favorite remove <position>` / `play [position]` | Remove or queue a favorite; omit the play position to queue all. Maximum 100 favorites. |
| `[p]audioset` | Show player policy settings. |
| `[p]audioset setup` | Open a three-minute guided settings panel with a DJ role picker and feature switches. |
| `[p]audioset panel <true\|false>` | Control automatic player panels. |
| `[p]audioset dj [@Role]` | Require a DJ role for destructive controls. Omit the role to clear it. |
| `[p]audioset voteskip <true\|false>` | Let non-DJ listeners vote to skip. |

`audioset` requires Red admin or Manage Server. Open controls remain the default. With a DJ role, DJs, members with Manage Server, and bot owners can manage playback; other members can still queue music. Vote skip requires at least half of current human listeners, rounded up, and each person counts once per track. Nonprivileged controls require sharing the bot's voice channel when a policy is enabled. A play request cannot move an active protected player to another channel. Private saved collections contain source URLs and track metadata, never extracted playback streams; they survive reloads, while the live queue remains transient.

There are now 36 slash music/settings actions, including `/playlist list`, `/favorite list`, and `/audioset status`. After updating and reloading, run `[p]slash sync` to upload the additions.

## Daily YouTube playback checks

The bot owner can enable a daily check that privately reports failures:

```text
[p]audiocheck enable
[p]audiocheck now
```

Enable it in the server to test. The bot sends a setup DM to your account before enabling, so allow direct messages from the bot. The default schedule is **09:00 America/Chicago**, following Central daylight saving time. The first scheduled check is the next occurrence of that time; `now` tests immediately and counts as today's check. Successful scheduled checks send no messages.

The probe uses the same yt-dlp lookup, stream resolution, FFmpeg decoder, native player, and Discord audio thread as `play`. It sends three seconds of decoded audio **at zero volume** through a temporary voice connection, then closes the decoder and disconnects. It chooses the busiest available ordinary voice channel unless you supply a dedicated channel with `audiocheck enable <voice channel>`. Channel permissions and capacity still apply. No songs are added to the normal queue and its volume/repeat settings are untouched.

If any voice connection or retained music queue is present, the probe waits and retries in 15 minutes, including connections owned by other cogs. Disabling AudioPlus in the test server also postpones the check. This avoids interrupting listening sessions; a bot that is continuously in voice can keep postponing its daily probe.

| Owner-only text command | Purpose |
| --- | --- |
| `[p]audiocheck` | Show schedule, recipient, test video, latest result, and failed DM delivery. |
| `[p]audiocheck enable [voice channel]` | Monitor this server and send failure DMs to the owner who invokes it. Omit the channel for automatic selection. |
| `[p]audiocheck disable` | Stop daily checks, cancel an active probe, and clear pending alerts. |
| `[p]audiocheck now` | Run the configured test immediately. Failures also DM the configured recipient when monitoring is enabled. |
| `[p]audiocheck time 21:00 America/Chicago` | Change the 24-hour local time and IANA timezone. |
| `[p]audiocheck video <YouTube video URL>` | Change the public test video. Choose one with at least three seconds of audio. |

There is one monitor per bot. Enabling it in another server replaces the test server and recipient. The default video is Blender's *Big Buck Bunny*. A failure can mean extraction, dependencies, voice permissions/connection, decoder problems, or a removed/restricted test video; the DM identifies the available cause and includes the public video link. It does not establish that all YouTube videos are broken. A successful check confirms decoding and an active sending voice client, but cannot prove another Discord user received or heard the audio.

The scheduler runs inside the cog while Red is online, checks due work once per minute, and persists its daily cursor across restarts/reloads. After downtime, it runs one overdue check when the current day's check time has passed. Failed DM delivery is retained and retried every 15 minutes; successful delivery clears the pending alert. Probes time out after 150 seconds and clean up their owned resources. Changing settings or disabling/unloading the cog cancels an active probe. The schedule uses Python's timezone database; install `tzdata` in the bot environment if your container lacks `America/Chicago`.

## Troubleshooting

1. Run `[p]audiostatus`. Install missing packages/binaries in the Red environment using the setup above. AudioPlus also supports existing Downloader voice libraries; restart Red after changing a voice library. If a native library cannot import, diagnostics identify PyNaCl or davey separately. These checks verify dependencies, not live provider access or voice delivery.
2. Run `[p]stop`, then `[p]tone`. If it fails, inspect Red's voice permissions, UDP egress, FFmpeg/Opus availability, and access to the MP3 source.
3. If direct audio works but YouTube fails, update yt-dlp and its matching EJS package, verify Deno/Node meets the required version, and retry a public track. Some provider requests can require authentication or be denied by a provider even with current extraction software. This cog does not automatically collect browser cookies or bypass authentication.
4. Test SoundCloud independently with `[p]play scsearch:artist and song`. SoundCloud access is independent of YouTube access.
5. Playback errors appear in the request channel and remain in local diagnostics until the next successful track start. Dependency installation, lookup, voice connection, and decoder failures are reported separately.

The bot owner can update extraction packages without a cog source change:

```text
[p]pipinstall yt-dlp[default]
```

For a breakage already fixed in yt-dlp's nightly channel, the owner can use `[p]pipinstall --pre yt-dlp[default]`, following yt-dlp's release guidance. Red's Downloader passes pip arguments through. No automatic package upgrades run while playing music.

For bug reports, include Red/Discord.py versions, `[p]audiostatus`, `[p]playerstate`, the public query/URL, and the matching Red error. Remove tokens, cookies, passwords, and signed stream URLs before sharing logs.

## Stored data and lifecycle

Legacy global node settings remain in Red Config, including their old password. Native playback ignores them. The daily monitor adds an optional `watchdog` section, disabled by default, without changing those legacy values. Red initializes the added defaults on existing installations. It stores the recipient's Discord ID, test server/channel IDs, public test video URL, schedule/timezone, daily cursor, latest safe result, and pending failure alert/delivery state. User-data hooks export that recipient's monitor record or remove it and disable checking. Deletion does not remove already delivered Discord DMs.

Guild Config additionally stores music panel/DJ/vote preferences, and member-specific saved playlists/favorites containing supplied public source URLs and track metadata. These are exported/deleted by the user's Red data hooks. The new sections use merged defaults, preserving all legacy values. The cog does not store extracted signed streams, listening histories, audio files, or yt-dlp disk caches. Normal command contexts, errors, live queues, volume, repeat settings, skip votes, and panel references stay in memory.

Unload closes only AudioPlus's players and cancels the daily scheduler/probe, owned lookups/decoders, and idle timers. Other cogs' voice connections are left alone. Removing a guild also closes its player. Each cog remains independently installable through Downloader.

## Development and references

Regression tests cover Red Config/command compatibility, real Red hybrid command registration, slash option conversion and callbacks, initial response deferral, automatic channel selection, the 10-second idle deadline and cancellation, queue races, paused playback, stale callbacks, repeat, reconnect recovery, provider errors, process cancellation, and real yt-dlp/FFmpeg against a local HTTP audio fixture. The native Discord audio thread and Opus encoding are exercised against local audio too. Discord command synchronization, voice networking, and external YouTube/SoundCloud behavior are mocked. A successful test suite does not establish live playback on your server.

- [yt-dlp documentation](https://github.com/yt-dlp/yt-dlp)
- [yt-dlp JavaScript runtime setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
- [Discord.py voice example](https://github.com/Rapptz/discord.py/blob/master/examples/basic_voice.py)
- [PhasecoreX Red image](https://github.com/PhasecoreX/docker-red-discordbot)
