"""MCP-Server: Loxone-Config-Projektdateien lesen, verstehen und in Kopien bearbeiten.

Lesen (project_*), Baustein-Katalog (catalog_*), Bearbeiten (block_*, ref_add).
Bearbeitet wird ein Arbeitsstand im Speicher; gespeichert wird nur in eine neue Datei.

Start:  uv run server.py
Ordner mit den Projektdateien: Umgebungsvariable LOXONE_PROJECTS_DIR,
sonst Dokumente\\Loxone\\Loxone Config\\Projects.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from analyze import analyze, summarize
from catalog import Catalog
from editor import Editor
from live import Miniserver
from loxproject import Project, ProjectError
from recipes import RECIPES, Recipes
from sync import autosave_report, sync_report

mcp = MCPServer("mcp-server-for-loxone-gen1")

_project: Project | None = None
_catalog: Catalog | None = None
_edit: Editor | None = None  # Arbeitsstand mit ungespeicherten Änderungen


def _editor() -> Editor:
    """Arbeitsstand; wird beim ersten Schreibzugriff aus dem geöffneten Projekt angelegt."""
    global _edit
    if _edit is None:
        if not _cat().types:
            raise ProjectError("Der Katalog ist leer. Bitte zuerst catalog_learn aufrufen.")
        _edit = Editor(_current().path, _cat())
    return _edit


def _cat() -> Catalog:
    global _catalog
    if _catalog is None:
        path = os.environ.get("LOXONE_CATALOG") or Path(__file__).parent / "catalog.json"
        _catalog = Catalog(path)
    return _catalog


def _projects_dir() -> Path:
    env = os.environ.get("LOXONE_PROJECTS_DIR")
    if env:
        return Path(env)
    return Path.home() / "Documents" / "Loxone" / "Loxone Config" / "Projects"


def _project_files() -> list[Path]:
    d = _projects_dir()
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".loxone"]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


SETTINGS = Path(__file__).parent / "settings.json"


def _settings() -> dict:
    import json
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _main_files() -> list[Path]:
    """Dateien des Hauptprojekts (Muster aus settings.json, z. B. 'my_home_v*.Loxone'),
    neueste zuerst; ohne Muster alle Projektdateien."""
    import fnmatch
    pat = _settings().get("hauptprojekt_muster")
    files = _project_files()
    if pat:
        files = [p for p in files if fnmatch.fnmatch(p.name.lower(), pat.lower())]
    return files


def _current() -> Project:
    """Geöffnetes Projekt; lädt neu, wenn die Datei inzwischen gespeichert wurde."""
    global _project
    if _edit is not None:
        return _edit.project  # Lesewerkzeuge zeigen den Arbeitsstand samt Änderungen
    if _project is None:
        files = _main_files()
        if not files:
            raise ProjectError(
                f"Kein Projekt geöffnet und keine .Loxone-Datei in {_projects_dir()} gefunden. "
                "Bitte project_open mit einem Pfad aufrufen."
            )
        _project = Project(files[0])
    elif _project.path.is_file() and _project.path.stat().st_mtime != _project.mtime:
        _project = Project(_project.path)
    return _project


def _safe(fn):
    try:
        return fn()
    except ProjectError as e:
        return {"fehler": str(e)}


@mcp.tool()
def project_list_files() -> dict:
    """Lists the Loxone project files (.Loxone) in the project folder, newest first."""
    files = _project_files()
    return {
        "ordner": str(_projects_dir()),
        "geoeffnet": str(_project.path) if _project else None,
        "dateien": [
            {
                "name": p.name,
                "pfad": str(p),
                "kb": round(p.stat().st_size / 1024),
                "geaendert": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            }
            for p in files
        ],
    }


@mcp.tool()
def project_set_main(pattern: str | None = None) -> dict:
    """Defines which files belong to the main project (pattern with *, e.g. 'my_home_v*.Loxone'). project_open without a path then opens the newest matching file. Without pattern: show the current setting."""
    import json
    def run():
        s = _settings()
        if pattern:
            s["hauptprojekt_muster"] = pattern
            SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        files = _main_files()
        return {"hauptprojekt_muster": s.get("hauptprojekt_muster"),
                "neueste_datei": files[0].name if files else None, "anzahl_dateien": len(files)}
    return _safe(run)


@mcp.tool()
def project_open(path: str | None = None) -> dict:
    """Opens a Loxone project file for reading and returns an overview (title, version, pages, most common block types). Without a path, the newest file of the main project is used (see project_set_main); a bare file name is looked up in the project folder. The field 'abgleich' compares it with the last Miniserver backup and the Loxone Config autosave. The file is never modified."""
    def run():
        global _project, _edit
        if _edit is not None and _edit.log:
            raise ProjectError(f"Es gibt {len(_edit.log)} ungespeicherte Änderungen. "
                               "Bitte zuerst project_save oder project_discard aufrufen.")
        _edit = None
        if path:
            p = Path(path)
            if not p.is_absolute():
                p = _projects_dir() / path
            _project = Project(p)
        else:
            _project = None
        pr = _current()
        out = pr.summary()
        import loadcheck
        out["abgleich"] = loadcheck.open_check(pr)  # Miniserver-Sicherung + automatische Sicherung
        return out
    return _safe(run)


@mcp.tool()
def verify_load(file: str | None = None, cleanup: bool = False) -> dict:
    """Call after loading a file into the Miniserver with Loxone Config: waits until the Miniserver answers again (up to 3 min), backs up the running program (Git) and compares it with the expected project file (default: newest file of the main project). cleanup=True moves older versions of the main project to the recycle bin on success."""
    import loadcheck
    def run():
        files = _main_files()
        if file:
            p = Path(file)
            exp = p if p.is_absolute() else _projects_dir() / file
        elif files:
            exp = files[0]
        else:
            raise ProjectError("Keine Datei des Hauptprojekts gefunden.")
        return loadcheck.verify_load(exp, files, cleanup)
    return _safe(run)


@mcp.tool()
def project_list_pages() -> dict:
    """Lists all program pages of the open project with room, category and number of blocks."""
    def run():
        pr = _current()
        return {
            "datei": pr.path.name,
            "seiten": [
                {
                    "titel": p.title,
                    "bausteine": len(p.elem.findall("C")),
                    "raum": pr.room(p),
                    "kategorie": pr.category(p),
                }
                for p in pr.pages
            ],
        }
    return _safe(run)


@mcp.tool()
def project_get_page(page: str, include_unconnected: bool = False) -> dict:
    """Shows one program page: its blocks, their settings and connections, with placeholders resolved."""
    def run():
        pr = _current()
        return pr.page_view(pr.find_page(page), include_unconnected)
    return _safe(run)


@mcp.tool()
def project_find(query: str | None = None, type: str | None = None, room: str | None = None,
                 category: str | None = None, page: str | None = None, limit: int = 50) -> dict:
    """Searches blocks by title/description, type, room, category or page."""
    def run():
        return _current().find(query, type, room, category, page, max(1, min(limit, 300)))
    return _safe(run)


@mcp.tool()
def project_get_block(block: str, page: str | None = None) -> dict:
    """Shows one block in detail: connectors, set values, sources and targets, and values that deviate from the usual defaults."""
    def run():
        pr = _current()
        b = pr.logical(pr.resolve(block, page))
        view = pr.block_view(b)
        dev = _cat().deviations(b)
        if dev:
            view["abweichend_vom_ueblichen_wert"] = dev
        return view
    return _safe(run)


@mcp.tool()
def project_trace(block: str, connector: str | None = None, direction: str = "forward",
                  max_depth: int = 4, page: str | None = None) -> dict:
    """Follows a signal forwards or backwards through the logic (across placeholders and pages)."""
    def run():
        pr = _current()
        if direction not in ("forward", "backward"):
            raise ProjectError("direction muss 'forward' oder 'backward' sein.")
        return pr.trace(pr.resolve(block, page), connector, direction, max(1, min(max_depth, 12)))
    return _safe(run)


@mcp.tool()
def catalog_learn(paths: list[str] | None = None, reset: bool = False) -> dict:
    """Learns block types, connectors and templates from project files (needed once before editing)."""
    def run():
        if paths:
            files = [Path(p) if Path(p).is_absolute() else _projects_dir() / p for p in paths]
        else:
            files = _project_files()
        return _cat().learn(files, reset)
    return _safe(run)


@mcp.tool()
def catalog_list_types(query: str | None = None, include_peripherals: bool = False) -> dict:
    """Lists the block types known to the catalog."""
    return _safe(lambda: _cat().list_types(query, include_peripherals))


@mcp.tool()
def catalog_get_type(type: str, full_template: bool = False) -> dict:
    """Shows connectors, parameters and template of one block type."""
    return _safe(lambda: _cat().get_type(type, full_template))


@mcp.tool()
def block_add(page: str, type: str, title: str, x: int | None = None, y: int | None = None,
              params: dict | None = None) -> dict:
    """Adds a new logic block of a catalog type to a page (placed inside the sheet, not overlapping), optionally with parameter values."""
    return _safe(lambda: _editor().add_block(page, type, title, x, y, params))


@mcp.tool()
def ref_add(page: str, target: str, kind: str | None = None, x: int | None = None, y: int | None = None) -> dict:
    """Adds a placeholder (input or output reference) for a block or peripheral object to a page, so it can be wired there."""
    return _safe(lambda: _editor().add_ref(page, target, kind, x, y))


@mcp.tool()
def block_connect(from_block: str, to_block: str, to_connector: str | None = None,
                  from_connector: str | None = None, page: str | None = None) -> dict:
    """Connects an output of one block to an input of another on the same page."""
    return _safe(lambda: _editor().connect(from_block, from_connector, to_block, to_connector, page))


@mcp.tool()
def block_disconnect(from_block: str, to_block: str, to_connector: str | None = None,
                     from_connector: str | None = None, page: str | None = None) -> dict:
    """Removes a connection between two blocks on a page."""
    return _safe(lambda: _editor().disconnect(from_block, from_connector, to_block, to_connector, page))


@mcp.tool()
def block_set_params(block: str, params: dict, page: str | None = None) -> dict:
    """Sets connector default values, attributes (e.g. Formula, Title) or the description (name in the app) of a block."""
    return _safe(lambda: _editor().set_params(block, params, page))


@mcp.tool()
def block_set_inverted(block: str, connector: str, inverted: bool = True, page: str | None = None) -> dict:
    """Inverts an input of a block or removes the inversion. A free, inverted input is permanently on (e.g. to lock something)."""
    return _safe(lambda: _editor().set_inverted(block, connector, inverted, page))


@mcp.tool()
def io_add_virtual_input(title: str, analog: bool = True, min_val: float | None = None, max_val: float | None = None,
                         room: str | None = None, category: str | None = None, visu: bool = False,
                         unit: str | None = None, text: bool = False, desc: str | None = None) -> dict:
    """Adds a virtual input under 'Virtual inputs'. Values are set via HTTP (/dev/sps/io/<title>/<value>), e.g. from Home Assistant. visu=True shows it in the app, unit is the display format (e.g. '<v.1>°'). text=True creates a text input (send the value URL-encoded; WARNING: querying /state on a text input overwrites it). desc is the name in the app. System variables are fed via an output placeholder of the system variable, not via 'source'."""
    return _safe(lambda: _editor().add_virtual_input(title, analog, min_val, max_val, room, category, visu, unit,
                                                     text, desc))


@mcp.tool()
def build_plan(steps: list[dict], save_as: str | None = None) -> dict:
    """Runs a whole build plan in one call - all or nothing. Each step is a dict with 'op' and its arguments, e.g. {"op":"baustein","typ":"And","titel":"Rain and open","seite":"Page 2","als":"and1"} or {"op":"verbinden","von":"ha_rain","nach":"$and1","nach_eingang":"I2","seite":"Page 2"}. Ops (German keys, see plan.py): baustein, kopie, status, eingang, befehl, rs232, platzhalter, verbinden, trennen, werte, negieren, anzeige, statistik, position, loeschen, peripherie_loeschen, seite, mitteilung, kategorie_umhaengen, kategorie_loeschen. 'als' names a new object, '$name' refers to it. Missing placeholders are created by 'verbinden'. Afterwards the result is validated; with save_as (new file name) it is saved if validation passes."""
    import plan
    def run():
        ed = _editor()
        res = plan.run_plan(ed, steps)
        res["pruefung"] = ed.validate()
        if save_as:
            if not res["pruefung"]["in_ordnung"]:
                raise ProjectError("Nicht gespeichert, die Prüfung meldet Fehler: " + " | ".join(res["pruefung"]["fehler"]))
            res["gespeichert"] = project_save(save_as)
        if len(str(res)) > 20000:  # Antwort kurz halten
            res["ergebnisse"] = [{"schritt": e["schritt"], "op": e["op"]} for e in res["ergebnisse"]]
        return res
    return _safe(run)


@mcp.tool()
def project_text(page: str | None = None) -> dict:
    """Readable text view of the program (or working copy): per page the blocks with settings and inputs ('I1 <- Source.Output') and the outputs to peripherals. Without page: whole project (long)."""
    import textview
    def run():
        t = textview.render(_current(), page)
        if len(t) > 60000:
            t = t[:60000] + "\n… (gekürzt – bitte eine Seite angeben)"
        return {"text": t}
    return _safe(run)


@mcp.tool()
def backup_miniserver() -> dict:
    """Backs up the program currently running in the Miniserver (newest file in /prog via FTP, download only) as XML plus a readable text view into a local Git repository and reports what changed since the last backup. Nothing is stored if nothing changed."""
    import backup
    return _safe(backup.backup)


@mcp.tool()
def page_add(title: str, room: str | None = None, category: str | None = None) -> dict:
    """Adds a new, empty program page (after the last page), optionally with room and category."""
    return _safe(lambda: _editor().add_page(title, room, category))


@mcp.tool()
def notification_add(title: str, text: str, source: str, page: str, source_key: str | None = None,
                     min_time_ms: int = 5000) -> dict:
    """Adds a Loxone notification (push/message in the Loxone app) and wires 'source' to it on 'page'. 'text' is the message sent on a rising edge of 'source'."""
    return _safe(lambda: _editor().add_notification(title, text, source, page, source_key, min_time_ms))


@mcp.tool()
def project_lint() -> dict:
    """Pre-load check of the whole project (or working copy): errors Loxone Config drops silently (invalid placeholder outputs, dangling connections, placeholders without target) plus blocks outside the sheet, overlaps, titles with < or %, placeholders without connection."""
    from analyze import analyze as _an, lint_layout
    def run():
        pr = _current()
        found = [f for f in _an(pr) if f["stufe"] == "fehler"] + lint_layout(pr)
        seen, uniq = set(), []
        for f in found:
            k = (f["art"], f["ort"], f["befund"])
            if k not in seen:
                seen.add(k)
                uniq.append(f)
        from collections import Counter as _C
        return {"anzahl": len(uniq), "je_art": dict(_C(f["art"] for f in uniq)), "befunde": uniq[:60],
                "hinweis": "Überlappungen in Bestandsseiten können Absicht sein; neue Bausteine sollten keine haben."}
    return _safe(run)


@mcp.tool()
def block_set_position(block: str, x: int, y: int, page: str | None = None) -> dict:
    """Moves a block or placeholder on its page (x/y in Loxone units, grid 96; the sheet extends to about 23,400 x 16,300)."""
    return _safe(lambda: _editor().set_position(block, x, y, page))


@mcp.tool()
def block_clone(block: str, page: str, title: str, source_page: str | None = None) -> dict:
    """Adds a copy of an existing block with all settings and sub-objects to a page, without its connections. This also creates large blocks such as lighting controller, room controller or automatic blinds that block_add cannot create. Then wire it (block_connect) and check the settings."""
    return _safe(lambda: _editor().clone_block(block, page, title, source_page))


@mcp.tool()
def io_set_statistics(block: str, enabled: bool = True) -> dict:
    """Enables statistics for an analog input or memory (on every change, accumulated - as Loxone Config does for measurements) or disables it."""
    return _safe(lambda: _editor().set_statistics(block, "jede_aenderung" if enabled else "aus"))


@mcp.tool()
def icon_find(query: str) -> dict:
    """Searches the Loxone icon library by name or keyword (German keywords work best), e.g. 'sonne', 'regen', 'thermometer'. Returns name and UUID for state_add."""
    import icons
    return _safe(lambda: {"treffer": icons.find(query)} if icons.library() else
                 {"fehler": "Symbolbibliothek nicht gefunden (ProgramData\\Loxone\\Loxone Config *\\IconLibrary.zip)."})


@mcp.tool()
def state_add(page: str, title: str, source: str, states: list[dict], compare: str = "gleich",
              default_text: str = "", default_icon: str | None = None, default_color: str | None = None,
              room: str | None = None, category: str | None = None, visu: bool = True) -> dict:
    """Adds a status block that shows its own text, icon and colour per state in the app, and wires 'source' to I1 (placeholder created if needed). states: [{"wert": 1, "text": "Sunny", "symbol": "sun", "farbe": "#ffc800"}, ...]. compare='gleich': state applies if I1 equals wert; compare='ab': I1 >= wert, checked top to bottom (largest threshold first). Text may contain <v1> or <v1.1> (value of I1). default_* applies if no state matches. Room/category default to those of 'source'."""
    return _safe(lambda: _editor().add_state(page, title, source, states, compare, default_text, default_icon,
                                             default_color, None, room, category, visu))


@mcp.tool()
def io_add_command(title: str, cmd_on: str, cmd_off: str | None = None, parent: str = "Home Assistant",
                   source: str | None = None, page: str | None = None, source_key: str | None = None) -> dict:
    """Adds a virtual output command under an existing virtual output (default 'Home Assistant'), e.g. a push: cmd_on='/api/webhook/<your-webhook-id>?msg=Text'. With source and page it is wired there right away (output placeholder)."""
    return _safe(lambda: _editor().add_command(title, cmd_on, cmd_off, parent, source, page, source_key))


@mcp.tool()
def rs232_add(kind: str, title: str, code: str, device: str | None = None, code_off: str | None = None,
              analog: bool | None = None) -> dict:
    """Adds a command (kind='befehl', code = command bytes) or a sensor (kind='sensor', code = check pattern such as \\x07\\xF0\\x00\\xDE\\x14\\s3\\1) under an RS232 extension, modelled on existing objects there. Wire it on pages with ref_add/block_connect."""
    return _safe(lambda: _editor().add_rs232(kind, title, code, device, code_off, analog))


@mcp.tool()
def io_set_display(block: str, visible: bool | None = None, unit: str | None = None) -> dict:
    """Shows or hides an input, memory or constant in the app and sets its display format, e.g. '<v.1>°' or '<v>%'."""
    return _safe(lambda: _editor().set_display(block, visible, unit))


@mcp.tool()
def state_style(block: str, styles: list[dict], base_icon: str | None = None, page: str | None = None) -> dict:
    """Sets colour and icon of individual states of an existing status block (app display).
    styles: [{"text": "offen", "farbe": "#ff0054", "symbol": "window-open"}, ...] or {"nr": 1, ...};
    base_icon = base icon of the tile. Find icons with icon_find."""
    return _safe(lambda: _editor().set_state_style(block, styles, base_icon, page))


@mcp.tool()
def block_set_room_category(block: str, room: str | None = None, category: str | None = None,
                            page: str | None = None) -> dict:
    """Sets room and/or category of a single object (block, input, output)."""
    return _safe(lambda: _editor().set_room_category(block, room, category, page))


@mcp.tool()
def app_set(block: str, favorite: bool | None = None, rating: int | None = None, icon: str | None = None,
            page: str | None = None) -> dict:
    """App properties of an object: favourite, order (rating 0-10, higher = further up), icon.
    Change the app name of virtual inputs via the description (block_set_params Desc), not the title -
    Home Assistant addresses them by title."""
    return _safe(lambda: _editor().set_app(block, favorite, rating, icon, page))


@mcp.tool()
def structure_favorite(kind: str, name: str, on: bool = True) -> dict:
    """Sets or removes a room (kind='raum') or category (kind='kategorie') as favourite in the app."""
    return _safe(lambda: _editor().set_structure_favorite(kind, name, on))


@mcp.tool()
def heating_schedule_set(block: str, comfort: dict, page: str | None = None) -> dict:
    """Sets the heating schedule of an Intelligent Room Controller V2. comfort: {"1": [["15:00","22:00"]], ...}
    with day 1 = Monday ... 7 = Sunday; all times without an entry are eco. The schedule is replaced completely."""
    return _safe(lambda: _editor().set_heating_schedule(block, {int(k): v for k, v in comfort.items()}, page))


@mcp.tool()
def schedule_set(block: str, days: dict, page: str | None = None) -> dict:
    """Sets the times of a schedule (DayTimer) per weekday. days: {"1": [["05:35","05:40"]], "6": []}
    with day 1 = Monday ... 7 = Sunday; an empty list clears the day. Only the given days are replaced;
    special days and entries for all days stay. The output is on inside the time window.
    Note: assumes the usual list of 10 operating modes; check the schedule's modes first."""
    return _safe(lambda: _editor().set_schedule(block, {int(k): v for k, v in days.items()}, page))


