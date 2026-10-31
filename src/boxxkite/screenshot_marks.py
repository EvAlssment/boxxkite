"""Annotate screenshots with caller-supplied interaction targets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from typing import TypedDict


class InteractionBox(TypedDict):
    x: float
    y: float
    width: float
    height: float
    label: str


def annotate_screenshot(
    screenshot: bytes, elements: Sequence[InteractionBox]
) -> tuple[bytes, dict[int, dict[str, object]]]:
    """Return a PNG with numbered boxes and a 1-based index-to-element map.

    Coordinates are pixels in the screenshot's image space. Input order defines
    numbering; this helper does not discover elements or perform interactions.
    Install ``boxxkite-sandbox[screenshot-marks]`` to use it.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise ImportError(
            "Screenshot annotation requires Pillow; install "
            "boxxkite-sandbox[screenshot-marks]"
        ) from exc

    if not isinstance(screenshot, bytes):
        raise TypeError("screenshot must be bytes")
    try:
        from io import BytesIO

        image = Image.open(BytesIO(screenshot)).convert("RGB")
    except Exception as exc:
        raise ValueError("screenshot must contain a valid image") from exc

    width, height = image.size
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    mapping: dict[int, dict[str, object]] = {}
    for index, element in enumerate(elements, start=1):
        if not isinstance(element, Mapping):
            raise TypeError("each element must be a mapping")
        try:
            x, y, box_width, box_height = (
                float(element[key]) for key in ("x", "y", "width", "height")
            )
            label = element["label"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("each element needs x, y, width, height, and label") from exc
        if not isinstance(label, str) or not label.strip():
            raise ValueError("element labels must be non-empty strings")
        if not all(isfinite(value) for value in (x, y, box_width, box_height)):
            raise ValueError("box coordinates must be finite numbers")
        if (
            x < 0
            or y < 0
            or box_width <= 0
            or box_height <= 0
            or x + box_width > width
            or y + box_height > height
        ):
            raise ValueError(f"element {index} box is outside the screenshot bounds")

        left, top = round(x), round(y)
        right, bottom = round(x + box_width), round(y + box_height)
        draw.rectangle((left, top, right, bottom), outline=(255, 48, 48), width=2)
        text = str(index)
        bounds = draw.textbbox((0, 0), text, font=font)
        badge_width = bounds[2] - bounds[0] + 8
        badge_height = bounds[3] - bounds[1] + 6
        badge_left = min(left, max(0, width - badge_width))
        badge_top = min(top, max(0, height - badge_height))
        draw.rounded_rectangle(
            (badge_left, badge_top, badge_left + badge_width, badge_top + badge_height),
            radius=3,
            fill=(255, 48, 48),
        )
        draw.text(
            (badge_left + 4, badge_top + 2), text, fill=(255, 255, 255), font=font
        )
        mapping[index] = {
            "label": label,
            "box": (x, y, box_width, box_height),
        }

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), mapping
