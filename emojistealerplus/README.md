# EmojiStealerPlus

When a member uses an external custom emoji, Scarlet yoinks a copy into that server. Static and animated emojis are supported in messages, edited messages and reactions. Ordinary Unicode and emojis already belonging to that server are ignored. Messages are not deleted or rewritten.

## Install

```text
[p]cog install kevin emojistealerplus
[p]load emojistealerplus
[p]slash enablecog emojistealerplus
[p]slash sync
```

`[p]` is your prefix. Use your configured repository alias if it differs from `kevin`. The cog works independently and shares the same theme as the other Kevin cogs, including optional SettingsHub theme overrides.

Automatic message/edit/reaction capture starts enabled. The bot needs **Create Expressions** in the server, and **Message Content** to inspect member messages. Reaction events must be enabled for reaction capture. Notices additionally need View Channel and Send Messages, with Embed Links optional. Discord still determines which external images are available and whether an upload succeeds.

## Commands

All commands require Red administrator access or Manage Server and run in a server. Prefix and slash actions share checks.

| Prefix | Slash | Purpose |
| --- | --- | --- |
| `[p]emoji` | `/emoji status` | Show capture settings, mapping count and last failure type. |
| `[p]emoji enabled false` | `/emoji enabled` | Pause automatic capture; `true` resumes it. |
| `[p]emoji reactions false` | `/emoji reactions` | Pause only reaction capture. |
| `[p]emoji notify true` | `/emoji notify` | Announce each newly uploaded emoji in its source channel; default false. |
| `[p]emoji channel #chat` | `/emoji channel` | Limit automatic capture to a text channel and its threads; omit the channel to monitor all. |
| `[p]yoink <custom emoji>` | `/yoink` | Copy one custom emoji manually, including while automatic capture is paused. |

## Limits and behavior

Capture ignores bot/webhook messages and bot reactions. One owned worker handles at most 100 queued distinct server/emoji pairs, with two seconds between jobs; excess arrivals are skipped and may be captured on subsequent use. The worker rechecks enablement and Red's per-server disable setting. Unload cancels the worker, clears pending jobs and closes its HTTP session.

Uploads fetch current server emojis and honor separate static/animated slot limits using Discord.py's server limit. Existing emojis are never deleted to make room. Original ID mappings and copied image SHA-256 fingerprints prevent duplicate captures, including concurrent manual/automatic copies. Names are restricted to valid characters and disambiguated when already used. Deleted destinations are pruned during subsequent capture. Identical images already present before this cog copied them cannot be detected without downloading every server emoji.

Images are fetched only from Discord's emoji CDN, without redirects, with a ten-second HTTP deadline and 256 KiB streaming cap. PNG/JPEG/GIF signatures are checked; animated captures must remain GIF. Discord uploads have a thirty-second deadline and may still fail because of platform capacity or rate limits. Status stores only a failure type, never raw response bodies. Optional notices use the shared theme and suppress mentions.

## Data

Red Config retains settings and at most 1000 source/destination mappings, names, animation flags and image fingerprints. No member IDs, chat text or image files are persisted by the cog. Pending emoji and channel IDs remain in bounded memory until processed/unloaded. User-data hooks return no personal records. Copied emojis and posted notices remain on Discord until removed there.

## References

- [Discord emoji API](https://docs.discord.com/developers/resources/emoji)
- [Discord permissions](https://docs.discord.com/developers/topics/permissions)