@mcp.tool()
def page_color_by_function(page: str | None = None) -> dict:
    """Colours blocks and placeholders on program pages by function (category: lighting yellow,
    shading light blue, heating/climate orange, alarm red, ventilation violet, weather turquoise,
    access pink, irrigation green). Logic gets the colour of what it controls; colours set by hand
    stay. Without page: all pages."""
    return _safe(lambda: _editor().color_by_function(page))


@mcp.tool()
def periphery_remove(block: str) -> dict:
    """Deletes an unused virtual input/output, an empty virtual output, a constant or the weather server. Refuses while the object is still used anywhere; permission entries are removed with it."""
    return _safe(lambda: _editor().remove_periphery(block))


@mcp.tool()
def structure_list(kind: str) -> dict:
    """Lists all rooms (kind='raum') or categories (kind='kategorie') with their number of assignments."""
    return _safe(lambda: _editor().list_structure(kind))


@mcp.tool()
def structure_remove(kind: str, name: str) -> dict:
    """Deletes an empty room or category including its permission group."""
    return _safe(lambda: _editor().remove_structure(kind, name))


@mcp.tool()
def structure_reassign(kind: str, source: str, target: str) -> dict:
    """Moves all assignments from one room/category to another."""
    return _safe(lambda: _editor().reassign_structure(kind, source, target))


