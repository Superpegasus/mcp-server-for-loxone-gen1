"""Bauplan: viele Bearbeitungsschritte in einem Aufruf, als Einheit (alles oder nichts).

Jeder Schritt ist ein dict mit "op" und den Angaben des jeweiligen Werkzeugs. Mit "als": "name"
bekommt ein neu angelegtes Objekt einen Namen; spätere Schritte verweisen darauf mit "$name".
Bei "verbinden" werden fehlende Platzhalter auf der Seite automatisch angelegt (Eingangs-Platzhalter
für Quellen von anderen Seiten/Peripherie, Ausgangs-Platzhalter für Ziele wie Ausgangsbefehle).

Ops (deutsch, englische Kurzform in Klammern):
  baustein (add)        typ, titel, seite, [x, y], [werte]
  kopie (clone)         von, seite, titel, [von_seite]
  status (state)        seite, titel, quelle, zustaende, [vergleich, standard_text, standard_symbol,
                        standard_farbe, raum, kategorie, sichtbar, quelle_ausgang]
  eingang (input)       titel, [analog, min, max, raum, kategorie, sichtbar, einheit, text, name]
  befehl (command)      titel, ein, [aus, unter, quelle, seite, quelle_ausgang]
  rs232                 art ('befehl'|'sensor'), titel, code, [geraet, code_aus, analog]
  platzhalter (ref)     ziel, seite, [art 'input'|'output']
  verbinden (connect)   von, nach, seite, [von_ausgang, nach_eingang]
  trennen (disconnect)  von, nach, seite, [von_ausgang, nach_eingang]
  werte (set)           baustein, werte, [seite]
  negieren (invert)     baustein, eingang, [aus=False], [seite]
  anzeige (display)     baustein, [sichtbar, einheit]
  statistik (stats)     baustein, [an=True]
  position (move)       baustein, x, y, [seite]
  loeschen (remove)     baustein, [seite]
  peripherie_loeschen   baustein   (ungenutzter virtueller Ein-/Ausgang, Konstante …)
  seite                 titel, [raum, kategorie]
  mitteilung            titel, text, quelle, seite, [quelle_ausgang, min_ms]
  kategorie_umhaengen   von, nach, [art]    kategorie_loeschen  name, [art]
  statusstil            baustein, stile [{text|nr, farbe, symbol}], [symbol (Grundsymbol)]
  raum_kategorie        baustein, [raum], [kategorie]   (einzelnes Objekt umhängen)
  app                   baustein, [favorit], [reihenfolge 0–10], [symbol]
  favorit_struktur      art ('raum'|'kategorie'), name, [an=True]
  heizplan              baustein, komfort {Tag 1=Mo..7=So: [[von, bis], ...]}  (sonst Eco)
  faerben               [seite]   (Bausteine nach Funktion/Kategorie einfärben, ohne Seite: alle)
"""
from __future__ import annotations

import re

from editor import Editor
from loxproject import ProjectError

ALIASES = {"add": "baustein", "clone": "kopie", "state": "status", "input": "eingang", "command": "befehl",
           "ref": "platzhalter", "connect": "verbinden", "disconnect": "trennen", "set": "werte", "invert": "negieren",
           "display": "anzeige", "stats": "statistik", "move": "position", "remove": "loeschen"}
HASH_RE = re.compile(r"#([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4})")


def _first_hash(res) -> str | None:
    if isinstance(res, dict):
        for k in ("kennung", "angelegt"):
            v = res.get(k)
            if isinstance(v, str):
                m = HASH_RE.search(v)
                if m:
                    return "#" + m.group(1)
    return None


