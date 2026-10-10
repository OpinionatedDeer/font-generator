# font-generator

Builds bitmap fonts for the [Org-Calendar](https://github.com/OpinionatedDeer/Org-Calendar) e-ink display.
It rasterizes the glyphs you select from ordinary TTF/OTF fonts and writes them in the EFT format the firmware reads from LittleFS.
Glyphs are fixed-size 1-bit cells, stored exactly the way the panel's framebuffer expects them (1 = white, most significant bit = leftmost pixel), so drawing a glyph on the device is a plain byte copy.

The format is specified in Org-Calendar's [`docs/Font-Format.md`](https://github.com/OpinionatedDeer/Org-Calendar/blob/main/docs/Font-Format.md).
This repository contains the PC-side tools:

- **`font-generator`** builds fonts from YAML configs.
- **`font-viewer`** reads a built font the way the firmware will and checks it against the spec.

## Requirements

- Python 3.13 or newer (`.python-version` pins 3.14)
- [uv](https://docs.astral.sh/uv/)
- The TTF/OTF files your config refers to. They aren't in the repository (`*.ttf` is gitignored); put them in `fonts/`.

## Setup

```sh
uv sync
```

This creates `.venv/` and installs both commands.

## Quick start

```sh
uv run font-generator                    # build every config/*.yaml into output/
uv run font-viewer output/regular16      # summary and format check
uv run font-viewer output/regular16 -t "Standup 09:30 ☕" --png preview.png
```

Run the commands from the repository root: `font-generator` looks for `config/*.yaml` there, and font paths in configs are relative to the current directory.

## Building fonts

`font-generator` builds every `config/*.yaml`. To build a single config:

```sh
uv run python -m font_generator.font_converter path/to/config.yaml
```

Python prints a harmless `RuntimeWarning` from `runpy` for this form.

Each config produces one font directory, called a TypeSize (e.g. `regular16`):

```
output/
├── regular16/            the font: copy this to fonts/ on the device's LittleFS
│   ├── info              cell size, baseline, format version
│   ├── undefined.bmp     shown for characters the font doesn't have
│   ├── 000000/           one directory per 256-codepoint range
│   │   ├── index         which codepoints exist
│   │   └── 000020.bmp    32-glyph chunks
│   └── 01F600/
│       ├── wide          glyphs in this range are two cells wide
│       ├── index
│       └── 01F600.bmp
└── regular16.log         build log (not part of the font)
```

- The font directory is deleted and rebuilt on every run, so it never keeps leftovers from an older config.
- After writing, the generator reads the font back with the same reader `font-viewer` uses. If the output breaks the format, the build fails.
- The terminal shows one line per kind of warning. The build log has the details: which font each glyph came from, the sizes `fit` chose, and every warning with its codepoints.
- Copying the font onto the device's flash isn't automated yet.

Every config option, and advice on choosing cell sizes, is in [docs/configuration.md](docs/configuration.md).

## Inspecting fonts

`font-viewer` shares no code with the generator. It's written from the spec, so it checks the output the way the device will read it.

```sh
uv run font-viewer output/regular16                          # summary and format check
uv run font-viewer output/regular16 -g U+0041 g U+1F600      # glyphs as ASCII art
uv run font-viewer output/regular16 -t "Mon 12 Oct ☀️"       # a line of text as ASCII art
uv run font-viewer output/regular16 --png sheet.png          # every glyph, one row per chunk
uv run font-viewer output/regular16 -t "Hello" --png hello.png --scale 3
uv run font-viewer output/regular16 -t "Line 1" -t "Line 2" --framebuffer screen.bin
```

- `--framebuffer` writes 48,000 bytes: the full 800×480 screen in the format `eink_display()` takes.
- A font whose name ends in `inv` is read as inverted (white on black).
- The exit status is 1 when the format check finds problems.

## Project layout

```
config/                build configs, one per font
fonts/                 source TTF/OTF files (not tracked)
output/                built fonts and build logs (not tracked)
src/font_generator/
  main.py              font-generator: builds every config/*.yaml
  font_converter.py    the generator
  font_viewer.py       font-viewer: independent reader, format check, rendering
docs/
  configuration.md     config reference
```