@mcp.tool()
def block_remove(block: str, page: str | None = None) -> dict:
    """Deletes a block or placeholder from a page together with its connections."""
    return _safe(lambda: _editor().remove_block(block, page))


@mcp.tool()
def block_addable_types() -> dict:
    """Lists the block types that block_add can create safely."""
    def run():
        types = sorted(n for n, t in _cat().types.items() if Editor._addable(n, t) is None)
        return {"anzahl": len(types), "typen": types}
    return _safe(run)


@mcp.tool()
def project_changes() -> dict:
    """Lists all changes of the working copy compared with the opened file (new/removed blocks, connections, values)."""
    def run():
        if _edit is None or not _edit.log:
            return {"aenderungen": 0, "hinweis": "Es gibt keine ungespeicherten Änderungen."}
        return _edit.changes()
    return _safe(run)


@mcp.tool()
def project_validate() -> dict:
    """Checks the working copy for problems introduced by the changes (duplicate IDs, broken references, permissions) and warns about layout issues of new blocks."""
    def run():
        if _edit is None:
            return {"in_ordnung": True, "fehler": [], "hinweis": "Es gibt keine ungespeicherten Änderungen."}
        return _edit.validate()
    return _safe(run)


@mcp.tool()
def project_undo() -> dict:
    """Undoes the last change of the working copy."""
    def run():
        if _edit is None:
            raise ProjectError("Es gibt nichts rückgängig zu machen.")
        return _edit.undo()
    return _safe(run)


