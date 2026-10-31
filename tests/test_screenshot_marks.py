from io import BytesIO

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image

from boxxkite.screenshot_marks import annotate_screenshot


def _screenshot() -> bytes:
    output = BytesIO()
    Image.new("RGB", (100, 80), "white").save(output, format="PNG")
    return output.getvalue()


def test_annotation_numbers_in_input_order_and_maps_elements():
    annotated, mapping = annotate_screenshot(
        _screenshot(),
        [
            {"x": 5, "y": 10, "width": 20, "height": 15, "label": "Search"},
            {"x": 40, "y": 30, "width": 25, "height": 20, "label": "Submit"},
        ],
    )

    assert list(mapping) == [1, 2]
    assert mapping[1] == {"label": "Search", "box": (5.0, 10.0, 20.0, 15.0)}
    assert mapping[2] == {"label": "Submit", "box": (40.0, 30.0, 25.0, 20.0)}
    result = Image.open(BytesIO(annotated))
    assert result.format == "PNG"
    assert result.size == (100, 80)
    assert result.getpixel((5, 10)) != (255, 255, 255)


@pytest.mark.parametrize(
    "box",
    [
        {"x": -1, "y": 0, "width": 5, "height": 5, "label": "bad"},
        {"x": 0, "y": 0, "width": 101, "height": 5, "label": "bad"},
        {"x": 0, "y": 0, "width": 0, "height": 5, "label": "bad"},
        {"x": float("nan"), "y": 0, "width": 5, "height": 5, "label": "bad"},
    ],
)
def test_rejects_invalid_bounds(box):
    with pytest.raises(ValueError):
        annotate_screenshot(_screenshot(), [box])


def test_rejects_invalid_image_bytes():
    with pytest.raises(ValueError, match="valid image"):
        annotate_screenshot(b"not an image", [])
