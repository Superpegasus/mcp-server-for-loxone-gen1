"""Abgleich (Stufe „1“): Projektdatei ↔ automatische Sicherung von Loxone Config ↔ Miniserver.

Hintergrund: Loxone Config legt beim Arbeiten eine automatische Sicherung unter
%TEMP%\\Loxone\\Loxone Config\\AutoSave\\<Projekt-UUID>.Loxone ab. Alle Fassungen eines
Projekts haben dieselbe Projekt-UUID. Nimmt man beim Öffnen die angebotene Sicherung an,
lädt Config deren (oft alten) Stand statt der geöffneten Datei.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from loxproject import Project, ProjectError

MAX_LIST = 25


def autosave_path(p: Project) -> Path | None:
    doc = p.root.find("C[@Type='Document']")
    if doc is None or not doc.get("U"):
        return None
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    return Path(temp) / "Loxone" / "Loxone Config" / "AutoSave" / f"{doc.get('U')}.Loxone"


def _edge_key(p: Project, e) -> tuple[str, str]:
    return (e.src.connector(e.src_key).uuid, e.dst.connector(e.dst_key).uuid)


def diff_projects(a: Project, b: Project, common_types_only: bool = False) -> dict:
    """Inhaltliche Unterschiede von a nach b (Bausteine, Verbindungen, Namen, Werte).
    Darstellung, Position und Zeitstempel werden ignoriert. common_types_only: nur Objekttypen
    vergleichen, die in beiden vorkommen (für gekürzte Programmauszüge ohne Bedienelemente)."""
    if common_types_only:
        # Programmauszüge lassen Bedienelemente und unbenutzte Peripherie weg: nur Programmseiten vergleichen
        keep = lambda x: x.page is not None  # noqa: E731
    else:
        keep = lambda x: True  # noqa: E731
    only_a = [x for u, x in a.blocks.items() if u not in b.blocks and keep(x)]
    only_b = [x for u, x in b.blocks.items() if u not in a.blocks and keep(x)]
    ea = {_edge_key(a, e): f"{a.label(e.src, e.src_key)} -> {a.label(e.dst, e.dst_key)}" for e in a.edges}
    eb = {_edge_key(b, e): f"{b.label(e.src, e.src_key)} -> {b.label(e.dst, e.dst_key)}" for e in b.edges}
    changed = []
    for u, x in b.blocks.items():
        o = a.blocks.get(u)
        if o is None:
            continue
        # Platzhalter-Titel zieht Loxone Config beim Laden selbst auf den Namen des Ziels nach
        if o.title != x.title and not b.is_ref(x):
            changed.append(f"{b.name(x)}: Name '{o.title}' -> '{x.title}'")
        ot, nt = o.elem.find("Timer"), x.elem.find("Timer")   # Heizplan der Raumregler
        if ot is not None and nt is not None and not common_types_only:
            sig = lambda t: [tuple(sorted(e.attrib.items())) for e in t.findall("Entry")]  # noqa: E731
            if sig(ot) != sig(nt):
                changed.append(f"{b.name(x)}: Heizplan geändert")
        for c in x.connectors:
            oc = o.connector(c.key)
            if oc is None:
                continue
            # Standardwert an einem verbundenen Eingang ist wirkungslos; Config lässt ihn beim Laden weg
            if oc.default != c.default and not (common_types_only and oc.default is None) and not (c.sources or oc.sources):
                changed.append(f"{b.name(x)}.{c.key}: Wert {oc.default} -> {c.default}")
            if oc.inverted != c.inverted:
                changed.append(f"{b.name(x)}.{c.key}: {'jetzt negiert' if c.inverted else 'nicht mehr negiert'}")
    removed_e = [v for k, v in ea.items() if k not in eb]
    added_e = [v for k, v in eb.items() if k not in ea]

    def fmt(p: Project, xs):
        return [("Platzhalter für " if p.is_ref(x) else "") + f"{p.name(x)} ({p.location(x)})" for x in xs]

    total = len(only_a) + len(only_b) + len(removed_e) + len(added_e) + len(changed)
    return {
        "gleich": total == 0,
        "anzahl_unterschiede": total,
        "bausteine_nur_vorher": fmt(a, only_a)[:MAX_LIST],
        "bausteine_nur_nachher": fmt(b, only_b)[:MAX_LIST],
        "verbindungen_nur_vorher": removed_e[:MAX_LIST],
        "verbindungen_nur_nachher": added_e[:MAX_LIST],
        "geaenderte_werte": changed[:MAX_LIST],
        **({"hinweis": f"Listen auf {MAX_LIST} Einträge gekürzt."} if total > MAX_LIST else {}),
    }


def _when(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


def autosave_report(p: Project) -> dict | None:
    """Kurzbericht zur automatischen Sicherung, oder None, wenn es keine gibt."""
    path = autosave_path(p)
    if path is None or not path.is_file():
        return None
    try:
        s = Project(path)
    except ProjectError as e:
        return {"datei": str(path), "lesbar": False, "fehler": str(e)}
    d = diff_projects(p, s)
    rep = {"datei": str(path), "geaendert": _when(path), "gleich_wie_projektdatei": d["gleich"]}
    if not d["gleich"]:
        rep["warnung"] = ("Loxone Config bietet beim Öffnen diese automatische Sicherung an. Sie weicht von der "
                          "Projektdatei ab. Rückfrage beim Öffnen ablehnen, sonst lädt Config diesen Stand.")
        rep["unterschiede_projektdatei_zu_sicherung"] = d
    return rep


def load_miniserver_dump(path: str | Path, project_path: Path) -> Project:
    """Programm aus einer Textdatei, wie sie get_loxone_xml des Loxone-MCP liefert."""
    raw = Path(path).read_bytes().decode("utf-8", "replace")
    start, end = raw.find("<?xml"), raw.find("</ControlList>")
    if start < 0 or end < 0:
        raise ProjectError("In der Datei steht kein vollständiges Loxone-Programm.")
    return Project(project_path, raw[start:end + len("</ControlList>")].encode("utf-8"))


def sync_report(p: Project, miniserver=None, dump_path: str | None = None) -> dict:
    doc = p.root.find("C[@Type='Document']")
    out: dict = {"projektdatei": {"datei": p.path.name, "geaendert": _when(p.path),
                                  "von_config_gespeichert": doc.get("Date") if doc is not None else None}}
    a = autosave_report(p)
    out["automatische_sicherung"] = a or "keine vorhanden"
    hints = []
    if a and not a.get("gleich_wie_projektdatei", True):
        hints.append("Automatische Sicherung weicht ab: beim Öffnen in Config die Rückfrage ablehnen.")

    if miniserver is not None:
        try:
            changed = miniserver.program_changed()
            ms = {"letzte_programmaenderung": changed}
            file_time = datetime.fromtimestamp(p.path.stat().st_mtime)
            prog_time = datetime.strptime(changed, "%Y-%m-%d %H:%M:%S")
            if file_time > prog_time:
                ms["einschaetzung"] = "Die Projektdatei ist neuer als das Programm im Miniserver; vermutlich noch nicht geladen."
                hints.append("Projektdatei in Loxone Config öffnen und in den Miniserver speichern.")
            else:
                ms["einschaetzung"] = ("Das Programm im Miniserver wurde nach der Projektdatei geändert. Ob der Inhalt "
                                       "gleich ist, zeigt nur ein Programmvergleich (miniserver_xml_path).")
            out["miniserver"] = ms
        except (ProjectError, ValueError) as e:
            out["miniserver"] = {"fehler": str(e)}
    if dump_path:
        m = load_miniserver_dump(dump_path, p.path)
        d = diff_projects(m, p, common_types_only=True)
        out["vergleich_miniserver_zu_projektdatei"] = d
        if d["gleich"]:
            hints.append("Programm im Miniserver und Projektdatei sind inhaltlich gleich.")
        else:
            hints.append(f"{d['anzahl_unterschiede']} inhaltliche Unterschiede zwischen Miniserver und Projektdatei "
                         "('nur_vorher' = nur im Miniserver, 'nur_nachher' = nur in der Datei).")
    out["empfehlung"] = hints or ["Keine Auffälligkeiten."]
    return out
