"""Lade-Assistent und Abgleich beim Öffnen.

verify_load: nach dem Laden in Config warten, bis der Miniserver wieder antwortet, das laufende
Programm sichern (backup.py) und mit der erwarteten Projektdatei vergleichen. Auf Wunsch danach
ältere Versionen des Hauptprojekts in den Papierkorb legen.

open_check: beim Öffnen eines Projekts mit der letzten Miniserver-Sicherung und der
automatischen Sicherung von Config abgleichen.
"""
from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import backup
from live import Miniserver
from loxproject import Project, ProjectError
from sync import autosave_report, diff_projects


def _ms_version(timeout_s: int) -> tuple[str | None, int]:
    """Wartet, bis der Miniserver antwortet; gibt (LoxAPPversion3, Wartesekunden) zurück."""
    t0 = time.time()
    last_err = None
    while time.time() - t0 <= timeout_s:
        try:
            ll = Miniserver().ll("sps/LoxAPPversion3")
            return str(ll.get("value")), int(time.time() - t0)
        except ProjectError as e:  # 503 während des Neustarts, kurz nicht erreichbar
            last_err = e
            time.sleep(10)
    raise ProjectError(f"Miniserver antwortet nach {timeout_s} s nicht ({last_err}).")


def _short_diff(d: dict) -> dict:
    return {k: (v[:12] if isinstance(v, list) else v) for k, v in d.items() if k != "gleich"}


def recycle(paths: list[Path]) -> list[str]:
    """Dateien in den Papierkorb legen (nicht endgültig löschen). Unter Windows der echte Papierkorb,
    sonst ein Ordner '.loxone-papierkorb' neben den Dateien."""
    import os
    import shutil
    done = []
    if os.name != "nt":
        for p in paths:
            trash = p.parent / ".loxone-papierkorb"
            trash.mkdir(exist_ok=True)
            shutil.move(str(p), str(trash / p.name))
            done.append(p.name)
        return done
    for p in paths:
        # Pfad als PowerShell-Literal einbetten (-Command reicht keine $args weiter)
        lit = "'" + str(p).replace("'", "''") + "'"
        ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
              f"[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile({lit},'OnlyErrorDialogs','SendToRecycleBin')")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
        if r.returncode == 0 and not p.exists():
            done.append(p.name)
    return done


def verify_load(expected: Path, main_files: list[Path], cleanup: bool = False, timeout_s: int = 180) -> dict:
    version, waited = _ms_version(timeout_s)
    exp = Project(expected)
    doc = exp.root.find("C[@Type='Document']")
    exp_date = doc.get("Date") if doc is not None else None
    b = backup.backup()
    ms = Project(Path(b["ordner"]) / "Miniserver.Loxone")
    d = diff_projects(exp, ms)
    out = {"miniserver_programm_seit": version, "gewartet_s": waited, "erwartet": expected.name,
           "projektzeit_datei": exp_date, "sicherung": {k: b[k] for k in ("geaendert", "datei") if k in b}}
    older = bool(version and exp_date and str(version)[:19] < str(exp_date)[:19])
    if d.get("gleich") and older:
        # Inhalt passt (Heizpläne, Werte …), aber der Miniserver hat die Datei nicht nach ihrem Speichern bekommen
        out["ergebnis"] = (f"ACHTUNG – das Programm im Miniserver ({version}) ist älter als {expected.name} ({exp_date}). "
                           "Vermutlich wurde die Datei noch nicht geladen.")
    elif d.get("gleich"):
        out["ergebnis"] = f"OK – im Miniserver läuft {expected.name}."
        if cleanup:
            old = [p for p in main_files if p != expected]
            old += [p.with_name(p.name + ".backup") for p in main_files if p.with_name(p.name + ".backup").exists()]
            out["papierkorb"] = recycle([p for p in old if p.exists()])
    else:
        out["ergebnis"] = (f"ACHTUNG – im Miniserver läuft NICHT {expected.name}. Häufige Ursachen: in Config die "
                           "automatische Sicherung angenommen, ältere Datei geladen, oder Projektzeit der Datei älter "
                           "als das Programm im Miniserver.")
        out["unterschiede_miniserver_zu_datei"] = _short_diff(diff_projects(ms, exp))
    return out


def open_check(p: Project) -> dict:
    """Abgleich beim Öffnen: Miniserver-Sicherung und automatische Sicherung von Config."""
    out: dict = {}
    ms_file = backup.DEFAULT_DIR / "Miniserver.Loxone"
    if ms_file.is_file():
        when = datetime.fromtimestamp(ms_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        try:
            d = diff_projects(Project(ms_file), p)
            out["miniserver_sicherung"] = {"stand": when, "gleich": d.get("gleich", False)}
            if not d.get("gleich"):
                out["miniserver_sicherung"]["unterschiede"] = _short_diff(d)
                out["miniserver_sicherung"]["hinweis"] = ("Datei weicht vom letzten gesicherten Miniserver-Programm ab "
                                                          "(eigene Änderungen in Config oder noch nicht geladen). "
                                                          "Mit backup_miniserver den aktuellen Stand holen.")
        except Exception as e:
            out["miniserver_sicherung"] = f"Vergleich nicht möglich: {e}"
    else:
        out["miniserver_sicherung"] = "noch keine – backup_miniserver aufrufen"
    a = autosave_report(p)
    if a and not a.get("gleich_wie_projektdatei", True):
        out["automatische_sicherung"] = a.get("warnung")
    return out
