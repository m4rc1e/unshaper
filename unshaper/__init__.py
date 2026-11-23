"""Reverse HarfBuzz shaping: trace glyphs back to their input characters.

Quick start::

    from fontTools.ttLib import TTFont
    from unshaper import unshaper

    results = unshaper(TTFont("Font.ttf"))   # or unshaper("Font.ttf")
    results["f_i"].sequence                  # 'fi'
    results["f_i"].features                  # ['liga'] or [] when default
"""

from unshaper.unshape import UnshapeResult, main, to_dict, unshape, unshaper

__all__ = ["UnshapeResult", "main", "to_dict", "unshape", "unshaper"]
