"""Layout aller Programmseiten (nur Positionen). Aufruf: python layout_all.py SRC DST [speichern] [png:Seite,...]"""
import json, sys, collections
from pathlib import Path
SRV = Path(__file__).resolve().parent
sys.path.insert(0, str(SRV))
from catalog import Catalog
from editor import Editor

SKIP: set[str] = set()   # Seiten, die von Hand geordnet sind und bleiben sollen
W_SHEET, H_SHEET = 23400, 16300
M = 480
SNAP = lambda v: int(round(v / 96)) * 96
SINK_TYPES = {"OutputRef", "OutputRefLM"}

def geo(b):
    g = lambda k: int(b.elem.get(k) or 0)
    w, h = g("Px2") - g("Px"), g("Py2") - g("Py")
    if w <= 0 or h <= 0:
        w, h = 2112, 192
    return g("Px"), g("Py"), w, h

def layout(p, page, gx, gy):
    blocks = [b for b in p.blocks.values() if b.page is page and b.parent is page]
    ids = {b.uuid: b for b in blocks}
    succ, pred = collections.defaultdict(set), collections.defaultdict(set)
    for e in p.edges:
        if e.src.uuid in ids and e.dst.uuid in ids and e.src.uuid != e.dst.uuid:
            succ[e.src.uuid].add(e.dst.uuid); pred[e.dst.uuid].add(e.src.uuid)
    # Komponenten
    parent = {u: u for u in ids}
    def find(u):
        while parent[u] != u:
            parent[u] = parent[parent[u]]; u = parent[u]
        return u
    for u in ids:
        for v in succ[u]: parent[find(u)] = find(v)
    comps = collections.defaultdict(list)
    for u in ids: comps[find(u)].append(u)
    bands, singles = [], []
    for members in comps.values():
        if len(members) == 1:
            singles.append(members[0]); continue
        # Schichten (längster Weg, Zyklen werden durch Durchlaufgrenze abgefangen)
        layer = {u: 0 for u in members}
        for _ in range(len(members)):
            ch = False
            for u in members:
                for v in succ[u]:
                    if layer[v] < layer[u] + 1 and layer[u] + 1 <= len(members):
                        layer[v] = layer[u] + 1; ch = True
            if not ch: break
        top = max(layer.values())
        for u in members:
            if ids[u].type in SINK_TYPES and not succ[u]:
                layer[u] = top
        cols = collections.defaultdict(list)
        for u in members: cols[layer[u]].append(u)
        key0 = lambda u: (geo(ids[u])[1], geo(ids[u])[0])
        for k in cols: cols[k].sort(key=key0)
        order = {u: i for k in sorted(cols) for i, u in enumerate(cols[k])}
        for _ in range(3):   # Schwerpunkt-Sortierung gegen Kreuzungen
            for k in sorted(cols):
                if k == 0: continue
                def bary(u):
                    ps = [order[x] for x in pred[u] if x in order and layer[x] < layer[u]]
                    return (sum(ps) / len(ps)) if ps else order[u]
                cols[k].sort(key=lambda u: (bary(u), key0(u)))
                for i, u in enumerate(cols[k]): order[u] = i
        bands.append((min(key0(u) for u in members), cols))
    bands.sort(key=lambda b: b[0])
    # Bänder zu Teilspalten (mit Umbruch hoher Schichten), dann Bänder in Spalten packen (größte zuerst)
    placed = {}
    max_h = H_SHEET - M - 96
    gap_b = 576 if gy >= 192 else 288
    infos = []
    for _, cols in bands:
        subcols = []
        for k in sorted(cols):
            chunk, h = [], 0
            for u in cols[k]:
                bh = geo(ids[u])[3] + gy
                if chunk and h + bh > max_h:
                    subcols.append(chunk); chunk, h = [], 0
                chunk.append(u); h += bh
            if chunk: subcols.append(chunk)
        heights = [sum(geo(ids[u])[3] + gy for u in c) - gy for c in subcols]
        widths = [max(geo(ids[u])[2] for u in c) for c in subcols]
        infos.append((max(heights), sum(widths) + gx * (len(widths) - 1), subcols, widths))
    infos.sort(key=lambda t: -t[0])
    columns = []   # [Resthöhe-Verbrauch, Breite, [Bänder]]
    for info in infos:
        for col in columns:
            if col[0] + info[0] <= H_SHEET - 2 * M:
                col[0] += info[0] + gap_b; col[1] = max(col[1], info[1]); col[2].append(info); break
        else:
            columns.append([info[0] + gap_b, info[1], [info]])
    x0 = M
    for col in columns:
        y0 = M
        for band_h, band_w, subcols, widths in col[2]:
            x = x0
            for c, w in zip(subcols, widths):
                y = y0
                for u in c:
                    placed[u] = (x, y); y += geo(ids[u])[3] + gy
                x += w + gx
            y0 += band_h + gap_b
        x0 += col[1] + 960 if gx > 96 else col[1] + 480
    col_x_start, cur_col_w = x0, 0
    out = {u: (SNAP(x), SNAP(y)) for u, (x, y) in placed.items()}
    ok = all(x + geo(ids[u])[2] <= W_SHEET and y + geo(ids[u])[3] <= H_SHEET for u, (x, y) in out.items())
    return ids, out, ok