@mcp.tool()
def project_discard() -> dict:
    """Discards all unsaved changes of the working copy."""
    def run():
        global _edit
        n = len(_edit.log) if _edit else 0
        _edit = None
        return {"verworfen": n}
    return _safe(run)


@mcp.tool()
def project_save(filename: str) -> dict:
    """Saves the working copy as a NEW file in the project folder (never overwrites). Validation runs first; with errors nothing is saved. The project timestamp is set to now. Open the new file in Loxone Config, check it and load it into the Miniserver yourself."""
    def run():
        global _edit, _project
        if _edit is None or not _edit.log:
            raise ProjectError("Es gibt keine Änderungen zu speichern.")
        name = Path(filename).name
        if not name.lower().endswith(".loxone"):
            name += ".Loxone"
        result = _edit.save(_projects_dir() / name)
        result["aenderungsliste"] = _edit.changes()
        _project = Project(_projects_dir() / name)
        _edit = None
        a = autosave_report(_project)
        if a:
            result["achtung"] = ("Es gibt eine automatische Sicherung von Loxone Config für dieses Projekt. Beim Öffnen "
                                 "der neuen Datei die Rückfrage zur automatischen Sicherung ablehnen, sonst lädt Config "
                                 "den alten Stand. Am sichersten: Config vorher ohne Speichern schließen.")
        return result
    return _safe(run)


