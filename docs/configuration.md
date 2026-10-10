# Configuration

Each YAML file in `config/` describes one font, written to one TypeSize directory such as `output/regular16/`.
[`config/font.yaml`](../config/font.yaml) is a working example.

```yaml
name: regular16
font_size: 16
width: 8
height: 16

fonts:
  - font: fonts/0xProtoNerdFont-Regular.ttf
    fit: true
    ranges:
      - U+0020-U+007E

  - font: /usr/share/fonts/noto-emoji/NotoEmoji-Regular.ttf
    wide: true
    fit: true
    ranges:
      - U+1F600-U+1F64F
```

## Global options

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Directory name: `<style><cell height>[inv]`, where style is `regular`, `bold`, `italic` or `bolditalic`, e.g. `regular16` or `bold24inv`. The number must equal `height`, and `inv` must be used exactly when `invert` is true. |
| `width` | required | Cell width in pixels. Keep it a multiple of 8 so the device can copy glyph rows byte by byte; the build warns otherwise. Wide glyphs use twice this width. |
| `height` | required | Cell height in pixels, which is also the line pitch. |
| `font_size` | `16` | Default size for every font entry. |
| `baseline` | automatic | Row of the baseline, counted from the top of the cell. Automatic centers the first entry's ascent and descent in the cell. |
| `invert` | `false` | White glyphs on black cells, e.g. for a highlighted row. The format is the same, and the device draws it the same way. |
| `blank` | `[U+200D, U+FE0E, U+FE0F]` | Codepoints stored as empty normal-width glyphs, so an emoji sequence like ❤️ (U+2764 U+FE0F) shows a blank cell instead of the undefined box. Set to `[]` to turn this off. If a font entry selects one of these codepoints and its font has it, the font's glyph is used instead. |
| `output` | `output` | Where font directories and build logs are written. |
| `preview` | `false` | After building, print glyphs as ASCII art, read back from the stored files. |
| `preview_count` | `10` | How many glyphs to preview. |

## Font entries

`fonts` is an ordered list. Each entry selects codepoints from one font file.

| Key | Default | Meaning |
|---|---|---|
| `font` | required | Path to a TTF/OTF file, absolute or relative to the current directory. The single-config command also looks relative to the config file. |
| `ranges` | `[]` | Inclusive ranges, e.g. `U+0020-U+007E`. Surrogates (U+D800–U+DFFF) inside a range are dropped with a warning. |
| `individual` | `[]` | Single codepoints. |
| `font_size` | global `font_size` | Size to render this entry at. With `fit`, it's the starting size. |
| `fit` | `false` | Shrink this entry one pixel size at a time, down to 4 px, until none of its glyphs is clipped. |
| `align` | `center` | `center` centers each glyph's ink horizontally in its cell. `left` keeps the font's own placement: use it for box-drawing and block characters, which must touch the cell edges to connect. |
| `wide` | `false` | Two cells wide. Every 256-codepoint range this entry's glyphs fall in becomes a wide range. |

### Writing codepoints

Always write `U+XXXX`, e.g. `U+0041`. YAML reads unquoted numbers before the generator sees them, so they silently become the wrong character: `0041` turns into U+0033 and `0x41` into U+0065. A quoted single character such as `'A'` also works.

### Which font wins

Entries are applied top to bottom. A later entry overrides earlier ones for the codepoints it selects, **but only if its font actually contains the glyph** (checked against the font's character map). A fallback font listed later therefore can't replace a real glyph with its "missing glyph" box.
Codepoints that no font contains are left out, and the device shows `undefined.bmp` for them.

### Sizing: `font_size`, `fit` and the baseline

Cells have a fixed size, and the parts of a glyph that fall outside the cell are clipped. The build log lists each clipped glyph, the edge it crossed, and the largest size at which its entry would fit.

- `fit: true` picks the largest size, up to the entry's `font_size`, at which every glyph of the entry fits. The whole entry gets one size, so its glyphs stay consistent with each other.
- The log names the glyph that blocked the next size up (e.g. `U+1F4FF clips at 13px`). Moving that glyph to its own entry lets the rest of the entry be bigger.
- The first entry is the primary font. With an automatic `baseline`, the baseline follows the primary font's size, so fitting the primary entry also moves the baseline. Other entries are fitted against that baseline.
- `align: center` fixes glyphs that only stick out past their left edge (some accented capitals). `fit` only has to deal with glyphs that are genuinely too big.

### Wide ranges: emoji and icons

Text cells are narrow (8 px for `regular16`), and emoji and icons need about twice that. Entries with `wide: true` are stored in double-width cells. The whole 256-codepoint range each glyph belongs to is marked wide by an empty `wide` file in the range directory. The device's lookup returns each glyph's width, so text, icons and emoji go through the same drawing code.

- A range is either all wide or all normal. If one range gets glyphs from both a wide and a normal entry, the build stops and names the range and the entries. Emoji and icon blocks (U+1F300–U+1FAFF, Nerd Font icons in U+E000–U+F8FF) contain only symbols, so this rarely matters. Don't put an emoji from General Punctuation (e.g. ‼ U+203C) in a wide entry if your text font also uses that range.
- Use a **monochrome outline** emoji font, such as Noto Emoji. Colour emoji fonts (e.g. Noto Color Emoji) store colour bitmaps that don't rasterize properly to 1-bit.
- Emoji ZWJ sequences such as 🧑‍💻 are drawn as their parts with a blank cell between them, because the format has no zero-width glyphs (see `blank`).

## Choosing a cell size

The format has no per-glyph advance, so the cell width *is* the letter spacing. Pick a width close to your text font's character width, or text looks spaced out.

- `8×16` cells give 100 columns × 30 rows on the 800×480 panel. With 0xProto Nerd Font, `fit` settles at 14 px and nothing clips.
- Proportional fonts (e.g. Fira Sans) have to shrink a lot to fit narrow cells, because their widest glyph sets the size of the whole entry. Use a monospace font for every text character set.
- Keep the width a multiple of 8. Other widths work, but the device then has to bit-shift every glyph row.

## Build log and warnings

The terminal prints the number of warnings per category, and `output/<name>.log` lists them all.

| Category | Meaning |
|---|---|
| `missing-in-source` | A later entry selected a glyph its font doesn't have, so the earlier font's glyph is kept. |
| `missing-everywhere` | No entry's font has this codepoint, so the device will show `undefined.bmp`. Expected for unassigned codepoints inside a selected range. |
| `surrogates` | A range spanned U+D800–U+DFFF, and those codepoints were dropped. |
| `clipped` | Part of a glyph fell outside its cell. Lists the edges and the size at which the entry would fit. |
| `fit` | `fit` couldn't make an entry fit even at 4 px, so the entry keeps its configured size. |
| `layout` | `width` isn't a multiple of 8. |
| `format` | The built font failed the format check, and the build fails. |

Configuration mistakes (a bad name, a missing font file, a range that mixes wide and normal glyphs, ...) stop the build with an error message. With `font-generator`, the message is the last line of a Python traceback.
