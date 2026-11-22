"""Netzwerk-Prüfungen (nur lesen): Erreichbarkeit der Adressen virtueller Ausgänge, Yeelight-Suche im LAN."""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from loxproject import Project

DEFAULT_PORTS = {"http": 80, "https": 443}


def parse_address(addr: str) -> tuple[str, str, int | None] | None:
    """'tcp://192.168.1.5:55443' -> ('tcp', '192.168.1.5', 55443); 'http://host' -> ('http', 'host', 80)."""
    if not addr or "://" not in addr:
        return None
    u = urlsplit(addr.strip())
    if not u.hostname:
        return None
    try:
        port = u.port
    except ValueError:
        port = None
    return u.scheme.lower(), u.hostname, port or DEFAULT_PORTS.get(u.scheme.lower())


def tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ping(host: str) -> bool:
    flag = "-n" if sys.platform.startswith("win") else "-c"
    try:
        r = subprocess.run(["ping", flag, "1", "-w" if sys.platform.startswith("win") else "-W",
                            "1000" if sys.platform.startswith("win") else "1", host],
                           capture_output=True, timeout=5)
        return r.returncode == 0 and b"TTL=" in r.stdout.upper()
    except (OSError, subprocess.TimeoutExpired):
        return False


def check_outputs(p: Project) -> list[dict]:
    """Alle virtuellen Ausgänge mit Adresse: Ping und (bei tcp/http) Port-Test."""
    rows = []
    for b in p.blocks.values():
        if b.type != "VirtualOut":
            continue
        parsed = parse_address(b.elem.get("Address", ""))
        if not parsed:
            continue
        rows.append((b, *parsed))

    def one(row):
        b, scheme, host, port = row
        res = {"ausgang": b.title, "adresse": b.elem.get("Address"), "ping": ping(host)}
        if scheme in ("tcp", "http", "https") and port:
            res["port_offen"] = tcp_open(host, port)
        res["befehle"] = sum(1 for c in b.elem.findall("C"))
        res["ok"] = res.get("port_offen", res["ping"])
        return res
    with ThreadPoolExecutor(16) as ex:
        out = list(ex.map(one, rows))
    return sorted(out, key=lambda r: (r["ok"], r["ausgang"]))


def _arp_table() -> dict[str, str]:
    try:
        txt = subprocess.run(["arp", "-a"], capture_output=True, timeout=5).stdout.decode(errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return {m.group(1): m.group(2).replace("-", ":").lower()
            for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})", txt)}


def local_network(hint_host: str | None = None) -> str:
    """/24-Netz des Hinweis-Hosts (z. B. Miniserver) oder der eigenen Adresse."""
    ip = None
    if hint_host:
        try:
            ip = socket.gethostbyname(hint_host)
        except OSError:
            ip = None
    if not ip:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
    return str(ipaddress.ip_network(ip + "/24", strict=False))


KNOWN_PORTS = {55443: "Yeelight LAN", 80: "HTTP", 8081: "HTTP (Tasmota/Shelly)", 1883: "MQTT"}


def find_device(network: str, mac: str | None = None) -> list[dict]:
    """Ping an alle Adressen im Netz (füllt die ARP-Tabelle), dann Gerät(e) per MAC suchen.
    Ohne mac: alle Geräte. Je Treffer werden typische Steuer-Ports geprüft. Nur lesen."""
    net = ipaddress.ip_network(network, strict=False)
    if net.num_addresses > 1024:
        raise ValueError("Bitte höchstens ein /22-Netz durchsuchen.")
    with ThreadPoolExecutor(64) as ex:
        list(ex.map(lambda ip: ping(str(ip)), net.hosts()))
    arp = _arp_table()
    want = mac.lower().replace("-", ":") if mac else None
    hits = [(ip, m) for ip, m in arp.items() if ipaddress.ip_address(ip) in net and (want is None or m == want)]
    out = []
    for ip, m in sorted(hits, key=lambda x: ipaddress.ip_address(x[0])):
        ports = [f"{p} {n}" for p, n in KNOWN_PORTS.items() if tcp_open(ip, p, 0.6)] if want else None
        out.append({"ip": ip, "mac": m, **({"offene_ports": ports} if ports is not None else {})})
    return out


def yeelight_find(network: str) -> list[dict]:
    """Sucht Yeelights (offener Port 55443) und liest Zustand per get_prop – ändert nichts."""
    net = ipaddress.ip_network(network, strict=False)
    if net.num_addresses > 1024:
        raise ValueError("Bitte höchstens ein /22-Netz durchsuchen.")

    def probe(ip):
        ip = str(ip)
        try:
            with socket.create_connection((ip, 55443), timeout=0.8) as s:
                s.settimeout(1.5)
                s.sendall(b'{"id":1,"method":"get_prop","params":["power","bright","ct","name","color_mode"]}\r\n')
                data = s.recv(1024).decode(errors="replace")
        except OSError:
            return None
        m = re.search(r'"result"\s*:\s*\[([^\]]*)\]', data)
        if not m:
            return None
        vals = [v.strip().strip('"') for v in m.group(1).split(",")]
        keys = ["an", "helligkeit", "farbtemperatur", "name", "farbmodus"]
        return {"ip": ip, **dict(zip(keys, vals))}
    with ThreadPoolExecutor(64) as ex:
        hits = [h for h in ex.map(probe, net.hosts()) if h]
    arp = _arp_table()
    for h in hits:
        h["an"] = h.get("an") == "on"
        h["mac"] = arp.get(h["ip"])
    return hits
