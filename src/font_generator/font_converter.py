from __future__ import annotations

import argparse
import re
import shutil
import struct
import sys
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont, TTLibError
from PIL import Image, ImageDraw, ImageFont


# TypeSize directory name: style + nominal size, with an "inv" suffix for
# visually inverted (white-on-black) variants, e.g. regular16 or bold24inv.
NAME_PATTERN = re.compile(r"(regular|bold|italic|bolditalic)(\d+)(inv)?")

# Smallest size tried when shrinking a source to fit the glyph cell.
MIN_FIT_SIZE = 4


class FontConverter:
    """Build an EFT font directory from an ordered list of font sources.

    The configuration is expected to have global output dimensions and an
    ordered ``fonts`` list. When a codepoint appears in more than one source,
    the last source selecting it that actually contains the glyph wins.
    """

    def __init__(self, config: dict[str, Any], config_path: Path | None = None):
        if not isinstance(config, dict):
            raise TypeError("The YAML root must be a mapping/object")

        self.config = config
        self.config_path = config_path
        self.name = str(config.get("name", "")).strip()
        if not self.name:
            raise ValueError("Missing required configuration field: name")

        # The name is a directory that gets deleted and rebuilt on every run,
        # so it is restricted to the TypeSize naming convention.
        self.invert = bool(config.get("invert", False))
        match = NAME_PATTERN.fullmatch(self.name)
        if match is None:
            raise ValueError(
                f"Invalid name {self.name!r}. Expected <style><size>[inv] with style "
                "regular, bold, italic or bolditalic, e.g. regular16 or bold24inv"
            )
        if bool(match.group(3)) != self.invert:
            raise ValueError(
                f"Invalid name {self.name!r}: the 'inv' suffix must be used "
                "exactly when invert is true"
            )

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

            font_size = int(font_config.get("font_size", self.font_size))
            if font_size <= 0:
                raise ValueError(f"fonts[{index - 1}].font_size must be greater than 0")

            # center: center each glyph's ink horizontally in the cell.
            # left: keep the font's own placement (origin at the left edge),
            # needed for box drawing glyphs that must touch the cell edges.
            align = str(font_config.get("align", "center")).strip().lower()
            if align not in ("center", "left"):
                raise ValueError(f"fonts[{index - 1}].align must be 'center' or 'left'")

            # The cmap tells which glyphs the font really has; rendering a
            # missing one would silently produce the font's .notdef box.
            try:
                with TTFont(str(font_path), fontNumber=0, lazy=True) as tt_font:
                    cmap = set(tt_font.getBestCmap() or ())
            except TTLibError as exc:
                raise ValueError(
                    f"Cannot read the cmap of fonts[{index - 1}] ({font_path}): {exc}"
                ) from exc

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

            source: dict[str, Any] = {
                "index": index - 1,
                "path": font_path,
                "configured_size": font_size,
                "fit": bool(font_config.get("fit", False)),
                "align": align,
                "cmap": cmap,
                "ranges": ranges,
                "individual": individual,
                "label": f"font #{index} ({font_path})",
            }
            self.load_font(source, font_size)
            source["name"], source["style"] = source["font"].getname()
            self.fonts.append(source)

        # Baseline is a global property of the output bitmap. If unspecified,
        # it follows the primary font (see auto_baseline).
        self.baseline_explicit = config.get("baseline") is not None
        if self.baseline_explicit:
            self.baseline = int(config["baseline"])
        else:
            primary = self.fonts[0]
            self.baseline = self.auto_baseline(primary["ascent"], primary["descent"])

        if not 0 <= self.baseline <= self.height:
            raise ValueError("baseline must be between 0 and height")

        # Bitmap sizes: rows are byte-padded, with pixels stored MSB-first.
        self.row_bytes = (self.width + 7) // 8
        self.glyph_bytes = self.row_bytes * self.height

        # Framebuffer-native polarity: 1 = white paper, 0 = black ink. An
        # inverted font draws white glyphs on black, so its background is 0.
        self.background_byte = 0x00 if self.invert else 0xFF

        # Filled by get_glyphs(): maps each selected codepoint to the font
        # definition that wins it. Assignments are deliberately top-to-bottom.
        self.glyph_sources: dict[int, dict[str, Any]] = {}

        # Collected while generating and written to the build log; the
        # terminal only shows a count per category.
        self.warnings: dict[str, list[str]] = {}
        self.notes: list[str] = []

    # ======================================================
    # Font loading and metrics
    # ======================================================

    @staticmethod
    def load_font(source: dict[str, Any], size: int) -> None:
        font = ImageFont.truetype(str(source["path"]), size)
        source["font"] = font
        source["size"] = size
        source["ascent"], source["descent"] = font.getmetrics()

    def auto_baseline(self, ascent: int, descent: int) -> int:
        # Vertically center the ascent/descent metrics in the cell. If the
        # font's line height exceeds the cell, both ends get clipped.
        baseline = (self.height + ascent - descent) // 2
        return max(0, min(baseline, self.height))

    def warn(self, category: str, message: str) -> None:
        self.warnings.setdefault(category, []).append(message)

    def describe(self) -> list[str]:
        lines = ["Font sources:"]
        for index, source in enumerate(self.fonts, start=1):
            size = f"{source['size']}px"
            if source["size"] != source["configured_size"]:
                size += f" (fit from {source['configured_size']}px)"
            lines.append(
                f"  {index}. {source['name']}, {source['style']} "
                f"({source['path']}) — {size}, align={source['align']}, "
                f"ascent={source['ascent']}px, "
                f"descent={source['descent']}px"
            )
        lines.append(f"Glyph cell: {self.width}x{self.height}px")
        lines.append(
            f"Baseline: {self.baseline}px"
            + (" (explicit)" if self.baseline_explicit else " (automatic)")
        )
        lines.append(
            "Polarity: "
            + ("inverted (white on black)" if self.invert else "normal (black on white)")
        )
        lines.append(f"Glyph storage: {self.glyph_bytes} bytes")
        return lines

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
        """Get all selected codepoints; the last font containing one wins."""
        self.glyph_sources = {}
        missing: dict[int, list[dict[str, Any]]] = {}

        for source in self.fonts:
            selected: set[int] = set()

            for value in source["ranges"]:
                start, end = self.parse_range(value)
                # Surrogates are not Unicode scalar values and can't be stored.
                valid = {
                    codepoint
                    for codepoint in range(start, end + 1)
                    if self.is_valid_codepoint(codepoint)
                }
                if len(valid) != end - start + 1:
                    self.warn(
                        "surrogates",
                        f"{value} in {source['label']}: dropped U+D800-U+DFFF",
                    )
                selected.update(valid)

            for value in source["individual"]:
                selected.add(self.parse_codepoint(value))

            # Overwrite the source for matching codepoints. This is the core
            # overlay rule: later entries in `fonts` take precedence, but only
            # for glyphs they actually contain, so a font lacking a glyph
            # never replaces an earlier font's real glyph with .notdef.
            for codepoint in selected:
                if codepoint in source["cmap"]:
                    self.glyph_sources[codepoint] = source
                else:
                    missing.setdefault(codepoint, []).append(source)

        for codepoint, sources in sorted(missing.items()):
            winner = self.glyph_sources.get(codepoint)
            if winner is None:
                lacking = ", ".join(source["label"] for source in sources)
                self.warn(
                    "missing-everywhere",
                    f"U+{codepoint:04X} not in {lacking}; undefined.bmp will be used",
                )
                continue

            # A font earlier than the winner would have been overridden
            # anyway; only a later font that lacked the glyph is worth noting.
            later = [source for source in sources if source["index"] > winner["index"]]
            if later:
                lacking = ", ".join(source["label"] for source in later)
                self.warn(
                    "missing-in-source",
                    f"U+{codepoint:04X} not in {lacking}; using {winner['label']}",
                )

        return sorted(self.glyph_sources)

    def won_by(self, source: dict[str, Any], glyphs: list[int]) -> list[int]:
        return [codepoint for codepoint in glyphs if self.glyph_sources[codepoint] is source]

    # ======================================================
    # Render glyph / convert bitmap
    # ======================================================

    def render_glyph(
        self,
        codepoint: int,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None,
        baseline: int | None = None,
        align: str | None = None,
    ) -> tuple[Image.Image, list[str]]:
        """Render a glyph into the cell; also report which cell edges, if
        any, clipped it. Font and alignment default to the winning source."""
        source = self.glyph_sources.get(codepoint)
        if font is None:
            if source is None:
                raise KeyError(f"No font source selected for U+{codepoint:04X}")
            font = source["font"]
        if baseline is None:
            baseline = self.baseline
        if align is None:
            align = source["align"] if source is not None else "left"

        # Draw on a canvas with a margin around the cell, so ink that falls
        # outside the cell is still there to be detected before cropping.
        pad = max(self.width, self.height)
        canvas = Image.new("1", (self.width + 2 * pad, self.height + 2 * pad), 0)
        draw = ImageDraw.Draw(canvas)
        draw.text(
            (pad, baseline + pad),
            chr(codepoint),
            font=font,
            fill=1,
            anchor="ls",
        )

        # The cell is a window into the canvas. Centering moves the window
        # over the ink horizontally; the baseline alone decides the vertical
        # position, so text from different fonts still lines up.
        window = pad
        clipped: list[str] = []
        bbox = canvas.getbbox()
        if bbox is not None:
            left, top, right, bottom = bbox
            if align == "center":
                window = left - (self.width - (right - left)) // 2
            outside = (
                ("left", left < window),
                ("top", top < pad),
                ("right", right > window + self.width),
                ("bottom", bottom > pad + self.height),
            )
            clipped = [edge for edge, is_outside in outside if is_outside]

        image = canvas.crop((window, pad, window + self.width, pad + self.height))
        return image, clipped

    def image_to_bitmap(self, image: Image.Image) -> bytes:
        output = bytearray(self.glyph_bytes)
        for y in range(self.height):
            for x in range(self.width):
                if image.getpixel((x, y)):
                    byte_index = y * self.row_bytes + x // 8
                    bit = 7 - (x & 7)
                    output[byte_index] |= 1 << bit

        # Ink was packed as 1 above. The framebuffer uses 1 for white paper,
        # so flip everything; this also turns row padding into background.
        if not self.invert:
            output = bytearray(byte ^ 0xFF for byte in output)
        return bytes(output)

    def generate_glyph(self, codepoint: int) -> bytes:
        return self.image_to_bitmap(self.render_glyph(codepoint)[0])

    # ======================================================
    # Fitting glyphs into the cell
    # ======================================================

    def fit_size(
        self,
        source: dict[str, Any],
        codepoints: list[int],
        primary: bool,
    ) -> tuple[int | None, int | None]:
        """Find the largest size, starting at the configured one, at which
        none of ``codepoints`` is clipped.

        Returns ``(size, limit)``: ``limit`` is the first glyph that still
        clips one size larger, and ``size`` is None if even MIN_FIT_SIZE clips.
        The whole source is sized as one, so its glyphs stay consistent.
        """
        limit = None
        for size in range(source["configured_size"], MIN_FIT_SIZE - 1, -1):
            font = ImageFont.truetype(str(source["path"]), size)
            baseline = self.baseline
            if primary and not self.baseline_explicit:
                baseline = self.auto_baseline(*font.getmetrics())

            clipped = [
                codepoint
                for codepoint in codepoints
                if self.render_glyph(codepoint, font, baseline, source["align"])[1]
            ]
            if not clipped:
                return size, limit
            limit = clipped[0]
        return None, limit

    def resolve_sizes(self, glyphs: list[int]) -> None:
        """Shrink every source with ``fit: true`` until its glyphs fit.

        The primary source goes first because it sets the automatic baseline
        that every other source is then fitted against.
        """
        for source in self.fonts:
            if not source["fit"]:
                continue

            primary = source["index"] == 0
            configured = source["configured_size"]
            size, limit = self.fit_size(source, self.won_by(source, glyphs), primary)
            if size is None:
                self.warn(
                    "fit",
                    f"{source['label']}: U+{limit:04X} still clips at "
                    f"{MIN_FIT_SIZE}px; keeping {configured}px",
                )
                continue

            self.load_font(source, size)
            if primary and not self.baseline_explicit:
                self.baseline = self.auto_baseline(source["ascent"], source["descent"])

            if limit is None:
                self.notes.append(f"Fit: {source['label']} fits at {size}px")
            else:
                self.notes.append(
                    f"Fit: {source['label']} {configured}px -> {size}px "
                    f"(U+{limit:04X} clips at {size + 1}px)"
                )

    def warn_clipped(
        self,
        glyphs: list[int],
        clipped: dict[int, list[tuple[int, list[str]]]],
    ) -> None:
        for index, entries in sorted(clipped.items()):
            source = self.fonts[index]
            size, limit = self.fit_size(source, self.won_by(source, glyphs), index == 0)
            if size is None:
                hint = (
                    f"source does not fit even at {MIN_FIT_SIZE}px "
                    f"(U+{limit:04X} still clips)"
                )
            else:
                hint = f"source fits at {size}px"

            for codepoint, edges in entries:
                self.warn(
                    "clipped",
                    f"U+{codepoint:04X} from {source['label']} at "
                    f"{source['size']}px, clipped {'/'.join(edges)}; {hint}",
                )

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
            # important: an overlay must not blank out unrelated earlier glyphs
            # that occupy other slots in this same 32-codepoint chunk. Unused
            # slots hold plain background.
            chunk_data = bytearray([self.background_byte]) * (self.glyph_bytes * 32)
            for codepoint in chunk_glyphs:
                slot = codepoint & 0x1F
                offset = slot * self.glyph_bytes
                bitmap = bitmaps[codepoint]
                chunk_data[offset : offset + self.glyph_bytes] = bitmap

            (range_dir / f"{chunk:06X}.bmp").write_bytes(bytes(chunk_data))

    # ======================================================
    # Preview
    # ======================================================

    def preview_glyph(self, codepoint: int) -> None:
        source = self.glyph_sources[codepoint]
        image, _ = self.render_glyph(codepoint, source["font"])
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
    # Build log
    # ======================================================

    def write_log(self, glyphs: list[int], summary: list[str]) -> None:
        """Write the full build log next to the font directory (so it is
        never flashed) and print one line per warning category."""
        lines = [f"Config: {self.config_path or '(not given)'}", ""]
        lines += self.describe()
        if self.notes:
            lines += ["", *self.notes]
        if summary:
            lines += ["", *summary]

        lines += ["", "Glyphs:"]
        for codepoint in glyphs:
            source = self.glyph_sources[codepoint]
            lines.append(
                f"  U+{codepoint:04X} {chr(codepoint)!r} <- {source['label']} "
                f"at {source['size']}px"
            )

        for category, messages in self.warnings.items():
            lines += ["", f"Warnings ({category}): {len(messages)}"]
            lines += [f"  {message}" for message in messages]

        self.output_root.mkdir(parents=True, exist_ok=True)
        log_path = self.output_root / f"{self.name}.log"
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        if self.warnings:
            print("\nWarnings:")
            for category, messages in self.warnings.items():
                print(f"  {category}: {len(messages)}")
        print(f"Build log:      {log_path}")

    # ======================================================
    # Generate output
    # ======================================================

    def generate(self) -> None:
        glyphs = self.get_glyphs()
        self.resolve_sizes(glyphs)

        print("\n".join(self.describe()))
        print()
        print(f"Generating: {self.name}")
        print(f"Font sources: {len(self.fonts)}")
        print(f"Glyphs selected (after overlays): {len(glyphs)}")

        if not glyphs:
            print("No glyphs selected; nothing to write.")
            self.write_log(glyphs, [])
            return

        # Rebuild the font directory from scratch: range directories or
        # chunks left over from an earlier config would still be found by
        # the device. The name is validated, but refuse anything (such as a
        # symlink) that resolves outside the output root.
        output_dir = self.output_root / self.name
        self.output_root.mkdir(parents=True, exist_ok=True)
        if output_dir.resolve().parent != self.output_root.resolve():
            raise ValueError(
                f"Refusing to replace {output_dir}: it resolves outside {self.output_root}"
            )
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir()

        self.write_info(output_dir)
        self.write_undefined(output_dir)

        print("\nRasterizing final glyphs...")
        bitmaps: dict[int, bytes] = {}
        clipped: dict[int, list[tuple[int, list[str]]]] = {}
        for codepoint in glyphs:
            source = self.glyph_sources[codepoint]
            image, edges = self.render_glyph(codepoint, source["font"])
            bitmaps[codepoint] = self.image_to_bitmap(image)
            if edges:
                clipped.setdefault(source["index"], []).append((codepoint, edges))
        self.warn_clipped(glyphs, clipped)

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

        summary = [
            f"Glyphs:         {len(glyphs)}",
            f"Ranges:         {len(ranges)}",
            f"Chunks:         {chunk_count}",
            f"Glyph size:     {self.glyph_bytes} bytes",
            f"Index storage:  {index_bytes} bytes",
            f"Bitmap storage: {bitmap_bytes} bytes",
            f"Total storage:  {total_bytes} bytes",
            f"Output:         {output_dir}",
        ]

        print()
        print("=" * 60)
        print(f"Generated:      {self.name}")
        print("=" * 60)
        print("\n".join(summary))
        self.write_log(glyphs, summary)

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
        FontConverter(config, args.config).generate()
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
