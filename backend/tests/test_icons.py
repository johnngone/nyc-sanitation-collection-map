import struct
import zlib
from pathlib import Path
from xml.etree import ElementTree


PUBLIC = Path(__file__).resolve().parents[2] / "frontend" / "public"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def decode_rgba_png(path: Path) -> tuple[int, int, bytes]:
    payload = path.read_bytes()
    assert payload.startswith(PNG_SIGNATURE)
    offset = len(PNG_SIGNATURE)
    compressed = bytearray()
    width = height = 0

    while offset < len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_data = payload[offset + 8 : offset + 8 + length]
        offset += length + 12
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", chunk_data)
            )
            assert (bit_depth, color_type, compression, filtering, interlace) == (
                8,
                6,
                0,
                0,
                0,
            )
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    scanlines = zlib.decompress(compressed)
    stride = width * 4
    rows: list[bytearray] = []
    cursor = 0
    for _ in range(height):
        filter_type = scanlines[cursor]
        cursor += 1
        row = bytearray(scanlines[cursor : cursor + stride])
        cursor += stride
        prior = rows[-1] if rows else bytearray(stride)
        for index in range(stride):
            left = row[index - 4] if index >= 4 else 0
            above = prior[index]
            upper_left = prior[index - 4] if index >= 4 else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 0xFF
            elif filter_type == 2:
                row[index] = (row[index] + above) & 0xFF
            elif filter_type == 3:
                row[index] = (row[index] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                estimate = left + above - upper_left
                distances = (
                    abs(estimate - left),
                    abs(estimate - above),
                    abs(estimate - upper_left),
                )
                predictor = (left, above, upper_left)[distances.index(min(distances))]
                row[index] = (row[index] + predictor) & 0xFF
            else:
                assert filter_type == 0
        rows.append(row)
    return width, height, b"".join(rows)


def test_svg_favicon_is_vectorized_and_preserves_pointed_corner() -> None:
    source = (PUBLIC / "favicon.svg").read_text(encoding="utf-8")
    root = ElementTree.fromstring(source)
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    paths = root.findall(".//svg:path", namespace)

    assert root.attrib["viewBox"] == "0 0 512 512"
    assert [path.attrib["fill"] for path in paths] == [
        "#f04035",
        "#18bfe6",
        "#fecd01",
    ]
    assert not root.findall(".//svg:image", namespace)
    assert "data:image" not in source
    assert "L 511 511 511 375.244" in paths[0].attrib["d"]


def test_png_icons_have_expected_dimensions_and_backgrounds() -> None:
    favicon_width, favicon_height, favicon = decode_rgba_png(
        PUBLIC / "favicon-96x96.png"
    )
    assert (favicon_width, favicon_height) == (96, 96)
    assert 0 in favicon[3::4]
    assert 255 in favicon[3::4]

    for filename, expected_size in (
        ("apple-touch-icon.png", 180),
        ("web-app-manifest-192x192.png", 192),
        ("web-app-manifest-512x512.png", 512),
    ):
        width, height, pixels = decode_rgba_png(PUBLIC / filename)
        assert (width, height) == (expected_size, expected_size)
        assert set(pixels[3::4]) == {255}
        assert pixels[:4] == b"\xff\xff\xff\xff"


def test_ico_contains_desktop_favicon_sizes() -> None:
    payload = (PUBLIC / "favicon.ico").read_bytes()
    reserved, image_type, count = struct.unpack("<HHH", payload[:6])
    sizes = []
    for index in range(count):
        width, height = payload[6 + index * 16 : 8 + index * 16]
        sizes.append((width or 256, height or 256))

    assert (reserved, image_type) == (0, 1)
    assert sizes == [(16, 16), (32, 32), (48, 48)]
