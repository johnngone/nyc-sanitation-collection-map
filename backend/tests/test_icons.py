from pathlib import Path
from xml.etree import ElementTree


PUBLIC = Path(__file__).resolve().parents[2] / "frontend" / "public"


def test_svg_favicon_is_vectorized_and_preserves_pointed_corner() -> None:
    source = (PUBLIC / "favicon.svg").read_text(encoding="utf-8")
    root = ElementTree.fromstring(source)
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    paths = root.findall(".//svg:path", namespace)

    assert [path.attrib["fill"] for path in paths] == [
        "#f04035",
        "#18bfe6",
        "#fecd01",
    ]
    assert not root.findall(".//svg:image", namespace)
    assert "L 511 511 511 375.244" in paths[0].attrib["d"]
