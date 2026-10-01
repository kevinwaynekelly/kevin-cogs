"""Bounded local PNG rendering using the common Discord palette."""

import io

from .presentation import COLORS


def display_number(value):
    value = max(0, int(value))
    if value.bit_length() > 14000:
        return "Very large"
    if value < 10**12:
        return f"{value:,}"
    digits = str(value)
    return digits[0] + "." + digits[1:3] + "e" + str(len(digits) - 1)


def render_card(name, level, xp, lower, upper, position, badges):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (900, 340), "#171923")
    draw = ImageDraw.Draw(image)

    def font(size):
        return ImageFont.load_default(size=size)

    title, body, small = font(34), font(21), font(16)
    draw.rounded_rectangle((16, 16, 884, 324), radius=22, fill="#232637")
    info, success = f"#{COLORS['info']:06x}", f"#{COLORS['success']:06x}"
    draw.rounded_rectangle((16, 16, 24, 324), radius=4, fill=info)
    draw.text((48, 47), "LEVEL", font=small, fill="#ABB2C8")
    level_text, level_size = display_number(level), 52
    while level_size > 16 and draw.textbbox((0, 0), level_text, font=font(level_size))[2] > 100:
        level_size -= 2
    draw.text((46, 82), level_text, font=font(level_size), fill="white")
    name = " ".join(name.split())[:80] or "Member"
    while draw.textbbox((0, 0), name, font=title)[2] > 675:
        name = name[:-2] + "…"
    draw.text((164, 44), name, font=title, fill="white")
    draw.text((164, 99), f"SERVER RANK  #{display_number(position)}", font=body, fill="#ABB2C8")
    fraction = 1.0 if upper is None else min(1.0, max(0, xp - lower) / max(1, upper - lower))
    draw.rounded_rectangle((164, 158, 844, 180), radius=11, fill="#34384E")
    width = int(680 * fraction)
    if width:
        draw.rounded_rectangle((164, 158, 164 + width, 180), radius=min(11, width // 2), fill=info)
    progress = (
        "Maximum level reached"
        if upper is None
        else f"{display_number(max(0, xp - lower))} / {display_number(max(1, upper - lower))} XP to next level"
    )
    draw.text((164, 201), progress, font=body, fill="white")
    draw.text((164, 249), f"{badges} ACHIEVEMENTS EARNED", font=small, fill=success)
    draw.text((48, 291), "Kevin's Cogs · Level", font=small, fill="#ABB2C8")
    draw.text((610, 291), f"TOTAL XP  {display_number(xp)}", font=small, fill="#ABB2C8")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