# ---------- Abgleich ----------
@mcp.tool()
def sync_check(miniserver_xml_path: str | None = None, ask_miniserver: bool = True) -> dict:
    """Compares project file, Loxone Config autosave and Miniserver program (pass miniserver_xml_path, e.g. the Miniserver-Backup file, for a content comparison). Call before every change."""
    def run():
        ms = None
        if ask_miniserver:
            try:
                ms = Miniserver()
            except ProjectError:
                ms = None
        return sync_report(_current(), ms, miniserver_xml_path)
    return _safe(run)


# ---------- Analyse ----------
@mcp.tool()
def project_analyze(stufe: str | None = None) -> dict:
    """Analyses the project: errors, clean-up candidates and improvement suggestions (stufe = fehler | aufraeumen | verbesserung)."""
    return _safe(lambda: summarize(analyze(_current()), stufe))


# ---------- Livewerte (nur lesend) ----------
INPUT_TYPES = {"VirtualIn", "DigitalIn", "VirtualUdpInCmd", "VirtualHttpInCmd", "LoxAIRsensor", "LoxAIRAsensor",
               "TreeSensor", "TreeAsensor", "Lox1wireAsensor", "Lox232sensor", "AnalogIn", "VoltageIn", "Constant"}
MULTI_OUT = {"LightController2", "AutoJalousie", "HeatIRoomController2", "CentralShade"}


