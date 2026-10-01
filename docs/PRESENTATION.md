# Discord presentation

All five cogs use one visual theme for their own command replies, nested command help, input errors, event notices, and direct messages.

Every command and group also has a description for Red's native help formatter. The main help menu lists readable summaries, while help for a group or individual command includes its purpose, syntax, and relevant details.

![Sample cog output](presentation-preview.svg)

This preview uses actual command and event payloads with sample data. It illustrates the theme; Discord controls the final layout, fonts, and emoji rendering.

## Design

| Element | Convention |
| --- | --- |
| Heading | `CogName · Section`, with the same naming pattern throughout. |
| Normal accent | Indigo, `#818CF8`. |
| Success / additions | Mint, `#34D399`. |
| Warning / changes | Amber, `#FBBF24`. |
| Error / destructive events | Rose, `#FB7185`. |
| Settings | Named sections, bold labels, working channel and role mentions. |
| Commands | Inline code with the server's actual prefix. |
| Footer | `Kevin's Cogs`, command guidance where applicable, and page numbers for long results. |
| Events | Existing timestamps, attribution, IDs, thumbnails, and compact-header preferences are retained. |

Settings confirmations are small success cards rather than checkmark reactions. AudioPlus has a sectioned command overview and a now-playing card. LevelPlus member cards show an avatar, total XP, level, and progress to the next level. OwoPlus previews separate transformation metadata from the output.

## Delivery and compatibility

Long descriptions and fields are paginated within Discord's individual and combined embed limits, counting UTF-16 units. Lists and transformed previews retain their complete text. Export attachments appear on the first delivered part only.

Command replies respect Red's embed preference. If the bot lacks **Embed Links**, replies use text with the same headings and sections. Channel notices also fall back to text when that permission is unavailable. Permission checks, command arguments, aliases, Config identifiers, defaults, and saved records remain unchanged.

Custom welcome and goodbye templates keep their text and formatting inside the themed notice. Level-up templates remain message text paired with a level/XP card, retaining the bot's existing mention policy. Routine command responses and logs suppress mentions. OwoPlus webhook reposts keep their transformed content and original sender presentation. Red's global help command, permission-denial messages, and unexpected-exception reporting remain controlled by Red.

## Maintaining the theme

Each cog includes `presentation.py` so Red Downloader can install it independently. Edit the canonical copy in `audioplus`, then copy it to the other four cogs; the consistency test rejects drift. The helper owns colors, heading/footer styling, pagination, fallback text, confirmations, nested help, and input-error formatting. Individual cogs own their screen content and event semantics.

Regenerate the preview with the development dependencies installed:

```sh
python -m scripts.preview_presentation
```

The preview generator uses temporary Config storage and mocked Discord/Lavalink objects. It does not connect to or post on Discord.
