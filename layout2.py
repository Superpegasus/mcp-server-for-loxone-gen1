"""Layout von Programmseiten, bei dem Verbindungen möglichst nicht über Bausteine laufen.

Schichtenlayout (Eingänge links, Ausgänge rechts) mit Umleitungsspuren: Verbindungen, die mehrere Schichten
überspringen, reservieren in jeder Zwischenschicht eine Spur auf der geraden Linie von Quelle zu Ziel; die
Bausteine werden um diese Spuren herum gepackt. Aufruf als Modul: layout2.layout(project, page, gx, gap)."""
from __future__ import annotations

import collections

W_SHEET, H_SHEET, M = 23400, 16300, 480
SNAP = lambda v: int(round(v / 96)) * 96  # noqa: E731
SINK_TYPES = {"OutputRef", "OutputRefLM"}


def geo(b):
    g = lambda k: int(b.elem.get(k) or 0)  # noqa: E731
    w, h = g("Px2") - g("Px"), g("Py2") - g("Py")
    if w <= 0 or h <= 0:
        w, h = 2112, 192
    return g("Px"), g("Py"), w, h


def _seg_hits_rect(x1, y1, x2, y2, rx, ry, rw, rh, pad=24):
    """Liang-Barsky: schneidet die Strecke das Rechteck (mit kleinem Rand)?"""
    xmin, xmax, ymin, ymax = rx - pad, rx + rw + pad, ry - pad, ry + rh + pad
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p_, q_ in ((-dx, x1 - xmin), (dx, xmax - x1), (-dy, y1 - ymin), (dy, ymax - y1)):
        if p_ == 0:
            if q_ < 0:
                return False
        else:
            t = q_ / p_
            if p_ < 0:
                if t > t1:
                    return False
                t0 = max(t0, t)
            else:
                if t < t0:
                    return False
                t1 = min(t1, t)
    return t0 <= t1


def collisions(p, page_blocks: dict, pos: dict) -> int:
    """Zahl der Verbindungen, die (gerade gezeichnet) über einen fremden Baustein laufen."""
    ids = set(pos)
    rects = {u: (pos[u][0], pos[u][1], geo(page_blocks[u])[2], geo(page_blocks[u])[3]) for u in ids}
    n = 0
    for e in p.edges:
        a, b = e.src.uuid, e.dst.uuid
        if a in ids and b in ids and a != b:
            ax, ay, aw, ah = rects[a]
            bx, by, bw, bh = rects[b]
            x1, y1, x2, y2 = ax + aw, ay + ah / 2, bx, by + bh / 2
            for u, (rx, ry, rw, rh) in rects.items():
                if u != a and u != b and _seg_hits_rect(x1, y1, x2, y2, rx, ry, rw, rh):
                    n += 1
                    break
    return n


