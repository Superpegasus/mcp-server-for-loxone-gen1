"""Bearbeitungssitzung (Stufe 3 und 4): prüft jede Änderung, führt sie auf einem
Arbeitsstand im Speicher aus und speichert nur in eine neue Datei.

Die Quelldatei wird nie verändert. Jede Operation ist einzeln rückgängig zu machen.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from catalog import Catalog
from loxproject import Block, Project, ProjectError
from writer import ProjectText, WriteError

ALLOWED_PLACEHOLDERS = {"Cr", "Pr"}
PROTECTED_ATTRS = {"Type", "U", "V", "Nio", "Ref", "LinkRefType", "Px", "Py", "Px2", "Py2"}
GRID = 96
MAX_UNDO = 30


def _snap(v: int) -> int:
    return int(round(v / GRID)) * GRID


def _px(b: Block, name: str) -> int:
    try:
        return int(b.elem.get(name, "0") or 0)
    except ValueError:
        return 0


class Editor:
    def __init__(self, source: Path, catalog: Catalog):
        self.source = Path(source)
        self.catalog = catalog
        self.pt = ProjectText(self.source.read_bytes())
        self.base = Project(self.source)
        self.project = self.base
        self.undo_stack: list[tuple[str, str]] = []
        self.log: list[str] = []

    # ---------- Ablauf ----------
    def _apply(self, description: str, fn) -> None:
        before = self.pt.text
        try:
            fn()
            self.project = Project(self.source, self.pt.data())
        except (WriteError, ProjectError, ET.ParseError) as e:
            self.pt.text = before
            self.project = Project(self.source, self.pt.data())
            raise ProjectError(str(e)) from e
        self.undo_stack.append((before, description))
        del self.undo_stack[:-MAX_UNDO]
        self.log.append(description)

    def undo(self) -> dict:
        if not self.undo_stack:
            raise ProjectError("Es gibt nichts rückgängig zu machen.")
        text, description = self.undo_stack.pop()
        self.pt.text = text
        self.project = Project(self.source, self.pt.data())
        self.log.pop()
        return {"rueckgaengig": description, "verbleibende_schritte": len(self.log)}

    # ---------- Auflösen ----------
    def _is_input_ref(self, b: Block) -> bool:
        return self.project.is_ref(b) and b.type.startswith("InputRef")

    def _is_output_ref(self, b: Block) -> bool:
        return self.project.is_ref(b) and b.type.startswith("OutputRef")

    def _page_block(self, spec: str, page: str | None, role: str) -> Block:
        """Baustein, wie er auf einer Seite liegt. Für Geräte und Merker anderer Seiten
        wird der passende Platzhalter auf der Seite gesucht."""
        pr = self.project
        pg = pr.find_page(page) if page else None
        try:
            b = pr.resolve(spec, page) if page else pr.resolve(spec, None)
        except ProjectError:
            if not page:
                raise
            b = pr.resolve(spec, None)  # liegt nicht auf der Seite: dort nach einem Platzhalter suchen
        if b.page is not None and (pg is None or b.page is pg):
            return b
        if pg is None:
            raise ProjectError(f"'{pr.name(b)}' liegt auf keiner Seite. Bitte 'page' angeben.")
        target = pr.logical(b)
        want = self._is_input_ref if role == "source" else self._is_output_ref
        refs = [r for r in pr.blocks.values() if r.page is pg and pr.target(r) is target and want(r)]
        if not refs:
            art = "Eingangs" if role == "source" else "Ausgangs"
            raise ProjectError(f"Auf Seite {pg.title} gibt es keinen {art}-Platzhalter für "
                               f"'{pr.name(target)}'. Bitte zuerst ref_add aufrufen.")
        return refs[0]

    def _ref_output_key(self, ref: Block) -> str:
        """Loxone nutzt bei Platzhaltern AQ, nur bei digitalen Merkern Q; andere Verbindungen verwirft Config."""
        t = self.project.target(ref)
        if ref.elem.get("Analog") == "true" or t is None or t.type != "Memory":
            return "AQ"
        return "Q"

    def _direction(self, b: Block, key: str) -> tuple[str | None, bool]:
        t = self.catalog.types.get(b.type)
        if not t:
            return None, False
        for c in t["anschluesse"]:
            if c["name"].lower() == key.lower():
                return c["richtung"], not c.get("richtung_unsicher", False)
        return None, False

    def _endpoints(self, src: str, src_key: str | None, dst: str, dst_key: str | None, page: str | None):
        pr = self.project
        s = self._page_block(src, page, "source")
        d = self._page_block(dst, page or (s.page.title if s.page else None), "dest")
        if s.page is not d.page:
            raise ProjectError(f"Quelle (Seite {s.page.title}) und Ziel (Seite {d.page.title}) liegen auf "
                               "verschiedenen Seiten. Verbindungen werden nur innerhalb einer Seite gesetzt; "
                               "für seitenübergreifende Signale einen Merker und ref_add verwenden.")
        if s is d:
            raise ProjectError("Ein Baustein kann nicht mit sich selbst verbunden werden.")
        if self._is_output_ref(s):
            raise ProjectError(f"'{pr.name(s)}' ist ein Ausgangs-Platzhalter und kann keine Quelle sein.")
        if self._is_input_ref(d):
            raise ProjectError(f"'{pr.name(d)}' ist ein Eingangs-Platzhalter und kann kein Ziel sein.")
        if self._is_input_ref(s):
            need = self._ref_output_key(s)
            if src_key and src_key.lower() != need.lower():
                raise ProjectError(f"Bei diesem Platzhalter ist nur der Ausgang {need} gültig, nicht {src_key}.")
            src_key = need
        elif not src_key:
            t = self.catalog.types.get(s.type, {})
            outs = [c["name"] for c in t.get("anschluesse", []) if c["richtung"] == "ausgang"]
            if len(outs) != 1:
                raise ProjectError(f"Bitte den Ausgang angeben. '{pr.name(s)}' hat: {', '.join(outs) or 'unbekannt'}")
            src_key = outs[0]
        if self._is_output_ref(d):
            dst_key = "AI"
        elif not dst_key:
            raise ProjectError("Bitte den Eingang am Ziel angeben (to_connector).")
        sc, dc = s.connector(src_key), d.connector(dst_key)
        if sc is None:
            raise ProjectError(f"'{pr.name(s)}' hat keinen Anschluss {src_key}. Vorhanden: {', '.join(c.key for c in s.connectors)}")
        if dc is None:
            raise ProjectError(f"'{pr.name(d)}' hat keinen Anschluss {dst_key}. Vorhanden: {', '.join(c.key for c in d.connectors)}")
        if not self._is_input_ref(s):
            direction, sure = self._direction(s, sc.key)
            if direction == "eingang" and sure:
                raise ProjectError(f"{sc.key} an '{pr.name(s)}' ist ein Eingang und kann keine Quelle sein.")
        if not self._is_output_ref(d):
            direction, sure = self._direction(d, dc.key)
            if direction == "ausgang" and sure:
                raise ProjectError(f"{dc.key} an '{pr.name(d)}' ist ein Ausgang und kann kein Ziel sein.")
        return s, sc, d, dc

    # ---------- Operationen ----------
    def connect(self, src: str, src_key: str | None, dst: str, dst_key: str | None, page: str | None) -> dict:
        s, sc, d, dc = self._endpoints(src, src_key, dst, dst_key, page)
        if any(u == sc.uuid for u, _ in dc.sources):
            raise ProjectError("Diese Verbindung besteht bereits.")
        text = f"{self.project.label(s, sc.key, s.page)} -> {self.project.label(d, dc.key, s.page)} (Seite {s.page.title})"
        self._apply(f"Verbunden: {text}", lambda: self.pt.add_in(dc.uuid, sc.uuid))
        out = {"verbunden": text}
        if dc.default not in (None, ""):
            out["hinweis"] = f"Der Eingang hatte den festen Wert {dc.default}; die Verbindung hat jetzt Vorrang."
        return out

    def disconnect(self, src: str, src_key: str | None, dst: str, dst_key: str | None, page: str | None) -> dict:
        s, sc, d, dc = self._endpoints(src, src_key, dst, dst_key, page)
        if not any(u == sc.uuid for u, _ in dc.sources):
            raise ProjectError("Diese Verbindung besteht nicht.")
        text = f"{self.project.label(s, sc.key, s.page)} -> {self.project.label(d, dc.key, s.page)} (Seite {s.page.title})"
        self._apply(f"Gelöst: {text}", lambda: self.pt.remove_in(dc.uuid, sc.uuid))
        return {"geloest": text}

    def set_params(self, block: str, params: dict, page: str | None) -> dict:
        pr = self.project
        b = pr.resolve(block, page)
        if pr.is_ref(b):
            raise ProjectError("Platzhalter haben keine eigenen Werte. Bitte den eigentlichen Baustein angeben.")
        if not params:
            raise ProjectError("Keine Werte angegeben.")
        steps, done = [], []
        for name, value in params.items():
            val = None if value is None or str(value) == "" else str(value)
            con = b.connector(name)
            if con is not None:
                direction, sure = self._direction(b, con.key)
                if direction == "ausgang" and sure:
                    raise ProjectError(f"{con.key} ist ein Ausgang und hat keinen einstellbaren Wert.")
                steps.append(lambda u=con.uuid, v=val: self.pt.set_co_default(u, v))
                done.append(f"{con.key}: {con.default} -> {val}")
            elif name.lower() in ("title", "titel"):
                if not val:
                    raise ProjectError("Der Titel darf nicht leer sein.")
                steps.append(lambda v=val: self.pt.set_block_attr(b.uuid, "Title", v))
                # Platzhalter auf Programmseiten mit gleichem Titel mitnehmen (wie Loxone Config)
                for r in pr.blocks.values():
                    if pr.target(r) is b and r.title == b.title:
                        steps.append(lambda u=r.uuid, v=val: self.pt.set_block_attr(u, "Title", v))
                done.append(f"Titel: {b.title} -> {val}")
            elif name in b.elem.attrib and name not in PROTECTED_ATTRS:
                steps.append(lambda n=name, v=val: self.pt.set_block_attr(b.uuid, n, v))
                done.append(f"{name}: {b.elem.get(name)} -> {val if val is not None else '(entfernt)'}")
            elif name in ("Desc", "Beschreibung") and val is not None:
                # Beschreibung: die App zeigt sie statt des Titels an
                steps.append(lambda v=val: self.pt.add_block_attr(b.uuid, "Desc", v))
                done.append(f"Beschreibung (Name in der App): {val}")
            else:
                ok = [c.key for c in b.connectors] + ["Titel"] + [a for a in b.elem.attrib if a not in PROTECTED_ATTRS and a != "Title"]
                raise ProjectError(f"'{name}' lässt sich an diesem Baustein nicht setzen. Möglich: {', '.join(ok)}")
        self._apply(f"Werte geändert an {pr.name(b)}: {'; '.join(done)}", lambda: [s() for s in steps])
        return {"baustein": pr.name(b), "geaendert": done}

    def set_inverted(self, block: str, connector: str, inverted: bool, page: str | None) -> dict:
        pr = self.project
        b = pr.resolve(block, page)
        if pr.is_ref(b):
            raise ProjectError("Platzhalter haben keine eigenen Eingänge. Bitte den eigentlichen Baustein angeben.")
        con = b.connector(connector)
        if con is None:
            raise ProjectError(f"'{pr.name(b)}' hat keinen Anschluss {connector}. Vorhanden: {', '.join(c.key for c in b.connectors)}")
        direction, sure = self._direction(b, con.key)
        if direction == "ausgang" and sure:
            raise ProjectError(f"{con.key} ist ein Ausgang und kann nicht negiert werden.")
        if con.inverted == inverted:
            raise ProjectError(f"{con.key} ist bereits {'negiert' if inverted else 'nicht negiert'}.")
        state = "negiert" if inverted else "nicht mehr negiert"
        self._apply(f"Eingang {con.key} an {pr.name(b)} {state}", lambda: self.pt.set_co_inverted(con.uuid, inverted))
        out = {"baustein": pr.name(b), "eingang": con.key, "negiert": inverted}
        if inverted and not con.sources:
            out["wirkung"] = "Der Eingang ist frei und negiert, also dauerhaft Ein."
        return out

    # ---------- Peripherie: virtuelle Eingänge ----------
    def add_virtual_input(self, title: str, analog: bool = True, min_val: float | None = None,
                          max_val: float | None = None, room: str | None = None, category: str | None = None,
                          visu: bool = False, unit: str | None = None, text: bool = False,
                          desc: str | None = None) -> dict:
        """Legt einen virtuellen Eingang unter 'Virtuelle Eingänge' an (z. B. für Werte, die Home
        Assistant per HTTP setzt: /dev/sps/io/<Titel>/<Wert>). text=True legt einen
        virtuellen Texteingang (VirtualTextIn, Ausgang TQ) an."""
        pr = self.project
        title = (title or "").strip()
        if not title or re.search(r"[/\\<>\"&]", title):
            raise ProjectError("Ungültiger Titel. Er darf nicht leer sein und keine / \\ < > \" & enthalten.")
        if any(b.title.lower() == title.lower() and b.type.startswith("Virtual") for b in pr.blocks.values()):
            raise ProjectError(f"Es gibt schon einen virtuellen Ein- oder Ausgang namens '{title}'.")
        caption = next((b for b in pr.blocks.values() if b.type == "VirtualInCaption"), None)
        if caption is None:
            raise ProjectError("Der Ordner 'Virtuelle Eingänge' wurde im Projekt nicht gefunden.")
        sample = next((b for b in caption.elem.findall("C") if b.get("Type") == "VirtualIn"), None)
        if sample is None:
            raise ProjectError("Kein vorhandener virtueller Eingang als Vorlage gefunden.")
        nums = [int(m) for m in re.findall(r'IName="VI(\d+)"', self.pt.text)]
        iname = f"VI{(max(nums) if nums else 0) + 1}"

        def find(kind, spec):
            if not spec:
                return None
            ids = [u for u, t in (pr.places if kind == "raum" else pr.categories).items() if t.lower() == spec.lower()]
            if len(ids) != 1:
                raise ProjectError(f"{kind.capitalize()} '{spec}' nicht eindeutig gefunden.")
            return ids[0]

        if text:
            # Aufbau wie von Loxone Config angelegt (gelernt aus Loxone-Beispielprojekten, V 179)
            tnums = [int(m) for m in re.findall(r'IName="VTI(\d+)"', self.pt.text)]
            iname = f"VTI{(max(tnums) if tnums else 0) + 1}"
            block_uuid, (tq,) = self.pt.new_block_uuids(1)
            attrs = {"Type": "VirtualTextIn", "IName": iname, "V": sample.get("V", "179"), "U": block_uuid,
                     "Title": title, "Cl": "238,238,238", "Nio": "1", "WF": "16400"}
        else:
            block_uuid, (q, qm) = self.pt.new_block_uuids(2)
            attrs = {"Type": "VirtualIn", "IName": iname, "V": sample.get("V", "179"), "U": block_uuid, "Title": title,
                     "Cl": sample.get("Cl", "141,255,112"), "Nio": "2", "WF": sample.get("WF", "16400"),
                     "Analog": "true" if analog else "false"}
            if analog and min_val is not None:
                attrs["MinVal"] = str(min_val)
            if analog and max_val is not None:
                attrs["MaxVal"] = str(max_val)
        if desc:
            attrs = {"Type": attrs.pop("Type"), "Desc": desc, **attrs}
        elem = ET.Element("C", attrs)
        if text:
            ET.SubElement(elem, "Co", {"K": "TQ", "U": tq})
        else:
            ET.SubElement(elem, "Co", {"K": "Q", "U": q})
            ET.SubElement(elem, "Co", {"K": "Qm", "U": qm})
        io = {}
        if visu:
            io["Visu"] = "true"
        cr, prm = find("kategorie", category), find("raum", room)
        if cr:
            io["Cr"] = cr
        if prm:
            io["Pr"] = prm
        if io:
            ET.SubElement(elem, "IoData", io)
        if text:
            ET.SubElement(elem, "Display", {"Unit": unit or "<v.1>"})
        elif unit or visu:
            # Type 1 + StateOnly = in der App nur als Wert anzeigen (sonst zeigt Loxone ein analoges
            # Eingangsobjekt als Taster oder gar nicht) – so legt es auch Loxone Config an.
            d = {"Type": "1"}
            if unit:
                d["Unit"] = unit
            d["StateOnly"] = "true"
            ET.SubElement(elem, "Display", d)

        def run():
            self.pt.insert_block(caption.uuid, elem)
            self.pt.bump("NextObj", 1)

        kind = "Text" if text else ("analog" if analog else "digital")
        self._apply(f"Neu: virtueller Eingang '{title}' ({iname}, {kind})", run)
        return {"angelegt": title, "kennung": "#" + self.project.short(self.project.blocks[block_uuid]),
                "uuid": block_uuid, "iname": iname,
                "setzen_per_http": f"/dev/sps/io/{title}/<Wert>"}

    def set_display(self, spec: str, visible: bool | None = None, unit: str | None = None) -> dict:
        """Macht einen Eingang in der App sichtbar/unsichtbar (IoData Visu) und setzt die
        Anzeigeeinheit (Display Unit, z. B. '<v.1>°' oder '<v>%')."""
        pr = self.project
        b = pr.logical(pr.resolve(spec, None))
        hide_only = visible is False and unit is None and b.elem.find("IoData") is not None
        if not hide_only and b.type not in ("VirtualIn", "VirtualTextIn", "VirtualHttpInCmd", "VirtualUdpInCmd", "Memory", "Constant", "StateV"):
            raise ProjectError(f"'{pr.name(b)}': Anzeige lässt sich nur für Eingänge, Merker, Konstanten und Statusanzeigen setzen.")
        if b.type == "StateV" and not hide_only and (b.elem.find("Display") is None or visible is not None):
            raise ProjectError(f"'{pr.name(b)}': bei Statusanzeigen lässt sich nur die Einheit ändern.")
        io = b.elem.find("IoData")
        disp = b.elem.find("Display")
        done = []
        is_text = b.type in ("VirtualTextIn", "StateV")  # nur Sichtbarkeit/Einheit, kein Display Type

        def run():
            if visible is not None:
                if io is None:
                    self.pt.insert_block(b.uuid, ET.Element("IoData", {"Visu": "true"} if visible else {}))
                else:
                    self.pt.set_child_attr(b.uuid, "IoData", "Visu", "true" if visible else None)
                done.append(f"in der App {'sichtbar' if visible else 'ausgeblendet'}")
            if is_text:
                if unit is not None:
                    self.pt.set_child_attr(b.uuid, "Display", "Unit", unit)
                    done.append(f"Einheit {unit}")
            elif visible or unit is not None:
                # In der App nur als Wert anzeigen (Display Type 1 + StateOnly), wie Loxone Config es anlegt
                if disp is None:
                    d = {"Type": "1"}
                    if unit is not None:
                        d["Unit"] = unit
                    d["StateOnly"] = "true"
                    self.pt.insert_block(b.uuid, ET.Element("Display", d))
                else:
                    if b.type in ("VirtualIn", "VirtualHttpInCmd", "VirtualUdpInCmd") and disp.get("Type") is None:
                        self.pt.set_child_attr(b.uuid, "Display", "Type", "1")
                        self.pt.set_child_attr(b.uuid, "Display", "StateOnly", "true")
                    if unit is not None:
                        self.pt.set_child_attr(b.uuid, "Display", "Unit", unit)
                if unit is not None:
                    done.append(f"Einheit {unit}")
                done.append("Anzeige als Wert")

        if visible is None and unit is None:
            raise ProjectError("Nichts zu ändern: visible oder unit angeben.")
        self._apply(f"Anzeige von {pr.name(b)}: " + ", ".join(
            [f"{'sichtbar' if visible else 'ausgeblendet'}"] * (visible is not None) + [f"Einheit {unit}"] * (unit is not None)), run)
        return {"baustein": pr.name(b), "geaendert": done}

    # ---------- Farben ----------
    # Blockfarben (Cl, "R,G,B") je Kategorie. Gelb/Hellblau/Orange/Rot entsprechen dem Schema,
    # das im Projekt schon von Hand verwendet wird.
    FUNCTION_COLORS = {
        "Beleuchtung": "255,230,71", "Lichtsensor": "255,230,71", "Bewegungssensor": "255,230,71",
        "Beschattung": "115,224,255",
        "Heizung": "255,125,48", "Klima": "255,125,48",
        "Alarm": "255,0,84",
        "Lüftung": "170,140,255",
        "Wetter": "0,210,190",
        "Zutritt": "255,150,220",
        "Bewässerung": "70,190,90",
    }
    DEFAULT_COLORS = {"141,255,112", "0,0,0"}   # Standard für Logik-Bausteine bzw. Platzhalter
    NEUTRAL_COLORS = DEFAULT_COLORS | {"238,238,238"}

    def color_by_function(self, page: str | None = None, max_depth: int = 6) -> dict:
        """Färbt Bausteine und Platzhalter auf Programmseiten nach ihrer Funktion (Kategorie). Von Hand
        gesetzte Farben bleiben. Logik ohne eigene Kategorie bekommt die Farbe dessen, was sie
        steuert (nächstes gefärbtes Ziel), sonst die ihrer Quellen."""
        pr = self.project
        if page:
            page_ids = {pr.find_page(page).uuid}
        else:
            page_ids = {b.page.uuid for b in pr.blocks.values() if b.page is not None}
        blocks = [b for b in pr.blocks.values() if b.page is not None and b.page.uuid in page_ids
                  and b.uuid != b.page.uuid and b.elem.get("Cl") is not None]

        def own_cat(b: Block) -> str | None:
            for cand in (b, pr.target(b)):
                if cand is None:
                    continue
                io = cand.elem.find("IoData")
                name = pr.categories.get(io.get("Cr") if io is not None else "")
                if name and name in self.FUNCTION_COLORS:
                    return name
            return None

        seed: dict[str, str] = {}
        for b in blocks:
            cl = b.elem.get("Cl")
            if cl not in self.NEUTRAL_COLORS:
                seed[b.uuid] = cl           # Farbe von Hand gesetzt
            elif pr.logical(b).type in ("Mode", "CalendarEntry"):
                continue                    # Betriebsmodi gehören zu keiner Funktion
            else:
                c = own_cat(b)
                if c:
                    seed[b.uuid] = self.FUNCTION_COLORS[c]

        def search(start: Block, edges, nxt) -> str | None:
            frontier, seen = [start], {start.uuid}
            for _ in range(max_depth):
                votes = Counter()
                new = []
                for b in frontier:
                    for e in edges.get(b.uuid, []):
                        n = nxt(e)
                        if n.uuid in seen or n.page is None or n.page.uuid != start.page.uuid:
                            continue
                        seen.add(n.uuid)
                        if n.uuid in seed:
                            votes[seed[n.uuid]] += 1
                        else:
                            new.append(n)
                if votes:
                    return votes.most_common(1)[0][0]
                frontier = new
                if not frontier:
                    break
            return None

        alarm = self.FUNCTION_COLORS["Alarm"]
        message_types = {"VirtualOutCmd", "Notification", "OutputRefLM"}
        changes: dict[str, str] = {}
        for b in blocks:
            if b.elem.get("Cl") not in self.DEFAULT_COLORS:
                continue
            fwd = search(b, pr.out_edges, lambda e: e.dst)
            back = search(b, pr.in_edges, lambda e: e.src)
            if (pr.logical(b).type in message_types) or b.type in message_types:
                col = back or seed.get(b.uuid) or fwd       # Meldungen: Farbe des Auslösers
            elif back == alarm and b.type in ("State", "StateV"):
                col = alarm                                 # Anzeige von Brand-/Wassermeldern
            elif b.uuid in seed:
                col = seed[b.uuid]
            elif alarm in (fwd, back):
                col = alarm                                 # Sicherheit immer sichtbar rot
            else:
                col = fwd or back
            if col and col != b.elem.get("Cl"):
                changes[b.uuid] = col
        if not changes:
            return {"gefaerbt": 0, "hinweis": "Nichts zu färben."}
        names = {}
        for k, v in self.FUNCTION_COLORS.items():
            names.setdefault(v, k)
        stat = Counter(names.get(c, f"Farbe {c}") for c in changes.values())
        self._apply(f"Bausteine nach Funktion gefärbt: {len(changes)} ("
                    + ", ".join(f"{k} {v}" for k, v in stat.most_common()) + ")",
                    lambda: [self.pt.set_block_attr(u, "Cl", c) for u, c in changes.items()])
        return {"gefaerbt": len(changes), "je_funktion": dict(stat), "seiten": len(page_ids),
                "farben": dict(self.FUNCTION_COLORS)}

    def set_heating_schedule(self, block: str, comfort: dict, page: str | None = None) -> dict:
        """Heizplan eines Raumreglers (Intelligenter Raumregler V2) setzen. comfort: {Tag: [(von, bis), ...]}
        mit Tag 1 = Montag … 7 = Sonntag und Zeiten als 'HH:MM' oder Minuten; alles andere ist Eco."""
        pr = self.project
        b = pr.logical(pr.resolve(block, page))
        if b.type != "HeatIRoomController2" or b.elem.find("Timer") is None:
            raise ProjectError(f"'{pr.name(b)}' ist kein Raumregler mit Heizplan.")

        def mins(x):
            if isinstance(x, int):
                return x
            h, m = str(x).split(":")
            return int(h) * 60 + int(m)
        entries = [(None, 1440, None, 2)]                       # Standard: Eco
        for day in sorted(int(d) for d in comfort):
            if not 1 <= day <= 7:
                raise ProjectError("Tage 1 (Mo) bis 7 (So).")
            for frm, to in comfort[day] if day in comfort else comfort[str(day)]:
                f, t_ = mins(frm), mins(to)
                if not 0 <= f < t_ <= 1440:
                    raise ProjectError(f"Ungültiges Zeitfenster {frm}–{to}.")
                entries.append((f or None, t_, day, 1))
        old = b.elem.find("Timer")
        n = len(old.findall("Entry"))
        self._apply(f"Heizplan {pr.name(b)}: {len(entries)} Einträge (vorher {n})",
                    lambda: self.pt.set_timer_entries(b.uuid, entries))
        return {"baustein": pr.name(b), "eintraege": len(entries)}

    def move_blocks_to_page(self, blocks: list[str], page: str, from_page: str | None = None) -> dict:
        """Verschiebt Bausteine (und Platzhalter) auf eine andere Programmseite. Erlaubt nur, wenn sie
        keine Verbindung zu Bausteinen behalten, die auf der alten Seite bleiben (ganze Gruppen)."""
        pr = self.project
        target = pr.find_page(page)
        items = [pr.resolve(b, from_page) for b in blocks]
        if any(b.page is None or b.parent is not b.page for b in items):
            raise ProjectError("Nur Bausteine, die direkt auf einer Programmseite liegen, lassen sich verschieben.")
        moving = {b.uuid for b in items}
        for e in pr.edges:
            if (e.src.uuid in moving) != (e.dst.uuid in moving) and e.src.page is not None and e.src.page is e.dst.page:
                raise ProjectError(f"'{pr.name(e.src)}' und '{pr.name(e.dst)}' sind verbunden, aber nur einer soll die Seite wechseln. Nur ganze Gruppen verschieben.")
        names = [pr.name(b) for b in items]
        origin = sorted({b.page.title for b in items})
        self._apply(f"Verschoben auf Seite {page}: {len(items)} Bausteine (von {', '.join(origin)})",
                    lambda: [self.pt.move_block(b.uuid, target.uuid) for b in items])
        return {"verschoben": len(items), "nach": page, "von": origin, "bausteine": names[:12]}

    def set_schedule(self, block: str, days: dict, page: str | None = None) -> dict:
        """Zeiten einer Schaltuhr (DayTimer) je Wochentag setzen. days: {Tag: [(von, bis), ...]} mit
        Tag 1 = Montag … 7 = Sonntag und Zeiten 'HH:MM' oder Minuten; eine leere Liste löscht den Tag.
        Nur die genannten Tage werden ersetzt. Eintrag Ix = 2 + Tag (3–9 = Mo–So); Einträge ohne Ix und
        Ix 1/2 (Sondertage) bleiben unverändert. Der Ausgang ist während der Zeit an (V = 1)."""
        pr = self.project
        b = pr.logical(pr.resolve(block, page))
        if b.type != "DayTimer":
            raise ProjectError(f"'{pr.name(b)}' ist keine Schaltuhr (DayTimer).")

        def mins(x):
            if isinstance(x, int):
                return x
            h, m = str(x).split(":")
            return int(h) * 60 + int(m)
        new: dict[int, list] = {}
        for d, spans in days.items():
            d = int(d)
            if not 1 <= d <= 7:
                raise ProjectError("Tage 1 (Mo) bis 7 (So).")
            rows = []
            for frm, to in spans:
                f, t_ = mins(frm), mins(to)
                if not 0 <= f < t_ <= 1440:
                    raise ProjectError(f"Ungültiges Zeitfenster {frm}–{to}.")
                rows.append((f or None, t_, 2 + d, 1))
            new[2 + d] = rows
        keep, old_n = [], 0
        for e in b.elem.findall("Entry"):
            old_n += 1
            ix = int(e.get("Ix")) if e.get("Ix") is not None else None
            if ix in new:
                continue
            keep.append((int(e.get("From") or 0) or None, int(e.get("To")), ix, int(e.get("V", "1"))))
        entries = keep + [r for rows in new.values() for r in rows]
        entries.sort(key=lambda r: (-1 if r[2] is None else r[2], r[0] or 0))
        self._apply(f"Schaltuhr {pr.name(b)}: {len(entries)} Einträge (vorher {old_n})",
                    lambda: self.pt.set_daytimer_entries(b.uuid, entries))
        return {"baustein": pr.name(b), "eintraege": len(entries), "vorher": old_n}

    def set_room_category(self, block: str, room: str | None = None, category: str | None = None,
                          page: str | None = None) -> dict:
        """Raum und/oder Kategorie eines einzelnen Objekts setzen (wie in Config unter Eigenschaften)."""
        pr = self.project
        b = pr.logical(pr.resolve(block, page))
        if b.elem.find("IoData") is None:
            raise ProjectError(f"'{pr.name(b)}' hat keine Raum-/Kategorie-Angabe (kein IoData).")

        def lookup(table: dict, name: str, what: str) -> str:
            hits = [u for u, n in table.items() if n.lower() == name.lower()]
            if len(hits) != 1:
                raise ProjectError(f"{what} '{name}' nicht gefunden. Vorhanden: {', '.join(sorted(set(table.values())))}")
            return hits[0]
        ops, done = [], []
        if category:
            ops.append(("Cr", lookup(pr.categories, category, "Kategorie")))
            done.append(f"Kategorie {pr.category(b)} -> {category}")
        if room:
            ops.append(("Pr", lookup(pr.places, room, "Raum")))
            done.append(f"Raum {pr.room(b)} -> {room}")
        if not ops:
            raise ProjectError("room oder category angeben.")
        self._apply(f"{pr.name(b)}: " + ", ".join(done),
                    lambda: [self.pt.set_child_attr(b.uuid, "IoData", k, v) for k, v in ops])
        return {"baustein": pr.name(b), "geaendert": done}

    def set_app(self, block: str, favorite: bool | None = None, rating: int | None = None,
                icon: str | None = None, page: str | None = None) -> dict:
        """App-Eigenschaften eines Objekts: Favorit, Reihenfolge (Rating, höher = weiter oben) und Symbol."""
        import icons
        pr = self.project
        b = pr.logical(pr.resolve(block, page))
        if b.elem.find("IoData") is None:
            raise ProjectError(f"'{pr.name(b)}' erscheint nicht in der App (kein IoData).")
        ops, done = [], []
        if favorite is not None:
            ops.append(("UseInFav", "true" if favorite else None))
            done.append("Favorit" if favorite else "kein Favorit")
        if rating is not None:
            if not 0 <= int(rating) <= 10:
                raise ProjectError("Reihenfolge (Rating) 0–10.")
            ops.append(("Rating", str(int(rating)) if int(rating) else None))
            done.append(f"Reihenfolge {int(rating)}")
        if icon:
            ops.append(("IconV", icons.resolve(icon)))
            done.append(f"Symbol {icon}")
        if not ops:
            raise ProjectError("favorite, rating oder icon angeben.")
        self._apply(f"App {pr.name(b)}: " + ", ".join(done),
                    lambda: [self.pt.set_child_attr(b.uuid, "IoData", k, v) for k, v in ops])
        return {"baustein": pr.name(b), "geaendert": done}

    def set_structure_favorite(self, kind: str, name: str, on: bool = True) -> dict:
        """Raum ('raum') oder Kategorie ('kategorie') als Favorit in der App markieren oder entfernen."""
        pr = self.project
        table = pr.places if kind == "raum" else pr.categories if kind == "kategorie" else None
        if table is None:
            raise ProjectError("kind muss 'raum' oder 'kategorie' sein.")
        hits = [u for u, n in table.items() if n.lower() == name.lower()]
        if len(hits) != 1:
            raise ProjectError(f"{kind} '{name}' nicht gefunden.")
        el = next(e for e in pr.root.iter("C") if e.get("U") == hits[0])
        has = el.get("UseFav") is not None

        def run():
            if on and has:
                self.pt.set_block_attr(hits[0], "UseFav", "true")
            elif on:
                self.pt.add_block_attr(hits[0], "UseFav", "true")
            elif has:
                self.pt.set_block_attr(hits[0], "UseFav", None)
        self._apply(f"{kind.capitalize()} {name}: {'Favorit' if on else 'kein Favorit'}", run)
        return {kind: name, "favorit": on}

    def set_state_style(self, block: str, styles: list[dict], base_icon: str | None = None,
                        page: str | None = None) -> dict:
        """Farbe und Symbol einzelner Zustände eines vorhandenen Status-Bausteins (State) setzen.
        styles: [{"text": "offen", "farbe": "#ff0054", "symbol": "window-open"}, ...]; statt 'text'
        geht 'nr' (1 = erster Zustand; der letzte ist der Standard-Zustand)."""
        import icons
        pr = self.project
        b = pr.logical(pr.resolve(block, page))
        if b.type != "State" or b.elem.find("StateTexts") is None:
            raise ProjectError(f"'{pr.name(b)}' ist kein Status-Baustein mit Zuständen.")
        rows = b.elem.find("StateTexts").findall("StateText")
        ops, done = [], []
        for s in styles:
            if "nr" in s:
                i = int(s["nr"]) - 1
                if not 0 <= i < len(rows):
                    raise ProjectError(f"Zustand Nr. {s['nr']} gibt es nicht (1–{len(rows)}).")
            else:
                hits = [i for i, r in enumerate(rows) if (r.get("Text") or "") == str(s.get("text", ""))]
                if len(hits) != 1:
                    raise ProjectError(f"Zustand '{s.get('text')}' nicht eindeutig gefunden. Vorhanden: "
                                       + ", ".join(repr(r.get("Text")) for r in rows))
                i = hits[0]
            if s.get("farbe"):
                ops.append((i, "IcC", str(icons.color(s["farbe"]))))
            if s.get("symbol"):
                ops.append((i, "Icon", icons.resolve(s["symbol"])))
            done.append(f"{rows[i].get('Text') or '(Standard)'}: "
                        + ", ".join(x for x in (s.get("farbe"), s.get("symbol")) if x))
        icon_v = icons.resolve(base_icon) if base_icon else None
        if not ops and not icon_v:
            raise ProjectError("Nichts zu ändern: farbe oder symbol angeben.")

        def run():
            for i, k, v in ops:
                self.pt.set_child_attr(b.uuid, "StateText", k, v, i)
            if icon_v:
                self.pt.set_child_attr(b.uuid, "IoData", "IconV", icon_v)
        self._apply(f"Status-Stil {pr.name(b)}: " + "; ".join(done), run)
        return {"baustein": pr.name(b), "geaendert": done}

    # ---------- Statistik ----------
    # StatsType wie in Loxone Config: 1 = bei jeder Änderung, 6/7/9/11 = Mittelwert-/Intervallarten
    STATS_TYPES = {"aus": None, "jede_aenderung": "1"}

    def set_statistics(self, spec: str, mode: str = "jede_aenderung", accumulate: bool = True) -> dict:
        """Schaltet die Statistik eines Eingangs ein oder aus (Attribute StatsType/StatsAcc, so wie
        Loxone Config sie z. B. für den früheren weatherapi-Niederschlag gesetzt hatte)."""
        pr = self.project
        b = pr.logical(pr.resolve(spec, None))
        if b.type not in ("VirtualIn", "VirtualHttpInCmd", "VirtualUdpInCmd", "Memory"):
            raise ProjectError(f"'{pr.name(b)}': Statistik nur für analoge Eingänge und Merker.")
        if mode not in self.STATS_TYPES:
            raise ProjectError(f"Unbekannte Art '{mode}'. Möglich: {', '.join(self.STATS_TYPES)}")
        st = self.STATS_TYPES[mode]

        def run():
            for name, val in (("StatsType", st), ("StatsAcc", "true" if st and accumulate else None)):
                if name in b.elem.attrib:
                    self.pt.set_block_attr(b.uuid, name, val)
                elif val is not None:
                    self.pt.add_block_attr(b.uuid, name, val)

        self._apply(f"Statistik an {pr.name(b)}: {mode}", run)
        return {"baustein": pr.name(b), "statistik": mode, "aufsummiert": bool(st and accumulate)}

    # ---------- Position ----------
    def set_position(self, block: str, x: int, y: int, page: str | None = None) -> dict:
        """Verschiebt einen Baustein oder Platzhalter auf einer Seite (Raster 96, innerhalb des Blattes)."""
        pr = self.project
        b = pr.resolve(block, page)
        if b.page is None:
            raise ProjectError(f"'{pr.name(b)}' liegt auf keiner Programmseite.")
        w, h = _px(b, "Px2") - _px(b, "Px"), _px(b, "Py2") - _px(b, "Py")
        if w <= 0 or h <= 0:   # nie platziert (Config lässt 0-Werte weg): Standardgröße eines Platzhalters
            w, h = 2112, 192
        x, y = _snap(x), _snap(y)
        if x < 0 or y < 0 or x + w > self.SHEET_W or y + h > self.SHEET_H:
            raise ProjectError(f"Position liegt außerhalb des Blattes (max. {self.SHEET_W - w}, {self.SHEET_H - h}).")

        def run():
            self.pt.set_geometry(b.uuid, x, y, x + w, y + h)
        self._apply(f"Verschoben: {pr.name(b)} -> {x},{y}", run)
        return {"baustein": pr.name(b), "seite": b.page.title, "position": {"x": x, "y": y}}

    # ---------- Baustein klonen (auch große: Lichtsteuerung, Raumregler, Rollladen …) ----------
    CLONE_SKIP = {"Page", "Document", "InputRef", "OutputRef", "OutputRefLM"}

    def clone_block(self, block: str, page: str, title: str, page_of_source: str | None = None) -> dict:
        """Legt eine Kopie eines vorhandenen Bausteins (mit allen Einstellungen und Unterobjekten wie
        Lichtkreisen, Stimmungen, Zeitplänen) auf einer Seite an – ohne seine Verbindungen. Alle
        internen Kennungen werden neu vergeben; Verweise auf Räume, Kategorien, Symbole und Rechte
        bleiben. So lassen sich auch große Bausteine anlegen, die block_add nicht kann."""
        import copy
        pr = self.project
        src = pr.resolve(block, page_of_source)
        if pr.is_ref(src) or src.type in self.CLONE_SKIP or src.page is None:
            raise ProjectError(f"'{pr.name(src)}' ist kein Logikbaustein auf einer Seite.")
        if not title or not title.strip() or re.search(r"[<>%]", title):
            raise ProjectError("Titel fehlt oder enthält < > %.")
        pg = pr.find_page(page)
        el = copy.deepcopy(src.elem)
        el.tail = None
        # alle Kennungen innerhalb des Bausteins einsammeln und neu vergeben
        old = [e.get("U") for e in el.iter() if e.get("U")]
        # interne Zustandskennungen (z. B. SpStates am Rollladen) brauchen ebenfalls neue Werte
        extra = [t for e in el.iter() for t in (e.get("SpStates") or "").split(",") if t and t not in old]
        new_main, rest = self.pt.new_block_uuids(len(old) - 1 + len(extra))
        remap = dict(zip(old + extra, [new_main] + list(rest)))
        for e in el.iter():
            for k, v in list(e.attrib.items()):
                if k == "U":
                    e.set(k, remap[v])
                elif any(o in v for o in remap):
                    parts = re.split(r"([,;\s])", v)
                    e.set(k, "".join(remap.get(p_, p_) for p_ in parts))
            # Verbindungen nach außen entfernen, interne behalten
            for inn in [i for i in e if i.tag == "In"]:
                if inn.get("Input") not in remap.values():
                    e.remove(inn)
            if e.tag == "Co":
                n = len([i for i in e if i.tag == "In"])
                if n:
                    e.set("Nc", str(n))
                else:
                    e.attrib.pop("Nc", None)
        el.attrib.pop("LtE", None)
        el.set("Title", title.strip())
        w, h = _px(src, "Px2") - _px(src, "Px"), _px(src, "Py2") - _px(src, "Py")
        x, y = self._free_spot(pg, "logic", w, h)
        for k, v in (("Px", x), ("Py", y), ("Px2", x + w), ("Py2", y + h)):
            el.set(k, str(v))
        n_c = sum(1 for e in el.iter() if e.tag == "C")

        def run():
            self.pt.insert_block(pg.uuid, el)
            self.pt.bump("NextObj", n_c)
        self._apply(f"Neu: Kopie von {pr.name(src)} als '{title}' auf Seite {pg.title}", run)
        b = self.project.blocks[new_main]
        return {"angelegt": self.project.name(b), "seite": pg.title, "position": {"x": x, "y": y},
                "unterobjekte": n_c - 1, "anschluesse": [c.key for c in b.connectors][:40],
                "hinweis": "Ohne Verbindungen angelegt. Einstellungen (Stimmungen, Zeiten …) stammen vom Original; "
                           "bitte in Config prüfen und anpassen."}

    # ---------- neue Programmseite ----------
    def add_page(self, title: str, room: str | None = None, category: str | None = None) -> dict:
        """Legt eine leere Programmseite an (hinter der letzten vorhandenen Seite), optional mit
        Raum und Kategorie – Aufbau wie von Loxone Config: <C Type="Page"><IoData Cr Pr/></C>."""
        pr = self.project
        title = (title or "").strip()
        if not title or re.search(r"[<>%]", title):
            raise ProjectError("Titel fehlt oder enthält < > %.")
        if any(p.title.lower() == title.lower() for p in pr.pages):
            raise ProjectError(f"Eine Seite '{title}' gibt es schon.")
        if not pr.pages:
            raise ProjectError("Keine vorhandene Seite als Vorlage gefunden.")
        last = pr.pages[-1]

        def pick(kind, spec):
            if not spec:
                return None
            ids = [u for u, t in (pr.places if kind == "raum" else pr.categories).items() if t.lower() == spec.lower()]
            if len(ids) != 1:
                raise ProjectError(f"{kind.capitalize()} '{spec}' nicht eindeutig gefunden.")
            return ids[0]
        io = {k: v for k, v in (("Cr", pick("kategorie", category)), ("Pr", pick("raum", room))) if v}
        puuid, _ = self.pt.new_block_uuids(0)
        el = ET.Element("C", {"Type": "Page", "V": last.elem.get("V", "179"), "U": puuid, "Title": title,
                              "WF": last.elem.get("WF", "16384")})
        ET.SubElement(el, "IoData", io)

        def run():
            self.pt.insert_block(last.parent.uuid, el)
            self.pt.bump("NextObj", 1)
        self._apply(f"Neue Seite '{title}'", run)
        return {"angelegt": title, "kennung": "#" + self.project.short(self.project.blocks[puuid]),
                "raum": room, "kategorie": category}

    # ---------- Loxone-Mitteilung (Push der Loxone-App) ----------
    def add_notification(self, title: str, text: str, source: str, page: str, source_key: str | None = None,
                         min_time_ms: int = 5000) -> dict:
        """Legt eine Mitteilung (Loxone-App-Push/Systemmeldung) an und schließt 'source' auf 'page'
        über einen Mitteilungs-Platzhalter (OutputRefLM) an. text = Meldungstext bei steigender Flanke."""
        import copy
        pr = self.project
        if not title or re.search(r"[<>%]", title):
            raise ProjectError("Titel fehlt oder enthält < > %.")
        ex = next((b for b in pr.blocks.values() if b.type == "Notification" and b.parent is not None
                   and b.parent.type == "LoggerOutCaption" and not b.elem.get("Sys")), None)
        exr = next((b for b in pr.blocks.values() if b.type == "OutputRefLM" and b.elem.find("LoggerMailer") is not None), None)
        if ex is None or exr is None:
            raise ProjectError("Keine vorhandene Mitteilung mit Platzhalter als Vorlage im Projekt.")
        if any(b.type == "Notification" and b.title.lower() == title.lower() for b in pr.blocks.values()):
            raise ProjectError(f"Eine Mitteilung '{title}' gibt es schon.")
        pg = pr.find_page(page)

        def run():
            note = self._clone_periphery(ex, {"Title": title}, "Mitteilung")
            el = copy.deepcopy(exr.elem)
            el.tail = None
            cos = el.findall("Co")
            ruuid, cus = self.pt.new_block_uuids(len(cos))
            w, h = _px(exr, "Px2") - _px(exr, "Px"), _px(exr, "Py2") - _px(exr, "Py")
            x, y = self._free_spot(pg, "out", w, h)
            for k in ("LtE", "Desc"):
                el.attrib.pop(k, None)
            el.attrib.update({"U": ruuid, "Title": title, "Ref": note.uuid, "Px": str(x), "Py": str(y),
                              "Px2": str(x + w), "Py2": str(y + h)})
            for co, cu in zip(cos, cus):
                for i in list(co):
                    co.remove(i)
                co.attrib.pop("Nc", None)
                co.set("U", cu)
            lm = el.find("LoggerMailer")
            lm.attrib.clear()
            lm.attrib.update({"RefNotification": note.uuid, "On": text, "MinimumTime": str(int(min_time_ms)), "NL": "1"})

            def ins():
                self.pt.insert_block(pg.uuid, el)
                self.pt.bump("NextObj", 1)
            self._apply(f"Neu: Mitteilungs-Platzhalter '{title}' auf Seite {pg.title}", ins)
            self.connect(source, source_key, "#" + self.project.short(self.project.blocks[ruuid]), None, pg.title)
            return {"angelegt": self.project.name(note), "seite": pg.title, "text": text}
        return self._atomic(run)

    # ---------- mehrere Schritte als eine Einheit ----------
    def _atomic(self, fn):
        # Stand komplett merken: der Rückgängig-Speicher ist begrenzt (MAX_UNDO), lange Baupläne
        # ließen sich sonst nicht vollständig zurückrollen und die eigentliche Fehlermeldung ginge verloren.
        text, log, stack = self.pt.text, list(self.log), list(self.undo_stack)
        try:
            return fn()
        except Exception:
            self.pt.text = text
            self.project = Project(self.source, self.pt.data())
            self.log[:] = log
            self.undo_stack[:] = stack
            raise

    def _clone_periphery(self, example: Block, attrs: dict, label: str) -> Block:
        """Peripherie-Objekt nach Vorbild im selben Ordner anlegen: neue UUIDs, ohne Verbindungen."""
        import copy
        el = copy.deepcopy(example.elem)
        cos = el.findall("Co")
        buuid, cus = self.pt.new_block_uuids(len(cos))
        el.set("U", buuid)
        el.attrib.pop("LtE", None)
        for k, v in attrs.items():
            if v is None:
                el.attrib.pop(k, None)
            else:
                el.set(k, str(v))
        for co, cu in zip(cos, cus):
            for i in list(co):
                co.remove(i)
            co.attrib.pop("Nc", None)
            co.set("U", cu)
        el.tail = None

        def run():
            self.pt.insert_block(example.parent.uuid, el)
            self.pt.bump("NextObj", 1)
        self._apply(f"Neu: {label} '{attrs.get('Title')}'", run)
        return self.project.blocks[buuid]

    def _next_iname(self, prefix: str, siblings: list[Block]) -> str:
        nums = [int(m.group(1)) for b in siblings for m in [re.fullmatch(re.escape(prefix) + r"(\d+)", b.elem.get("IName") or "")] if m]
        return f"{prefix}{(max(nums) if nums else 0) + 1}"

    def _signal_on_page(self, target: Block, page: Block) -> str:
        """Kennung, unter der target auf page als Quelle angeschlossen werden kann (legt Platzhalter an)."""
        pr = self.project
        if target.page is not None and target.page.uuid == page.uuid:
            return "#" + pr.short(target)
        for r in pr.blocks.values():
            if r.page and r.page.uuid == page.uuid and r.type == "InputRef" and pr.target(r).uuid == target.uuid:
                return "#" + pr.short(r)
        before = set(pr.blocks)
        self.add_ref(page.title, "#" + pr.short(target), "input", None, None)
        new = [b for u, b in self.project.blocks.items() if u not in before and b.type == "InputRef"]
        return "#" + self.project.short(new[0])

    # ---------- Status mit Symbol und Farbe ----------
    def add_state(self, page: str, title: str, source: str, states: list[dict], compare: str = "gleich",
                  default_text: str = "", default_icon: str | None = None, default_color: str | None = None,
                  base_icon: str | None = None, room: str | None = None, category: str | None = None,
                  visu: bool = True, source_key: str | None = None) -> dict:
        """Status-Baustein (Typ State) mit eigenem Text, Symbol und Farbe je Zustand.
        states: [{"wert": 1, "text": "Sonnig", "symbol": "sun", "farbe": "#ffc800"}, ...]
        compare 'gleich' = I1 gleich Wert; 'ab' = I1 >= Wert (Zustände von oben nach unten geprüft,
        der erste Treffer gilt – also größte Schwelle zuerst). Im Text zeigt <v1> bzw. <v1.1> den Wert."""
        import icons
        from xml.sax.saxutils import quoteattr  # noqa: F401  (ET maskiert selbst)
        pr = self.project
        pg = pr.find_page(page)
        if not title or not title.strip() or re.search(r"[<>%]", title):
            raise ProjectError("Titel fehlt oder enthält < > % (Config ersetzt diese Zeichen).")
        if not states:
            raise ProjectError("Mindestens einen Zustand angeben.")
        if compare not in ("gleich", "ab"):
            raise ProjectError("compare muss 'gleich' oder 'ab' sein.")
        ex = next((b for b in pr.blocks.values() if b.type == "State" and not pr.is_ref(b) and b.elem.find("StateTexts") is not None), None)
        if ex is None:
            raise ProjectError("Kein vorhandener Status-Baustein als Vorlage im Projekt.")
        src = pr.logical(pr.resolve(source, None))
        rows = []
        for s in states:
            if "wert" not in s or "text" not in s:
                raise ProjectError("Jeder Zustand braucht 'wert' und 'text'.")
            v = s["wert"]
            a = {"Valid": "true", "ValidV": "true", "Text": str(s["text"]), "TextV": "0",
                 "Icon": icons.resolve(s.get("symbol")), "IcC": str(icons.color(s.get("farbe"))),
                 "Input0": "1", "CondV0": str(v), "CondT0": str(v)}
            if compare == "ab":
                a = {**{k: a[k] for k in ("Valid", "ValidV", "Text", "TextV", "Icon", "IcC", "Input0")},
                     "Cond0": "2", "CondV0": str(v), "CondT0": str(v)}  # 2 = ">=" (1 wäre ">")
            rows.append(a)
        rows.append({"Valid": "true", "ValidV": "true", "Text": default_text, "TextV": "0",
                     "Icon": icons.resolve(default_icon), "IcC": str(icons.color(default_color, "#aaaaaa"))})
        keys = [c.get("K") for c in ex.elem.findall("Co")]
        w, h = _px(ex, "Px2") - _px(ex, "Px"), _px(ex, "Py2") - _px(ex, "Py")
        x, y = self._free_spot(pg, "logic", w, h)
        buuid, cus = self.pt.new_block_uuids(len(keys))
        elem = ET.Element("C", {"Type": "State", "V": ex.elem.get("V", "179"), "U": buuid, "Title": title.strip(),
                                "Px": str(x), "Py": str(y), "Px2": str(x + w), "Py2": str(y + h),
                                "Cl": ex.elem.get("Cl", "141,255,112"), "Nio": ex.elem.get("Nio", "11")})
        for k, cu in zip(keys, cus):
            ET.SubElement(elem, "Co", {"K": k, "U": cu})

        def pick(kind, spec, fallback):
            if spec:
                ids = [u for u, t in (pr.places if kind == "raum" else pr.categories).items() if t.lower() == spec.lower()]
                if len(ids) != 1:
                    raise ProjectError(f"{kind.capitalize()} '{spec}' nicht eindeutig gefunden.")
                return ids[0]
            return fallback
        sio = src.elem.find("IoData")
        io = {}
        if visu:
            io["Visu"] = "true"
        cr = pick("kategorie", category, sio.get("Cr") if sio is not None else pr._iodata(pg, "Cr"))
        prm = pick("raum", room, sio.get("Pr") if sio is not None else pr._iodata(pg, "Pr"))
        if cr:
            io["Cr"] = cr
        if prm:
            io["Pr"] = prm
        if base_icon or states:
            io["IconV"] = icons.resolve(base_icon or states[0].get("symbol"))
        ET.SubElement(elem, "IoData", io)
        ET.SubElement(elem, "Display", {"Unit": "<v.1>"})
        st = ET.SubElement(elem, "StateTexts", {"Num": str(len(rows))})
        for a in rows:
            ET.SubElement(st, "StateText", a)

        def run():
            def add():
                self.pt.insert_block(pg.uuid, elem)
                self.pt.bump("NextObj", 1)
            self._apply(f"Neu: Status '{title}' auf Seite {pg.title} ({len(states)} Zustände, {compare})", add)
            s = self._signal_on_page(src, pg)
            # Logikbausteine mit mehreren Ausgängen (z. B. Formel: AQ/TQ): analogen Ausgang nehmen
            on_page = src.page is not None and src.page.uuid == pg.uuid
            key = source_key or ("AQ" if on_page and src.connector("AQ") is not None else None)
            self.connect(s, key, "#" + self.project.short(self.project.blocks[buuid]), "I1", pg.title)
        self._atomic(run)
        return {"angelegt": self.project.name(self.project.blocks[buuid]), "seite": pg.title,
                "position": {"x": x, "y": y}, "zustaende": len(states), "eingang": pr.name(src)}

    # ---------- Ausgangsbefehl (z. B. Push über Home-Assistant-Webhook) ----------
    def add_command(self, title: str, cmd_on: str, cmd_off: str | None = None, parent: str = "Home Assistant",
                    source: str | None = None, page: str | None = None, source_key: str | None = None) -> dict:
        """Virtueller Ausgangsbefehl unter einem vorhandenen virtuellen Ausgang (z. B. 'Home Assistant'),
        nach dem Muster eines vorhandenen Befehls dort. Optional gleich auf einer Seite an 'source' anschließen."""
        pr = self.project
        outs = [b for b in pr.blocks.values() if b.type == "VirtualOut" and b.title.lower() == parent.lower()]
        if len(outs) != 1:
            raise ProjectError(f"Virtueller Ausgang '{parent}' nicht eindeutig gefunden ({len(outs)}).")
        sib = [b for b in pr.blocks.values() if b.type == "VirtualOutCmd" and b.parent and b.parent.uuid == outs[0].uuid]
        ex = sib[0] if sib else next((b for b in pr.blocks.values() if b.type == "VirtualOutCmd"), None)
        if ex is None:
            raise ProjectError("Kein vorhandener Ausgangsbefehl als Vorlage im Projekt.")
        if any(b.title.lower() == title.lower() for b in sib):
            raise ProjectError(f"Unter '{parent}' gibt es schon einen Befehl '{title}'.")
        allc = [b for b in pr.blocks.values() if b.type == "VirtualOutCmd"]

        def run():
            import copy
            if ex.parent.uuid != outs[0].uuid:
                raise ProjectError(f"Unter '{parent}' gibt es noch keinen Befehl als Vorlage.")
            cmd = self._clone_periphery(ex, {"Title": title, "IName": self._next_iname("VQC", allc),
                                             "CmdOn": cmd_on, "CmdOff": cmd_off}, "Ausgangsbefehl")
            res = {"angelegt": self.project.name(cmd), "unter": parent}
            if source and page:
                pg = self.project.find_page(page)
                before = set(self.project.blocks)
                self.add_ref(pg.title, "#" + self.project.short(cmd), "output", None, None)
                ref = next(b for u, b in self.project.blocks.items() if u not in before)
                self.connect(source, source_key, "#" + self.project.short(ref), None, pg.title)
                res["angeschlossen"] = f"{source} -> {cmd.title} (Seite {pg.title})"
            return res
        return self._atomic(run)

    # ---------- RS232 (Comm232): Befehl oder Sensor ----------
    def add_rs232(self, kind: str, title: str, code: str, device: str | None = None, code_off: str | None = None,
                  analog: bool | None = None) -> dict:
        """RS232-Befehl (kind='befehl', code = CmdOn, z. B. '\\x07\\xF0\\x00\\xD3\\x01\\x52\\xD3\\x07\\x0F')
        oder RS232-Sensor (kind='sensor', code = Check-Muster, z. B. '\\x07\\xF0\\x00\\xDE\\x14\\s3\\1')
        unter einer vorhandenen RS232-Extension, nach dem Muster eines vorhandenen Objekts dort."""
        pr = self.project
        typ = {"befehl": "Lox232actor", "sensor": "Lox232sensor"}.get(kind)
        if typ is None:
            raise ProjectError("kind muss 'befehl' oder 'sensor' sein.")
        devs = [b for b in pr.blocks.values() if b.type == "Comm232" and (device is None or b.title.lower() == device.lower())]
        if len(devs) != 1:
            raise ProjectError(f"RS232-Extension {'„' + device + '“ ' if device else ''}nicht eindeutig ({len(devs)} gefunden).")

        def under(b, d):
            x = b.parent
            while x is not None:
                if x.uuid == d.uuid:
                    return True
                x = x.parent
            return False
        sib = [b for b in pr.blocks.values() if b.type == typ and under(b, devs[0])]
        if not sib:
            raise ProjectError(f"Unter '{devs[0].title}' gibt es noch keinen {kind} als Vorlage.")
        if any(b.title.lower() == title.lower() for b in sib):
            raise ProjectError(f"'{title}' gibt es dort schon.")
        ex = next((b for b in sib if analog is None or (b.elem.get("Analog") == "true") == analog), sib[0])
        attrs = {"Title": title, "IName": self._next_iname("Q" if typ == "Lox232actor" else "I", sib)}
        if typ == "Lox232actor":
            attrs.update({"CmdOn": code, "CmdOff": code_off})
        else:
            attrs["Check"] = code
        if analog is not None:
            attrs["Analog"] = "true" if analog else None
        b = self._atomic(lambda: self._clone_periphery(ex, attrs, "RS232-" + kind))
        return {"angelegt": self.project.name(b), "unter": devs[0].title, "code": code}

    # ---------- Peripherie löschen ----------
    REMOVABLE_PERIPHERY = {"WeatherServer", "VirtualIn", "VirtualTextIn", "VirtualOutCmd", "VirtualUdpInCmd",
                           "VirtualHttpInCmd", "VirtualHttpIn", "Constant", "VirtualOut", "Lox232sensor", "Lox232actor"}

    def remove_periphery(self, spec: str) -> dict:
        """Löscht ein Peripherie-Objekt (virtueller Ein-/Ausgang, Konstante, Wetterserver), wenn es
        nirgends mehr verwendet wird. Berechtigungseinträge darauf werden mit entfernt."""
        pr = self.project
        b = pr.logical(pr.resolve(spec, None))
        if b.type not in self.REMOVABLE_PERIPHERY:
            raise ProjectError(f"'{pr.name(b)}' ist kein löschbares Peripherie-Objekt ({', '.join(sorted(self.REMOVABLE_PERIPHERY))}).")
        if b.page is not None:
            raise ProjectError("Das Objekt liegt auf einer Programmseite; dafür block_remove verwenden.")
        if b.type == "VirtualOut" and b.elem.find("C") is not None:
            raise ProjectError(f"'{pr.name(b)}' enthält noch Befehle; erst diese löschen (periphery_remove).")
        own = {e.get("U") for e in b.elem.iter() if e.get("U")}
        for e in b.elem.iter():
            for k in ("SpStates", "VisuUUID"):
                for v in (e.get(k) or "").split(","):
                    if v:
                        own.add(v)
        inside = ET.tostring(b.elem, encoding="unicode")
        text = self.pt.text
        prm = 0
        for u in own:
            n_prm = text.count(f'<PRM REF="{u}"')
            if text.count(u) - inside.count(u) - n_prm > 0:
                users = [f"{e.tag} {x.get('Title') or ''}".strip() for x in pr.root.iter("C") if x is not b.elem
                         for e in [x] if any(u in v for k2, v in x.attrib.items() if k2 != "U")][:5]
                raise ProjectError(f"'{b.title}' wird noch verwendet" + (f" (z. B. {', '.join(users)})" if users else "")
                                   + ". Erst die Verwendung entfernen.")
            prm += n_prm
        if any(r for r in pr.blocks.values() if pr.target(r) is b):
            raise ProjectError(f"Auf '{b.title}' verweisen noch Platzhalter auf Programmseiten.")

        def run():
            for u in own:
                self.pt.remove_permission_refs(u)
            self.pt.remove_block(b.uuid)

        name = pr.name(b)
        self._apply(f"Gelöscht: {name} ({pr.location(b)})", run)
        return {"geloescht": name, "berechtigungseintraege_entfernt": prm}

    # ---------- Räume und Kategorien ----------
    _STRUCT = {"raum": ("Place", "Pr"), "kategorie": ("Category", "Cr")}

    def _struct(self, kind: str, spec: str) -> Block:
        kind = kind.lower()
        if kind not in self._STRUCT:
            raise ProjectError("kind muss 'raum' oder 'kategorie' sein.")
        typ = self._STRUCT[kind][0]
        pr = self.project
        hits = [b for b in pr.blocks.values() if b.type == typ and (b.uuid.startswith(spec.lstrip("#")) or b.title == spec)]
        if not hits:
            raise ProjectError(f"{kind.capitalize()} '{spec}' nicht gefunden.")
        if len(hits) > 1:
            used = [b for b in hits if self._struct_usage(b, kind)]
            free = [b for b in hits if not self._struct_usage(b, kind)]
            raise ProjectError(f"'{spec}' gibt es {len(hits)}-mal. Bitte die #Kennung angeben: " + "; ".join(
                f"#{pr.short(b)} ({'genutzt' if b in used else 'leer'})" for b in used + free))
        return hits[0]

    def _struct_usage(self, b: Block, kind: str) -> int:
        attr = self._STRUCT[kind.lower()][1]
        return sum(1 for e in self.project.root.iter() if e.get(attr) == b.uuid)

    def list_structure(self, kind: str) -> dict:
        kind = kind.lower()
        if kind not in self._STRUCT:
            raise ProjectError("kind muss 'raum' oder 'kategorie' sein.")
        typ = self._STRUCT[kind][0]
        rows = []
        for b in self.project.blocks.values():
            if b.type == typ:
                rows.append({"name": b.title, "id": self.project.short(b), "zuordnungen": self._struct_usage(b, kind),
                             **({"standard": True} if b.elem.get("First") == "true" else {})})
        return {"art": kind, "anzahl": len(rows), "leer": sum(1 for r in rows if not r["zuordnungen"]), "eintraege": rows}

    def remove_structure(self, kind: str, spec: str) -> dict:
        """Löscht einen leeren Raum oder eine leere Kategorie samt Berechtigungsgruppe."""
        pr = self.project
        b = self._struct(kind, spec)
        if b.elem.get("First") == "true":
            raise ProjectError("Der Standard-Eintrag 'Nicht zugeordnet' wird nicht gelöscht.")
        n = self._struct_usage(b, kind)
        if n:
            raise ProjectError(f"'{b.title}' ist noch {n}-mal zugeordnet. Erst mit structure_reassign umhängen.")
        rgr = b.elem.get("RGR")
        group = pr.blocks.get(rgr) if rgr else None
        if rgr and (group is None or group.type != "RightGroup"):
            raise ProjectError("Die zugehörige Berechtigungsgruppe fehlt oder ist unerwartet; bitte in Loxone Config löschen.")
        # Jede Fundstelle der Kennungen muss zu dem gehören, was hier entfernt wird
        text = self.pt.text
        expect_place = 1 + text.count(f'ctrlUuid="{b.uuid}"')
        if text.count(b.uuid) != expect_place:
            raise ProjectError(f"'{b.title}' wird an unbekannter Stelle verwendet; bitte in Loxone Config löschen.")
        if group is not None:
            expect_group = 2 + text.count(f'<PRM REF="{rgr}"')
            if text.count(rgr) != expect_group:
                raise ProjectError(f"Die Berechtigungsgruppe von '{b.title}' wird an unbekannter Stelle verwendet; "
                                   "bitte in Loxone Config löschen.")
        counts = {}

        def run():
            counts["nd"] = self.pt.remove_line_element(r'<nd\b[^>]*\sctrlUuid="%s"[^>]*/>' % re.escape(b.uuid))
            self.pt.remove_block(b.uuid)
            if group is not None:
                counts["prm"] = self.pt.remove_permission_refs(rgr)
                self.pt.remove_block(rgr)

        self._apply(f"{kind.capitalize()} gelöscht: {b.title}", run)
        return {"geloescht": b.title, "art": kind, "berechtigungseintraege_entfernt": counts.get("prm", 0)}

    def reassign_structure(self, kind: str, source: str, target: str) -> dict:
        """Hängt alle Zuordnungen von einem Raum/einer Kategorie auf einen anderen um."""
        a, b = self._struct(kind, source), self._struct(kind, target)
        if a is b:
            raise ProjectError("Quelle und Ziel sind identisch.")
        attr = self._STRUCT[kind.lower()][1]
        n = self._struct_usage(a, kind)
        if not n:
            raise ProjectError(f"'{a.title}' hat keine Zuordnungen.")
        self._apply(f"{kind.capitalize()} umgehängt: {n} Zuordnungen von '{a.title}' auf '{b.title}'",
                    lambda: self.pt.replace_attr_value(attr, a.uuid, b.uuid))
        return {"umgehaengt": n, "von": a.title, "nach": b.title}

    def addable_types(self) -> list[str]:
        return sorted(n for n, t in self.catalog.types.items() if self._addable(n, t) is None)

    @staticmethod
    def _addable(name: str, t: dict) -> str | None:
        if "Ref" in name:
            return "Platzhalter werden mit ref_add angelegt."
        if not t.get("beispiele_auf_seiten"):
            return "Dieser Typ ist kein Baustein für Programmseiten."
        if t.get("unterobjekte"):
            return "Der Typ enthält Unterobjekte."
        tpl = t["vorlage_xml"]
        extra = set(re.findall(r"\{verweis:(\w+)\}", tpl)) - ALLOWED_PLACEHOLDERS
        if extra or "{uuid:?}" in tpl:
            return "Der Typ verweist auf andere Objekte (" + ", ".join(sorted(extra) or ["interne Kennungen"]) + ")."
        return None

    # Druckblatt einer Seite (PaperFormat 2 = A3 quer): größte Ausdehnung vorhandener Seiten liegt
    # bei ca. 23 100 x 15 700; Bausteine darunter/rechts davon liegen in Config außerhalb des Blattes.
    SHEET_W, SHEET_H = 23400, 16300

    def _free_spot(self, page: Block, column: str, width: int, height: int = 1080) -> tuple[int, int]:
        x, y = self._free_spot_raw(page, column, width)
        if self._spot_ok(page, x, y, width, height):
            return x, y
        # sonst: erste freie Stelle auf dem Blatt suchen (zeilenweise von oben links, mit Abstand)
        for yy in range(480, self.SHEET_H - height, 192):
            for xx in range(480, self.SHEET_W - width, 192):
                if self._spot_ok(page, xx, yy, width, height):
                    return _snap(xx), _snap(yy)
        raise ProjectError(f"Auf der Seite {page.title} ist kein freier Platz mehr auf dem Blatt "
                           f"({width} x {height}). Bitte in Config Platz schaffen oder eine Seite angeben.")

    def _spot_ok(self, page: Block, x: int, y: int, w: int, h: int, gap: int = 96) -> bool:
        if x < 0 or y < 0 or x + w > self.SHEET_W or y + h > self.SHEET_H:
            return False
        for c in page.elem.findall("C"):
            if c.get("Px") is None:
                continue
            a = (int(c.get("Px")) - gap, int(c.get("Py")) - gap, int(c.get("Px2")) + gap, int(c.get("Py2")) + gap)
            if x < a[2] and a[0] < x + w and y < a[3] and a[1] < y + h:
                return False
        return True

    def _free_spot_raw(self, page: Block, column: str, width: int) -> tuple[int, int]:
        blocks = [self.project.blocks[c.get("U")] for c in page.elem.findall("C") if c.get("U") in self.project.blocks]
        if not blocks:
            return 4800, 4800
        bottom = max(_px(b, "Py2") for b in blocks)
        ins = [b for b in blocks if self._is_input_ref(b)]
        outs = [b for b in blocks if self._is_output_ref(b)]
        logic = [b for b in blocks if not self.project.is_ref(b)]
        if column == "in":
            x = min(_px(b, "Px") for b in ins) if ins else min(_px(b, "Px") for b in blocks) - width - 960
            y = max(_px(b, "Py2") for b in ins) if ins else min(_px(b, "Py") for b in blocks)
            return max(_snap(x), 0), _snap(y)
        if column == "out":
            x = max(_px(b, "Px") for b in outs) if outs else max(_px(b, "Px2") for b in blocks) + 960
            y = max(_px(b, "Py2") for b in outs) if outs else min(_px(b, "Py") for b in blocks)
            return _snap(x), _snap(y)
        x = min(_px(b, "Px") for b in logic) if logic else min(_px(b, "Px") for b in blocks) + 3072
        return _snap(x), _snap(bottom + 384)

    def add_block(self, page: str, type_: str, title: str, x: int | None, y: int | None, params: dict | None) -> dict:
        pr = self.project
        pg = pr.find_page(page)
        name, t = self.catalog.find_type(type_)
        why = self._addable(name, t)
        if why:
            raise ProjectError(f"Typ {name} kann ich nicht sicher neu anlegen: {why} Bitte in Loxone Config "
                               "anlegen und speichern; verdrahten und einstellen kann ich ihn danach.")
        if not title or not title.strip():
            raise ProjectError("Bitte einen Titel angeben.")
        size = t.get("groesse", {"breite": 1344, "hoehe": 696})
        if x is None or y is None:
            x, y = self._free_spot(pg, "logic", size["breite"], size["hoehe"])
        x, y = _snap(x), _snap(y)
        n_co = len(re.findall(r"\{uuid:[^}]+\}", t["vorlage_xml"]))
        block_uuid, co_uuids = self.pt.new_block_uuids(n_co)
        it = iter(co_uuids)
        tpl = re.sub(r"\{uuid:[^}]+\}", lambda m: next(it), t["vorlage_xml"])
        tpl = (tpl.replace("{uuid}", block_uuid).replace("{px}", str(x)).replace("{py}", str(y))
                  .replace("{px2}", str(x + size["breite"])).replace("{py2}", str(y + size["hoehe"]))
                  .replace("{verweis:Cr}", pr._iodata(pg, "Cr") or "").replace("{verweis:Pr}", pr._iodata(pg, "Pr") or ""))
        elem = ET.fromstring(tpl)
        elem.set("Title", title.strip())
        cos = {c.get("K", "").lower(): c for c in elem.findall("Co")}
        set_values = []
        for k, v in (params or {}).items():
            co = cos.get(k.lower())
            if co is None and k in elem.attrib and k not in PROTECTED_ATTRS and k != "Title":
                elem.set(k, str(v))
                set_values.append(f"{k}={v}")
                continue
            if co is None:
                props = [a for a in elem.attrib if a not in PROTECTED_ATTRS and a != "Title"]
                raise ProjectError(f"Typ {name} hat weder Anschluss noch Eigenschaft '{k}'. Anschlüsse: "
                                   f"{', '.join(c.get('K') for c in elem.findall('Co'))}. Eigenschaften: {', '.join(props)}")
            direction, sure = next(((c["richtung"], not c.get("richtung_unsicher")) for c in t["anschluesse"]
                                    if c["name"].lower() == k.lower()), (None, False))
            if direction == "ausgang" and sure:
                raise ProjectError(f"{co.get('K')} ist ein Ausgang und hat keinen einstellbaren Wert.")
            # Def gehört vor U
            attrib = dict(co.attrib)
            attrib.pop("Def", None)
            co.attrib.clear()
            for ak, av in attrib.items():
                if ak == "U":
                    co.set("Def", str(v))
                co.set(ak, av)
            set_values.append(f"{co.get('K')}={v}")

        def run():
            self.pt.insert_block(pg.uuid, elem)
            self.pt.bump("NextObj", 1)
            if name == "Memory":
                self.pt.bump("NextMem", 1)

        desc = f"Neu: {title.strip()} [{name}] auf Seite {pg.title}" + (f" mit {', '.join(set_values)}" if set_values else "")
        self._apply(desc, run)
        b = self.project.blocks[block_uuid]
        return {"angelegt": self.project.name(b), "seite": pg.title, "position": {"x": x, "y": y},
                "anschluesse": [c.key for c in b.connectors], "gesetzte_werte": set_values}

    def add_ref(self, page: str, target: str, kind: str | None, x: int | None, y: int | None) -> dict:
        pr = self.project
        pg = pr.find_page(page)
        t = pr.logical(pr.resolve(target, None))
        if t.type in ("Page", "Place", "Category", "Document"):
            raise ProjectError(f"Für {t.type} gibt es keine Platzhalter.")
        refs = [r for r in pr.blocks.values() if pr.is_ref(r) and pr.target(r).type == t.type]
        kinds = {"input": [r for r in refs if r.type == "InputRef"], "output": [r for r in refs if r.type == "OutputRef"]}
        if kind is None:
            present = [k for k, v in kinds.items() if v]
            if len(present) != 1:
                raise ProjectError(f"Bitte kind angeben ('input' zum Lesen, 'output' zum Schreiben). "
                                   f"Für Typ {t.type} bekannt: {', '.join(present) or 'keine Beispiele im Projekt'}.")
            kind = present[0]
        if kind not in kinds:
            raise ProjectError("kind muss 'input' oder 'output' sein.")
        cands = kinds[kind]
        if not cands:
            raise ProjectError(f"Im Projekt gibt es noch keinen {kind}-Platzhalter für den Typ {t.type}, von dem ich "
                               "den Aufbau übernehmen könnte. Bitte einmal in Loxone Config auf eine Seite ziehen.")
        same = [r for r in cands if pr.target(r) is t]
        if not same:
            same = [r for r in cands if pr.target(r).elem.get("Analog") == t.elem.get("Analog")
                    and pr.target(r).elem.get("Tp") == t.elem.get("Tp")]
        if not same:
            raise ProjectError(f"Kein vergleichbarer Platzhalter für '{pr.name(t)}' im Projekt gefunden.")
        sig = Counter((r.elem.get("WF"), r.elem.get("Analog"), r.elem.get("LinkRefType")) for r in same).most_common(1)[0][0]
        ex = next(r for r in same if (r.elem.get("WF"), r.elem.get("Analog"), r.elem.get("LinkRefType")) == sig)
        ex_t = pr.target(ex)
        w, h = _px(ex, "Px2") - _px(ex, "Px"), _px(ex, "Py2") - _px(ex, "Py")
        if x is None or y is None:
            x, y = self._free_spot(pg, "in" if kind == "input" else "out", w, h)
        x, y = _snap(x), _snap(y)
        block_uuid, co_uuids = self.pt.new_block_uuids(len(ex.connectors))
        elem = ET.Element("C")
        place = {"U": block_uuid, "Title": t.title, "Px": str(x), "Py": str(y), "Px2": str(x + w), "Py2": str(y + h), "Ref": t.uuid}
        for k, v in ex.elem.attrib.items():
            if k != "LtE":
                elem.set(k, place.get(k, v))
        target_links: list[tuple[str, str]] = []  # (Zielanschluss am Objekt, neuer Platzhalter-Anschluss)
        for con, cu in zip(ex.connectors, co_uuids):
            co = ET.SubElement(elem, "Co", {"K": con.key})
            srcs = []
            for su, _ in con.sources:  # interne Verbindung Objekt -> Platzhalter nachbilden
                hit = pr.co_index.get(su)
                if hit is None or (hit[0] is not ex_t and not pr._related(hit[0], ex_t)):
                    continue  # normale Verdrahtung auf der Seite des Beispiels, gehört nicht zum Aufbau
                if hit[0] is not ex_t:
                    raise ProjectError("Der Beispiel-Platzhalter ist ungewöhnlich verdrahtet; ich lege keinen neuen an.")
                mine = t.connector(hit[1].key)
                if mine is None:
                    raise ProjectError(f"'{pr.name(t)}' hat keinen Anschluss {hit[1].key}.")
                srcs.append(mine.uuid)
            if srcs:
                co.set("Nc", str(len(srcs)))
            co.set("U", cu)
            for s in srcs:
                ET.SubElement(co, "In", {"Input": s})
            for tc in ex_t.connectors:  # interne Verbindung Platzhalter -> Objekt nachbilden
                if any(su == con.uuid for su, _ in tc.sources):
                    mine = t.connector(tc.key)
                    if mine is None:
                        raise ProjectError(f"'{pr.name(t)}' hat keinen Anschluss {tc.key}.")
                    target_links.append((mine.uuid, cu))

        def run():
            self.pt.insert_block(pg.uuid, elem)
            for dst, src in target_links:
                self.pt.add_in(dst, src)
            self.pt.bump("NextObj", 1)

        art = "Eingangs" if kind == "input" else "Ausgangs"
        self._apply(f"Neu: {art}-Platzhalter für {pr.name(t)} auf Seite {pg.title}", run)
        b = self.project.blocks[block_uuid]
        out = {"angelegt": f"{art}-Platzhalter #{self.project.short(b)} für {self.project.name(t)}",
               "seite": pg.title, "position": {"x": x, "y": y}}
        if kind == "input":
            out["ausgang"] = self._ref_output_key(b)
        else:
            out["eingang"] = "AI"
        return out

    def remove_block(self, block: str, page: str | None) -> dict:
        pr = self.project
        b = pr.resolve(block, page)
        if b.page is None:
            raise ProjectError("Nur Bausteine auf Programmseiten lassen sich löschen, keine Geräte oder Räume.")
        if b.elem.findall("C"):
            raise ProjectError("Der Baustein enthält Unterobjekte; bitte in Loxone Config löschen.")
        if not pr.is_ref(b) and any(pr.target(r) is b for r in pr.blocks.values()):
            raise ProjectError("Auf diesen Baustein verweisen Platzhalter auf anderen Seiten. Bitte zuerst diese entfernen.")
        own = {c.uuid for c in b.connectors}
        cuts = [(c.uuid, su) for o in pr.blocks.values() if o is not b for c in o.connectors for su, _ in c.sources if su in own]
        name, pg = ("Platzhalter für " if pr.is_ref(b) else "") + pr.name(b), b.page.title

        def run():
            for dst, src in cuts:
                self.pt.remove_in(dst, src)
            self.pt.remove_permission_refs(b.uuid)   # Benutzerrechte auf das Objekt mit entfernen
            self.pt.remove_block(b.uuid)

        self._apply(f"Gelöscht: {name} von Seite {pg} ({len(cuts)} Verbindungen gelöst)", run)
        return {"geloescht": name, "seite": pg, "geloeste_verbindungen": len(cuts)}

    # ---------- Prüfen ----------
    def _issues(self, pr: Project) -> dict[tuple, str]:
        """Auffälligkeiten als {Schlüssel: Text}. Der Schlüssel enthält nur Kennungen, damit
        eine Umbenennung eine vorhandene Auffälligkeit nicht wie eine neue aussehen lässt."""
        out: dict[tuple, str] = {}
        uuids = [e.get("U") for e in pr.root.iter() if e.get("U")]
        for u, n in Counter(uuids).items():
            if n > 1:
                out[("dup", u)] = f"UUID {u} ist {n}-mal vergeben."
        doc = pr.root.find("C[@Type='Document']")
        n_obj = sum(1 for e in pr.root.iter("C") if e.get("U"))
        if doc is not None and doc.get("NumO") and int(doc.get("NumO")) != n_obj:
            out[("numo",)] = f"Objektzähler NumO ist {doc.get('NumO')}, gezählt wurden {n_obj}."
        if pr.dangling:
            out[("dangling",)] = f"{pr.dangling} Verbindungen zeigen auf einen Anschluss, den es nicht gibt."
        known = {e.get("U") for e in pr.root.iter() if e.get("U")}
        for e in pr.root.iter():
            for attr in ("Pr", "Cr", "RGR", "ctrlUuid", "REF", "source"):
                v = e.get(attr)
                if v and v not in known and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{16}", v):
                    out[("ref", e.tag, attr, v)] = f"{e.tag}.{attr} verweist auf ein Objekt, das es nicht gibt ({v})."
            if e.tag == "PH" and e.get("NUM") is not None and int(e.get("NUM")) != len(e.findall("PRM")):
                out[("ph", e.get("NUM"), len(e.findall("PRM")))] = (
                    f"Berechtigungsliste: Zähler NUM={e.get('NUM')}, Einträge {len(e.findall('PRM'))}.")
        for b in pr.blocks.values():
            if b.elem.get("Nio") and int(b.elem.get("Nio")) != len(b.connectors):
                out[("nio", b.uuid)] = f"{pr.name(b)}: Anschlusszähler Nio stimmt nicht."
            for co in b.elem.findall("Co"):
                if int(co.get("Nc", "0")) != len(co.findall("In")):
                    out[("nc", co.get("U"))] = f"{pr.name(b)}.{co.get('K')}: Verbindungszähler Nc stimmt nicht."
            ref = b.elem.get("Ref")
            if ref and ref not in pr.blocks and "Ref" in b.type:
                out[("phref", b.uuid)] = f"Platzhalter {pr.name(b)} verweist auf ein Objekt, das es nicht gibt."
        for e in pr.edges:
            where = f"{pr.label(e.src, e.src_key)} -> {pr.label(e.dst, e.dst_key)}"
            k = (e.src.connector(e.src_key).uuid, e.dst.connector(e.dst_key).uuid)
            if e.src.page is not None and e.dst.page is not None and e.src.page is not e.dst.page:
                out[("cross",) + k] = f"Verbindung über Seitengrenzen: {where}"
            if pr.is_ref(e.src) and e.src.type == "InputRef":
                t = pr.target(e.src)
                need = "AQ" if e.src.elem.get("Analog") == "true" or t.type != "Memory" else "Q"
                if e.src_key != need:
                    out[("refout",) + k] = f"Platzhalter-Ausgang {e.src_key} statt {need}; Loxone Config verwirft das: {where}"
            else:
                direction, sure = self._dir_for(e.src, e.src_key)
                if direction == "eingang" and sure:
                    out[("srcin",) + k] = f"Ein Eingang dient als Quelle: {where}"
            if not pr.is_ref(e.dst):
                direction, sure = self._dir_for(e.dst, e.dst_key)
                if direction == "ausgang" and sure:
                    out[("dstout",) + k] = f"Ein Ausgang dient als Ziel: {where}"
        return out

    def _dir_for(self, b: Block, key: str) -> tuple[str | None, bool]:
        return self._direction(b, key)

    def validate(self) -> dict:
        now, before = self._issues(self.project), self._issues(self.base)
        new = [msg for key, msg in now.items() if key not in before]
        # Prüfungen vor dem Laden (Blatt, Überlappung, Sonderzeichen) nur für neue/geänderte Bausteine
        from analyze import lint_layout
        changed = {u for u, b in self.project.blocks.items()
                   if u not in self.base.blocks or ET.tostring(b.elem) != ET.tostring(self.base.blocks[u].elem)}
        warn = [f"{f['befund']} ({f['ort']})" for f in lint_layout(self.project, changed)] if changed else []
        out = {"in_ordnung": not new, "fehler": sorted(new),
               "hinweis": "Geprüft werden nur Auffälligkeiten, die durch die Änderungen neu entstanden sind."}
        if warn:
            out["warnungen"] = warn[:30]
        return out

    def changes(self) -> dict:
        a, b = self.base, self.project
        added = [("Platzhalter für " if b.is_ref(x) else "") + f"{b.name(x)} ({b.location(x)})"
                 for u, x in b.blocks.items() if u not in a.blocks]
        removed = [f"{a.name(x)} ({a.location(x)})" for u, x in a.blocks.items() if u not in b.blocks]

        def edge_map(p: Project) -> dict:
            return {(e.src.connector(e.src_key).uuid, e.dst.connector(e.dst_key).uuid):
                    f"{p.label(e.src, e.src_key)} -> {p.label(e.dst, e.dst_key)}" for e in p.edges}

        ea, eb = edge_map(a), edge_map(b)
        values = []
        for u, x in b.blocks.items():
            o = a.blocks.get(u)
            if o is None:
                continue
            for c in x.connectors:
                oc = o.connector(c.key)
                if oc is not None and oc.default != c.default:
                    values.append(f"{b.name(x)}.{c.key}: {oc.default} -> {c.default}")
                if oc is not None and oc.inverted != c.inverted:
                    values.append(f"{b.name(x)}.{c.key}: {'jetzt negiert' if c.inverted else 'nicht mehr negiert'}")
            for k, v in x.elem.attrib.items():
                if k not in ("Nio", "NumO") and o.elem.get(k) != v:
                    values.append(f"{b.name(x)} {k}: {o.elem.get(k)} -> {v}")
        return {
            "quelle": self.source.name,
            "schritte": list(self.log),
            "neue_bausteine": added,
            "geloeschte_bausteine": removed,
            "neue_verbindungen": [eb[k] for k in eb if k not in ea],
            "geloeste_verbindungen": [ea[k] for k in ea if k not in eb],
            "geaenderte_werte": values,
        }

    def _stamp_document(self) -> None:
        """Setzt den Projektzeitpunkt (Document Date/DateS) auf jetzt. Sonst hält Loxone Config beim
        Laden das Programm im Miniserver für neuer, übernimmt es und verwirft die Änderungen
        (am 2026-11-17 so passiert). Date = Ortszeit, DateS = Sekunden seit 1.1.2009 in UTC
        (geprüft: 2026-11-17 19:54:02 MEZ -> 564173642)."""
        import datetime as _dt
        now = _dt.datetime.now().replace(microsecond=0)
        utc = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        date_s = int((utc - _dt.datetime(2009, 1, 1)).total_seconds())
        m = re.search(r'<C Type="Document"[^>]*>', self.pt.text)
        if not m:
            return
        tag = m.group(0)
        new = re.sub(r'\bDate="[^"]*"', f'Date="{now:%Y-%m-%d %H:%M:%S}"', tag, count=1)
        new = re.sub(r'\bDateS="\d+"', f'DateS="{date_s}"', new, count=1)
        self.pt.text = self.pt.text[:m.start()] + new + self.pt.text[m.end():]

    def save(self, target: Path) -> dict:
        if not self.log:
            raise ProjectError("Es gibt keine Änderungen zu speichern.")
        check = self.validate()
        if not check["in_ordnung"]:
            raise ProjectError("Nicht gespeichert, die Prüfung hat Fehler gefunden: " + " | ".join(check["fehler"]))
        if target.resolve() == self.source.resolve():
            raise ProjectError("Die Quelldatei wird nie überschrieben. Bitte einen anderen Namen wählen.")
        if target.exists():
            raise ProjectError(f"Die Datei {target.name} existiert bereits. Bitte einen anderen Namen wählen.")
        self._stamp_document()
        target.write_bytes(self.pt.data())
        Project(target)  # muss sich wieder lesen lassen
        return {"gespeichert": str(target), "aenderungen": len(self.log),
                "hinweis": "Bitte in Loxone Config öffnen und prüfen, bevor die Datei in den Miniserver geladen wird."}
