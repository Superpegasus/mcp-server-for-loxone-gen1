"""Mijia/Yeelight-Lampe ohne App einrichten. Muss auf einem Rechner MIT WLAN laufen.

  uv run python mijia_setup.py suchen [ip]        Geräte im Netz (oder eine IP) per miIO finden
  uv run python mijia_setup.py einrichten         Rechner vorher mit dem Hotspot der Lampe verbinden
                                                  (z. B. 'yeelink-light-…'); fragt WLAN-Name und Passwort ab
  uv run python mijia_setup.py token <ip>         Token eines schon eingerichteten Geräts eintippen (unsichtbar),
                                                  wird geprüft und lokal gespeichert
  uv run python mijia_setup.py lan <ip>           nach dem Einrichten: Yeelight-LAN-Steuerung einschalten

Das WLAN-Passwort wird nur hier eingetippt (unsichtbar) und direkt an die Lampe geschickt – es wird
nirgends gespeichert. Der Geräte-Token landet in ~/.loxone-mijia-tokens.json (nicht im Repository).
"""
import getpass
import sys

import mijia


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "suchen"
    if cmd == "suchen":
        hits = mijia.discover(argv[2] if len(argv) > 2 else "255.255.255.255")
        for h in hits:
            print(f"{h['ip']:16} Geräte-ID {h['did']}  {'Einrichtungsmodus (Token bekannt)' if h['token'] else 'eingerichtet'}")
        print(f"{len(hits)} Gerät(e).")
    elif cmd == "einrichten":
        ip = argv[2] if len(argv) > 2 else "192.168.13.1"
        hits = mijia.discover(ip, 4.0)
        if not hits:
            sys.exit(f"Keine Antwort von {ip}. Ist der Rechner mit dem Hotspot der Lampe verbunden?")
        h = hits[0]
        if not h["token"]:
            sys.exit("Gerät ist schon eingerichtet (gibt keinen Token heraus). Erst zurücksetzen.")
        mijia.save_token(h["did"], h["token"], "Tischlampe Ankleide")
        print(f"Lampe gefunden, Geräte-ID {h['did']}, Token gespeichert.")
        ssid = input("WLAN-Name (SSID) des Heimnetzes: ").strip()
        pw = getpass.getpass("WLAN-Passwort (wird nicht angezeigt): ")
        print("Sende WLAN-Daten …", mijia.configure_wifi(ip, h["token"], ssid, pw))
        print("Fertig. Die Lampe startet neu und verbindet sich. Rechner wieder ins Heimnetz, dann:")
        print("  uv run python mijia_setup.py suchen   und   uv run python mijia_setup.py lan <ip>")
    elif cmd == "token":
        ip = argv[2]
        h = mijia.discover(ip, 3.0)
        if not h:
            sys.exit(f"{ip} antwortet nicht.")
        tok = getpass.getpass("Token (32 Hex-Zeichen, wird nicht angezeigt): ").strip().lower()
        if len(tok) != 32:
            sys.exit("Token muss 32 Hex-Zeichen lang sein.")
        info = mijia.send(ip, tok, "miIO.info", [])       # prüft den Token
        mijia.save_token(h[0]["did"], tok, info.get("model", "") if isinstance(info, dict) else "")
        print("Token stimmt und ist gespeichert. Modell:", info.get("model") if isinstance(info, dict) else info)
    elif cmd == "lan":
        ip = argv[2]
        h = mijia.discover(ip, 3.0)
        if not h:
            sys.exit(f"{ip} antwortet nicht.")
        tok = mijia.load_token(h[0]["did"])
        if not tok:
            sys.exit("Kein gespeicherter Token für dieses Gerät.")
        print("LAN-Steuerung:", mijia.enable_yeelight_lan(ip, tok))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
