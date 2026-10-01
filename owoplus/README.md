# OwoPlus

Optional server-wide message transformations with keyword replacements, random owo text, and English 5-7-5 haiku formatting.

[Repository setup and installation](../README.md)

OwoPlus reposts transformed text through a webhook using the author's display name and avatar, then attempts to delete the original message. Reposts are new Discord messages. If downloading, sending, or deleting fails, the original is retained and the cog attempts to remove any partial reposts. Rollback can also fail if Discord denies deletion or is unavailable.


Commands, confirmations, and previews use the [shared visual theme](../docs/PRESENTATION.md). Previews separate transformation details from output and paginate long fields. Replies respect Red's embed preference and fall back to text when embeds are unavailable. Webhook reposts keep their original sender presentation and transformed content.

## Setup

`[p]` means your bot's command prefix. The cog is disabled by default:

```text
[p]load owoplus
[p]owoplus diag
[p]owoplus preview dude, look at this
[p]owoplus enable
```

Commands require Red administrator access or the **Manage Server** permission and can only run in a server. The bot needs **View Channel**, **Send Messages**, **Manage Messages**, and **Manage Webhooks** in channels where messages will be transformed, plus **Embed Links** to display themed command cards. The `test` command also needs **Read Message History**. Threads use a webhook in their parent text or forum channel and need the applicable thread access/send permissions. Enable the bot's Message Content intent for text processing.

Once enabled, processing applies throughout the server wherever the bot has access. There is no channel allowlist or exclusion command in this version.

## Transformation behavior

Eligible messages come from humans in a server. Bot messages, webhook messages, messages starting with a recognized bot prefix, and attachment-only messages are skipped. Bot owners are exempt by default.

1. With haiku formatting enabled, eligible text detected as 5-7-5 is reposted as three lines ending in 🌸. This takes priority over the random transformation.
2. Otherwise, each eligible message has a 1-in-N chance of full owo transformation. The default is `N = 1000`.
3. When the random roll misses, these whole-word keywords still transform. Messages without a keyword remain unchanged.

| Original word | Replacement |
| --- | --- |
| `now` | `meow` |
| `bro` | `bwo` |
| `dude` | `duwde` |
| `bud` | `bwud` |

Keyword matching is case-insensitive and preserves the word's capitalization pattern. Inline code and fenced code blocks are excluded from owo transformations; text containing code is excluded from haiku detection. Full transformation intensity is chosen automatically from message length.

Reposted messages suppress mentions and split long output into parts. All attachments are downloaded before sending and copied to the first part, up to Discord's ten-file limit. Messages with original embeds or more than ten attachments are skipped to preserve their rich content. A failed download leaves the original intact. Long-message splitting preserves whitespace and respects the UTF-16 message limit. Webhooks are owned by this bot, created once per channel during concurrent requests, and kept in a bounded cache.

## Commands

| Command | Purpose |
| --- | --- |
| `[p]owoplus` | Show server settings. |
| `[p]owoplus help` | Show built-in command help. |
| `[p]owoplus enable` / `[p]owoplus disable` | Enable or disable automatic transformations server-wide. |
| `[p]owoplus onein <N>` | Set the full-transformation probability to `1/N`, from `1` to `1000000`. |
| `[p]owoplus prob add @user <N>` | Override the probability for one member. |
| `[p]owoplus prob remove @user` | Remove a member's override. |
| `[p]owoplus prob list` | List probability overrides. |
| `[p]owoplus ownerbypass [on\|off]` | Show or set bot-owner exemption. |
| `[p]owoplus poem` | Show the haiku setting. |
| `[p]owoplus poem on` / `[p]owoplus poem off` | Enable or disable haiku formatting. |
| `[p]owoplus poem diag <text>` | Show syllable counts, detected breaks, and haiku output. |
| `[p]owoplus preview <text>` | Preview a random transformation using the server probability, without replacing a message. |
| `[p]owoplus diag` | Show settings and relevant permissions in the current channel. |
| `[p]owoplus test` | Find your latest eligible message among the previous 50 messages, repost it, and attempt to delete it. |

`test` performs a real webhook repost and deletion attempt, even while automatic processing is disabled or owner bypass is enabled. It may repost unchanged text. Use `preview` for a read-only sample. `preview` uses the server probability rather than the caller's override. Test and automatic processing use the same renderer, including italic haiku formatting and per-user probability overrides. Preview uses the server probability. Rendering and optional syllable-engine initialization run outside the event loop; syllable lookups use a bounded cache.

## Optional syllable packages

No extra package is required to load the cog. If available in the bot's Python environment, the syllable engine tries `pronouncing`, then `g2p_en`, then `pyphen`, followed by its built-in vowel-group heuristic. Missing optional imports, failed backend initialization, and failed word lookups fall through to other backends. Haiku results can vary with the installed backends and pronunciation estimates.

Detection considers English alphabetic words, accepts 3 to 32 words and at most 300 normalized characters, and requires word boundaries that total exactly 5, 7, and 5 syllables. Installing an optional backend does not guarantee accurate meter for every word.

## Stored data

Red Config stores server settings and member IDs associated with probability overrides. It does not persist message contents in Config. Webhook references and syllable lookup caches are held in memory. Transformed text, copied attachments, and the author's display name/avatar are sent to Discord as webhook messages and remain there until removed.

Red's user-data export/deletion hooks return or remove probability overrides across servers. Already-posted webhook messages are managed in Discord.
