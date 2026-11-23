# unshaper

A reverse HarfBuzz shaper.

HarfBuzz takes a sequence of input characters and returns the output glyphs.
unshaper does the opposite. For every glyph in a font, it finds the input
characters and the OpenType features that produce that glyph. This lets you
display unencoded glyphs, such as ligatures and conjuncts, with real text.

**Warning: this tool was built by vibecoding. The code is a mess.** It works
on the fonts we have tried, but do not expect clean architecture, full test
coverage, or stable output formats.

## How it works

1. Build a reverse index of the GSUB table. For each output glyph, record
   which rules can produce it and from which input glyphs.
2. Rank the rules by HarfBuzz feature order across the Indic, USE, Arabic
   and default shapers. Rules from later features produce the final glyph
   forms, so they are tried first.
3. Propagate the ranks from contextual lookups (types 5 and 6) to the
   lookups they reference.
4. Trace each glyph back through the index. The trace uses caching and
   cycle detection. Multi-character candidates are verified by shaping them
   forward with HarfBuzz.

## Install

```
pip install git+https://github.com/m4rc1e/unshaper
```

Or, from a clone:

```
git clone https://github.com/m4rc1e/unshaper
cd unshaper
pip install -r requirements.txt
```

Requires Python 3.10 or later.

## Usage

Write the results as JSON:

```
unshaper Font.ttf -o out.json
```

Write an HTML report and open it in a browser:

```
unshaper Font.ttf -o out.html --open
```

The suffix of the `-o` path selects the format. With no `-o`, unshaper
writes a temporary HTML file and opens it.

You can also run it without installing the package:

```
python -m unshaper Font.ttf -o out.json
python unshaper/unshape.py Font.ttf -o out.json
```

### JSON layout

```json
{
  "font": "HindSiliguri-Bold.ttf",
  "summary": {"glyphs": 821, "mapped": 817, "unmapped": 4},
  "glyphs": {
    "bnBH_LA": {
      "sequence": "ভ্ল",
      "codepoints": ["U+09AD", "U+09CD", "U+09B2"],
      "all_sequences": [],
      "features": []
    },
    "glyph00051": {
      "sequence": "ch",
      "codepoints": ["U+0063", "U+0068"],
      "all_sequences": [],
      "features": ["dlig"]
    }
  },
  "kern_pairs": [{"first": "A", "second": "V", "value": -80, "all_sequences": []}],
  "mark_attachments": [{"base": "ক", "mark": "ঁ", "base_anchor": [408, 671], "mark_anchor": [-126, 671]}]
}
```

- `glyphs` is keyed by glyph name.
- `sequence` is the chosen input text. It is `null` when no sequence was
  found, for example for `.notdef`.
- `codepoints` lists the Unicode scalar values of `sequence`.
- `all_sequences` lists every candidate sequence when a contextual rule
  gives several.
- `features` lists the non-default OpenType features that must be enabled
  to reach the glyph. An empty list means the glyph appears with default
  shaping.
- `kern_pairs` and `mark_attachments` come from the GPOS table. The
  entries use text sequences, not glyph names. Large fonts produce many
  entries.

### Python API

```python
from fontTools.ttLib import TTFont
from unshaper import unshaper

results = unshaper(TTFont("Font.ttf"))   # or unshaper("Font.ttf")

r = results["f_i"]
r.sequence        # 'fi'            input text, or None if unresolved
r.all_sequences   # []              candidates when context gives several
r.features        # ['liga']        non-default features needed, [] if none
```

`unshaper` takes a fontTools `TTFont` or a path and returns a dict keyed
by glyph name. `unshape` is the same function under its older name. To
get the same structure as the JSON output, including GPOS data:

```python
from pathlib import Path
from unshaper import to_dict
from unshaper.gpos import extract_gpos

path = Path("Font.ttf")
font = TTFont(path)
results = unshaper(font)
data = to_dict(path, results, extract_gpos(font, results))
```

## Origin

Extracted from the `fontdiffai` project, where it provides the input text
for unencoded glyphs in font diff reports.
