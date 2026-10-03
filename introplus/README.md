# IntroPlus

Personal YouTube entrance clips for regular Discord voice channels, using AudioPlus's native yt-dlp and FFmpeg approach. IntroPlus downloads the selected segment ahead of time and plays a local PCM copy on joins. It can be installed on its own, shares the repository's presentation theme and supports prefix and slash commands.

## Install

```text
[p]repo update kevin
[p]cog install kevin introplus
[p]load introplus
[p]slash enablecog introplus
[p]slash sync
```

`[p]` is your prefix, such as `!`; use your own repository alias if different. Slash enable/sync requires the bot owner. If AudioPlus is installed, also update and reload it to enable voice handoff and the shared connection lock.

Downloader installs `yt-dlp[default]`, including EJS. Native playback requires **PyNaCl>=1.5.0,<1.6**, **davey>=0.1.6**, **FFmpeg**, **libopus**, and **Deno 2.3+ or Node.js 22+** in Red's environment. Install native voice libraries once and restart Red after changing them; cog updates do not reinstall these libraries. AudioPlus's [container setup guide](../audioplus/README.md) and optional owner-only `audiorepair` apply to the same dependencies. IntroPlus never installs packages during playback. Enable Discord's Voice States and Members intents so joins and current server membership are available.

The bot needs **View Channel**, **Connect**, and **Speak** in the destination. Stage channels are excluded. A full channel is skipped unless the bot has Move Members. Red/cog disabling, current channel permissions, member presence and saved settings are checked again after media resolution and voice connection.

## Choose an intro

```text
[p]intro set 8 your entrance song
[p]intro start 35
[p]intro test
```

This chooses a video, starts at second 35, and plays up to eight seconds when you join voice. A YouTube video link can replace search terms. Setting another video resets the start offset to zero. Duration supports **0.5–30 seconds**; start supports **0–86,400 seconds**, before the known end of the video. Playback ends early if the video ends first. PCM trimming uses 20 ms frames and never plays the full video just because it is longer than the selected clip.

## Faster local playback

Setting a video or changing its duration/start begins a background download of **only the selected audio segment**. Use `[p]intro show` or `/intro show` to see whether the local copy is ready, preparing or failed. A first download still needs YouTube extraction; later joins read the prepared PCM file directly, without another YouTube request, expiring stream URL or FFmpeg startup. Discord still needs to establish voice if the bot is disconnected, and simultaneous joins still wait their turn.

Ready copies live in Red's persistent IntroPlus data directory and survive cog reloads and bot restarts. On upgrade, existing saved intros are prepared automatically in the background, with two concurrent preparations and at most 256 missing clips scheduled per load. A missing or evicted segment is prepared when previewed or requested. If a download fails, `intro test` retries it and `intro status` shows the safe playback result.

The cache holds at most **256 clips and 128 MiB**, evicting least recently used copies. A 30-second stereo PCM segment uses about 5.5 MiB. Up to two temporary downloads can exist alongside the ready cache. Changing a video or its timing cancels the previous preparation, removes the old copy and builds its replacement; volume changes apply while reading and need no download. Clearing a clip, member/server removal and user-data deletion remove the associated audio. Unload cancels pending downloads and removes partial files while preserving completed copies. Full-length videos and signed stream URLs are never saved.

Personal configuration is separate in each server. Members can set, view, change and clear their own clips. Members need no special permission to preview their own intro; they must be in a regular voice channel. Managers can assign/remove other members' intros and preview them in the manager's current channel. Automatic playback starts enabled, but only members with a saved clip have an intro. Clearing your clip opts out.

## Commands

Every row has the matching `/intro` slash subcommand, except the additional prefix-only `intro progress` alias for status.

| Prefix command | Purpose |
| --- | --- |
| `[p]intro` or `[p]intro status` | Server policy, your setup, local-copy readiness, queue and latest safe result. Slash: `/intro status`. |
| `[p]intro help` | Command help. |
| `[p]intro set <seconds> <video-or-search>` | Set your video and duration. |
| `[p]intro show [member]` | Show a member's public video, timing and local-copy readiness. |
| `[p]intro duration <seconds>` | Change your clip length. |
| `[p]intro start <seconds>` | Change your clip's starting point. |
| `[p]intro clear` | Remove your clip and cancel its pending playback. |
| `[p]intro test [member]` | Queue an audible preview in your current voice channel. Testing another member requires manager access. |
| `[p]intro assign <member> <seconds> <video-or-search>` | Manager: choose another member's clip. |
| `[p]intro remove <member>` | Manager: remove another member's clip. |
| `[p]intro enable <true-or-false>` | Manager: enable/disable automatic intros. |
| `[p]intro volume <1–100>` | Manager: set upcoming intro volume; default 70%. |
| `[p]intro cooldown <10–3600>` | Manager: set per-member automatic cooldown; default 60 seconds. |
| `[p]intro channel [voice-channel]` | Manager: restrict intros to one channel; omit to allow all regular voice channels. |
| `[p]intro stop` | Manager: stop current/queued intros while preserving AudioPlus music. |
| `[p]intro diagnostics` | Check local dependencies, cache size, pending preparations and latest result. Does not verify live YouTube or Discord access. |

