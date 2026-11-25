import base64
import io
from collections.abc import Mapping
from typing import cast

from PIL import Image

from mcp_servers.math_tools.limits import MAX_PNG_BYTES


def mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, dict)
    return cast(Mapping[str, object], value)


def sequence(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def text(value: object) -> str:
    assert isinstance(value, str)
    return value


def number(value: object) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def decode_png(value: object) -> bytes:
    png = base64.b64decode(text(value), validate=True)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) <= MAX_PNG_BYTES
    with Image.open(io.BytesIO(png)) as image:
        image.load()
        assert image.format == "PNG"
        assert image.width > 0
        assert image.height > 0
    return png
