"""Read and inspect EFT font directories, independently of the generator.

Everything here is written from Font-Format.md alone and shares no code with
the generator, so reading a font back with it checks that the output can be
read the way the firmware will read it.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path
from typing import NamedTuple

from PIL import Image


# Org-Calendar e-ink panel (eink-driver.h): 800x480, 1bpp, 1 = white.
DISPLAY_WIDTH = 800
DISPLAY_HEIGHT = 480

HEX6 = re.compile(r"[0-9A-F]{6}")
CHUNK_NAME = re.compile(r"([0-9A-F]{6})\.bmp")


class Glyph(NamedTuple):
    """A stored bitmap and the width of its cell in pixels."""

    data: bytes
    width: int


class EftFont:
    """One TypeSize directory, read the way the firmware reads it."""

    def __init__(self, font_dir: Path):
        self.dir = Path(font_dir)

        info = (self.dir / "info").read_bytes()
        if len(info) != 12:
            raise ValueError(f"{self.dir}/info is {len(info)} bytes, expected 12")
        magic, self.version, self.bpp, self.width, self.height, self.baseline = (
            struct.unpack("<4sBBHHH", info)
        )
        if magic != b"EFT\0":
            raise ValueError(f"{self.dir}/info has bad magic {magic!r}")
        if self.version != 1 or self.bpp != 1:
            raise ValueError(
                f"{self.dir}/info: unsupported version {self.version} / {self.bpp} bpp"
            )
        if self.width == 0 or self.height == 0:
            raise ValueError(f"{self.dir}/info: empty glyph cell")

        # info has no polarity flag: an inverted variant is marked only by
        # the "inv" suffix of its TypeSize directory name.
        self.inverted = self.dir.name.endswith("inv")
        self.background = 0x00 if self.inverted else 0xFF

        self.undefined = Glyph((self.dir / "undefined.bmp").read_bytes(), self.width)

    def glyph_bytes(self, width: int) -> int:
        return (width + 7) // 8 * self.height

    @staticmethod
    def is_wide(range_dir: Path) -> bool:
        return (range_dir / "wide").is_file()

    # ======================================================
    # Lookup (Font-Format.md section 10)
    # ======================================================

    def lookup(self, codepoint: int) -> Glyph | None:
        """Return the stored glyph, or None if the font doesn't have it."""
        range_dir = self.dir / f"{codepoint & ~0xFF:06X}"
        if not range_dir.is_dir():
            return None

        index = (range_dir / "index").read_bytes()
        slot = codepoint & 0xFF
        if not index[slot >> 3] & (1 << (slot & 7)):
            return None

        width = self.width * 2 if self.is_wide(range_dir) else self.width
        size = self.glyph_bytes(width)
        chunk = range_dir / f"{codepoint & ~0x1F:06X}.bmp"
        with chunk.open("rb") as handle:
            handle.seek((codepoint & 0x1F) * size)
            return Glyph(handle.read(size), width)

    def glyph(self, codepoint: int) -> Glyph:
        """Like the renderer: fall back to undefined.bmp for missing glyphs."""
        glyph = self.lookup(codepoint)
        return self.undefined if glyph is None else glyph

    def range_dirs(self) -> list[Path]:
        return [
            path
            for path in sorted(self.dir.iterdir())
            if path.is_dir() and HEX6.fullmatch(path.name)
        ]

    def codepoints(self) -> list[int]:
        """Every codepoint whose presence bit is set."""
        present = []
        for range_dir in self.range_dirs():
            index_path = range_dir / "index"
            if not index_path.is_file():
                continue  # reported by verify()
            index = index_path.read_bytes()
            if len(index) != 32:
                continue  # reported by verify()

            start = int(range_dir.name, 16)
            present += [
                start + slot
                for slot in range(256)
                if index[slot >> 3] & (1 << (slot & 7))
            ]
        return present

    # ======================================================
    # Pixels
    # ======================================================

    def image(self, glyph: Glyph) -> Image.Image:
        # Glyph rows are byte-padded, MSB-first and 1 = white: exactly PIL's
        # raw mode "1" layout, so the bytes load without any conversion.
        return Image.frombytes("1", (glyph.width, self.height), glyph.data)

    def ink(self, glyph: Glyph) -> list[list[bool]]:
        """Pixels that differ from the background, i.e. the glyph shape."""
        row_bytes = (glyph.width + 7) // 8
        background = self.background & 1
        return [
            [
                (glyph.data[y * row_bytes + x // 8] >> (7 - (x & 7))) & 1 != background
                for x in range(glyph.width)
            ]
            for y in range(self.height)
        ]


# ======================================================
# Format check
# ======================================================

def verify(font: EftFont) -> list[str]:
    """Check a font directory against Font-Format.md; return the problems."""
    problems = []
    if font.baseline > font.height:
        problems.append(f"info: baseline {font.baseline} is below the cell")
    if len(font.undefined.data) != font.glyph_bytes(font.width):
        problems.append(
            f"undefined.bmp is {len(font.undefined.data)} bytes, "
            f"expected {font.glyph_bytes(font.width)}"
        )
    else:
        problems += padding_problems(font, font.undefined, "undefined.bmp")

    for entry in sorted(font.dir.iterdir()):
        if entry.name in ("info", "undefined.bmp"):
            continue
        if not (entry.is_dir() and HEX6.fullmatch(entry.name)):
            problems.append(f"{entry.name}: unexpected entry")
            continue
        problems += verify_range(font, entry)
    return problems


def verify_range(font: EftFont, range_dir: Path) -> list[str]:
    name = range_dir.name
    start = int(name, 16)
    problems = []
    if start & 0xFF or start > 0x10FF00:
        problems.append(f"{name}/: not the start of a 256-codepoint range")

    index_path = range_dir / "index"
    if not index_path.is_file():
        return problems + [f"{name}/index is missing"]
    index = index_path.read_bytes()
    if len(index) != 32:
        return problems + [f"{name}/index is {len(index)} bytes, expected 32"]

    present = {
        start + slot for slot in range(256) if index[slot >> 3] & (1 << (slot & 7))
    }
    if not present:
        problems.append(f"{name}/: index is empty, so the range should not exist")
    if any(0xD800 <= codepoint <= 0xDFFF for codepoint in present):
        problems.append(f"{name}/: surrogates are marked present")

    width = font.width * 2 if font.is_wide(range_dir) else font.width
    size = font.glyph_bytes(width)

    expected = {codepoint & ~0x1F for codepoint in present}
    found = set()
    for path in sorted(range_dir.iterdir()):
        if path.name == "index" or (path.name == "wide" and path.is_file()):
            continue
        match = CHUNK_NAME.fullmatch(path.name)
        if match is None:
            problems.append(f"{name}/{path.name}: unexpected entry")
            continue

        chunk = int(match.group(1), 16)
        found.add(chunk)
        if chunk & 0x1F or chunk & ~0xFF != start:
            problems.append(f"{name}/{path.name}: not a 32-codepoint chunk of this range")

        data = path.read_bytes()
        if len(data) != 32 * size:
            problems.append(f"{name}/{path.name} is {len(data)} bytes, expected {32 * size}")
            continue

        for slot in range(32):
            codepoint = chunk + slot
            glyph = Glyph(data[slot * size : (slot + 1) * size], width)
            if codepoint in present:
                problems += padding_problems(font, glyph, f"U+{codepoint:04X}")
            elif any(byte != font.background for byte in glyph.data):
                problems.append(f"U+{codepoint:04X}: unused slot is not background")

    for chunk in sorted(expected - found):
        problems.append(f"{name}/{chunk:06X}.bmp is missing for glyphs set in the index")
    for chunk in sorted(found - expected):
        problems.append(f"{name}/{chunk:06X}.bmp exists but no glyph in it is set")
    return problems


def padding_problems(font: EftFont, glyph: Glyph, label: str) -> list[str]:
    if glyph.width % 8 == 0:
        return []
    row_bytes = (glyph.width + 7) // 8
    mask = 0xFF >> (glyph.width % 8)
    for row in range(font.height):
        last = glyph.data[row * row_bytes + row_bytes - 1]
        if last & mask != font.background & mask:
            return [f"{label}: row padding bits are not background"]
    return []


# ======================================================
# Rendering
# ======================================================

def glyph_lines(font: EftFont, codepoint: int) -> list[str]:
    """ASCII art of one stored glyph, two characters per pixel."""
    glyph = font.lookup(codepoint)
    title = f"U+{codepoint:04X} {chr(codepoint)!r}"
    if glyph is None:
        glyph = font.undefined
        title += " (not in font, showing undefined.bmp)"
    elif glyph.width != font.width:
        title += " (wide)"

    lines = [title]
    for y, row in enumerate(font.ink(glyph)):
        line = "".join("##" if pixel else ". " for pixel in row)
        lines.append(line + ("  <- baseline" if y == font.baseline else ""))
    return lines


def text_lines(font: EftFont, text: str) -> list[str]:
    """ASCII art of a line of text, one cell per character."""
    cells = [font.ink(font.glyph(ord(character))) for character in text]
    return [
        "".join("#" if pixel else "." for cell in cells for pixel in cell[y])
        for y in range(font.height)
    ]


def text_width(font: EftFont, text: str) -> int:
    return sum(font.glyph(ord(character)).width for character in text)


def render(
    font: EftFont,
    lines: list[str],
    size: tuple[int, int] | None = None,
) -> Image.Image:
    """Draw text like the firmware: copy each glyph's cell onto white and
    advance by its width.

    Without ``size`` the image fits the text; with the display size it is a
    full framebuffer.
    """
    if size is None:
        width = max((text_width(font, line) for line in lines), default=0)
        size = (max(1, width), max(1, len(lines) * font.height))

    screen = Image.new("1", size, 1)
    for row, line in enumerate(lines):
        x = 0
        for character in line:
            glyph = font.glyph(ord(character))
            screen.paste(font.image(glyph), (x, row * font.height))
            x += glyph.width
    return screen


def glyph_sheet(font: EftFont) -> list[str]:
    """One line per stored 32-codepoint chunk, mirroring the file layout."""
    chunks: dict[int, list[str]] = {}
    for codepoint in font.codepoints():
        line = chunks.setdefault(codepoint & ~0x1F, [" "] * 32)
        line[codepoint & 0x1F] = chr(codepoint)
    return ["".join(line) for _, line in sorted(chunks.items())]


# ======================================================
# Command line
# ======================================================

def parse_codepoint(value: str) -> int:
    """Parse U+0041, 0x41 or a single literal character."""
    if len(value) == 1:
        return ord(value)
    upper = value.upper()
    if upper.startswith(("U+", "0X")):
        return int(upper[2:], 16)
    raise ValueError(f"Invalid codepoint {value!r}; use U+0041, 0x41 or A")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect an EFT font directory the way the firmware reads it."
    )
    parser.add_argument("font_dir", type=Path, help="TypeSize directory, e.g. output/regular16")
    parser.add_argument(
        "-g", "--glyph", nargs="+", default=[], metavar="CP",
        help="print stored glyphs, e.g. U+0041 0x67 A",
    )
    parser.add_argument(
        "-t", "--text", action="append", default=[],
        help="print a line of text as the device would draw it (repeatable)",
    )
    parser.add_argument(
        "--png", type=Path,
        help="save the text lines as an image, or every glyph if no --text",
    )
    parser.add_argument(
        "--framebuffer", type=Path,
        help=f"save the text as a {DISPLAY_WIDTH}x{DISPLAY_HEIGHT} framebuffer "
        "in eink_display() format",
    )
    parser.add_argument("--scale", type=int, default=2, help="PNG scale factor (default 2)")
    args = parser.parse_args(argv)

    try:
        font = EftFont(args.font_dir)
        problems = verify(font)
        codepoints = font.codepoints()
        ranges = [
            path.name + (" (wide)" if font.is_wide(path) else "")
            for path in font.range_dirs()
        ]

        print(
            f"{font.dir.name}: {font.width}x{font.height}px, baseline {font.baseline}, "
            + ("inverted (white on black)" if font.inverted else "normal (black on white)")
            + f", {font.glyph_bytes(font.width)} bytes/glyph"
        )
        print(f"{len(codepoints)} glyphs in {len(ranges)} range(s): {', '.join(ranges)}")
        print(f"Format check: {'OK' if not problems else f'{len(problems)} problem(s)'}")
        for problem in problems:
            print(f"  {problem}")

        for value in args.glyph:
            print()
            print("\n".join(glyph_lines(font, parse_codepoint(value))))

        for text in args.text:
            if text_width(font, text) > DISPLAY_WIDTH:
                print(f"\nNote: {text!r} is wider than the display ({DISPLAY_WIDTH}px)")
            print()
            print("\n".join(text_lines(font, text)))

        if args.png:
            lines = args.text or glyph_sheet(font)
            image = render(font, lines)
            if args.scale > 1:
                image = image.resize(
                    (image.width * args.scale, image.height * args.scale), Image.NEAREST
                )
            image.save(args.png)
            print(f"\nSaved {args.png}")

        if args.framebuffer:
            if not args.text:
                raise ValueError("--framebuffer needs at least one --text line")
            screen = render(font, args.text, (DISPLAY_WIDTH, DISPLAY_HEIGHT))
            # PIL packs mode "1" MSB-first with 1 = white, the panel's format.
            args.framebuffer.write_bytes(screen.tobytes())
            print(f"Saved {args.framebuffer} ({DISPLAY_WIDTH * DISPLAY_HEIGHT // 8} bytes)")
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