def run(src, dst, save, png):
    ed = Editor(Path(src), Catalog(SRV / "catalog.json"))
    p = ed.project
    plans, report = {}, []
    for page in p.pages:
        if page.title in SKIP: continue
        for gx, gy in ((672, 192), (480, 192), (288, 192), (192, 96), (96, 96)):
            ids, out, ok = layout(p, page, gx, gy)
            if ok: break
        report.append((page.title, len(ids), "ok" if ok else "ZU GROSS", gx))
        if ok: plans[page.title] = (ids, out)
    for r in report: print(r)
    if png:
        from PIL import Image, ImageDraw
        for t in png:
            ids, out = plans[t]
            sc = 0.05
            im = Image.new("RGB", (int(W_SHEET * sc), int(H_SHEET * sc)), "white"); d = ImageDraw.Draw(im)
            for e in p.edges:
                if e.src.uuid in out and e.dst.uuid in out:
                    sx, sy = out[e.src.uuid]; _, _, sw, sh = geo(e.src); dx, dy = out[e.dst.uuid]; _, _, _, dh = geo(e.dst)
                    d.line([((sx + sw) * sc, (sy + sh / 2) * sc), (dx * sc, (dy + dh / 2) * sc)], fill=(170, 170, 170))
            for u, (x, y) in out.items():
                _, _, w, h = geo(ids[u]); col = (120, 190, 255) if ids[u].type == "InputRef" else (255, 190, 120) if ids[u].type.startswith("OutputRef") else (150, 220, 150)
                d.rectangle([x * sc, y * sc, (x + w) * sc, (y + h) * sc], fill=col, outline=(60, 60, 60))
            im.save(Path(sys.argv[0]).parent / f"layout_{t.replace(' ', '_').replace('.', '_').encode('ascii','ignore').decode()}.png")
    if save:
        def apply():
            for t, (ids, out) in plans.items():
                for u, (x, y) in out.items():
                    _, _, w, h = geo(ids[u])
                    ed.pt.set_geometry(u, x, y, x + w, y + h)
        ed._apply(f"Layout: {len(plans)} Seiten geordnet", apply)
        v = ed.validate(); print("Prüfung:", v["in_ordnung"], v["fehler"][:3])
        if not v["in_ordnung"] or Path(dst).exists(): sys.exit("nicht gespeichert")
        print(json.dumps(ed.save(Path(dst)), ensure_ascii=False)[:120])

if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    save = "speichern" in sys.argv[3:]
    png = [a[4:].split(",") for a in sys.argv[3:] if a.startswith("png:")]
    run(src, dst, save, png[0] if png else [])