def layout(p, page, gx: int = 672, gap: int = 192):
    """Gibt (Blockverzeichnis, Positionen, passt aufs Blatt) zurück."""
    blocks = [b for b in p.blocks.values() if b.page is page and b.parent is page]
    ids = {b.uuid: b for b in blocks}
    succ, pred = collections.defaultdict(set), collections.defaultdict(set)
    for e in p.edges:
        if e.src.uuid in ids and e.dst.uuid in ids and e.src.uuid != e.dst.uuid:
            succ[e.src.uuid].add(e.dst.uuid)
            pred[e.dst.uuid].add(e.src.uuid)
    parent = {u: u for u in ids}

    def find(u):
        while parent[u] != u:
            parent[u] = parent[parent[u]]
            u = parent[u]
        return u
    for u in ids:
        for v in succ[u]:
            parent[find(u)] = find(v)
    comps = collections.defaultdict(list)
    for u in ids:
        comps[find(u)].append(u)
    key0 = lambda u: (geo(ids[u])[1], geo(ids[u])[0])  # noqa: E731
    bands, singles = [], []
    for members in comps.values():
        if len(members) == 1:
            singles.append(members[0])
            continue
        # Rückkopplungen (Zyklen) für die Schichtenbildung ignorieren: sonst werden Schichten künstlich aufgebläht
        dag = {u: set(succ[u]) for u in members}
        state = {u: 0 for u in members}   # 0 neu, 1 auf dem Stapel, 2 fertig
        for root in sorted(members, key=lambda u: (len(pred[u]), key0(u))):
            if state[root]:
                continue
            stack = [(root, iter(sorted(dag[root], key=key0)))]
            state[root] = 1
            while stack:
                u, it = stack[-1]
                for v in it:
                    if state[v] == 1:
                        dag[u].discard(v)      # Rückkante
                    elif state[v] == 0:
                        state[v] = 1
                        stack.append((v, iter(sorted(dag[v], key=key0))))
                        break
                else:
                    state[u] = 2
                    stack.pop()
        layer = {u: 0 for u in members}
        for _ in range(len(members)):
            ch = False
            for u in members:
                for v in dag[u]:
                    if layer[v] < layer[u] + 1:
                        layer[v] = layer[u] + 1
                        ch = True
            if not ch:
                break
        top = max(layer.values())
        for u in members:
            if ids[u].type in SINK_TYPES and not dag[u]:
                layer[u] = top
        # Knoten (echt + Spuren) und Kanten zwischen benachbarten Schichten
        height = {u: geo(ids[u])[3] for u in members}
        width = {u: geo(ids[u])[2] for u in members}
        lay = dict(layer)
        nxt, prv = collections.defaultdict(list), collections.defaultdict(list)
        chains = []   # (Quelle, Ziel, [Spuren])
        dn = 0
        for u in members:
            for v in succ[u]:
                if lay[v] <= lay[u]:
                    continue
                prev = u
                chain = []
                for k in range(lay[u] + 1, lay[v]):
                    d = f"~{dn}"
                    dn += 1
                    lay[d] = k
                    height[d] = 96
                    width[d] = 0
                    nxt[prev].append(d)
                    prv[d].append(prev)
                    prev = d
                    chain.append(d)
                nxt[prev].append(v)
                prv[v].append(prev)
                chains.append((u, v, chain))
        cols = collections.defaultdict(list)
        for u in lay:
            cols[lay[u]].append(u)
        for k in cols:
            cols[k].sort(key=lambda u: key0(u) if u in ids else (1e9, 0))
        order = {u: i for k in sorted(cols) for i, u in enumerate(cols[k])}

        def bary(u, nb):
            ps = [order[x] for x in nb[u] if x in order]
            return (sum(ps) / len(ps)) if ps else order[u]
        for sweep in range(4):
            ks = sorted(cols)
            if sweep % 2:
                ks = ks[::-1]
            for k in ks:
                nb = prv if sweep % 2 == 0 else nxt
                if (sweep % 2 == 0 and k == 0) or (sweep % 2 and k == max(cols)):
                    continue
                cols[k].sort(key=lambda u: (bary(u, nb), order[u]))
                for i, u in enumerate(cols[k]):
                    order[u] = i
        # Startkoordinaten: gestapelt
        y = {}
        for k in cols:
            c = 0
            for u in cols[k]:
                y[u] = c
                c += height[u] + gap
        # Verfeinern: Baustein zum Mittel der Nachbarn ziehen, dann Überlappungen auflösen (Reihenfolge bleibt)
        def settle(k, nb):
            want = {}
            for u in cols[k]:
                ns = [y[x] + height[x] / 2 for x in nb[u] if x in y]
                want[u] = (sum(ns) / len(ns) - height[u] / 2) if ns else y[u]
            bottom = None
            for u in cols[k]:
                top = want[u] if bottom is None else max(want[u], bottom + gap)
                y[u] = top
                bottom = top + height[u]
        kmax = max(cols)
        for it in range(8):
            for k in sorted(cols):
                if 0 < k < kmax:
                    settle(k, prv)
            for k in sorted(cols, reverse=True):
                if 0 < k < kmax:
                    settle(k, nxt)
            low = min(y.values())
            for u in y:
                y[u] -= low
        base = min(y.values())
        for u in y:
            y[u] -= base
        # Spuren auf die Gerade Quelle-Ziel legen und Bausteine um sie herum neu packen (ein Durchgang)
        for u, v, chain in chains:
            if chain:
                ya, yb = y[u] + height[u] / 2, y[v] + height[v] / 2
                for i, d in enumerate(chain):
                    y[d] = ya + (yb - ya) * (i + 1) / (len(chain) + 1) - 48
        for k in sorted(cols):
            cols[k].sort(key=lambda u: (y[u], order[u]))
            bottom = None
            for u in cols[k]:
                top = y[u] if bottom is None else max(y[u], bottom + gap)
                y[u] = top
                bottom = top + height[u]
        base = min(y.values())
        wcol = {k: max([width[u] for u in cols[k]] + [0]) for k in cols}
        xk, x = {}, 0
        for k in sorted(cols):
            xk[k] = x
            x += wcol[k] + (gx if wcol[k] else gx // 3)
        local = {u: (xk[lay[u]], y[u] - base) for u in members}
        bw = max(local[u][0] + width[u] for u in members)
        bh = max(local[u][1] + height[u] for u in members)
        bands.append((bh, bw, local))
    # Bänder in Spalten packen (größte zuerst)
    gap_b = 576
    bands.sort(key=lambda b: -b[0])
    columns = []
    for bh, bw, local in bands:
        for col in columns:
            if col[0] + bh <= H_SHEET - 2 * M:
                col[3].append((col[0], local))
                col[0] += bh + gap_b
                col[1] = max(col[1], bw)
                break
        else:
            columns.append([bh + gap_b, bw, 0, [(0, local)]])
    placed, x0 = {}, M
    for col in columns:
        for y0, local in col[3]:
            for u, (lx, ly) in local.items():
                placed[u] = (x0 + lx, M + y0 + ly)
        x0 += col[1] + 960
    if singles:
        singles.sort(key=key0)
        yy, xx, colw = M, x0, 0
        for u in singles:
            _, _, w, h = geo(ids[u])
            if yy + h > H_SHEET - M:
                xx += colw + 480
                yy, colw = M, 0
            placed[u] = (xx, yy)
            yy += h + 288
            colw = max(colw, w)
    out = {u: (SNAP(x), SNAP(yv)) for u, (x, yv) in placed.items()}
    ok = all(x + geo(ids[u])[2] <= W_SHEET and yv + geo(ids[u])[3] <= H_SHEET for u, (x, yv) in out.items())
    return ids, out, ok
