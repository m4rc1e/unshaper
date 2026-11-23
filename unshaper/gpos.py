"""GPOS table extraction: kerning pairs and mark attachment positions."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from fontTools.ttLib import TTFont

from unshaper.unshape import UnshapeResult


@dataclass
class KernPair:
    first: str
    second: str
    value: int
    all_sequences: list[str] = field(default_factory=list)

    @property
    def sequence(self) -> str:
        return self.first + self.second


@dataclass
class MarkAttachment:
    base: str
    mark: str
    base_anchor: tuple[int, int]
    mark_anchor: tuple[int, int]

    @property
    def sequence(self) -> str:
        return self.base + self.mark


@dataclass
class GPOSResult:
    kern_pairs: list[KernPair] = field(default_factory=list)
    mark_attachments: list[MarkAttachment] = field(default_factory=list)


def extract_gpos(
    font: TTFont,
    rmap: dict[str, UnshapeResult],
) -> GPOSResult:
    if "GPOS" not in font:
        return GPOSResult()

    gpos = font["GPOS"].table
    if not gpos.LookupList:
        return GPOSResult()

    seq_of = {gn: r.sequence for gn, r in rmap.items() if r.sequence}
    result = GPOSResult()

    for lookup in gpos.LookupList.Lookup:
        for subtable in lookup.SubTable:
            st = subtable
            if lookup.LookupType == 9 and hasattr(st, "ExtSubTable"):
                st = st.ExtSubTable

            effective_type = getattr(st, "LookupType", lookup.LookupType)
            if effective_type == 2:
                _extract_pair_pos(st, seq_of, result.kern_pairs)
            elif effective_type == 4:
                _extract_mark_base(st, seq_of, result.mark_attachments)

    return result


# ---- Kern pairs (PairPos, type 2) ----------------------------------------

def _kern_value(vr) -> int:
    if vr is None:
        return 0
    return getattr(vr, "XAdvance", 0) or 0


def _extract_pair_pos(
    st, seq_of: dict[str, str], out: list[KernPair],
) -> None:
    fmt = getattr(st, "Format", 0)
    if fmt == 1:
        _extract_pair_pos_fmt1(st, seq_of, out)
    elif fmt == 2:
        _extract_pair_pos_fmt2(st, seq_of, out)


def _extract_pair_pos_fmt1(
    st, seq_of: dict[str, str], out: list[KernPair],
) -> None:
    """Individual pair adjustments."""
    if not hasattr(st, "PairSet") or not st.PairSet:
        return
    coverage_glyphs = st.Coverage.glyphs if st.Coverage else []
    for first_glyph, pair_set in zip(coverage_glyphs, st.PairSet):
        first_seq = seq_of.get(first_glyph)
        if not first_seq:
            continue
        for pvr in pair_set.PairValueRecord:
            second_seq = seq_of.get(pvr.SecondGlyph)
            if not second_seq:
                continue
            val = _kern_value(pvr.Value1)
            if val:
                out.append(KernPair(first=first_seq, second=second_seq, value=val))


def _extract_pair_pos_fmt2(
    st, seq_of: dict[str, str], out: list[KernPair],
) -> None:
    """Class-based pair adjustments."""
    if not hasattr(st, "Class1Record") or not st.Class1Record:
        return

    # Build class → glyphs mappings.
    # Keep coverage order so the representative glyph of each class is
    # stable from run to run (set iteration order is not).
    coverage_list = list(st.Coverage.glyphs) if st.Coverage else []
    coverage_glyphs = frozenset(coverage_list)

    class1_map: dict[int, list[str]] = defaultdict(list)
    if st.ClassDef1 and st.ClassDef1.classDefs:
        for glyph, cls in st.ClassDef1.classDefs.items():
            if glyph in coverage_glyphs:
                class1_map[cls].append(glyph)
    # Class 0 = coverage glyphs not assigned to any class.
    assigned1 = set()
    for glyphs in class1_map.values():
        assigned1.update(glyphs)
    class1_map[0].extend(g for g in coverage_list if g not in assigned1)

    class2_map: dict[int, list[str]] = defaultdict(list)
    if st.ClassDef2 and st.ClassDef2.classDefs:
        for glyph, cls in st.ClassDef2.classDefs.items():
            class2_map[cls].append(glyph)
    # Class 0 for ClassDef2 = all glyphs not in any class.
    # Too large to enumerate; skip class 0 for second position.

    for c1_idx, c1_rec in enumerate(st.Class1Record):
        first_glyphs = class1_map.get(c1_idx)
        if not first_glyphs:
            continue
        first_seqs = [
            (g, seq_of[g]) for g in first_glyphs if g in seq_of
        ]
        if not first_seqs:
            continue

        for c2_idx, c2_rec in enumerate(c1_rec.Class2Record):
            val = _kern_value(c2_rec.Value1)
            if not val:
                continue
            second_glyphs = class2_map.get(c2_idx)
            if not second_glyphs:
                continue
            second_seqs = [
                (g, seq_of[g]) for g in second_glyphs if g in seq_of
            ]
            if not second_seqs:
                continue

            # Build all pairs in this class combination.
            all_seqs: list[str] = []
            seen: set[str] = set()
            for _, fs in first_seqs:
                for _, ss in second_seqs:
                    pair = fs + ss
                    if pair not in seen:
                        seen.add(pair)
                        all_seqs.append(pair)

            out.append(KernPair(
                first=first_seqs[0][1],
                second=second_seqs[0][1],
                value=val,
                all_sequences=all_seqs,
            ))


# ---- Mark-to-base positioning (MarkBasePos, type 4) -----------------------

def _anchor_xy(anchor) -> tuple[int, int]:
    if anchor is None:
        return (0, 0)
    return (getattr(anchor, "XCoordinate", 0) or 0,
            getattr(anchor, "YCoordinate", 0) or 0)


def _extract_mark_base(
    st, seq_of: dict[str, str], out: list[MarkAttachment],
) -> None:
    if not all(hasattr(st, a) for a in (
        "MarkCoverage", "BaseCoverage", "MarkArray", "BaseArray",
    )):
        return

    mark_glyphs = st.MarkCoverage.glyphs if st.MarkCoverage else []
    base_glyphs = st.BaseCoverage.glyphs if st.BaseCoverage else []

    if not mark_glyphs or not base_glyphs:
        return

    mark_records = st.MarkArray.MarkRecord if st.MarkArray else []
    base_records = st.BaseArray.BaseRecord if st.BaseArray else []

    for mark_idx, mark_glyph in enumerate(mark_glyphs):
        mark_seq = seq_of.get(mark_glyph)
        if not mark_seq or mark_idx >= len(mark_records):
            continue

        mark_rec = mark_records[mark_idx]
        mark_class = mark_rec.Class
        mark_anchor = _anchor_xy(mark_rec.MarkAnchor)

        for base_idx, base_glyph in enumerate(base_glyphs):
            base_seq = seq_of.get(base_glyph)
            if not base_seq or base_idx >= len(base_records):
                continue

            base_rec = base_records[base_idx]
            if mark_class >= len(base_rec.BaseAnchor):
                continue
            base_anc = base_rec.BaseAnchor[mark_class]
            if base_anc is None:
                continue

            out.append(MarkAttachment(
                base=base_seq,
                mark=mark_seq,
                base_anchor=_anchor_xy(base_anc),
                mark_anchor=mark_anchor,
            ))
