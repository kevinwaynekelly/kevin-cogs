"""Keep member-controlled CSV text from becoming spreadsheet formulas."""


def spreadsheet_text(value):
    text = str(value)
    if text.startswith(("\t", "\r", "\n")) or text.lstrip(" \t\r\n\v\f").startswith(
        ("=", "+", "-", "@")
    ):
        return "'" + text
    return text
