"""Small geometry/clustering helpers shared across extractors and relations
(`extractors/pdf.py`, `pdf_tables.py`, `pdf_figures.py`,
`relations/propose.py`) — each of these groups nearby items (text lines,
table-border segments, vector-graphics shapes, text fragments) via the same
union-find-over-index-pairs pattern, and several also need IoU (intersection
over union) to judge bbox overlap/duplication. Centralized here instead of
being hand-rolled per module, following the same "small reusable module"
convention as `_image_util.py`.
"""
from __future__ import annotations

from typing import Callable


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Intersection-over-union of two (x0, y0, x1, y1) boxes, 0.0 if they
    don't overlap at all."""
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def cluster_indices(n: int, connected: Callable[[int, int], bool]) -> list[list[int]]:
    """Union-find over `range(n)`: joins i and j whenever `connected(i, j)`
    is true (tested for every unordered pair, so `connected` should be
    cheap), then returns each resulting cluster as a list of member indices.
    Every index 0..n-1 appears in exactly one cluster, including singletons.

    Callers still do their own domain-specific work with the returned
    groups (e.g. `extractors/pdf.py`'s `_reorder_lines` also needs to
    reassign left-to-right x-order within each cluster's original slots) —
    this only replaces the identical 5-line union-find body that used to be
    hand-rolled at each call site.
    """
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(n):
        for j in range(i + 1, n):
            if connected(i, j):
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    return list(clusters.values())
