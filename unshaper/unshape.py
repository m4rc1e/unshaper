"""Reverse HarfBuzz shaping engine.

Builds a reverse GSUB graph and traces each glyph back to input
characters, respecting the shaping engine's feature application order.

For complex scripts (Indic, USE, Arabic, etc.), the forward shaping
engine applies GSUB features in a specific order and performs
script-specific reordering between feature groups.  The reverse engine
inverts this by:

1. Building a reverse index: for each output glyph, which GSUB rules
   can produce it and from which input glyphs.
2. Assigning feature ranks so rules from later features (which produce
   the "final" glyph forms) are tried first during resolution.
3. Propagating feature ranks from contextual wrapper lookups (type 5/6)
   to the inner lookups they reference.
4. Recursively tracing each target glyph back through the reverse
   index, with caching, cycle detection, and HarfBuzz verification
   of multi-character candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import uharfbuzz as hb
from fontTools.ttLib import TTFont

if __package__ in (None, ""):
    # Run as a plain script: make `unshaper.gpos` importable.
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ZWJ = "‍"


@dataclass
class UnshapeResult:
    """Result of reverse-shaping a single glyph."""

    sequence: str | None = None
    all_sequences: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)


# Combined feature application order across all HarfBuzz shapers
# (Indic, USE, Arabic, Thai, Khmer, Hangul, default).  Position in
# this list determines the "rank" — rules from later features are
# tried first during reverse resolution because later features
# produce the final glyph forms.
_FEATURE_LIST = [
    # Pre-processing (all shapers)
    "locl", "ccmp",
    # Indic / USE basic shaping
    "nukt", "akhn", "rphf", "rkrf", "pref", "blwf", "abvf",
    "half", "pstf", "vatu", "cjct",
    # Arabic joining
    "stch", "isol", "fina", "fin2", "fin3", "medi", "med2", "init",
    # Presentation
    "abvs", "blws", "haln", "pres", "psts",
    # Standard typographic
    "rlig", "rclt", "calt", "clig", "liga", "mset", "vert", "vrt2",
]
_FEATURE_ORDER = {tag: i for i, tag in enumerate(_FEATURE_LIST)}
_DEFAULT_RANK = len(_FEATURE_LIST)
_MAX_DEPTH = 20

# Features applied by default during shaping — no CSS opt-in needed.
_DEFAULT_FEATURES = frozenset(_FEATURE_LIST) | {
    "kern", "mark", "mkmk", "curs", "dist", "abvm", "blwm",
}

# Pre-base matras: reordered before the base consonant by the shaping
# engine, so GSUB sees [matra, consonant] but user types [consonant, matra].
_PRE_BASE_MATRAS = frozenset({
    0x093F,  # Devanagari I
    0x09BF,  # Bengali I
    0x0A3F,  # Gurmukhi I
    0x0B3F,  # Oriya I
    0x0BBF,  # Tamil I
    0x0BC6,  # Tamil E
    0x0BC7,  # Tamil EE
    0x0BC8,  # Tamil AI
    0x0CBF,  # Kannada I
    0x0D3F,  # Malayalam I
    0x0D46,  # Malayalam E
    0x0D47,  # Malayalam EE
    0x0D48,  # Malayalam AI
})


@dataclass(slots=True)
class _Rule:
    input_glyphs: tuple[str, ...]
    feature_rank: int
    reorder_matra: bool = False
    feature_tags: frozenset[str] = frozenset()


class _ReverseShaper:
    """Reverse shaping engine for a single font."""

    def __init__(self, font: TTFont, font_path: Path):
        self.font = font
        self.cmap = font.getBestCmap() or {}

        self.cmap_reverse: dict[str, int] = {}
        for cp, gn in self.cmap.items():
            self.cmap_reverse.setdefault(gn, cp)

        self.hb_font = _make_hb_font(font_path)
        self.id_to_name = dict(enumerate(font.getGlyphOrder()))
        self.all_glyphs = set(font.getGlyphOrder())

        self._index: dict[str, list[_Rule]] = {}
        self._cache: dict[str, str | None] = {}
        self._resolving: set[str] = set()
        self._resolved_tags: dict[str, frozenset[str]] = {}
        # For contextual subs: all (input_glyph, *context_glyphs) tuples
        # per output glyph, plus whether to reorder pre-base matras.
        self._context_groups: dict[str, list[tuple[tuple[str, ...], bool]]] = {}

        if "GSUB" in font:
            self._build_index()

    # ---- public ---------------------------------------------------------

    def run(self) -> dict[str, UnshapeResult]:
        seqs: dict[str, str | None] = {}

        # Phase 1 — direct cmap
        for cp, gn in self.cmap.items():
            if gn not in seqs:
                seqs[gn] = chr(cp)

        # Phase 2 — shape each cmap char to catch default substitutions
        for cp in sorted(self.cmap):
            char = chr(cp)
            for gn in _shape_collect(self.hb_font, self.id_to_name, char):
                if gn not in seqs and gn in self.all_glyphs:
                    seqs[gn] = char

        # Phase 3 — recursive reverse-GSUB resolution
        for gn in self.font.getGlyphOrder():
            if gn not in seqs:
                seq = self._resolve(gn)
                if seq is not None:
                    seqs[gn] = seq

        # Phase 4 — ZWJ context fallback (Arabic positional forms etc.)
        zwj_glyphs = _shape_collect(self.hb_font, self.id_to_name, ZWJ)
        for cp in sorted(self.cmap):
            char = chr(cp)
            for text in (char + ZWJ, ZWJ + char, ZWJ + char + ZWJ):
                for gn in _shape_collect(
                    self.hb_font, self.id_to_name, text
                ) - zwj_glyphs:
                    if gn not in seqs and gn in self.all_glyphs:
                        seqs[gn] = char

        # Phase 5 — assemble results with context groups and features
        result: dict[str, UnshapeResult] = {}
        for gn in self.all_glyphs:
            seq = seqs.get(gn)
            all_seqs = self._resolve_context_group(gn)
            non_default = sorted(self._resolved_tags.get(gn, frozenset()))
            result[gn] = UnshapeResult(
                sequence=seq, all_sequences=all_seqs, features=non_default,
            )

        return result

    def _resolve_context_group(self, glyph: str) -> list[str]:
        """Resolve all coverage members to character sequences."""
        entries = self._context_groups.get(glyph)
        if not entries:
            return []
        sequences: list[str] = []
        seen: set[str] = set()
        for glyph_tuple, reorder in entries:
            parts = []
            ok = True
            for g in glyph_tuple:
                seq = self._resolve(g, depth=1)
                if seq is None:
                    ok = False
                    break
                parts.append(seq)
            if not ok:
                continue
            if reorder and len(parts) >= 2:
                candidate = "".join(parts[1:]) + parts[0]
            else:
                candidate = "".join(parts)
            if candidate not in seen:
                seen.add(candidate)
                sequences.append(candidate)
        return sequences

    # ---- reverse index construction -------------------------------------

    def _build_index(self) -> None:
        gsub = self.font["GSUB"].table
        if not gsub.LookupList:
            return

        # Map each lookup to its feature rank and feature tags.
        lookup_rank: dict[int, int] = {}
        lookup_tags: dict[int, set[str]] = {}
        if gsub.FeatureList:
            for rec in gsub.FeatureList.FeatureRecord:
                rank = _FEATURE_ORDER.get(rec.FeatureTag, _DEFAULT_RANK)
                for li in rec.Feature.LookupListIndex:
                    if li not in lookup_rank or rank > lookup_rank[li]:
                        lookup_rank[li] = rank
                    lookup_tags.setdefault(li, set()).add(rec.FeatureTag)

        # Propagate ranks and tags into inner lookups referenced by type 5/6.
        for idx, lookup in enumerate(gsub.LookupList.Lookup):
            parent_rank = lookup_rank.get(idx, -1)
            parent_tags = lookup_tags.get(idx, set())
            for inner in self._inner_indices(lookup):
                if inner not in lookup_rank or parent_rank > lookup_rank[inner]:
                    lookup_rank[inner] = parent_rank
                lookup_tags.setdefault(inner, set()).update(parent_tags)

        # Index every lookup's substitution rules.
        for idx, lookup in enumerate(gsub.LookupList.Lookup):
            rank = lookup_rank.get(idx, -1)
            tags = frozenset(lookup_tags.get(idx, set()))
            for sub in lookup.SubTable:
                st = sub
                if lookup.LookupType == 7 and hasattr(st, "ExtSubTable"):
                    st = st.ExtSubTable
                self._index_subtable(st, rank, tags)

        # Extract context-aware rules from type 5/6 contextual subs.
        for idx, lookup in enumerate(gsub.LookupList.Lookup):
            rank = lookup_rank.get(idx, -1)
            tags = frozenset(lookup_tags.get(idx, set()))
            for sub in lookup.SubTable:
                st = sub
                if lookup.LookupType == 7 and hasattr(st, "ExtSubTable"):
                    st = st.ExtSubTable
                self._index_contextual(st, rank, gsub.LookupList, tags)

    # -- contextual inner-lookup extraction --

    @staticmethod
    def _inner_indices(lookup) -> set[int]:
        out: set[int] = set()
        for sub in lookup.SubTable:
            st = sub
            if lookup.LookupType == 7 and hasattr(st, "ExtSubTable"):
                st = st.ExtSubTable
            _collect_subst_records(st, out)
        return out

    # -- subtable → reverse rules --

    def _index_subtable(
        self, st, rank: int, tags: frozenset[str] = frozenset()
    ) -> None:
        # Type 1 / 8: single sub  (mapping: str → str)
        # Type 2:     multiple sub (mapping: str → list[str])
        if hasattr(st, "mapping"):
            for inp, out in st.mapping.items():
                if isinstance(out, str):
                    self._add(out, (inp,), rank, tags=tags)
                elif isinstance(out, (list, tuple)):
                    for o in out:
                        self._add(o, (inp,), rank, tags=tags)

        # Type 3: alternate sub
        if hasattr(st, "alternates"):
            for inp, alts in st.alternates.items():
                for alt in alts:
                    self._add(alt, (inp,), rank, tags=tags)

        # Type 4: ligature sub
        if hasattr(st, "ligatures"):
            for first, ligs in st.ligatures.items():
                for lig in ligs:
                    inputs = (first,) + tuple(lig.Component)
                    self._add(lig.LigGlyph, inputs, rank, tags=tags)

    def _add(
        self, output: str, inputs: tuple[str, ...], rank: int,
        reorder_matra: bool = False,
        tags: frozenset[str] = frozenset(),
    ) -> None:
        self._index.setdefault(output, []).append(
            _Rule(input_glyphs=inputs, feature_rank=rank,
                  reorder_matra=reorder_matra, feature_tags=tags)
        )

    def _index_contextual(
        self, st, rank: int, lookup_list, tags: frozenset[str] = frozenset()
    ) -> None:
        """Extract context-aware reverse rules from type 6 format 3 subs."""
        if not (hasattr(st, "SubstLookupRecord") and st.SubstLookupRecord):
            return
        if not hasattr(st, "LookAheadCoverage"):
            return

        lookahead = st.LookAheadCoverage or []
        if not lookahead:
            return

        # Collect all glyph lists per lookahead position.
        la_glyph_lists = [
            la.glyphs
            for la in lookahead
            if hasattr(la, "glyphs") and la.glyphs
        ]
        if not la_glyph_lists:
            return

        for rec in st.SubstLookupRecord:
            if rec.LookupListIndex >= len(lookup_list.Lookup):
                continue
            inner = lookup_list.Lookup[rec.LookupListIndex]
            for inner_sub in inner.SubTable:
                ist = inner_sub
                if inner.LookupType == 7 and hasattr(ist, "ExtSubTable"):
                    ist = ist.ExtSubTable
                if not hasattr(ist, "mapping"):
                    continue
                for inp, out in ist.mapping.items():
                    if not isinstance(out, str):
                        continue
                    reorder = self.cmap_reverse.get(inp, 0) in _PRE_BASE_MATRAS

                    # Use first glyph from each position as representative.
                    rep_context = tuple(gl[0] for gl in la_glyph_lists)
                    self._add(out, (inp,) + rep_context, rank,
                              reorder_matra=reorder, tags=tags)

                    # Store ALL glyphs in coverage for context groups.
                    # For single lookahead position (common case),
                    # each glyph is a separate context entry.
                    if len(la_glyph_lists) == 1:
                        for ctx_glyph in la_glyph_lists[0]:
                            self._context_groups.setdefault(out, []).append(
                                ((inp, ctx_glyph), reorder)
                            )
                    else:
                        # Multi-position: store one entry per first-
                        # position glyph (representative cross-product).
                        for ctx_glyph in la_glyph_lists[0]:
                            rest = tuple(gl[0] for gl in la_glyph_lists[1:])
                            self._context_groups.setdefault(out, []).append(
                                ((inp, ctx_glyph) + rest, reorder)
                            )

    # ---- recursive resolution -------------------------------------------

    def _resolve(
        self, glyph: str, depth: int = 0
    ) -> str | None:
        if depth > _MAX_DEPTH:
            return None
        cached = self._cache.get(glyph)
        if cached is not None:
            return cached
        if glyph in self._resolving:
            return None

        if glyph in self.cmap_reverse:
            seq = chr(self.cmap_reverse[glyph])
            self._cache[glyph] = seq
            return seq

        self._resolving.add(glyph)
        result, tags = self._try_rules(glyph, depth)
        if result is not None:
            self._cache[glyph] = result
            if tags:
                self._resolved_tags[glyph] = tags
        self._resolving.discard(glyph)
        return result

    def _try_rules(
        self, glyph: str, depth: int
    ) -> tuple[str | None, frozenset[str]]:
        rules = self._index.get(glyph, [])
        fallback = None
        fallback_tags: frozenset[str] = frozenset()

        # Prefer: higher feature rank, then longer input (contextual
        # rules with lookahead context are more specific).
        for rule in sorted(
            rules, key=lambda r: (-r.feature_rank, -len(r.input_glyphs))
        ):
            parts: list[str] = []
            ok = True
            for inp in rule.input_glyphs:
                seq = self._resolve(inp, depth + 1)
                if seq is None:
                    ok = False
                    break
                parts.append(seq)
            if not ok:
                continue

            candidate = "".join(parts)

            # Undo Indic pre-base matra reordering: the shaping
            # engine moves pre-base matras before the consonant, so
            # GSUB sees [matra, consonant] but the user types
            # [consonant, matra].
            if rule.reorder_matra and len(parts) >= 2:
                candidate = "".join(parts[1:]) + parts[0]

            # Collect non-default feature tags from this rule
            # and all input glyphs in the chain.
            chain_tags = rule.feature_tags - _DEFAULT_FEATURES
            for inp in rule.input_glyphs:
                chain_tags |= self._resolved_tags.get(inp, frozenset())

            if len(candidate) == 1:
                return candidate, chain_tags

            # Intermediate levels: trust the GSUB reverse mapping.
            # Intermediates (half forms, rakar, etc.) are context-
            # dependent — they only appear when followed by another
            # consonant, so they can't be verified in isolation.
            if depth > 0:
                return candidate, chain_tags

            # Top level: verify with HarfBuzz forward shaping.
            if glyph in _shape_collect(
                self.hb_font, self.id_to_name, candidate
            ):
                return candidate, chain_tags

            # Track unverified fallback — last assignment wins,
            # which is the lowest-ranked (most basic) rule since
            # we iterate highest-rank first.
            fallback = candidate
            fallback_tags = chain_tags

        return fallback, fallback_tags


# ---- helpers ------------------------------------------------------------


def _make_hb_font(font_path: Path) -> hb.Font:
    with open(font_path, "rb") as f:
        blob = hb.Blob(f.read())
    face = hb.Face(blob)
    font = hb.Font(face)
    font.scale = (face.upem, face.upem)
    return font


def _shape_collect(
    hb_font: hb.Font, id_to_name: dict[int, str], text: str
) -> set[str]:
    """Shape *text* and return the set of glyph names produced."""
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(hb_font, buf)
    return {
        id_to_name[info.codepoint]
        for info in buf.glyph_infos
        if info.codepoint in id_to_name
        and id_to_name[info.codepoint] != ".notdef"
    }


def _collect_subst_records(obj, out: set[int]) -> None:
    """Extract inner lookup indices from type 5/6 subtables."""
    if hasattr(obj, "SubstLookupRecord") and obj.SubstLookupRecord:
        for rec in obj.SubstLookupRecord:
            out.add(rec.LookupListIndex)
    for set_attr in (
        "ChainSubRuleSet", "ChainSubClassSet",
        "SubRuleSet", "SubClassSet",
    ):
        rule_sets = getattr(obj, set_attr, None)
        if not rule_sets:
            continue
        for rs in rule_sets:
            if not rs:
                continue
            for rule_attr in (
                "ChainSubRule", "ChainSubClassRule",
                "SubRule", "SubClassRule",
            ):
                rules = getattr(rs, rule_attr, None)
                if not rules:
                    continue
                for rule in rules:
                    if hasattr(rule, "SubstLookupRecord") and rule.SubstLookupRecord:
                        for rec in rule.SubstLookupRecord:
                            out.add(rec.LookupListIndex)


# ---- public API ---------------------------------------------------------


def unshape(font: TTFont, font_path: Path) -> dict[str, UnshapeResult]:
    """Return {glyph_name: UnshapeResult}."""
    return _ReverseShaper(font, font_path).run()


# ---- CLI ----------------------------------------------------------------

def _generate_html(
    font_path: Path,
    rmap: dict[str, UnshapeResult],
    gpos_data=None,
) -> str:
    import base64

    data = font_path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    suffix = font_path.suffix.lower()
    mime = {".ttf": "font/ttf", ".otf": "font/otf",
            ".woff": "font/woff", ".woff2": "font/woff2"}.get(suffix, "font/ttf")
    uri = f"data:{mime};base64,{b64}"

    esc = lambda s: "".join(f"&#x{ord(c):04X};" for c in s)
    total = len(rmap)
    mapped = sum(1 for r in rmap.values() if r.sequence is not None)

    # ---- glyph cells ----
    glyph_cells: list[str] = []
    for glyph_name, r in sorted(rmap.items()):
        if r.sequence is None:
            continue
        feat_css = ""
        if r.features:
            feat_css = "font-feature-settings:" + ",".join(
                f'"{f}" 1' for f in r.features) + ";"
        if r.all_sequences and len(r.all_sequences) > 1:
            frames_js = "[" + ",".join(
                f'"{esc(seq)}"' for seq in r.all_sequences) + "]"
            glyph_html = (
                f'<span class="glyph" style="{feat_css}" '
                f'data-frames="{_html_attr_esc(frames_js)}">'
                f'{esc(r.sequence)}</span>')
        else:
            glyph_html = (
                f'<span class="glyph" style="{feat_css}">'
                f'{esc(r.sequence)}</span>')
        badge = ""
        if r.features:
            badge = "".join(
                f'<span class="feat">{f}</span>' for f in r.features)
        if r.all_sequences and len(r.all_sequences) > 1:
            badge += f'<span class="group">{len(r.all_sequences)}</span>'
        glyph_cells.append(
            f'<div class="cell">{glyph_html}'
            f'<div class="name">{glyph_name}</div>'
            f'<div class="badges">{badge}</div></div>')

    # ---- kern pair cells ----
    kern_section = ""
    mark_section = ""
    n_kern = 0
    n_mark_groups = 0
    if gpos_data:
        n_kern = len(gpos_data.kern_pairs)
        if gpos_data.kern_pairs:
            kern_cells: list[str] = []
            sorted_kerns = sorted(
                gpos_data.kern_pairs,
                key=lambda kp: -len(kp.all_sequences),
            )
            for kp in sorted_kerns:
                display = esc(kp.first) + esc(kp.second)
                if kp.all_sequences and len(kp.all_sequences) > 1:
                    frames_js = "[" + ",".join(
                        f'"{esc(s)}"' for s in kp.all_sequences) + "]"
                    kern_cells.append(
                        f'<div class="cell kern-cell">'
                        f'<span class="glyph kern-pair" '
                        f'data-frames="{_html_attr_esc(frames_js)}">'
                        f'{display}</span>'
                        f'<div class="kern-val">{kp.value}</div>'
                        f'<div class="badges">'
                        f'<span class="group">{len(kp.all_sequences)}</span>'
                        f'</div></div>')
                else:
                    kern_cells.append(
                        f'<div class="cell kern-cell">'
                        f'<span class="glyph kern-pair">{display}</span>'
                        f'<div class="kern-val">{kp.value}</div></div>')
            kern_section = (
                f'<h2 id="kern">Kerning ({len(gpos_data.kern_pairs)} pairs)</h2>'
                f'<input type="text" id="kern-filter" placeholder="Filter kern pairs…" class="filter">'
                f'<div class="grid kern-grid" id="kern-grid">{"".join(kern_cells)}</div>')

        if gpos_data.mark_attachments:
            from collections import OrderedDict
            base_groups: OrderedDict[str, list[str]] = OrderedDict()
            for ma in gpos_data.mark_attachments:
                base_groups.setdefault(ma.base, []).append(ma.mark)

            mark_cells: list[str] = []
            for base_seq, marks in base_groups.items():
                seen: list[str] = []
                for m in marks:
                    combo = base_seq + m
                    if combo not in seen:
                        seen.append(combo)
                first = esc(seen[0])
                if len(seen) > 1:
                    frames_js = "[" + ",".join(
                        f'"{esc(s)}"' for s in seen) + "]"
                    mark_cells.append(
                        f'<div class="cell">'
                        f'<span class="glyph" '
                        f'data-frames="{_html_attr_esc(frames_js)}">'
                        f'{first}</span>'
                        f'<div class="name">{esc(base_seq)}</div>'
                        f'<div class="badges">'
                        f'<span class="group">{len(seen)}</span>'
                        f'</div></div>')
                else:
                    mark_cells.append(
                        f'<div class="cell">'
                        f'<span class="glyph">{first}</span>'
                        f'<div class="name">{esc(base_seq)}</div>'
                        f'</div>')
            mark_section = (
                f'<h2 id="marks">Mark Attachments '
                f'({len(base_groups)} bases, '
                f'{len(gpos_data.mark_attachments)} combinations)</h2>'
                f'<input type="text" id="mark-filter" '
                f'placeholder="Filter marks…" class="filter">'
                f'<div class="grid" id="mark-grid">'
                f'{"".join(mark_cells)}</div>')
            n_mark_groups = len(base_groups)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Unshape — {font_path.name}</title>
<style>
@font-face {{ font-family: "TestFont"; src: url("{uri}"); }}
body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #fafafa; color: #1a1a1a; }}
h1 {{ font-size: 1.3rem; }}
h2 {{ font-size: 1.1rem; margin-top: 2.5rem; }}
nav {{ margin-bottom: 1.5rem; }}
nav a {{ margin-right: 1rem; font-size: 0.85rem; }}
.stats {{ color: #666; margin-bottom: 1rem; }}
.filter {{ width: 100%; max-width: 400px; padding: 6px 10px; margin-bottom: 8px;
    border: 1px solid #ccc; border-radius: 4px; font-size: 0.85rem; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 4px; }}
.cell {{ border: 1px solid #e0e0e0; border-radius: 4px; padding: 8px 4px 4px;
    text-align: center; background: #fff; overflow: hidden; }}
.glyph {{ font-family: "TestFont", sans-serif; font-size: 3rem; line-height: 1.2;
    display: block; min-height: 3.6rem; }}
.kern-pair {{ letter-spacing: 0; }}
.kern-val {{ font-size: 0.6rem; color: #c62828; font-family: monospace; }}
.name {{ font-size: 0.6rem; color: #888; margin-top: 4px; word-break: break-all; }}
.badges {{ margin-top: 2px; }}
.feat {{ display: inline-block; font-size: 0.55rem; background: #e8f0fe; color: #1a73e8;
    border-radius: 3px; padding: 0 3px; margin: 1px; }}
.group {{ display: inline-block; font-size: 0.55rem; background: #fce8e6; color: #d93025;
    border-radius: 3px; padding: 0 3px; margin: 1px; }}
.group::before {{ content: "\\00d7"; }}
.hidden {{ display: none; }}
</style>
</head>
<body>
<h1>Unshape — {font_path.name}</h1>
<nav>
    <a href="#glyphs">Glyphs ({mapped})</a>
    {"<a href='#kern'>Kerning (" + str(n_kern) + ")</a>" if n_kern else ""}
    {"<a href='#marks'>Marks (" + str(n_mark_groups) + ")</a>" if n_mark_groups else ""}
</nav>
<h2 id="glyphs">Glyphs ({mapped}/{total} mapped)</h2>
<div class="grid">{"".join(glyph_cells)}</div>
{kern_section}
{mark_section}
<script>
document.querySelectorAll("[data-frames]").forEach(el => {{
    const frames = JSON.parse(el.dataset.frames);
    if (frames.length < 2) return;
    let i = 0;
    setInterval(() => {{ i = (i + 1) % frames.length; el.innerHTML = frames[i]; }}, 800);
}});
function setupFilter(inputId, gridId) {{
    const input = document.getElementById(inputId);
    const grid = document.getElementById(gridId);
    if (!input || !grid) return;
    input.addEventListener("input", () => {{
        const q = input.value.toLowerCase();
        grid.querySelectorAll(".cell").forEach(c => {{
            c.classList.toggle("hidden", q && !c.textContent.toLowerCase().includes(q));
        }});
    }});
}}
setupFilter("kern-filter", "kern-grid");
setupFilter("mark-filter", "mark-grid");
</script>
</body>
</html>"""


