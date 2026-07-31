"""
Tests for connectors.readers — the BINARY source readers (utmp/lastlog/
faillock/wtmpdb/NetFlow v5/pcap/MRT/unified2). Every test SYNTHESIZES a valid
binary sample with struct.pack, writes it to a temp file, and asserts the
decoded records; plus graceful-degradation checks (empty/truncated/garbage
input never raises) and end-to-end normalization through the jsonl adapter /
merge path. Pure stdlib. Run:
    python3 python_backend/connectors/test_source_readers.py
"""
from __future__ import annotations
import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from connectors import log_sources as ls    # noqa: E402
from connectors import log_adapters as la   # noqa: E402
from connectors import readers as rd        # noqa: E402

_fails = 0


def check(name, cond, detail=""):
    global _fails
    if not cond:
        _fails += 1
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}{'' if cond or not detail else '  — ' + detail}")


class _Prof:
    def __init__(self, **kw):
        self.log_sources = kw.get("log_sources", [])
        self.action_log_file = kw.get("action_log_file", "")
        self.log_file = kw.get("log_file", "")


def _tmpfile(name, payload: bytes) -> str:
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(payload)
    return p


# ── struct layouts match the documented record sizes/offsets ──────────────────

def test_layout_sizes():
    print("binary layouts match documented record sizes:")
    check("utmp record = 384 bytes", rd._UTMP_SIZE == 384, str(rd._UTMP_SIZE))
    check("lastlog record = 292 bytes", rd._LASTLOG_SIZE == 292, str(rd._LASTLOG_SIZE))
    check("faillock tally = 64 bytes", rd._FAILLOCK_SIZE == 64, str(rd._FAILLOCK_SIZE))
    check("netflow v5 header = 24 bytes", rd._NF5_HDR_SIZE == 24, str(rd._NF5_HDR_SIZE))
    check("netflow v5 record = 48 bytes", rd._NF5_REC_SIZE == 48, str(rd._NF5_REC_SIZE))
    check("mrt header = 12 bytes", rd._MRT_HDR_SIZE == 12, str(rd._MRT_HDR_SIZE))
    check("unified2 IDS event = 52 bytes", rd._U2_EVENT_SIZE == 52, str(rd._U2_EVENT_SIZE))
    # bits/utmp.h field offsets: ut_user at 44, ut_host at 76, ut_tv.tv_sec at 340.
    rec = _pack_utmp(7, 4242, b"pts/0", b"alice", b"10.0.0.5", 1784628000, 500000)
    check("ut_user at offset 44", rec[44:49] == b"alice", str(rec[44:49]))
    check("ut_host at offset 76", rec[76:84] == b"10.0.0.5", str(rec[76:84]))
    check("ut_tv.tv_sec at offset 340",
          struct.unpack_from("<i", rec, 340)[0] == 1784628000)


# ── utmp / wtmp / btmp ─────────────────────────────────────────────────────────

def _pack_utmp(ut_type, pid, line, user, host, sec, usec):
    return struct.pack(rd._UTMP_FMT, ut_type, pid, line, b"id01", user, host,
                       0, 0, 1, sec, usec, b"\x00" * 16)


