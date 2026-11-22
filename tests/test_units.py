"""Unit tests without a Loxone project: python tests/test_units.py"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import icons  # noqa: E402
from analyze import BAD_TITLE  # noqa: E402
from backup import decompress_loxcc  # noqa: E402
from loxproject import Project, ProjectError  # noqa: E402
import plan  # noqa: E402


def loxcc(block: bytes, usize: int) -> bytes:
    return struct.pack("<LLLL", 0xAABBCCEE, len(block), usize, 0) + block


def test_loxcc_literals_and_match():
    # token 0x34: 3 literals "abc", match length 4+4=8, offset 3 -> "abc" + "abcabcab", then 2 literals "XY"
    block = bytes([0x34]) + b"abc" + bytes([3, 0]) + bytes([0x20]) + b"XY"
    assert decompress_loxcc(loxcc(block, 13)) == b"abcabcabcabXY"


def test_loxcc_bad_header():
    try:
        decompress_loxcc(b"\x00" * 20)
    except ProjectError:
        return
    raise AssertionError("expected ProjectError")


def test_color_bgr():
    assert icons.color("#ff0000") == 255           # red -> R
    assert icons.color("#0000ff") == 255 * 65536   # blue -> B<<16
    assert icons.color("#70ff8d") == 0x70 + 0xFF * 256 + 0x8D * 65536


def test_bad_title():
    assert BAD_TITLE.search("Pos 70%") and BAD_TITLE.search("a<b")
    assert not BAD_TITLE.search("Fenster offen (Anzahl > 0)")


def test_plan_aliases():
    assert plan.ALIASES["connect"] == "verbinden" and plan.ALIASES["disconnect"] == "trennen"
    assert plan._first_hash({"angelegt": "X [And] #21a0b93a-0058-5f07"}) == "#21a0b93a-0058-5f07"


def test_parser_digit_attribute(tmp=Path(__file__).with_name("_tmp.Loxone")):
    xml = ('<?xml version="1.0" encoding="utf-8"?><ControlList><C Type="Document" U="1" Title="t" 12hTF="false">'
           '</C></ControlList>')
    tmp.write_text(xml, encoding="utf-8")
    try:
        Project(tmp)  # must not raise although 12hTF is not valid XML
    finally:
        tmp.unlink()


def test_netcheck_parse_address():
    import netcheck
    assert netcheck.parse_address("tcp://192.168.1.5:55443") == ("tcp", "192.168.1.5", 55443)
    assert netcheck.parse_address("http://host") == ("http", "host", 80)
    assert netcheck.parse_address("") is None and netcheck.parse_address("keine-url") is None


def test_mijia_packet_roundtrip():
    import mijia
    token = bytes.fromhex("00112233445566778899aabbccddeeff")
    payload = b'{"id":1,"method":"miIO.info","params":[]}'
    pkt = mijia.build(token, 0x1234, 99, payload)
    res = mijia.parse(pkt, token)
    assert res["did"] == 0x1234 and res["stamp"] == 99 and res["daten"]["method"] == "miIO.info"
    hello_reply = bytes.fromhex("21310020" + "00000000" + "00001234" + "00000063") + token
    assert mijia.parse(hello_reply)["token"] == token.hex()


def test_daytimer_entries_and_permission_refs():
    from writer import ProjectText
    uid = "a-1-ffff112233445566"
    nl = "\n"
    raw = nl.join([
        "<ControlList>", '\t<C Type="Document" U="d-1" NumO="3">',
        '\t\t<C Type="DayTimer" U="' + uid + '" Title="Uhr" Nio="1" UserModes="3" N="2" Modes="x">',
        '\t\t\t<Entry From="450" To="480" Ix="2" V="1"/>', '\t\t\t<Entry From="335" To="340" Ix="3" V="1"/>',
        '\t\t\t<IoData Visu="true"/>', "\t\t</C>",
        '\t\t<PH NUM="1">', '\t\t\t<PRM REF="' + uid + '" ACT="3"/>', "\t\t</PH>",
        "\t</C>", "</ControlList>", ""]).encode()
    pt = ProjectText(raw)
    pt.set_daytimer_entries(uid, [(450, 480, 2, 1), (None, 100, 3, 1), (360, 370, 4, 1)])
    assert 'N="3"' in pt.text and pt.text.count("<Entry") == 3 and '<Entry To="100" Ix="3" V="1"/>' in pt.text
    assert pt.remove_permission_refs(uid) == 1 and 'NUM="0"' in pt.text


if __name__ == "__main__":
    tests = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
