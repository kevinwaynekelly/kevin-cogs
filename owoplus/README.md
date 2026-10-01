# OwoPlus

Optional scoped message transformations with keyword replacements, random owo text, and English 5-7-5 haiku formatting.

[Repository setup and installation](../README.md)

OwoPlus reposts transformed text through a webhook using the author's display name and avatar, then attempts to delete the original message. Reposts are new Discord messages. If downloading, sending, or deleting fails, the original is retained and the cog attempts to remove any partial reposts. Rollback can also fail if Discord denies deletion or is unavailable.


Commands, confirmations, and previews use the [shared visual theme](../docs/PRESENTATION.md). Previews separate transformation details from output and paginate long fields. Replies respect Red's embed preference and fall back to text when embeds are unavailable. Webhook reposts keep their original sender presentation and transformed content.

## Setup

`[p]` means your bot's command prefix. The cog is disabled by default:

```text
[p]load owoplus
[p]owo diag
[p]owo preview dude, look at this
[p]owo enable
```

Settings commands require Red administrator access or the **Manage Server** permission. Member commands below require no administrator permission. All commands run in a server. The bot needs **View Channel**, **Send Messages**, **Manage Messages**, and **Manage Webhooks** in channels where messages will be transformed, plus **Embed Links** to display themed command cards. The `test` command also needs **Read Message History**. Threads use a webhook in their parent text or forum channel and need the applicable thread access/send permissions. Enable the bot's Message Content intent for text processing.

Automatic processing defaults to all accessible channels. Configure an allowlist or exclusions with `owo channels`. Threads inherit their parent channel scope, exclusions take priority, and personal opt-outs apply before any transformation. An empty allowlist processes no channels.

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

Keyword matching is case-insensitive and preserves the word's capitalization pattern. Inline code and fenced code blocks are excluded from owo transformations; text containing code is excluded from haiku detection. Full transformation intensity defaults to automatic selection from message length, with optional fixed levels 1 to 5.

Reposted messages suppress mentions and split long output into parts. All attachments are downloaded before sending and copied to the first part, up to Discord's ten-file limit. Messages with original embeds or more than ten attachments are skipped to preserve their rich content. A failed download leaves the original intact. Long-message splitting preserves whitespace and respects the UTF-16 message limit. Webhooks are owned by this bot, created once per channel during concurrent requests, and kept in a bounded cache.

## Slash commands

The command root is `owo`; the old `owoplus` command name is removed. Installation and reload still use `owoplus`. Enable all 37 slash actions once as the bot owner:

```text
[p]slash enablecog owoplus
[p]slash sync
```

Use `/owo status` for settings. All leaf commands below have slash versions, including previews, enable/disable, probability overrides, haiku tools, diagnostics, and the existing message-repost test. Settings retain Red administrator or **Manage Server** checks. `/owooptout`, `/owoify`, and `/haiku` are available to members. Slash groups use offered subcommands; `/owo poem` alone is not an action. After updates, reload `owoplus` and run `slash sync` again. Custom Red rules referencing `owoplus ...` need to be reapplied under `owo ...`.

## Text commands

| Command | Purpose |
| --- | --- |
| `[p]owo` | Show server settings. |
| `[p]owo help` | Show built-in command help. |
| `[p]owo enable` / `[p]owo disable` | Enable or disable automatic transformations server-wide. |
| `[p]owo onein <N>` | Set the full-transformation probability to `1/N`, from `1` to `1000000`. |
| `[p]owo prob add @user <N>` | Override the probability for one member. |
| `[p]owo prob remove @user` | Remove a member's override. |
| `[p]owo prob list` | List probability overrides. |
| `[p]owo ownerbypass [on\|off]` | Show or set bot-owner exemption. |
| `[p]owo poem` | Show the haiku setting. |
| `[p]owo poem on` / `[p]owo poem off` | Enable or disable haiku formatting. |
| `[p]owo poem diag <text>` | Show syllable counts, detected breaks, and haiku output. |
| `[p]owo preview <text>` | Preview a random transformation using the server probability, without replacing a message. |
| `[p]owo diag` | Show settings and relevant permissions in the current channel. |
| `[p]owo test` | Find your latest eligible message among the previous 50 messages, repost it, and attempt to delete it. |

`test` performs a real webhook repost and deletion attempt, even while automatic processing is disabled or owner bypass is enabled. It may repost unchanged text. Use `preview` for a read-only sample. `preview` uses the server probability rather than the caller's override. Test and automatic processing use the same renderer, including italic haiku formatting and per-user probability overrides. Preview uses the server probability. Rendering and optional syllable-engine initialization run outside the event loop; syllable lookups use a bounded cache.

