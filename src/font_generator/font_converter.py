from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


class FontConverter:
    """Build an EFT font directory from an ordered list of font sources.

    The configuration is expected to have global output dimensions and an
    ordered ``fonts`` list. When a codepoint appears in more than one source,
    the last source selecting it wins.
    """

    def __init__(self, config: dict[str, Any]):
        if not isinstance(config, dict):
            raise TypeError("The YAML root must be a mapping/object")

        self.config = config
        self.name = str(config.get("name", "")).strip()
        if not self.name:
            raise ValueError("Missing required configuration field: name")

        self.font_size = int(config.get("font_size", 16))
        self.width = int(config.get("width", 0))
        self.height = int(config.get("height", 0))
        self.output_root = Path(config.get("output", "output"))
        self.preview_enabled = bool(config.get("preview", False))
        self.preview_count = int(config.get("preview_count", 10))

        if self.font_size <= 0:
            raise ValueError("font_size must be greater than 0")
        if self.width <= 0:
            raise ValueError("width must be greater than 0")
        if self.height <= 0:
            raise ValueError("height must be greater than 0")
        if self.width > 0xFFFF or self.height > 0xFFFF:
            raise ValueError("width and height must fit in an unsigned 16-bit integer")
        if self.preview_count < 0:
            raise ValueError("preview_count cannot be negative")

        font_configs = config.get("fonts")
        if not isinstance(font_configs, list) or not font_configs:
            raise ValueError("fonts must be a non-empty YAML list")

        # Load every font once. Relative paths are accepted as-is, so both
        # "SomeFont.ttf" and "fonts/SomeFont.ttf" work from the current folder.
        self.fonts: list[dict[str, Any]] = []
        for index, font_config in enumerate(font_configs, start=1):
            if not isinstance(font_config, dict):
                raise ValueError(f"fonts[{index - 1}] must be a mapping")

            font_value = font_config.get("font")
            if not isinstance(font_value, str) or not font_value.strip():
                raise ValueError(f"fonts[{index - 1}] is missing a valid 'font' path")

            font_path = Path(font_value).expanduser()
            if not font_path.is_file():
                raise FileNotFoundError(f"Font not found for fonts[{index - 1}]: {font_path}")

            font = ImageFont.truetype(str(font_path), self.font_size)
            font_name, font_style = font.getname()
            ascent, descent = font.getmetrics()

            ranges = font_config.get("ranges", [])
            individual = font_config.get("individual", [])
            if ranges is None:
                ranges = []
            if individual is None:
                individual = []
            if not isinstance(ranges, list):
                raise ValueError(f"fonts[{index - 1}].ranges must be a list")
            if not isinstance(individual, list):
                raise ValueError(f"fonts[{index - 1}].individual must be a list")

            self.fonts.append(
                {
                    "path": font_path,
                    "font": font,
                    "name": font_name,
                    "style": font_style,
                    "ascent": ascent,
                    "descent": descent,
                    "ranges": ranges,
                    "individual": individual,
                    "label": f"font #{index} ({font_path})",
                }
            )

        # Baseline is a global property of the output bitmap. If unspecified,
        # vertically center the primary font's ascent/descent metrics in the
        # cell. This commonly gives 13 for a 16px font in a 16px-high cell.
        self.baseline_explicit = config.get("baseline") is not None
        if self.baseline_explicit:
            self.baseline = int(config["baseline"])
        else:
            primary = self.fonts[0]
            self.baseline = (
                self.height + primary["ascent"] - primary["descent"]
            ) // 2
            self.baseline = max(0, min(self.baseline, self.height))

        if not 0 <= self.baseline <= self.height:
            raise ValueError("baseline must be between 0 and height")

        # Bitmap sizes: rows are byte-padded, with pixels stored MSB-first.
        self.row_bytes = (self.width + 7) // 8
        self.glyph_bytes = self.row_bytes * self.height

        # Filled by get_glyphs(): maps each selected codepoint to the font
        # definition that wins it. Assignments are deliberately top-to-bottom.
        self.glyph_sources: dict[int, dict[str, Any]] = {}

        print("Font sources:")
        for index, source in enumerate(self.fonts, start=1):
            print(
                f"  {index}. {source['name']}, {source['style']} "
                f"({source['path']}) — ascent={source['ascent']}px, "
                f"descent={source['descent']}px"
            )
        print(f"Glyph cell: {self.width}x{self.height}px")
        print(f"Font size: {self.font_size}px")
        print(
            f"Baseline: {self.baseline}px"
            + (" (explicit)" if self.baseline_explicit else " (automatic)")
        )
        print(f"Glyph storage: {self.glyph_bytes} bytes")

    # ======================================================
    # Unicode helpers
    # ======================================================

    @staticmethod
    def parse_codepoint(value: Any) -> int:
        """Parse U+0041, 0x41, 0041, or a single literal character."""
        text = str(value).strip()
        if not text:
            raise ValueError("Empty Unicode codepoint")

        upper = text.upper()
        if upper.startswith("U+"):
            digits = upper[2:]
        elif upper.startswith("0X"):
            digits = upper[2:]
        elif len(text) == 1:
            return ord(text)
        else:
            digits = text

        try:
            codepoint = int(digits, 16)
        except ValueError as exc:
            raise ValueError(f"Invalid Unicode codepoint: {text!r}") from exc

        if not FontConverter.is_valid_codepoint(codepoint):
            raise ValueError(f"Invalid Unicode codepoint: U+{codepoint:04X}")
        return codepoint

    @staticmethod
    def is_valid_codepoint(codepoint: int) -> bool:
        return (
            0 <= codepoint <= 0x10FFFF
            and not 0xD800 <= codepoint <= 0xDFFF
        )

    def parse_range(self, value: Any) -> tuple[int, int]:
        text = str(value).strip()
        if text.count("-") != 1:
            raise ValueError(
                f"Invalid range {text!r}. Expected format 'U+0020-U+007E'"
            )

        start_text, end_text = (part.strip() for part in text.split("-", 1))
        start = self.parse_codepoint(start_text)
        end = self.parse_codepoint(end_text)
        if start > end:
            raise ValueError(f"Invalid range {text!r}: start is greater than end")
        return start, end

    def get_glyphs(self) -> list[int]:
        """Get all selected codepoints; the last matching font wins."""
        self.glyph_sources = {}

        for source in self.fonts:
            selected: set[int] = set()

            for value in source["ranges"]:
                start, end = self.parse_range(value)
                selected.update(range(start, end + 1))

            for value in source["individual"]:
                selected.add(self.parse_codepoint(value))

            # Overwrite the source for matching codepoints. This is the core
            # overlay rule: later entries in `fonts` take precedence.
            for codepoint in selected:
                self.glyph_sources[codepoint] = source

        return sorted(self.glyph_sources)

    # ======================================================
    # Render glyph / convert bitmap
    # ======================================================

    def render_glyph(
        self,
        codepoint: int,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None,
    ) -> Image.Image:
        if font is None:
            source = self.glyph_sources.get(codepoint)
            if source is None:
                raise KeyError(f"No font source selected for U+{codepoint:04X}")
            font = source["font"]

        image = Image.new("1", (self.width, self.height), 0)
        draw = ImageDraw.Draw(image)
        draw.text(
            (0, self.baseline),
            chr(codepoint),
            font=font,
            fill=1,
            anchor="ls",
        )
        return image

    def image_to_bitmap(self, image: Image.Image) -> bytes:
        output = bytearray(self.glyph_bytes)
        for y in range(self.height):
            for x in range(self.width):
                if image.getpixel((x, y)):
                    byte_index = y * self.row_bytes + x // 8
                    bit = 7 - (x & 7)
                    output[byte_index] |= 1 << bit
        return bytes(output)

    def generate_glyph(self, codepoint: int) -> bytes:
        return self.image_to_bitmap(self.render_glyph(codepoint))

    # ======================================================
    # Metadata and undefined glyph
    # ======================================================

    def write_info(self, output_dir: Path) -> None:
        # <4sBBHHH is 12 bytes, matching the existing EFT info layout.
        data = struct.pack(
            "<4sBBHHH",
            b"EFT\0",
            1,
            1,
            self.width,
            self.height,
            self.baseline,
        )
        (output_dir / "info").write_bytes(data)

    def create_undefined_glyph(self) -> Image.Image:
        image = Image.new("1", (self.width, self.height), 0)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, self.width - 1, self.height - 1), outline=1)

        if self.width >= 5 and self.height >= 5:
            draw.line((2, 2, self.width - 3, self.height - 3), fill=1)
            draw.line((self.width - 3, 2, 2, self.height - 3), fill=1)
        return image

    def write_undefined(self, output_dir: Path) -> None:
        data = self.image_to_bitmap(self.create_undefined_glyph())
        (output_dir / "undefined.bmp").write_bytes(data)

    # ======================================================
    # Output file structure helpers
    # ======================================================

    @staticmethod
    def range_start(codepoint: int) -> int:
        return codepoint & ~0xFF

    @staticmethod
    def chunk_start(codepoint: int) -> int:
        return codepoint & ~0x1F

    def generate_range(
        self,
        range_start: int,
        glyphs: list[int],
        bitmaps: dict[int, bytes],
        output_dir: Path,
    ) -> None:
        range_dir = output_dir / f"{range_start:06X}"
        range_dir.mkdir(parents=True, exist_ok=True)

        # Presence index: one bit for each codepoint in this 256-codepoint page.
        index = bytearray(32)
        for codepoint in glyphs:
            slot = codepoint & 0xFF
            index[slot >> 3] |= 1 << (slot & 7)
        (range_dir / "index").write_bytes(bytes(index))

        chunks: dict[int, list[int]] = {}
        for codepoint in glyphs:
            chunk = self.chunk_start(codepoint)
            chunks.setdefault(chunk, []).append(codepoint)

        for chunk, chunk_glyphs in sorted(chunks.items()):
            # Rebuild each chunk from the final merged glyph map. This is
            # important: an overlay must not zero out unrelated earlier glyphs
            # that occupy other slots in this same 32-codepoint chunk.
            chunk_data = bytearray(self.glyph_bytes * 32)
            for codepoint in chunk_glyphs:
                slot = codepoint & 0x1F
                offset = slot * self.glyph_bytes
                bitmap = bitmaps[codepoint]
                chunk_data[offset : offset + self.glyph_bytes] = bitmap

            (range_dir / f"{chunk:04X}.bmp").write_bytes(bytes(chunk_data))

    # ======================================================
    # Preview
    # ======================================================

    def preview_glyph(self, codepoint: int) -> None:
        source = self.glyph_sources[codepoint]
        image = self.render_glyph(codepoint, source["font"])
        character = chr(codepoint)

        print()
        print(f"U+{codepoint:04X} {character!r} {self.width}x{self.height} — {source['path']}")
        print()
        for y in range(self.height):
            line = "".join(
                "##" if image.getpixel((x, y)) else "  "
                for x in range(self.width)
            )
            if y == self.baseline:
                print(line + "  <- baseline")
            else:
                print(line)
        print()

    # ======================================================
    # Generate output
    # ======================================================

    def generate(self) -> None:
        glyphs = self.get_glyphs()

        print()
        print(f"Generating: {self.name}")
        print(f"Font sources: {len(self.fonts)}")
        print(f"Glyphs selected (after overlays): {len(glyphs)}")

        if not glyphs:
            print("No glyphs selected; nothing to write.")
            return

        output_dir = self.output_root / self.name
        output_dir.mkdir(parents=True, exist_ok=True)

        self.write_info(output_dir)
        self.write_undefined(output_dir)

        print("\nRasterizing final glyphs...")
        bitmaps: dict[int, bytes] = {}
        for codepoint in glyphs:
            source = self.glyph_sources[codepoint]
            bitmaps[codepoint] = self.image_to_bitmap(
                self.render_glyph(codepoint, source["font"])
            )

        ranges: dict[int, list[int]] = {}
        for codepoint in glyphs:
            page = self.range_start(codepoint)
            ranges.setdefault(page, []).append(codepoint)

        for page, page_glyphs in sorted(ranges.items()):
            self.generate_range(page, page_glyphs, bitmaps, output_dir)

        chunk_count = sum(
            len({self.chunk_start(codepoint) for codepoint in page_glyphs})
            for page_glyphs in ranges.values()
        )
        index_bytes = len(ranges) * 32
        bitmap_bytes = chunk_count * 32 * self.glyph_bytes
        undefined_bytes = self.glyph_bytes
        info_bytes = 12
        total_bytes = info_bytes + undefined_bytes + index_bytes + bitmap_bytes

        print()
        print("=" * 60)
        print(f"Generated:      {self.name}")
        print("=" * 60)
        print(f"Glyphs:         {len(glyphs)}")
        print(f"Ranges:         {len(ranges)}")
        print(f"Chunks:         {chunk_count}")
        print(f"Glyph size:     {self.glyph_bytes} bytes")
        print(f"Index storage:  {index_bytes} bytes")
        print(f"Bitmap storage: {bitmap_bytes} bytes")
        print(f"Total storage:  {total_bytes} bytes")
        print(f"Output:         {output_dir}")

        if self.preview_enabled:
            count = min(self.preview_count, len(glyphs))
            print(f"\nPreviewing {count} glyph(s) in terminal...")
            for codepoint in glyphs[:count]:
                self.preview_glyph(codepoint)


def load_yaml_config(config_path: Path) -> dict[str, Any]:
    """Load a YAML file. Requires PyYAML (``pip install pyyaml``)."""
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required to load YAML config files. Install it with: pip install pyyaml"
        ) from exc

    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    if not isinstance(config, dict):
        raise ValueError("The YAML root must be a mapping/object")

    # Resolve font paths relative to the working directory first (preserving
    # prior behavior); if not found there, try relative to the YAML file.
    fonts = config.get("fonts", [])
    if isinstance(fonts, list):
        for source in fonts:
            if not isinstance(source, dict) or not isinstance(source.get("font"), str):
                continue
            font_path = Path(source["font"]).expanduser()
            if not font_path.is_absolute() and not font_path.is_file():
                config_relative = config_path.resolve().parent / font_path
                if config_relative.is_file():
                    source["font"] = str(config_relative)

    return config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate an EFT bitmap font from an ordered YAML font stack."
    )
    parser.add_argument("config", type=Path, help="Path to the YAML configuration file")
    args = parser.parse_args()

    try:
        config = load_yaml_config(args.config)
        FontConverter(config).generate()
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
