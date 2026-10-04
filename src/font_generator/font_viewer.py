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

