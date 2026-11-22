"""Xiaomi/Mijia miIO-Protokoll (UDP 54321) – Suche, Befehle, WLAN-Einrichtung.

Paket: 0x2131 | Länge (2) | 0 (4) | Geräte-ID (4) | Zeitstempel (4) | Prüfsumme bzw. Token (16) | AES-Daten.
Schlüssel = md5(token), IV = md5(Schlüssel + token), AES-128-CBC mit PKCS7.
Ein frisch zurückgesetztes Gerät (eigener Hotspot, meist 192.168.13.1) verrät seinen Token in der
Hello-Antwort; eingerichtete Geräte antworten dort mit ffff…/0000….
"""
from __future__ import annotations

import hashlib
import json
import socket
import struct
import time
from pathlib import Path

PORT = 54321
HELLO = bytes.fromhex("21310020" + "ff" * 28)
TOKEN_FILE = Path.home() / ".loxone-mijia-tokens.json"   # Geräte-Token lokal, nie ins Repository


def _md5(b: bytes) -> bytes:
    return hashlib.md5(b).digest()


def _cipher(token: bytes):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = _md5(token)
    return Cipher(algorithms.AES(key), modes.CBC(_md5(key + token)))


def encrypt(token: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives import padding
    p = padding.PKCS7(128).padder()
    raw = p.update(data) + p.finalize()
    e = _cipher(token).encryptor()
    return e.update(raw) + e.finalize()


def decrypt(token: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives import padding
    d = _cipher(token).decryptor()
    raw = d.update(data) + d.finalize()
    u = padding.PKCS7(128).unpadder()
    return u.update(raw) + u.finalize()


def build(token: bytes, did: int, stamp: int, payload: bytes) -> bytes:
    enc = encrypt(token, payload)
    head = struct.pack(">HHIII", 0x2131, 32 + len(enc), 0, did, stamp)
    return head + _md5(head + token + enc) + enc


def parse(packet: bytes, token: bytes | None = None) -> dict:
    magic, length, _unk, did, stamp = struct.unpack(">HHIII", packet[:16])
    if magic != 0x2131:
        raise ValueError("kein miIO-Paket")
    check = packet[16:32]
    res = {"did": did, "stamp": stamp, "laenge": length}
    if length == 32:                                   # Hello-Antwort
        res["token"] = None if check in (b"\xff" * 16, b"\x00" * 16) else check.hex()
        return res
    if token is None:
        raise ValueError("Antwort ist verschlüsselt, Token nötig")
    if _md5(packet[:16] + token + packet[32:length]) != check:
        raise ValueError("Prüfsumme falsch (falscher Token?)")
    res["daten"] = json.loads(decrypt(token, packet[32:length]).rstrip(b"\x00").decode())
    return res


def discover(target: str = "255.255.255.255", timeout: float = 3.0) -> list[dict]:
    """Hello an ein Gerät oder per Broadcast; antwortende Geräte mit ID (und Token, falls unkonfiguriert)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)
    found: dict[str, dict] = {}
    end = time.time() + timeout
    s.sendto(HELLO, (target, PORT))
    while time.time() < end:
        try:
            data, addr = s.recvfrom(1024)
        except (socket.timeout, ConnectionResetError):   # Windows: ICMP 'Port nicht erreichbar'
            continue
        try:
            found[addr[0]] = {"ip": addr[0], **parse(data)}
        except ValueError:
            pass
    s.close()
    return list(found.values())


def sweep(network: str, timeout: float = 3.0) -> list[dict]:
    """Hello an jede Adresse eines Netzes (Broadcast wird oft von der Firewall verschluckt)."""
    import ipaddress
    net = ipaddress.ip_network(network, strict=False)
    if net.num_addresses > 1024:
        raise ValueError("Bitte höchstens ein /22-Netz durchsuchen.")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        s.ioctl(socket.SIO_UDP_CONNRESET, False)
    s.settimeout(0.3)
    for ip in net.hosts():
        s.sendto(HELLO, (str(ip), PORT))
    found: dict[str, dict] = {}
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = s.recvfrom(1024)
        except (socket.timeout, ConnectionResetError):   # Windows: ICMP 'Port nicht erreichbar'
            continue
        try:
            found[addr[0]] = {"ip": addr[0], **parse(data)}
        except ValueError:
            pass
    s.close()
    return sorted(found.values(), key=lambda h: ipaddress.ip_address(h["ip"]))


def send(ip: str, token_hex: str, method: str, params, timeout: float = 5.0):
    """Befehl an ein Gerät (Token als Hex). Gibt 'result' zurück oder wirft bei 'error'."""
    token = bytes.fromhex(token_hex)
    hello = discover(ip, 2.0)
    if not hello:
        raise TimeoutError(f"{ip} antwortet nicht auf miIO (UDP {PORT}).")
    did, stamp = hello[0]["did"], hello[0]["stamp"]
    msg = json.dumps({"id": int(time.time()) % 100000, "method": method, "params": params}).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(build(token, did, stamp + 1, msg), (ip, PORT))
        data, _ = s.recvfrom(4096)
    finally:
        s.close()
    ans = parse(data, token)["daten"]
    if "error" in ans:
        raise RuntimeError(f"Gerät meldet Fehler: {ans['error']}")
    return ans.get("result")


def save_token(did: int, token_hex: str, note: str = "") -> None:
    d = json.loads(TOKEN_FILE.read_text(encoding="utf-8")) if TOKEN_FILE.exists() else {}
    d[str(did)] = {"token": token_hex, "notiz": note, "gespeichert": time.strftime("%Y-%m-%d %H:%M")}
    TOKEN_FILE.write_text(json.dumps(d, indent=1), encoding="utf-8")


def load_token(did: int) -> str | None:
    if not TOKEN_FILE.exists():
        return None
    return json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get(str(did), {}).get("token")


def configure_wifi(ip: str, token_hex: str, ssid: str, password: str) -> object:
    """Schickt einem Gerät im Einrichtungsmodus die WLAN-Daten (wie die Mi-Home-App)."""
    return send(ip, token_hex, "miIO.config_router", {"ssid": ssid, "passwd": password, "uid": 0})


def enable_yeelight_lan(ip: str, token_hex: str) -> object:
    """Schaltet bei Yeelight-Geräten die LAN-Steuerung (Port 55443) ein."""
    return send(ip, token_hex, "set_ps", ["cfg_lan_ctrl", "1"])