def test_utmp():
    print("utmp reader (glibc 384-byte records):")
    payload = (_pack_utmp(7, 4242, b"pts/0", b"alice", b"10.0.0.5",
                          1784628000, 500000)
               + _pack_utmp(8, 4242, b"pts/0", b"", b"", 1784628060, 0)
               + _pack_utmp(0, 0, b"", b"", b"", 0, 0))       # EMPTY → skipped
    p = _tmpfile("wtmp", payload)
    recs = rd.UtmpReader().read_records(p)
    check("2 records (EMPTY skipped)", len(recs) == 2, str(len(recs)))
    check("ts_ms exact (sec*1000 + usec//1000)",
          recs[0]["ts_ms"] == 1784628000000.0 + 500, str(recs[0]["ts_ms"]))
    check("USER_PROCESS fields", recs[0]["data"]["type_name"] == "USER_PROCESS"
          and recs[0]["data"]["user"] == "alice"
          and recs[0]["data"]["line"] == "pts/0"
          and recs[0]["data"]["host"] == "10.0.0.5"
          and recs[0]["data"]["pid"] == 4242, str(recs[0]["data"]))
    check("DEAD_PROCESS decoded", recs[1]["data"]["type_name"] == "DEAD_PROCESS")
    check("category=auth source=utmp", recs[0]["category"] == "auth"
          and recs[0]["source"] == "utmp")
    # IPv4 fallback from ut_addr_v6 when ut_host is empty
    rec = struct.pack(rd._UTMP_FMT, 7, 1, b"tty1", b"id02", b"root", b"",
                      0, 0, 1, 1784628000, 0, bytes([192, 168, 1, 7]) + b"\x00" * 12)
    recs = rd.UtmpReader().read_records(_tmpfile("utmp", rec))
    check("host backfilled from ut_addr_v6", recs
          and recs[0]["data"]["host"] == "192.168.1.7",
          str(recs and recs[0]["data"]["host"]))
    # btmp basename → failed logins at WARN
    p = _tmpfile("btmp", _pack_utmp(6, 9, b"ssh:notty", b"evil", b"1.2.3.4",
                                    1784628100, 0))
    recs = rd.UtmpReader().read_records(p)
    check("btmp → data.level WARN + 'failed login'",
          recs and recs[0]["data"].get("level") == "WARN"
          and recs[0]["data"]["message"].startswith("failed login"),
          str(recs and recs[0]["data"]))
    # tail_bytes stays record-aligned
    many = b"".join(_pack_utmp(7, i, b"pts/9", b"u%03d" % i, b"", 1784628000 + i, 0)
                    for i in range(10))
    recs = rd.UtmpReader().read_records(_tmpfile("wtmp", many), tail_bytes=384 * 3 + 100)
    check("aligned tail read decodes whole records",
          0 < len(recs) <= 4 and all(r["data"]["user"].startswith("u") for r in recs),
          str([r["data"]["user"] for r in recs]))


# ── lastlog ────────────────────────────────────────────────────────────────────

def test_lastlog():
    print("lastlog reader (292-byte per-UID records, sparse):")
    empty = struct.pack(rd._LASTLOG_FMT, 0, b"", b"")
    rec = struct.pack(rd._LASTLOG_FMT, 1784628000, b"pts/2", b"work.lan")
    p = _tmpfile("lastlog", empty * 1000 + rec)   # UID 1000 logged in, 0-999 never
    recs = rd.LastlogReader().read_records(p)
    check("1 record (sparse slots skipped)", len(recs) == 1, str(len(recs)))
    check("uid recovered from offset", recs and recs[0]["data"]["uid"] == 1000,
          str(recs and recs[0]["data"]))
    check("ts/line/host decoded", recs and recs[0]["ts_ms"] == 1784628000000.0
          and recs[0]["data"]["line"] == "pts/2"
          and recs[0]["data"]["host"] == "work.lan")


# ── faillock ───────────────────────────────────────────────────────────────────

def test_faillock():
    print("faillock reader (linux-pam struct tally, 64 bytes):")
    valid = struct.pack(rd._FAILLOCK_FMT, b"10.0.0.99", 0, 0x1 | 0x2, 1784628000)
    cleared = struct.pack(rd._FAILLOCK_FMT, b"gone", 0, 0x0, 1784628001)
    p = _tmpfile("alice", valid + cleared)        # file basename = username
    recs = rd.FaillockReader().read_records(p)
    check("1 valid record (cleared slot skipped)", len(recs) == 1, str(len(recs)))
    check("user from basename + rhost + WARN",
          recs and recs[0]["data"]["user"] == "alice"
          and recs[0]["data"]["src"] == "10.0.0.99"
          and recs[0]["data"]["src_kind"] == "rhost"
          and recs[0]["data"]["level"] == "WARN", str(recs and recs[0]["data"]))
    check("ts_ms from u64 seconds", recs and recs[0]["ts_ms"] == 1784628000000.0)


# ── wtmpdb / lastlog2 (sqlite) ─────────────────────────────────────────────────