Manager commands require Red administrator or Manage Server, subject to Red's normal owner/permission rules. Administrator controls and parent command restrictions apply to slash commands too. Slash replies are ephemeral; prefix replies use the shared theme and suppress mentions.

## Music coexistence and failure handling

When AudioPlus is actively playing PCM music **in the same channel**, IntroPlus overlays the clip with music at 30% of its normal volume for the intro. Music keeps advancing and its queue, repeat mode, history, requester accounting and playback position remain intact. After the clip ends, music returns to its normal volume. Stop/skip/track changes can interrupt an overlay; IntroPlus never restores a stale music source or resumes music that a user paused.

When no cog owns voice, IntroPlus joins, plays its clip and disconnects immediately afterward. Updated AudioPlus can cancel this temporary session and take over on `play`. The two cogs coordinate native connections through one per-server lock. If another cog owns voice, the bot is in another channel, AudioPlus is idle/paused/preparing, or the source uses Opus rather than PCM, the intro is skipped without moving or replacing that session. Intros do not interrupt arbitrary other cogs.

Each server handles one intro at a time, with at most five waiting, and queued requests expire after two minutes. At most 100 servers have active workers. Rapid duplicate joins and members still within cooldown do not queue extra clips. Mute/deafen changes in the same channel do not trigger intros. Moving to another regular voice channel counts as a join, subject to cooldown. Leaving voice, disabling intros or stopping cancels pending playback; background preparation may finish for a future join. Clearing/changing a clip, removing the server/member, privacy deletion or unloading also cancels associated preparation. Failures are recorded as bounded safe server results, visible through `intro status`; raw provider/HTTP errors and signed stream URLs are not printed.

YouTube resolution runs in owned cancellable subprocesses with a 45-second deadline. Cache preparation allows two concurrent jobs and at most 128 pending jobs; each FFmpeg download has a 60-second deadline and a frame/byte output limit. Voice connection uses a 30-second timeout; clip playback has a duration-plus-five-second deadline, and voice disconnect has a ten-second deadline. Cleanup closes/reaps owned FFmpeg processes and preserves active AudioPlus sources.

The shared PCM decoder reports safe HTTP status, network, unsupported-format/protocol or unreadable-copy categories when available. It drains FFmpeg stderr into a bounded 16 KiB memory buffer and discards it on cleanup; signed URLs, headers and arbitrary provider text are not exposed or saved. Remote FFmpeg transport allows HTTPS through a configured HTTP proxy. AudioPlus's fresh-stream playback retry applies to its music player; IntroPlus keeps its existing bounded clip preparation and playback deadlines.

## YouTube configuration

Lavalink's `youtube.oauth` and `youtube.remoteCipher` YAML do not configure this native backend. Both cogs use yt-dlp/EJS with a local JavaScript runtime. Current [yt-dlp guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#logging-in-with-oauth) says YouTube OAuth no longer works with yt-dlp and recommends cookies when account access is necessary. This cog uses public videos and stores no account credentials. Update yt-dlp/EJS and the JavaScript runtime when extraction fails.

## Data and verification

Saved records include the member's public video metadata and selected start/duration, plus server policy. The bounded local PCM cache associates the selected audio segment with server/member IDs and clip timing. No full-length downloads, yt-dlp cache, signed streams, OAuth/cipher secrets or cookies are persisted. Cooldowns, queued member IDs and safe server results are transient and bounded. User-data export returns only that user's saved choices/timing, not audio files. Deletion removes choices and cached segments and cancels related pending jobs/commands; member departure deletes that server's personal clip/cache, server departure deletes its settings/clips/cache, and unload clears transient work while preserving saved choices and ready copies.

Tests run real Red prefix/slash checks and Config, real PCM mixing and local FFmpeg segment downloads, seek/trimming and file playback. They verify lookup-free replays, restart reuse, timing replacement, LRU bounds, orphan cleanup, subprocess cancellation/timeout and privacy deletion. Discord and YouTube boundaries are mocked. Live Scarlet voice/YouTube playback remains unverified.