def _html_attr_esc(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _codepoints(s: str) -> list[str]:
    return [f"U+{ord(c):04X}" for c in s]


def to_dict(
    font_path: Path,
    rmap: dict[str, UnshapeResult],
    gpos_data=None,
) -> dict:
    """Build a JSON-ready dict for the unshape (and optional GPOS) results.

    Glyphs are keyed by glyph name. Each entry gives the chosen input
    sequence as text and as Unicode code points, every candidate
    sequence, and the non-default features that must be enabled to
    reach the glyph. Glyphs that could not be resolved have a null
    sequence.
    """
    glyphs = {}
    for glyph_name, r in sorted(rmap.items()):
        glyphs[glyph_name] = {
            "sequence": r.sequence,
            "codepoints": _codepoints(r.sequence) if r.sequence else None,
            "all_sequences": list(r.all_sequences),
            "features": list(r.features),
        }
    total = len(rmap)
    mapped = sum(1 for r in rmap.values() if r.sequence is not None)
    out = {
        "font": font_path.name,
        "summary": {"glyphs": total, "mapped": mapped, "unmapped": total - mapped},
        "glyphs": glyphs,
    }
    if gpos_data is not None:
        out["kern_pairs"] = [
            {
                "first": k.first,
                "second": k.second,
                "value": k.value,
                "all_sequences": list(k.all_sequences),
            }
            for k in gpos_data.kern_pairs
        ]
        out["mark_attachments"] = [
            {
                "base": m.base,
                "mark": m.mark,
                "base_anchor": list(m.base_anchor),
                "mark_anchor": list(m.mark_anchor),
            }
            for m in gpos_data.mark_attachments
        ]
    return out


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json
    import sys
    import tempfile
    import webbrowser

    parser = argparse.ArgumentParser(
        prog="unshape",
        description=(
            "Reverse-shape a font: trace every glyph back to the input "
            "characters and features that produce it."
        ),
    )
    parser.add_argument("font", type=Path, help="Path to a font file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output file. The suffix picks the format: .json or .html. "
             "Default: a temporary .html file that opens in the browser.",
    )
    parser.add_argument(
        "--open", action="store_true",
        help="Open an HTML report in the browser after writing it",
    )
    args = parser.parse_args(argv)

    if not args.font.exists():
        print(f"Error: {args.font} does not exist", file=sys.stderr)
        sys.exit(1)

    out = args.output
    if out is None:
        fmt = "html"
        out = Path(tempfile.mktemp(suffix=".html", prefix="unshape_"))
        args.open = True
    else:
        fmt = out.suffix.lower().lstrip(".")
        if fmt not in ("json", "html"):
            print(f"Error: output suffix must be .json or .html, got '{out.suffix}'",
                  file=sys.stderr)
            sys.exit(1)

    from unshaper.gpos import extract_gpos

    font = TTFont(args.font)
    rmap = unshape(font, args.font)
    gpos_data = extract_gpos(font, rmap)

    total = len(rmap)
    mapped = sum(1 for r in rmap.values() if r.sequence is not None)
    with_groups = sum(1 for r in rmap.values() if r.all_sequences)
    with_features = sum(1 for r in rmap.values() if r.features)

    if fmt == "json":
        data = to_dict(args.font, rmap, gpos_data)
        out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    else:
        out.write_text(_generate_html(args.font, rmap, gpos_data))

    print(f"Font: {args.font.name}")
    print(f"Mapped: {mapped}/{total} ({100*mapped/total:.1f}%)")
    print(f"Context groups: {with_groups}, Non-default features: {with_features}")
    print(f"Kern pairs: {len(gpos_data.kern_pairs)}, Mark attachments: {len(gpos_data.mark_attachments)}")
    print(f"Written to {out}")

    if args.open and fmt == "html":
        webbrowser.open(f"file://{out.resolve()}")


if __name__ == "__main__":
    main()