def test_wtmpdb():
    print("wtmpdb reader (sqlite wtmp + lastlog2 tables):")
    import sqlite3
    d = tempfile.mkdtemp()
    p = os.path.join(d, "wtmp.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE wtmp(ID INTEGER PRIMARY KEY, Type INTEGER,"
                " User TEXT NOT NULL, Login INTEGER, Logout INTEGER, TTY TEXT,"
                " RemoteHost TEXT, Service TEXT)")
    con.execute("INSERT INTO wtmp VALUES (1, 1, 'reboot', 1784628000000000,"
                " NULL, '~', '', '')")
    con.execute("INSERT INTO wtmp VALUES (2, 3, 'alice', 1784628010000000,"
                " 1784628999000000, 'pts/0', '10.0.0.5', 'sshd')")
    con.commit()
    con.close()
    recs = rd.WtmpdbReader().read_records(p)
    check("2 rows decoded", len(recs) == 2, str(len(recs)))
    check("µs epochs → ms", recs and recs[0]["ts_ms"] == 1784628000000.0
          and recs[1]["ts_ms"] == 1784628010000.0,
          str([r["ts_ms"] for r in recs]))
    check("user/tty/host/logout", recs[1]["data"]["user"] == "alice"
          and recs[1]["data"]["tty"] == "pts/0"
          and recs[1]["data"]["host"] == "10.0.0.5"
          and recs[1]["data"]["logout_ms"] == 1784628999000.0,
          str(recs[1]["data"]))
    # lastlog2 schema fallback (seconds epochs)
    p2 = os.path.join(d, "lastlog2.db")
    con = sqlite3.connect(p2)
    con.execute("CREATE TABLE lastlog2(Name TEXT PRIMARY KEY, Time INTEGER,"
                " TTY TEXT, RemoteHost TEXT, PamService TEXT)")
    con.execute("INSERT INTO lastlog2 VALUES ('alice', 1784628000, 'pts/1',"
                " 'home.lan', 'sshd')")
    con.commit()
    con.close()
    recs = rd.WtmpdbReader().read_records(p2)
    check("lastlog2 table fallback", len(recs) == 1
          and recs[0]["data"]["user"] == "alice"
          and recs[0]["data"]["table"] == "lastlog2"
          and recs[0]["ts_ms"] == 1784628000000.0,
          str(recs and (recs[0]["ts_ms"], recs[0]["data"])))
    # sqlite magic but garbage body → [] (no raise)
    p3 = _tmpfile("junk.db", b"SQLite format 3\x00" + b"\xff" * 500)
    check("sqlite-magic garbage → []", rd.WtmpdbReader().read_records(p3) == [])


# ── NetFlow v5 ─────────────────────────────────────────────────────────────────

def _nf5_datagram(count, uptime, secs, flows):
    hdr = struct.pack(rd._NF5_HDR, 5, count, uptime, secs, 0, 1, 0, 0, 0)
    body = b""
    for (sa, da, sp, dp, proto, pkts, octets, first, last) in flows:
        body += struct.pack(rd._NF5_REC, sa, da, 0, 1, 2, pkts, octets,
                            first, last, sp, dp, 0, 0x18, proto, 0, 65001,
                            65002, 24, 24, 0)
    return hdr + body


def test_netflow_v5():
    print("netflow_v5 reader (24-byte header + 48-byte flows):")
    ip_a = (10 << 24) | (0 << 16) | (0 << 8) | 1          # 10.0.0.1
    ip_b = (192 << 24) | (168 << 16) | (1 << 8) | 50      # 192.168.1.50
    # boot_ms = secs*1000 - uptime → flow ts = boot_ms + first
    dg = _nf5_datagram(2, 100_000, 1784628100, [
        (ip_a, ip_b, 443, 51234, 6, 10, 8400, 60_000, 90_000),
        (ip_b, ip_a, 51234, 443, 17, 2, 200, 70_000, 70_500),
    ])
    p = _tmpfile("netflow.bin", dg)
    recs = rd.NetflowV5Reader().read_records(p)
    check("2 flows decoded", len(recs) == 2, str(len(recs)))
    boot_ms = 1784628100 * 1000 - 100_000
    check("ts = header wallclock − sysuptime + first",
          recs and recs[0]["ts_ms"] == boot_ms + 60_000
          and recs[0]["data"]["last_ms"] == boot_ms + 90_000,
          str(recs and recs[0]["ts_ms"]))
    check("src/dst ip:port + proto", recs[0]["data"]["src"] == "10.0.0.1"
          and recs[0]["data"]["dst"] == "192.168.1.50"
          and recs[0]["data"]["src_port"] == 443
          and recs[0]["data"]["dst_port"] == 51234
          and recs[0]["data"]["proto_name"] == "tcp", str(recs[0]["data"]))
    check("bytes/packets", recs[0]["data"]["bytes"] == 8400
          and recs[0]["data"]["packets"] == 10)
    check("category=netflow", recs[0]["category"] == "netflow")
    # concatenated datagrams in one file
    p = _tmpfile("netflow2.bin", dg + _nf5_datagram(
        1, 100_000, 1784628200, [(ip_a, ip_b, 53, 53, 17, 1, 64, 1000, 1000)]))
    check("concatenated datagrams", len(rd.NetflowV5Reader().read_records(p)) == 3)
    # truncated mid-record → the valid prefix, no raise
    p = _tmpfile("netflow3.bin", dg[:24 + 48 + 20])
    recs = rd.NetflowV5Reader().read_records(p)
    check("truncated datagram → 1 whole flow", len(recs) == 1, str(len(recs)))