## Scope, preferences, and custom transformations

All of these commands have slash equivalents. Settings stay under `owo`; the three member commands are direct.

| Command | Purpose |
| --- | --- |
| `[p]owooptout [enabled=True]` | Opt out of automatic transformations, or use `False` to opt back in. |
| `[p]owoify <text>` | Render a full transformation without deleting or reposting a message. |
| `[p]haiku <text>` | Format detected 5-7-5 text without changing a source message. |
| `[p]owo channels mode <all\|allowlist>` | Choose server-wide or explicitly allowed channels. |
| `[p]owo channels allow <channel>` | Add a text/forum channel to the allowlist. |
| `[p]owo channels exclude <channel>` | Exclude a text/forum channel and its threads. |
| `[p]owo channels remove <channel>` | Remove the channel from both lists. |
| `[p]owo keywords <enabled>` | Toggle automatic keyword-triggered replacements. |
| `[p]owo intensity <0..5>` | Use automatic intensity at `0`, or a fixed transformation level. |
| `[p]owo cooldown <seconds>` | Limit automatic reposts per member, from `0` to `3600` seconds. |
| `[p]owo words list` | Show configured whole-word replacements. |
| `[p]owo words add <original> <replacement>` | Add or override a word, up to 100 custom entries. |
| `[p]owo words remove <original>` | Remove a custom word or disable a built-in word. |
| `[p]owo words reset <yes>` | Restore the four built-in replacements. |
| `[p]owo syllables list` | Show pronunciation corrections for this server. |
| `[p]owo syllables set <word> <count>` | Store a 1 to 10 syllable correction, up to 500 words. |
| `[p]owo syllables remove <word>` | Remove a correction. |
| `[p]owo setup` | Open requester-bound toggles and allow/exclude channel pickers. |

Member opt-outs apply to automatic processing, including haiku. Manual member commands work while automatic processing is disabled and do not use webhooks or delete messages. The administrator `test` command remains an explicit repost of the caller's own message. Custom words preserve capitalization and skip code; pronunciation corrections stay local to the server. Repost cooldowns are held in memory, reset on reload, and release their reservation after a failed repost.

The new `features` section uses Red's merged defaults. Existing probability overrides, enable state, owner bypass, and haiku settings are preserved without a manual migration.

## Optional syllable packages

No extra package is required to load the cog. If available in the bot's Python environment, the syllable engine tries `pronouncing`, then `g2p_en`, then `pyphen`, followed by its built-in vowel-group heuristic. Missing optional imports, failed backend initialization, and failed word lookups fall through to other backends. Haiku results can vary with the installed backends and pronunciation estimates.

Detection considers English alphabetic words, accepts 3 to 32 words and at most 300 normalized characters, and requires word boundaries that total exactly 5, 7, and 5 syllables. Installing an optional backend does not guarantee accurate meter for every word.

## Stored data

Red Config stores server settings, channel scope IDs, custom words and syllable corrections, intensity/cooldown preferences, and member IDs associated with probability overrides or personal opt-outs. It does not persist message contents in Config. Webhook references and syllable lookup caches are held in memory. Transformed text, copied attachments, and the author's display name/avatar are sent to Discord as webhook messages and remain there until removed.

Red's user-data export/deletion hooks return or remove probability overrides and personal opt-outs across servers. Already-posted webhook messages are managed in Discord.

## Channel styles and temporary modes

`[p]owo style set <channel> <owo|pirate|robot> [minutes]` selects a channel style. Zero minutes means permanent; 1 to 10080 minutes expires automatically, including after reload. Threads prefer their own active override, then their parent's override, then Owo. `[p]owo style clear <channel>` restores inheritance; `[p]owo style` lists settings. Store at most 100 active overrides. Expiry is evaluated on each message, so no message is transformed using an expired mode. These settings do not enable automatic processing.

Pirate and robot styles use their own whole-word dictionaries. Full pirate transformations add an Ahoy/Arrr frame; full robot transformations use uppercase transmission text. Both preserve URLs, mentions, emoji, and code. Custom words apply to every style and haiku retains priority. Probabilities, scopes, opt-outs, and cooldowns still apply. `[p]stylize <owo|pirate|robot> <text>` is a member preview without reposting or deleting messages. All style controls and the member preview have slash equivalents.

Channel IDs, style names, and expiry timestamps are saved in the additive `features.channel_styles` map. Expired entries are pruned when setting a new style, and deleted channels are removed. No message content or additional member records are collected.
