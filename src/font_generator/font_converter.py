from pathlib import Path
import struct

from PIL import Image, ImageFont, ImageDraw


class FontConverter:

    def __init__(self, config: dict):
        self.config = config

        # --------------------------------------------------
        # Configuration
        # --------------------------------------------------

        self.name = config["name"]
        self.font_path = Path(config["font"])

        self.font_size = int(
            config.get("font_size", 16)
        )

        self.width = int(config["width"])
        self.height = int(config["height"])
        self.baseline = int(config["baseline"])

        self.output_root = Path(
            config.get("output", "output")
        )

        self.preview_enabled = bool(
            config.get("preview", False)
        )

        self.preview_count = int(
            config.get("preview_count", 10)
        )

        # --------------------------------------------------
        # Validate
        # --------------------------------------------------

        if self.width <= 0:
            raise ValueError(
                "width must be greater than 0"
            )

        if self.height <= 0:
            raise ValueError(
                "height must be greater than 0"
            )

        if not 0 <= self.baseline <= self.height:
            raise ValueError(
                "baseline must be between 0 and height"
            )

        if not self.font_path.exists():
            raise FileNotFoundError(
                f"Font not found: {self.font_path}"
            )

        # --------------------------------------------------
        # Bitmap sizes
        # --------------------------------------------------

        self.row_bytes = (
            self.width + 7
        ) // 8

        self.glyph_bytes = (
            self.row_bytes * self.height
        )

        # --------------------------------------------------
        # Load font
        # --------------------------------------------------

        self.font = ImageFont.truetype(
            str(self.font_path),
            self.font_size,
        )

        self.font_name, self.font_style = (
            self.font.getname()
        )

        self.ascent, self.descent = (
            self.font.getmetrics()
        )

        # --------------------------------------------------
        # Information
        # --------------------------------------------------

        print(
            f"Font selected: "
            f"{self.font_name}, "
            f"{self.font_style}"
        )

        print(
            f"Font metrics: "
            f"ascent={self.ascent}px, "
            f"descent={self.descent}px"
        )

        print(
            f"Glyph cell: "
            f"{self.width}x{self.height}px"
        )

        print(
            f"Baseline: "
            f"{self.baseline}px"
        )

        print(
            f"Glyph storage: "
            f"{self.glyph_bytes} bytes"
        )

    # ======================================================
    # Unicode
    # ======================================================

    @staticmethod
    def parse_codepoint(value) -> int:
        """
        Accepted:

            U+0041
            0x41
            0041
            A
        """

        value = str(value).strip()

        if not value:
            raise ValueError(
                "Empty Unicode codepoint"
            )

        upper = value.upper()

        if upper.startswith("U+"):
            return int(
                upper[2:],
                16,
            )

        if upper.startswith("0X"):
            return int(
                upper[2:],
                16,
            )

        if len(value) == 1:
            return ord(value)

        return int(
            value,
            16,
        )

    @staticmethod
    def is_valid_codepoint(
        codepoint: int,
    ) -> bool:

        return (
            0 <= codepoint <= 0x10FFFF
            and not (
                0xD800
                <= codepoint
                <= 0xDFFF
            )
        )

    # ======================================================
    # Parse Unicode range
    # ======================================================

    def parse_range(self, value):

        value = str(value).strip()

        if "-" not in value:
            raise ValueError(
                f"Invalid range '{value}'. "
                f"Expected format "
                f"'U+0020-U+007E'"
            )

        start_text, end_text = (
            value.split("-", 1)
        )

        start = self.parse_codepoint(
            start_text
        )

        end = self.parse_codepoint(
            end_text
        )

        if start > end:
            raise ValueError(
                f"Invalid range '{value}': "
                f"start is greater than end"
            )

        if not self.is_valid_codepoint(
            start
        ):
            raise ValueError(
                f"Invalid codepoint: "
                f"U+{start:04X}"
            )

        if not self.is_valid_codepoint(
            end
        ):
            raise ValueError(
                f"Invalid codepoint: "
                f"U+{end:04X}"
            )

        return start, end

    # ======================================================
    # Get glyphs
    # ======================================================

    def get_glyphs(self):

        glyphs = set()

        # --------------------------------------------------
        # Ranges
        # --------------------------------------------------

        for value in self.config.get(
            "ranges",
            [],
        ):

            start, end = self.parse_range(
                value
            )

            for codepoint in range(
                start,
                end + 1,
            ):

                glyphs.add(codepoint)

        # --------------------------------------------------
        # Individual glyphs
        # --------------------------------------------------

        for value in self.config.get(
            "individual",
            [],
        ):

            codepoint = self.parse_codepoint(
                value
            )

            if not self.is_valid_codepoint(
                codepoint
            ):
                raise ValueError(
                    f"Invalid codepoint: "
                    f"U+{codepoint:04X}"
                )

            glyphs.add(codepoint)

        return sorted(glyphs)

    # ======================================================
    # Render glyph
    # ======================================================

    def render_glyph(
        self,
        codepoint: int,
    ) -> Image.Image:

        image = Image.new(
            "1",
            (
                self.width,
                self.height,
            ),
            0,
        )

        draw = ImageDraw.Draw(
            image
        )

        character = chr(codepoint)

        draw.text(
            (
                0,
                self.baseline,
            ),
            character,
            font=self.font,
            fill=1,
            anchor="ls",
        )

        return image

    # ======================================================
    # Convert to framebuffer bitmap
    # ======================================================

    def image_to_bitmap(
        self,
        image: Image.Image,
    ) -> bytes:

        output = bytearray(
            self.glyph_bytes
        )

        for y in range(
            self.height
        ):

            for x in range(
                self.width
            ):

                if image.getpixel(
                    (x, y)
                ):

                    byte_index = (
                        y * self.row_bytes
                        + x // 8
                    )

                    # MSB first.
                    bit = 7 - (
                        x & 7
                    )

                    output[
                        byte_index
                    ] |= (
                        1 << bit
                    )

        return bytes(output)

    # ======================================================
    # Generate one glyph
    # ======================================================

    def generate_glyph(
        self,
        codepoint: int,
    ) -> bytes:

        image = self.render_glyph(
            codepoint
        )

        return self.image_to_bitmap(
            image
        )

    # ======================================================
    # Write info
    # ======================================================

    def write_info(
        self,
        output_dir: Path,
    ):

        data = struct.pack(
            "<4sBBHHH",
            b"EFT\0",
            1,
            1,
            self.width,
            self.height,
            self.baseline,
        )

        (
            output_dir / "info"
        ).write_bytes(data)

    # ======================================================
    # Undefined glyph
    # ======================================================

    def create_undefined_glyph(self):

        image = Image.new(
            "1",
            (
                self.width,
                self.height,
            ),
            0,
        )

        draw = ImageDraw.Draw(
            image
        )

        # Border
        draw.rectangle(
            (
                0,
                0,
                self.width - 1,
                self.height - 1,
            ),
            outline=1,
        )

        # X
        if (
            self.width >= 5
            and self.height >= 5
        ):

            draw.line(
                (
                    2,
                    2,
                    self.width - 3,
                    self.height - 3,
                ),
                fill=1,
            )

            draw.line(
                (
                    self.width - 3,
                    2,
                    2,
                    self.height - 3,
                ),
                fill=1,
            )

        return image

    def write_undefined(
        self,
        output_dir: Path,
    ):

        image = (
            self.create_undefined_glyph()
        )

        data = self.image_to_bitmap(
            image
        )

        (
            output_dir / "undefined.bmp"
        ).write_bytes(data)

    # ======================================================
    # Range helpers
    # ======================================================

    @staticmethod
    def range_start(codepoint):

        return codepoint & ~0xFF

    @staticmethod
    def chunk_start(codepoint):

        return codepoint & ~0x1F

    # ======================================================
    # Generate one Unicode range
    # ======================================================

    def generate_range(
        self,
        range_start: int,
        glyphs: list[int],
        bitmaps: dict[int, bytes],
        output_dir: Path,
    ):

        range_dir = (
            output_dir
            / f"{range_start:06X}"
        )

        range_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        # --------------------------------------------------
        # 256-bit presence index
        # --------------------------------------------------

        index = bytearray(32)

        for codepoint in glyphs:

            slot = (
                codepoint & 0xFF
            )

            index[
                slot >> 3
            ] |= (
                1 << (
                    slot & 7
                )
            )

        (
            range_dir / "index"
        ).write_bytes(
            bytes(index)
        )

        # --------------------------------------------------
        # Group into 32-codepoint chunks
        # --------------------------------------------------

        chunks = {}

        for codepoint in glyphs:

            chunk = (
                self.chunk_start(
                    codepoint
                )
            )

            if chunk not in chunks:
                chunks[chunk] = []

            chunks[chunk].append(
                codepoint
            )

        # --------------------------------------------------
        # Write chunks
        # --------------------------------------------------

        for chunk, chunk_glyphs in sorted(
            chunks.items()
        ):

            chunk_data = bytearray(
                self.glyph_bytes * 32
            )

            for codepoint in chunk_glyphs:

                slot = (
                    codepoint & 0x1F
                )

                offset = (
                    slot
                    * self.glyph_bytes
                )

                chunk_data[
                    offset:
                    offset + self.glyph_bytes
                ] = bitmaps[
                    codepoint
                ]

            filename = (
                f"{chunk:04X}.bmp"
            )

            (
                range_dir / filename
            ).write_bytes(
                bytes(chunk_data)
            )

    # ======================================================
    # Preview
    # ======================================================
    def preview_glyph(self, codepoint: int):
        image = self.render_glyph(codepoint)

        character = chr(codepoint)

        print()
        print(
            f"U+{codepoint:04X} "
            f"'{character}' "
            f"{self.width}x{self.height}"
        )

        print()

        for y in range(self.height):

            row = []

            for x in range(self.width):

                pixel = image.getpixel((x, y))

                if pixel:
                    row.append("##")
                else:
                    row.append("  ")

            line = "".join(row)

            # Mark baseline
            if y == self.baseline:
                print(line + "  <- baseline")
            else:
                print(line)

        print()


    # ======================================================
    # Generate complete font
    # ======================================================

    def generate(self):

        glyphs = self.get_glyphs()

        print()
        print(
            f"Generating: {self.name}"
        )

        print(
            f"Glyphs requested: "
            f"{len(glyphs)}"
        )

        if not glyphs:
            print(
                "No glyphs selected."
            )
            return

        # --------------------------------------------------
        # Output directory
        # --------------------------------------------------

        output_dir = (
            self.output_root
            / self.name
        )

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        # --------------------------------------------------
        # Metadata
        # --------------------------------------------------

        self.write_info(
            output_dir
        )

        self.write_undefined(
            output_dir
        )

        # --------------------------------------------------
        # Rasterize
        # --------------------------------------------------

        print()
        print("Rasterizing...")

        bitmaps = {}

        for codepoint in glyphs:

            bitmaps[codepoint] = (
                self.generate_glyph(
                    codepoint
                )
            )

        # --------------------------------------------------
        # Group by 256-codepoint range
        # --------------------------------------------------

        ranges = {}

        for codepoint in glyphs:

            range_start = (
                self.range_start(
                    codepoint
                )
            )

            if range_start not in ranges:
                ranges[range_start] = []

            ranges[range_start].append(
                codepoint
            )

        # --------------------------------------------------
        # Generate ranges
        # --------------------------------------------------

        for range_start, range_glyphs in sorted(
            ranges.items()
        ):

            self.generate_range(
                range_start,
                range_glyphs,
                bitmaps,
                output_dir,
            )

        # --------------------------------------------------
        # Count chunks
        # --------------------------------------------------

        chunk_count = 0

        for range_glyphs in ranges.values():

            chunks = set()

            for codepoint in range_glyphs:

                chunks.add(
                    self.chunk_start(
                        codepoint
                    )
                )

            chunk_count += len(chunks)

        # --------------------------------------------------
        # Storage statistics
        # --------------------------------------------------

        info_bytes = 12

        undefined_bytes = (
            self.glyph_bytes
        )

        index_bytes = (
            len(ranges) * 32
        )

        bitmap_bytes = (
            chunk_count
            * 32
            * self.glyph_bytes
        )

        total_bytes = (
            info_bytes
            + undefined_bytes
            + index_bytes
            + bitmap_bytes
        )

        # --------------------------------------------------
        # Report
        # --------------------------------------------------

        print()
        print("=" * 60)
        print(
            f"Generated: {self.name}"
        )
        print("=" * 60)

        print(
            f"Glyphs:         {len(glyphs)}"
        )

        print(
            f"Ranges:         {len(ranges)}"
        )

        print(
            f"Chunks:         {chunk_count}"
        )

        print(
            f"Glyph size:     "
            f"{self.glyph_bytes} bytes"
        )

        print(
            f"Index storage:  "
            f"{index_bytes} bytes"
        )

        print(
            f"Bitmap storage: "
            f"{bitmap_bytes} bytes"
        )

        print(
            f"Total storage:  "
            f"{total_bytes} bytes"
        )

        print(
            f"Output:         "
            f"{output_dir}"
        )

        # --------------------------------------------------
        # Preview
        # --------------------------------------------------
        if self.preview_enabled:

            count = min(
                self.preview_count,
                len(glyphs),
            )

            print()
            print(
                f"Previewing {count} glyph(s) in terminal..."
            )

            for codepoint in glyphs[:count]:
                self.preview_glyph(codepoint)