# ── pcap ───────────────────────────────────────────────────────────────────────

def _pcap_file(endian, magic, frac1, linktype=1, packets=2):
    # magic is written in the FILE's byte order (a BE file's a1b2c3d4 reads as
    # d4c3b2a1 through a LE unpack — exactly how real swapped captures look).
    hdr = struct.pack(endian + "I", magic) + struct.pack(endian + "HHiIII",
                                                         2, 4, 0, 0, 65535, linktype)
    body = b""
    for i in range(packets):
        payload = bytes(20 + i)
        body += struct.pack(endian + "IIII", 1784628000 + i, frac1,
                            len(payload), len(payload) + 40) + payload
    return hdr + body


def test_pcap():
    print("pcap reader (µs/ns × LE/BE magics, per-packet ts/len):")
    p = _tmpfile("a.pcap", _pcap_file("<", 0xA1B2C3D4, 500_000))
    recs = rd.PcapReader().read_records(p)
    check("2 packets (LE µs)", len(recs) == 2, str(len(recs)))
    check("ts_ms = sec*1000 + µs/1000",
          recs and recs[0]["ts_ms"] == 1784628000000.0 + 500,
          str(recs and recs[0]["ts_ms"]))
    check("caplen/origlen/linktype", recs[0]["data"]["caplen"] == 20
          and recs[0]["data"]["origlen"] == 60
          and recs[0]["data"]["linktype_name"] == "EN10MB", str(recs[0]["data"]))
    # swapped byte order (big-endian file)
    p = _tmpfile("b.pcap", _pcap_file(">", 0xA1B2C3D4, 250_000))
    recs = rd.PcapReader().read_records(p)
    check("BE (swapped-magic) file decodes", len(recs) == 2
          and recs[0]["ts_ms"] == 1784628000000.0 + 250,
          str(recs and recs[0]["ts_ms"]))
    # nanosecond magic
    p = _tmpfile("c.pcap", _pcap_file("<", 0xA1B23C4D, 750_000_000))
    recs = rd.PcapReader().read_records(p)
    check("nanosecond magic → ms", len(recs) == 2
          and recs[0]["ts_ms"] == 1784628000000.0 + 750,
          str(recs and recs[0]["ts_ms"]))
    # DLT_PFLOG container (catalog pflog-pcap-dlt117)
    p = _tmpfile("pflog.pcap", _pcap_file("<", 0xA1B2C3D4, 0, linktype=117))
    recs = rd.PcapReader().read_records(p)
    check("linktype 117 → PFLOG", recs
          and recs[0]["data"]["linktype_name"] == "PFLOG",
          str(recs and recs[0]["data"]))
    # truncated final packet → valid prefix
    full = _pcap_file("<", 0xA1B2C3D4, 0)
    recs = rd.PcapReader().read_records(_tmpfile("t.pcap", full[:-10]))
    check("truncated final packet dropped", len(recs) == 1, str(len(recs)))


# ── MRT ────────────────────────────────────────────────────────────────────────

def test_mrt():
    print("mrt reader (RFC 6396 record headers):")
    # BGP4MP_ET (type 17): µs field heads the message body
    body1 = struct.pack(">I", 250_000) + b"\x00" * 8
    rec1 = struct.pack(rd._MRT_HDR, 1784628000, 17, 4, len(body1)) + body1
    body2 = b"\x00" * 16
    rec2 = struct.pack(rd._MRT_HDR, 1784628060, 13, 2, len(body2)) + body2
    p = _tmpfile("updates.mrt", rec1 + rec2)
    recs = rd.MrtReader().read_records(p)
    check("2 records decoded", len(recs) == 2, str(len(recs)))
    check("BGP4MP_ET ts includes µs",
          recs and recs[0]["ts_ms"] == 1784628000000.0 + 250
          and recs[0]["data"]["type_name"] == "BGP4MP_ET"
          and recs[0]["data"]["subtype_name"] == "MESSAGE_AS4",
          str(recs and (recs[0]["ts_ms"], recs[0]["data"])))
    check("TABLE_DUMP_V2 named", recs[1]["data"]["type_name"] == "TABLE_DUMP_V2"
          and recs[1]["data"]["subtype_name"] == "RIB_IPV4_UNICAST"
          and recs[1]["ts_ms"] == 1784628060000.0, str(recs[1]["data"]))
    check("category=bgp", recs[0]["category"] == "bgp")
    # truncated final record → valid prefix
    recs = rd.MrtReader().read_records(_tmpfile("t.mrt", (rec1 + rec2)[:-8]))
    check("truncated record dropped", len(recs) == 1, str(len(recs)))