def run_plan(ed: Editor, steps: list[dict]) -> dict:
    names: dict[str, str] = {}
    done: list[dict] = []

    def r(v):
        if isinstance(v, str) and v.startswith("$"):
            if v[1:] not in names:
                raise ProjectError(f"Unbekannter Name {v} (vorher mit 'als' vergeben?).")
            return names[v[1:]]
        return v

    def on_page(spec: str, page: str, role: str) -> str:
        """Liefert eine Angabe, die connect auf der Seite findet; legt Platzhalter bei Bedarf an."""
        pr = ed.project
        pg = pr.find_page(page)
        try:
            ed._page_block(spec, page, "source" if role == "source" else "dest")
            return spec
        except ProjectError:
            pass
        target = pr.logical(pr.resolve(spec, None))
        if role == "source":
            return ed._signal_on_page(target, pg)
        before = set(pr.blocks)
        ed.add_ref(pg.title, "#" + pr.short(target), "output", None, None)
        new = [b for u, b in ed.project.blocks.items() if u not in before]
        return "#" + ed.project.short(new[0])

    def one(i: int, s: dict):
        op = ALIASES.get(s.get("op", ""), s.get("op", ""))
        g = lambda k, d=None: r(s.get(k, d))  # noqa: E731
        if op == "baustein":
            res = ed.add_block(g("seite"), g("typ"), g("titel"), g("x"), g("y"), s.get("werte"))
        elif op == "kopie":
            res = ed.clone_block(g("von"), g("seite"), g("titel"), g("von_seite"))
        elif op == "status":
            res = ed.add_state(g("seite"), g("titel"), g("quelle"), s["zustaende"], s.get("vergleich", "gleich"),
                               s.get("standard_text", ""), s.get("standard_symbol"), s.get("standard_farbe"), None,
                               s.get("raum"), s.get("kategorie"), s.get("sichtbar", True), s.get("quelle_ausgang"))
        elif op == "eingang":
            res = ed.add_virtual_input(g("titel"), s.get("analog", True), s.get("min"), s.get("max"), s.get("raum"),
                                       s.get("kategorie"), s.get("sichtbar", False), s.get("einheit"), s.get("text", False),
                                       s.get("name"))
        elif op == "befehl":
            src = g("quelle")
            res = ed.add_command(g("titel"), s["ein"], s.get("aus"), s.get("unter", "Home Assistant"),
                                 on_page(src, g("seite"), "source") if src else None, g("seite"), s.get("quelle_ausgang"))
        elif op == "rs232":
            res = ed.add_rs232(s["art"], g("titel"), s["code"], s.get("geraet"), s.get("code_aus"), s.get("analog"))
        elif op == "platzhalter":
            res = ed.add_ref(g("seite"), g("ziel"), s.get("art"), s.get("x"), s.get("y"))
        elif op == "verbinden":
            pg = g("seite")
            src = on_page(g("von"), pg, "source")
            dst = on_page(g("nach"), pg, "dest")
            res = ed.connect(src, s.get("von_ausgang"), dst, s.get("nach_eingang"), pg)
        elif op == "trennen":
            res = ed.disconnect(g("von"), s.get("von_ausgang"), g("nach"), s.get("nach_eingang"), g("seite"))
        elif op == "werte":
            res = ed.set_params(g("baustein"), s["werte"], g("seite"))
        elif op == "negieren":
            res = ed.set_inverted(g("baustein"), s["eingang"], not s.get("aus", False), g("seite"))
        elif op == "anzeige":
            res = ed.set_display(g("baustein"), s.get("sichtbar"), s.get("einheit"))
        elif op == "statistik":
            res = ed.set_statistics(g("baustein"), "jede_aenderung" if s.get("an", True) else "aus")
        elif op == "position":
            res = ed.set_position(g("baustein"), int(s["x"]), int(s["y"]), g("seite"))
        elif op == "loeschen":
            res = ed.remove_block(g("baustein"), g("seite"))
        elif op == "peripherie_loeschen":
            res = ed.remove_periphery(g("baustein"))
        elif op == "seite":
            res = ed.add_page(g("titel"), s.get("raum"), s.get("kategorie"))
        elif op == "mitteilung":
            src = g("quelle")
            res = ed.add_notification(g("titel"), s["text"], on_page(src, g("seite"), "source"), g("seite"),
                                      s.get("quelle_ausgang"), s.get("min_ms", 5000))
        elif op == "statusstil":
            res = ed.set_state_style(g("baustein"), s["stile"], s.get("symbol"), g("seite"))
        elif op == "raum_kategorie":
            res = ed.set_room_category(g("baustein"), s.get("raum"), s.get("kategorie"), g("seite"))
        elif op == "app":
            res = ed.set_app(g("baustein"), s.get("favorit"), s.get("reihenfolge"), s.get("symbol"), g("seite"))
        elif op == "favorit_struktur":
            res = ed.set_structure_favorite(s["art"], s["name"], s.get("an", True))
        elif op == "heizplan":
            res = ed.set_heating_schedule(g("baustein"), {int(k): v for k, v in s["komfort"].items()}, g("seite"))
        elif op == "verschieben":
            res = ed.move_blocks_to_page(s["bausteine"], s["nach"], s.get("von_seite"))
        elif op == "schaltuhr":
            res = ed.set_schedule(g("baustein"), {int(k): v for k, v in s["tage"].items()}, g("seite"))
        elif op == "faerben":
            res = ed.color_by_function(g("seite"))
        elif op == "kategorie_umhaengen":
            res = ed.reassign_structure(s.get("art", "kategorie"), s["von"], s["nach"])
        elif op == "kategorie_loeschen":
            res = ed.remove_structure(s.get("art", "kategorie"), s["name"])
        else:
            raise ProjectError(f"Unbekannte op '{s.get('op')}'.")
        if s.get("als"):
            h = _first_hash(res)
            if not h:
                raise ProjectError(f"Schritt {i + 1}: 'als' geht nur bei Schritten, die etwas anlegen.")
            names[s["als"]] = h
        done.append({"schritt": i + 1, "op": op, "ergebnis": res})

    def all_steps():
        for i, s in enumerate(steps):
            try:
                one(i, s)
            except Exception as e:
                raise ProjectError(f"Schritt {i + 1} ({s.get('op')}): {e}") from e

    if not steps:
        raise ProjectError("Der Bauplan ist leer.")
    ed._atomic(all_steps)
    return {"schritte": len(done), "namen": names, "ergebnisse": done}
