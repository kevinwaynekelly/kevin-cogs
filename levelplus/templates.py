"""Bound announcement formatting before Python can allocate a padded field."""

import re
import unicodedata
from string import Formatter

MAX_TEMPLATE = 500
MAX_FIELD = 512
MAX_MESSAGE = 2000
FIELDS = {"user.mention", "user.name", "user.level", "user.xp"}
FORMATTER = Formatter()


def validate_template(text):
    """Allow documented fields and small format specs, without recursive expansion."""
    if not isinstance(text, str) or len(text) > MAX_TEMPLATE:
        raise ValueError("The level-up template must be at most 500 characters.")
    try:
        parts = tuple(FORMATTER.parse(text))
    except ValueError as exc:
        raise ValueError("The level-up template contains invalid braces.") from exc
    for _, field, spec, conversion in parts:
        if field is None:
            continue
        if field not in FIELDS:
            raise ValueError("Use only user.mention, user.name, user.level, and user.xp fields.")
        if conversion not in (None, "s", "r", "a"):
            raise ValueError("Template conversions must be !s, !r, or !a.")
        if len(spec) > 32 or "{" in spec or "}" in spec:
            raise ValueError("Nested or long template format specifications are not supported.")
        if any(int(number) > MAX_FIELD for number in re.findall(r"\d+", spec)):
            raise ValueError("Template format widths and precision must be at most 512.")
    return parts


def render_template(text, *, mention, name, level, xp):
    """Resolve exact fields with a finite allocation and message-size budget."""
    parts = validate_template(text)
    values = {"user.mention": mention, "user.name": name, "user.level": level, "user.xp": xp}
    rendered = []
    remaining = MAX_MESSAGE
    for literal, field, spec, conversion in parts:
        remaining -= len(literal)
        if remaining < 0:
            raise ValueError("The formatted level-up message exceeds 2000 characters.")
        rendered.append(literal)
        if field is not None:
            value = (
                FORMATTER.convert_field(values[field], conversion) if conversion else values[field]
            )
            output = FORMATTER.format_field(value, spec)
            remaining -= len(output)
            if remaining < 0:
                raise ValueError("The formatted level-up message exceeds 2000 characters.")
            rendered.append(output)
    return "".join(rendered)


def spreadsheet_cell(value):
    """Keep formula-like member aliases literal when a CSV is opened in a spreadsheet."""
    text = str(value)
    initial = next(
        (
            character
            for character in text
            if not character.isspace() and unicodedata.category(character) not in {"Cc", "Cf"}
        ),
        "",
    )
    # Python 3.10's CSV reader/writer reject embedded NULs. Preserve a readable escape in
    # the export, without changing the stored alias or letting NULs hide a formula marker.
    text = text.replace("\x00", "\\0")
    return "'" + text if initial in {"=", "+", "-", "@"} else text
