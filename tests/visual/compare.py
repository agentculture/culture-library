"""Pixel diff between a captured view and its reference."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageChops

THRESHOLD = 0.01  # at most 1% of pixels may differ
CHANNEL_TOLERANCE = 16  # a per-channel delta up to this is anti-aliasing, not a difference


def diff(actual_png: bytes, reference: Path) -> tuple[float, Image.Image | None, str]:
    """``(fraction of pixels that differ, diff image or None, note)``."""
    ref = Image.open(reference).convert("RGB")
    got = Image.open(io.BytesIO(actual_png)).convert("RGB")
    if ref.size != got.size:
        return 1.0, None, f"size {got.size} != reference {ref.size}"
    delta = ImageChops.difference(got, ref)
    bands = [b.point(lambda v: 255 if v > CHANNEL_TOLERANCE else 0) for b in delta.split()]
    mask = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2])
    changed = mask.histogram()[255]
    marked = Image.composite(
        Image.new("RGB", ref.size, (255, 0, 0)), ref.convert("L").convert("RGB"), mask
    )
    return changed / (ref.size[0] * ref.size[1]), marked, ""
