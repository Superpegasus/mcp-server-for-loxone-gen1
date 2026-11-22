"""Verfeinert die Anordnung einer Programmseite durch lokale Suche: einzelne Bausteine werden verschoben,
solange dadurch weniger Verbindungen (gerade gezeichnet) über fremde Bausteine laufen. Dabei bleiben Bausteine
überlappungsfrei, auf dem Blatt und die Verbindungen möglichst von links nach rechts und kurz."""
from __future__ import annotations

import math
import random

from layout2 import H_SHEET, W_SHEET, _seg_hits_rect, geo

GRID = 96
MARGIN = 96


def optimize(p, page, iters: int = 40000, seed: int = 1, start: dict | None = None, xlock: bool = False):
    """Gibt (Blockverzeichnis, neue Positionen, Kreuzungen vorher, Kreuzungen nachher) zurück."""
    ids = {b.uuid: b for b in p.blocks.values() if b.page is page and b.parent is page}
    size = {u: geo(b)[2:] for u, b in ids.items()}
    pos = dict(start) if start else {u: (geo(b)[0], geo(b)[1]) for u, b in ids.items()}
    edges = sorted({(e.src.uuid, e.dst.uuid) for e in p.edges
                    if e.src.uuid in ids and e.dst.uuid in ids and e.src.uuid != e.dst.uuid})
    inc = {u: [] for u in ids}
    for a, b in edges:
        inc[a].append((a, b))
        inc[b].append((a, b))
    rng = random.Random(seed)
    order = list(ids)

    def seg(a, b, pa=None, pb=None):
        ax, ay = pa or pos[a]
        bx, by = pb or pos[b]
        return ax + size[a][0], ay + size[a][1] / 2, bx, by + size[b][1] / 2

    def hit(e, v, pv=None, pa=None, pb=None):
        a, b = e
        x1, y1, x2, y2 = seg(a, b, pa, pb)
        rx, ry = pv or pos[v]
        return _seg_hits_rect(x1, y1, x2, y2, rx, ry, size[v][0], size[v][1])

    def local(u, pu):
        """Beiträge, an denen u beteiligt ist: (Kreuzungen, Strafe)."""
        cross, pen = 0, 0.0
        for e in inc[u]:
            a, b = e
            pa = pu if a == u else None
            pb = pu if b == u else None
            for v in ids:
                if v != a and v != b and hit(e, v, None, pa, pb):
                    cross += 1
            x1, _, x2, _ = seg(a, b, pa, pb)
            if x1 > x2 - 96:
                pen += 3.0
            pen += math.hypot(x2 - x1, seg(a, b, pa, pb)[3] - seg(a, b, pa, pb)[1]) / 4000.0
        for e in edges:
            if e[0] != u and e[1] != u and hit(e, u, pu):
                cross += 1
        return cross, pen

    def free(u, pu):
        x, y = pu
        w, h = size[u]
        if x < GRID or y < GRID or x + w > W_SHEET - GRID or y + h > H_SHEET - GRID:
            return False
        for v, (vx, vy) in pos.items():
            if v != u and x < vx + size[v][0] + MARGIN and vx < x + w + MARGIN and y < vy + size[v][1] + MARGIN and vy < y + h + MARGIN:
                return False
        return True

    def total():
        n = 0
        for e in edges:
            for v in ids:
                if v != e[0] and v != e[1] and hit(e, v):
                    n += 1
                    break
        return n
    before = total()
    for it in range(iters):
        u = rng.choice(order)
        x, y = pos[u]
        r = rng.random()
        if r < 0.6:
            s = 3000 if it < iters * 0.5 else 900
            nx, ny = (x if xlock else x + rng.gauss(0, s)), y + rng.gauss(0, s)
        else:   # in die Nähe der Nachbarn springen
            ns = [(pos[b][0] - size[u][0] - 480 if a == u else pos[a][0] + size[a][0] + 480,
                   pos[b][1] if a == u else pos[a][1]) for a, b in inc[u]]
            if not ns:
                continue
            cx = sum(n[0] for n in ns) / len(ns)
            cy = sum(n[1] for n in ns) / len(ns)
            nx, ny = (x if xlock else cx + rng.gauss(0, 1200)), cy + rng.gauss(0, 1200)
        nx, ny = round(nx / GRID) * GRID, round(ny / GRID) * GRID
        if (nx, ny) == (x, y) or not free(u, (nx, ny)):
            continue
        c0, p0 = local(u, (x, y))
        c1, p1 = local(u, (nx, ny))
        if (c1 - c0) * 10 + (p1 - p0) < 0:
            pos[u] = (nx, ny)
    return ids, pos, before, total()