@mcp.tool()
def net_check_outputs() -> dict:
    """Checks all virtual outputs with a network address (tcp://, http://, udp://): ping and port.
    Finds devices whose IP has changed (e.g. Yeelight, Shelly). Read-only, sends no commands."""
    import netcheck
    def run():
        rows = netcheck.check_outputs(_current())
        return {"ausgaenge": rows, "nicht_erreichbar": [r["ausgang"] for r in rows if not r["ok"]]}
    return _safe(run)


def _lan(network: str | None) -> str:
    import netcheck
    host = None
    h = os.environ.get("LOXONE_HOST", "")
    if h:
        from urllib.parse import urlsplit
        host = urlsplit(h if "://" in h else "http://" + h).hostname
    return network or netcheck.local_network(host)


@mcp.tool()
def yeelight_find(network: str | None = None) -> dict:
    """Searches the LAN for Yeelight lamps (port 55443, LAN control must be on) and reads their state
    (on/off, brightness, colour temperature) and MAC address. network e.g. '192.168.1.0/24';
    default: the Miniserver's network. Read-only."""
    import netcheck
    def run():
        net = _lan(network)
        return {"netz": net, "lampen": netcheck.yeelight_find(net),
                "hinweis": "Für Loxone eine feste IP im Router vergeben (DHCP-Reservierung über die MAC)."}
    return _safe(run)


