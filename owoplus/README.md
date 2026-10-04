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

Reposted messages suppress mentions and split long output into parts. All attachments are downloaded before sending and copied to the first part, up to Discord's ten-file limit. Reposts accept at most 8 MiB per attachment and 16 MiB of attachments per message, even in servers with higher upload limits. Both Discord's size metadata and the actual streamed bytes are checked. Downloads use only Discord attachment CDN URLs, reject redirects, and have a fifteen-second deadline. A failed, oversized or timed-out download leaves the original intact. Messages with original embeds or more than ten attachments are skipped to preserve their rich content. Long-message splitting preserves whitespace and respects the UTF-16 message limit. Webhooks are owned by this bot, created once per channel during concurrent requests, and kept in a bounded cache.

Automatic transformations and explicit repost tests share a maximum of four active operations across the bot and two per server. Busy arrivals are skipped immediately, with their originals retained, instead of creating pending download or rendering waiters. Each operation's attachment bytes are bounded to 16 MiB, so active attachment buffers total at most 64 MiB across the bot. Unload cancels owned active operations, rolls back unfinished webhook copies, closes attachment buffers and closes the download session.

## Slash commands

The command root is `owo`; installation and reload still use `owoplus`. Enable the cog's 55 slash actions once as the bot owner:

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

Red Config stores server settings, channel scope IDs, style dictionaries and decorations, custom words and syllable corrections, intensity/cooldown preferences, and member IDs associated with probability overrides or personal opt-outs. Explicit haiku submissions, author/approver/contest-creator IDs, votes, deadlines and results are stored for up to 90 days. No ordinary chat history is persisted. Undo holds original text only in memory for two minutes, up to 50 active records across servers. Webhook references and syllable lookup caches are held in memory. Transformed text, copied attachments, and the author's display name/avatar are sent to Discord as webhook messages and remain there until removed.

Red's user-data hooks export personal settings, submitted haiku, votes, attribution and active Undo text. Deletion removes personal settings/submissions/votes, clears active Undo text, anonymizes approval/creator attribution on other submissions and removes affected winner references. Already-posted webhook messages and announcements are managed in Discord.

## Channel styles and temporary modes

`[p]owo style set <channel> <owo|pirate|robot> [minutes]` selects a channel style. Zero minutes means permanent; 1 to 10080 minutes expires automatically, including after reload. Threads prefer their own active override, then their parent's override, then Owo. `[p]owo style clear <channel>` restores inheritance; `[p]owo style` lists settings. Store at most 100 active overrides. Expiry is evaluated on each message, so no message is transformed using an expired mode. These settings do not enable automatic processing.

Pirate and robot styles use their own whole-word dictionaries. Full pirate transformations add an Ahoy/Arrr frame; full robot transformations use uppercase transmission text. Both preserve URLs, mentions, emoji, and code. Custom words apply to every style and haiku retains priority. Probabilities, scopes, opt-outs, and cooldowns still apply. `[p]stylize <owo|pirate|robot> <text>` is a member preview without reposting or deleting messages. All style controls and the member preview have slash equivalents.

Channel IDs, style names, and expiry timestamps are saved in the additive `features.channel_styles` map. Expired entries are pruned when setting a new style, and deleted channels are removed.

## Custom styles and author Undo

These member and administrator commands also have slash versions:

| Command | Access and behavior |
| --- | --- |
| `[p]customstyle` | Members list named styles. |
| `[p]customstyle create space {"hello":"greetings","friend":"pilot"}` | Administrators create a whole-word dictionary. |
| `[p]customstyle decorate space "[SPACE] " " END" True` | Administrators set prefix, suffix and uppercase mode. Quote spaces in text commands; slash options are separate fields. |
| `[p]customstyle delete space` | Administrators remove a style and its channel overrides. |
| `[p]stylize space <text>` | Members preview a custom or built-in style. |
| `[p]owo style set #channel space [minutes]` | Administrators select a custom or built-in channel style. |
| `[p]owoundo [original_message_id]` | Authors restore their latest transformed message in the current channel. |
| `[p]owoundoset <enabled>` | Administrators toggle Undo controls for future reposts; default enabled. |

Keep up to ten custom styles, each with 50 word replacements, 60-character replacements and 80-character prefix/suffix. Styles use plain word dictionaries, not executable expressions. Links, mentions, emoji and code keep their content. Global custom words override style dictionaries. `stylize` and `owo style set` now accept a style name rather than a fixed slash choice list. Built-in names are reserved.

After a successful repost, a separate bot card offers **Undo transformation** for the original author. Each click repeats current Red command, server, channel and author checks. Undo edits the webhook copies back to the original text, retains attachments and removes extra transformed parts. The original Discord message ID cannot be recreated. A failed restore attempts to roll back changed text so the author can retry. A failed control card never removes a successful repost. Originals longer than 4,000 UTF-16 units have no Undo record. Controls expire after two minutes or unload and older records expire when the 50-record limit is reached.

## Haiku hall and contests

| Command | Access and behavior |
| --- | --- |
| `[p]haikuhall [entry_id]` | Members browse up to 20 recent approved submissions or view one haiku. |
| `[p]haikuhall submit <text>` | Members explicitly submit their own detected 5-7-5 haiku. |
| `[p]haikuhall review` | Administrators review pending text. |
| `[p]haikuhall approve <entry_id> [approved=True]` | Administrators approve a submission, or reject and remove it with `False`. |
| `[p]haikuhall remove <entry_id>` | Authors remove their own submissions; administrators can remove any. |
| `[p]haikucontest [contest_id]` | Members browse contests, entries and vote counts. |
| `[p]haikucontest create <hours> <title>` | Administrators create a 1 to 168 hour contest in the current channel. |
| `[p]haikucontest submit <contest_id> <text>` | Members enter once per contest. |
| `[p]haikucontest vote <contest_id> <entry_id>` | Members cast or change one vote; self-votes are rejected. |
| `[p]haikucontest withdraw <contest_id>` | Authors withdraw an entry and its votes before the deadline. |
| `[p]haikucontest close <contest_id>` | Administrators close early and announce the winner. |
| `[p]haikucontest delete <contest_id>` | Administrators delete stored entries, votes and results. |

All actions have slash equivalents. English meter detection uses the same approximate engine and server syllable corrections as `haiku`. Text is limited to 300 characters. Ordinary automatic haiku reposts do not enter the hall or a contest. Pending hall text is visible only to its author and administrators until approved. Keep up to 100 hall submissions, three pending per author, ten contests, 50 entries and 500 voters per contest, with a combined 1 MiB storage budget validated before saving. Records expire 90 days after creation, with hourly pruning even for inactive servers.

Voting stops at the deadline. An owned minute task closes expired contests and retries unavailable winner announcements. Most votes wins; ties go to the earliest submission, then entry ID. Results persist across reloads. Successful announcements are marked to avoid routine duplicate deliveries, although a process crash between sending and saving can repeat a notice. Cogs disabled in the server do not close or announce contests until enabled again. Winning entries enter the hall only through normal submission and approval.