# ── unified2 ───────────────────────────────────────────────────────────────────

def test_unified2():
    print("unified2 reader (BE framing + IDS event structs):")
    ip_a = (10 << 24) | 1          # 10.0.0.1
    ip_b = (10 << 24) | 2          # 10.0.0.2
    ev = struct.pack(rd._U2_EVENT_FMT, 3, 1001, 1784628000, 250_000,
                     2019401, 1, 2, 33, 1, ip_a, ip_b, 4444, 80, 6, 0, 0, 1)
    frame1 = struct.pack(">II", 7, len(ev)) + ev
    pkt = struct.pack(">IIIIIII", 3, 1001, 1784628000, 1784628000, 500_000,
                      1, 60) + b"\x00" * 60
    frame2 = struct.pack(">II", 2, len(pkt)) + pkt
    p = _tmpfile("snort.u2", frame1 + frame2)
    recs = rd.Unified2Reader().read_records(p)
    check("2 frames decoded", len(recs) == 2, str(len(recs)))
    check("IDS event fields", recs
          and recs[0]["data"]["signature_id"] == 2019401
          and recs[0]["data"]["src"] == "10.0.0.1"
          and recs[0]["data"]["dst"] == "10.0.0.2"
          and recs[0]["data"]["src_port"] == 4444
          and recs[0]["data"]["dst_port"] == 80
          and recs[0]["data"]["proto_name"] == "tcp"
          and recs[0]["data"]["blocked"] == 1,
          str(recs and recs[0]["data"]))
    check("priority 1 → level ERROR", recs[0]["data"]["level"] == "ERROR")
    check("event ts µs-precise", recs[0]["ts_ms"] == 1784628000000.0 + 250)
    check("PACKET frame summarized", recs[1]["data"]["record_type"] == "PACKET"
          and recs[1]["data"]["packet_length"] == 60
          and recs[1]["ts_ms"] == 1784628000000.0 + 500, str(recs[1]["data"]))
    # VLAN event (type 104) carries the extra fields
    ev104 = ev + struct.pack(">IHH", 42, 7, 0)
    p = _tmpfile("vlan.u2", struct.pack(">II", 104, len(ev104)) + ev104)
    recs = rd.Unified2Reader().read_records(p)
    check("type 104 decodes + vlan_id", recs
          and recs[0]["data"]["vlan_id"] == 7
          and recs[0]["data"]["signature_id"] == 2019401,
          str(recs and recs[0]["data"]))


# ── robustness: empty / truncated / garbage NEVER raise, for every reader ──────

def test_robustness_all_readers():
    print("robustness (all readers): empty/truncated/garbage → list, no raise:")
    import random
    rnd = random.Random(20260722)
    samples = {
        "empty": b"",
        "tiny": b"\x01\x02\x03",
        "0xff garbage": b"\xff" * 1024,
        "zeros": b"\x00" * 1024,
        "random garbage": bytes(rnd.getrandbits(8) for _ in range(2048)),
    }
    ok = True
    for name, cls in sorted(rd.BINARY_READERS.items()):
        for label, payload in samples.items():
            p = _tmpfile("x.bin", payload)
            try:
                res = cls().read_records(p)
            except Exception as e:                # pragma: no cover
                ok = False
                check(f"{name} on {label} raised", False, repr(e))
                continue
            if not isinstance(res, list):
                ok = False
                check(f"{name} on {label} returned non-list", False, str(type(res)))
        # deterministic-garbage inputs must decode to NOTHING (no fake events)
        p = _tmpfile("x.bin", b"\xff" * 1024)
        if cls().read_records(p) != []:
            ok = False
            check(f"{name} fabricated events from 0xff garbage", False)
        try:
            missing = cls().read_records("/no/such/dir/no.bin")
        except Exception as e:                    # pragma: no cover
            ok = False
            missing = None
            check(f"{name} raised on missing file", False, repr(e))
        if missing is not None and missing != []:
            ok = False
            check(f"{name} on missing file not []", False, str(missing))
    check("all readers degrade gracefully (0 raises)", ok)


