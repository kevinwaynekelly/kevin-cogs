"""Validate template costs before Python allocates formatted strings."""

import re
from string import Formatter

MAX_TEMPLATE = 2000
MAX_OUTPUT = 4000
MAX_FORMAT_NUMBER = 512
FIELDS = {"user", "mention", "server", "count", "created_at", "joined_at"}
_FORMATTER = Formatter()


def validate_template(template):
    """Allow documented fields and ordinary bounded width/precision formatting."""
    if not isinstance(template, str) or len(template) > MAX_TEMPLATE:
        raise ValueError("Use a welcome or goodbye template of at most 2000 characters.")
    try:
        parts = list(_FORMATTER.parse(template))
    except ValueError as error:
        raise ValueError("The message template has unmatched braces.") from error
    for _, field, spec, conversion in parts:
        if field is None:
            continue
        if field not in FIELDS:
            raise ValueError("Use only the documented welcome and goodbye placeholders.")
        if conversion not in (None, "s", "r", "a"):
            raise ValueError("The message template uses an unsupported conversion.")
        if len(spec) > 32 or "{" in spec or "}" in spec:
            raise ValueError("Nested and excessively long format specifications are unavailable.")
        for number in re.findall(r"\d+", spec):
            if len(number) > 3 or int(number) > MAX_FORMAT_NUMBER:
                raise ValueError("Template widths and precisions must be at most 512.")
    return parts


def render_template(template, values):
    """Bound input, each allocation and total output, including saved unsafe templates."""
    fallback = str(template)[:MAX_OUTPUT]
    try:
        parts = validate_template(template)
        rendered = []
        remaining = MAX_OUTPUT
        for literal, field, spec, conversion in parts:
            if len(literal) > remaining:
                raise ValueError("The formatted message is too long.")
            rendered.append(literal)
            remaining -= len(literal)
            if field is None:
                continue
            value = values[field]
            # Discord constrains these values, but also bound malformed legacy/cached inputs.
            if isinstance(value, str):
                value = value[:MAX_TEMPLATE]
            if conversion is not None:
                value = _FORMATTER.convert_field(value, conversion)
            result = _FORMATTER.format_field(value, spec)
            if len(result) > remaining:
                raise ValueError("The formatted message is too long.")
            rendered.append(result)
            remaining -= len(result)
        return "".join(rendered)
    except (ValueError, KeyError, TypeError, AttributeError):
        return fallback