@mcp.tool()
def net_find_device(mac: str | None = None, network: str | None = None) -> dict:
    """Finds a device in the LAN by MAC address (e.g. from the type plate) and checks typical control
    ports (Yeelight 55443, HTTP 80/8081, MQTT). Without mac: list of all devices with IP and MAC. Read-only."""
    import netcheck
    def run():
        net = _lan(network)
        hits = netcheck.find_device(net, mac)
        res = {"netz": net, "treffer": hits}
        if mac and not hits:
            res["hinweis"] = "Gerät nicht im Netz: Strom, WLAN-Verbindung (App des Herstellers) prüfen."
        return res
    return _safe(run)


@mcp.tool()
def mijia_find(network: str | None = None) -> dict:
    """Searches the LAN for Xiaomi/Mijia/Yeelight devices via miIO (UDP 54321) and shows IP, device ID
    (hex = ID in Home Assistant/Yeelight) and MAC. Read-only, no token needed. To set up new devices
    without the app: mijia_setup.py on a computer with Wi-Fi (the user types the password)."""
    import mijia
    import netcheck
    def run():
        net = _lan(network)
        arp = netcheck._arp_table()
        hits = [{"ip": d["ip"], "geraete_id": hex(d["did"]), "mac": arp.get(d["ip"]),
                 "einrichtungsmodus": bool(d["token"])} for d in mijia.sweep(net)]
        return {"netz": net, "geraete": hits}
    return _safe(run)


@mcp.tool()
def live_status() -> dict:
    """Read-only Miniserver status: last program change, CPU, memory, time."""
    return _safe(lambda: Miniserver().status())