# ── registration + normalization through the jsonl adapter / merge path ────────

def test_registered_and_normalized():
    print("registry + end-to-end normalization (reader → jsonl → timeline):")
    names = ls.list_readers()
    want = sorted(rd.BINARY_READERS)
    check("all 8 binary readers registered", all(n in names for n in want),
          str(names))

    # merge path: wtmp via profile {"reader": "utmp"} → read_normalized
    payload = (_pack_utmp(7, 10, b"pts/0", b"alice", b"work.lan", 1784628000, 0)
               + _pack_utmp(8, 10, b"pts/0", b"", b"", 1784628060, 0))
    p = _tmpfile("wtmp", payload)
    prof = _Prof(log_sources=[{"path": p, "reader": "utmp", "label": "wtmp"}])
    evs = ls.read_normalized(prof, limit=10)
    check("merge path yields 2 unified events", len(evs) == 2, str(len(evs)))
    ev = evs[0]
    check("unified schema keys present",
          all(k in ev for k in ("ts", "ts_ms", "category", "level", "source",
                                "trace_id", "frame_seq", "data", "raw")),
          str(sorted(ev)))
    check("ISO ts backfilled from ts_ms by jsonl adapter",
          isinstance(ev.get("ts"), str) and ev["ts"].endswith("Z")
          and ev["ts"].startswith("2026-07-21T"), str(ev.get("ts")))
    check("log_label/log_path attached", ev.get("log_label") == "wtmp"
          and ev.get("log_path") == p, str(ev.get("log_label")))
    check("timeline sorted ascending", evs[0]["ts_ms"] <= evs[1]["ts_ms"])

    # binary + text sources MERGE onto one timeline
    tl = os.path.join(os.path.dirname(p), "app.log")
    with open(tl, "w") as f:                      # explicit-UTC ts → deterministic
        f.write("2026-07-21T10:00:30.000Z ERROR app: mid-session crash\n")
    prof = _Prof(log_sources=[{"path": p, "reader": "utmp", "label": "wtmp"},
                              {"path": tl, "adapter": "auto", "label": "app"}])
    evs = ls.read_normalized(prof, limit=10)
    check("binary + text merged (3 events)", len(evs) == 3, str(len(evs)))
    check("text ERROR interleaved between the two logins",
          len(evs) == 3 and evs[1]["category"] == "error"
          and evs[1]["log_label"] == "app",
          str([(e.get("log_label"), e.get("category")) for e in evs]))

    # explicit jsonl-adapter passthrough proof on a reader dict
    import json as _json
    dg = _nf5_datagram(1, 1000, 1784628100,
                       [((10 << 24) | 1, (10 << 24) | 2, 1, 2, 6, 1, 64, 500, 600)])
    rec = rd.NetflowV5Reader().read_records(_tmpfile("nf.bin", dg))[0]
    ev = la.get_adapter("jsonl").parse_line(_json.dumps(rec))
    check("jsonl adapter passes reader dict through",
          ev is not None and ev["category"] == "netflow"
          and ev["source"] == "netflow.v5"
          and ev["data"]["message"].startswith("flow 10.0.0.1:1 -> 10.0.0.2:2"),
          str(ev))
    check("passthrough backfills ISO ts", bool(ev and ev.get("ts")))

    # btmp WARN records must survive a level filter through the merge
    pb = _tmpfile("btmp", _pack_utmp(6, 9, b"ssh:notty", b"evil", b"1.2.3.4",
                                     1784628100, 0))
    prof = _Prof(log_sources=[{"path": pb, "reader": "utmp", "label": "btmp"}])
    warns = ls.read_normalized(prof, level="WARN", limit=10)
    check("btmp failed login visible at WARN+ filter", len(warns) == 1
          and warns[0]["level"] == "WARN", str(warns))


if __name__ == "__main__":
    print("=" * 66)
    print("binary source-reader test suite")
    print("=" * 66)
    test_layout_sizes()
    test_utmp()
    test_lastlog()
    test_faillock()
    test_wtmpdb()
    test_netflow_v5()
    test_pcap()
    test_mrt()
    test_unified2()
    test_robustness_all_readers()
    test_registered_and_normalized()
    print("=" * 66)
    if _fails:
        print(f"RESULT: {_fails} FAILURE(S)")
        sys.exit(1)
    print("RESULT: all source-reader tests passed")
    sys.exit(0)
