"""Draw ``app/assets/favicon.png`` — the ladder, without an emoji font.

    python scripts/make_favicon.py

Streamlit turns an emoji ``page_icon`` into an SVG data URI with the character
as *text*, so the browser has to own a font covering that codepoint. U+1FA9C
(🪜) is Unicode 13.0 (2020) and many emoji fonts still lack it, which renders
the tab icon as an empty box. Drawing the glyph ourselves removes the
dependency entirely — the same pixels ship to every browser.

Kept as a script rather than a one-off so the icon is reproducible and the
reason it exists stays next to it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PIL import Image, ImageDraw

from fear_ladder import paths

OUTPUT = paths.PROJECT_ROOT / "app" / "assets" / "favicon.png"
#: Drawn large and downsampled, which is how the diagonals end up smooth.
SUPERSAMPLE = 8
SIZE = 128

BACKGROUND = (11, 26, 43, 255)  # deep navy, reads on light and dark tab bars
RAIL = (240, 244, 248, 255)
#: Top rung red, bottom rung green — the deeper the fear, the further UP you
#: climb, so the aggressive end is the top. Same red-fear / green-greed reading
#: as the dashboard palette and as every price chart.
RUNGS = [
    (178, 24, 43, 255),
    (214, 96, 77, 255),
    (244, 165, 130, 255),
    (166, 217, 106, 255),
    (26, 152, 80, 255),
]


def draw(size: int = SIZE) -> Image.Image:
    scale = SUPERSAMPLE
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), BACKGROUND)
    pen = ImageDraw.Draw(image)

    margin = int(canvas * 0.20)
    rail_width = int(canvas * 0.075)
    left = margin
    right = canvas - margin - rail_width
    top = int(canvas * 0.10)
    bottom = canvas - int(canvas * 0.10)

    for x in (left, right):
        pen.rounded_rectangle(
            [x, top, x + rail_width, bottom],
            radius=rail_width // 2,
            fill=RAIL,
        )

    inner_left = left + rail_width
    inner_right = right
    span = bottom - top
    gap = span / (len(RUNGS) + 1)
    for index, colour in enumerate(RUNGS):
        y = top + gap * (index + 1)
        half = rail_width * 0.42
        pen.rounded_rectangle(
            [inner_left, y - half, inner_right, y + half],
            radius=int(half),
            fill=colour,
        )

    return image.resize((size, size), Image.LANCZOS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument("--size", type=int, default=SIZE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    draw(args.size).save(args.out, "PNG", optimize=True)
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