@mcp.tool()
def live_page(page: str, max_values: int = 30) -> dict:
    """Current values of the blocks on a page (read only, limited number of requests)."""
    def run():
        pr = _current()
        pg = pr.find_page(page)
        ms = Miniserver()
        blocks = [pr.blocks[c.get("U")] for c in pg.elem.findall("C") if c.get("U") in pr.blocks]
        blocks.sort(key=lambda b: (int(b.elem.get("Px", "0") or 0), int(b.elem.get("Py", "0") or 0)))
        cache: dict[str, object] = {}
        rows, skipped = [], []
        limit = max(1, min(int(max_values), 38))
        for b in blocks:
            t = pr.target(b) or b
            if t.uuid not in cache:
                if len(cache) >= limit:
                    skipped.append(b.title)
                    continue
                try:
                    if t.type in TEXT_INPUTS:
                        cache[t.uuid] = TEXT_INPUT_NOTE
                    elif t.type in SCALED_ACTORS:
                        cache[t.uuid] = _actor_value(ms, t)
                    elif t.type in MULTI_OUT:
                        cache[t.uuid] = ms.outputs(t.uuid)
                    else:
                        cache[t.uuid] = ms.value(t.uuid, output=t.type not in INPUT_TYPES)
                except ProjectError as e:
                    cache[t.uuid] = f"nicht lesbar ({e})"
            rows.append({"baustein": ("Platzhalter für " if t is not b else "") + pr.name(t), "wert": cache[t.uuid]})
        out = {"seite": pg.title, "werte": rows, "abfragen": ms.requests}
        if skipped:
            out["nicht_abgefragt"] = skipped
            out["hinweis"] = "Zum Schutz des Miniservers abgebrochen; einzelne Bausteine mit live_block lesen."
        return out
    return _safe(run)


@mcp.tool()
def live_block(block: str, page: str | None = None) -> dict:
    """Current value of a block or device; for large blocks all outputs. Read only."""
    def run():
        pr = _current()
        b = pr.logical(pr.resolve(block, page))
        if b.type in TEXT_INPUTS:
            return {"baustein": pr.name(b), "ort": pr.location(b), "wert": TEXT_INPUT_NOTE}
        ms = Miniserver()
        if b.type in SCALED_ACTORS:
            val = _actor_value(ms, b)
        else:
            val = ms.outputs(b.uuid) if b.type in MULTI_OUT else ms.value(b.uuid, output=b.type not in INPUT_TYPES)
        return {"baustein": pr.name(b), "ort": pr.location(b), "wert": val}
    return _safe(run)


# Analoge Tree-Stellantriebe: 'astate' liefert am Miniserver immer 0 %, 'state' den Eingangswert (z. B. 0–10
# vom Raumregler). Ausgegeben wird der Eingang umgerechnet mit SourceValHigh -> DestValHigh (beobachtet 22.11.).
SCALED_ACTORS = {"TreeAactor"}


def _actor_value(ms, b) -> str:
    import re as _re
    raw = ms.value(b.uuid, output=False)
    m = _re.search(r"-?\d+(?:[.,]\d+)?", str(raw))
    if not m:
        return str(raw)
    x = float(m.group(0).replace(",", "."))
    p = {k: float(b.elem.get(k) or 0) for k in ("SourceValLow", "SourceValHigh", "DestValLow", "DestValHigh")}
    if p["SourceValHigh"] == p["SourceValLow"]:
        return str(raw)
    y = p["DestValLow"] + (x - p["SourceValLow"]) * (p["DestValHigh"] - p["DestValLow"]) / (p["SourceValHigh"] - p["SourceValLow"])
    return f"{round(y)} % (Eingang {x:g})"


# Bei virtuellen Texteingängen SETZT /jdev/sps/io/<name>/state (bzw. astate/all) den Text auf
# dieses Wort, statt ihn zu lesen (am 2026-11-17 am Miniserver beobachtet). Nie abfragen.
TEXT_INPUTS = {"VirtualTextIn"}
TEXT_INPUT_NOTE = ("nicht abgefragt: Texteingang – eine Abfrage per /state würde den Text überschreiben; "
                   "Wert in der App ansehen")


@mcp.tool()
def live_history(block: str, page: str | None = None) -> dict:
    """History of a block (e.g. why a blind moved: trigger and time). Read only."""
    def run():
        pr = _current()
        b = pr.logical(pr.resolve(block, page))
        return {"baustein": pr.name(b), "ort": pr.location(b), "verlauf": Miniserver().history(b.uuid)}
    return _safe(run)


# ---------- Funktionsvorlagen ----------
@mcp.tool()
def recipe_list() -> dict:
    """Lists the available recipes (complete functions that can be applied in one step)."""
    return {"vorlagen": RECIPES}


@mcp.tool()
def recipe_apply(name: str, params: dict | None = None) -> dict:
    """Applies a recipe with its parameters to the working copy (all or nothing)."""
    return _safe(lambda: Recipes(_editor()).apply(name, params or {}))


if __name__ == "__main__":
    mcp.run()
