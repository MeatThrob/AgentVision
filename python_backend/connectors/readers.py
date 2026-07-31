"""
Binary SOURCE READERS — fixed-layout binary logs decoded to unified-schema dicts
================================================================================
The 656 named text adapters (connectors/log_adapters.py + connectors/adapters/)
cover the line-parseable catalog. This module covers the catalog's BINARY tier:
formats that are not lines at all, but packed structs. Each reader implements
the SourceReader contract from connectors/log_sources.py —

    read_records(path, *, max_offset=0, tail_bytes=1_048_576) -> list[dict]

— decoding the file with pure-stdlib `struct` into dicts shaped like the
unified event schema ({ts_ms, category, source, data:{message, …}}), which the
merge layer routes through the `jsonl` adapter's native passthrough. Everything
still converges on the ONE unified timeline.

Design rules (every reader):
  • NEVER raises — truncated, empty, or garbage input returns [] or the valid
    prefix of records. Magic-less formats validate per-record sanity instead.
  • Bounded — fixed-record formats do an aligned TAIL read (`tail_bytes`);
    stream-framed formats (pcap/NetFlow/MRT/unified2 — variable framing, must
    scan from byte 0) read at most _MAX_BYTES and keep the newest _MAX_RECORDS.
  • max_offset>0 bounds the read to bytes [..max_offset] (capture alignment).
  • ts_ms is clamped to a sane epoch range so downstream ISO backfill in the
    jsonl adapter can never raise on a garbage timestamp.
  • Pure stdlib: struct/os/collections (+ sqlite3 for the wtmpdb reader).

This module deliberately does NOT import log_sources (no circular import). It
exposes BINARY_READERS = {name: class}; log_sources.py imports this module and
registers each reader in READER_REGISTRY. Profiles reference a reader as
{"path": …, "reader": "<name>"}.

────────────────────────────────────────────────────────────────────────────────
SHIPPED (this module) — 8 readers ⇒ 9 of the catalog's 68 binary entries
────────────────────────────────────────────────────────────────────────────────
  utmp        utmp-wtmp-btmp + utmp_wtmp_last — glibc 384-byte struct utmp
              (little-endian x86-64/aarch64 layout; see cross-platform note)
  lastlog     lastlog-binary — 292-byte {int32 ll_time; line[32]; host[256]},
              file indexed by UID (sparse)
  faillock    faillock-records — linux-pam struct tally, 64 bytes
              {char source[52]; u16 reserved; u16 status; u64 time}
              (modules/pam_faillock/faillock.h; one file per user)
  wtmpdb      wtmpdb-sqlite — Y2038-safe wtmpdb `wtmp` table AND lastlog2
              `lastlog2` table, via stdlib sqlite3 (read-only/immutable)
  netflow_v5  netflow-v5-datagram — 24-byte BE header + N×48-byte flow records;
              handles files of concatenated datagrams
  pcap        pflog-pcap-dlt117 (container level) + any classic pcap — all four
              magics (usec/nsec × LE/BE), one event per packet with real ts,
              caplen/origlen, linktype (117 = PFLOG). Payloads are NOT deep-
              parsed (the pfloghdr field layout varies by OS release — the
              per-packet ts/len/linktype records are the honest container view)
  mrt         mrt-bgp-dump (record level) — RFC 6396 12-byte header (+µs for
              _ET types): one event per record with ts + type/subtype names.
              BGP payload decode (prefixes/attributes) is NOT attempted
  unified2    snort-unified2 — BE (type,length) framing; FULL decode of IDS
              event types 7 and 104 (all catalog-listed fields), summary decode
              of PACKET (2) and the other event types, skip for the rest

────────────────────────────────────────────────────────────────────────────────
DEFERRED — the remaining 59 binary catalog entries, and WHY (honest roadmap)
────────────────────────────────────────────────────────────────────────────────
Template/stateful network telemetry (7): ipfix-message, netflow-v9-datagram,
  sflow-v5-datagram, bmp-message, dnstap-framestream, snmp-trap-pdu,
  hep3-capture. NetFlow v9/IPFIX need a template cache keyed by (exporter,
  sourceId, templateId) — data FlowSets are undecodable until the matching
  template record has been seen, so a correct reader is a stateful session,
  not a pure function of one file. sFlow v5 is a deep XDR tree of nested
  sample/record TLVs; dnstap is protobuf inside frame-streams; SNMP traps are
  ASN.1 BER. HOW TO ADD: give SourceReader an optional per-path state dict
  (template cache), implement v9+IPFIX first (shared IE table from the IANA
  registry), emit a raw-undecodable event when a data set's template is unseen.

Windows EVTX (1): windows-security-evtx. EVTX is a chunked binary-XML format
  (ElfFile/ElfChnk magics, per-chunk template tables, value substitution) — a
  correct parser is a substantial project (python-evtx exists as a third-party
  dep, excluded by the stdlib rule). The rendered-XML/JSON exports already
  route through the windows_evtx_text / windows_eventdata_xml / jsonl adapters.
  HOW TO ADD: implement chunk walk + binary-XML token decoder, or accept an
  optional third-party backend behind a feature check.

z/OS SMF family (19): smf-record-header, smf-record-header-binary,
  smf-type30-job-accounting, smf30-address-space, smf-type80-racf-audit,
  smf80-racf-audit, smf100-102-db2-trace, smf-db2-100-102,
  smf110-cics-monitoring, smf-cics-110, rmf-smf-70-79, smf-rmf-70-79,
  smf-tcpip-119, smf119-commserver-tcpip, smf115-116-mq,
  smf120-websphere-liberty, smf-websphere-liberty-120, zos-gtf-trace,
  zos-operlog-mdb. All are RDW-framed EBCDIC records where the generic header
  only classifies; the payload needs per-type self-defining-section decoders
  (offset/length/count triplets, packed-decimal dates, layouts defined by
  SYS1.MACLIB / DSNDQW* / ERBSMF* macros — SMF 110 even requires reading an
  in-stream dictionary record first). Text-side coverage exists (zos_syslog,
  racf_irradu00, rmf_postprocessor, …). HOW TO ADD: one `smf` reader that
  decodes the 18/24-byte header (RDW, type, 1/100s time, packed 0cyydddF
  date, EBCDIC SID) and emits classification events, then per-type section
  decoders incrementally (type 30 and 80 first).

IMS/CICS binary traces (4): ims-olds-log-binary, ims-olds-slds-log,
  cics-aux-internal-trace, cics-auxtrace-binary. EBCDIC, block-structured,
  layout tied to product internals; the catalog itself routes ingestion to the
  DFSERA10/DFHTUxxx printed forms (text adapters).

IBM i journal/outfile family (7): ibmi-cpyaudjrne-outfile, ibmi-joblog-outfile,
  ibmi-journal-receiver-raw, ibmi-qacgjrn-accounting,
  ibmi-qipfilter-qipnat-journal, ibmi-dspjrn-outfile-type1-5,
  ibmi-qhst-history-file. EBCDIC fixed-column database outfiles / raw journal
  receivers; correct decode needs the per-release model-file field tables
  (QASYxxJ5 etc.). Extracted text forms already route (ibmi_* adapters).

External-tool-first formats (21): perf.data, trace.dat, Java Flight Recorder
  (.jfr), async-profiler JFR, xbox_etw_etl, macos-unifiedlog-tracev3,
  macos-asl-binary-store, macos-powerlog-plsql (Apple-internal sqlite schema),
  xcode-xcresult-xcactivitylog, esp32_coredump, orbis_textdump_crashdump,
  PX4/MAVLink ULog, arm_swo_itm, aix-errlog-binary, tru64-binary-errlog,
  solaris-bsm-audit-binary, android-logcat-binary, websphere-hpel-binary,
  websphere-activity-service-log, mssql-xevents-xel, squid-swap-state
  (recognizer-only by design). For each, the catalog's own notes recommend the
  companion tool's text rendering (`perf script`, `jfr print`, `log show`,
  `syslog -f`, `errpt`, `praudit`, tracerpt, logViewer, …) — those renderings
  ALREADY route through existing text adapters. A raw stdlib re-implementation
  of each proprietary container is not worth the correctness risk; HOW TO ADD:
  shell-out readers gated on tool availability, emitting through the same
  dict contract.
"""
from __future__ import annotations

import os
import struct
from collections import deque

# Bounds shared by every reader in this module.
_MAX_BYTES = 16 * 1024 * 1024      # max bytes scanned per read (stream formats)
_MAX_RECORDS = 4000                # newest N records kept per read
_MAX_TS_MS = 4.2e12                # ~year 2103 — sanity ceiling for ts_ms


def _ms_or_none(ms) -> float | None:
    """Clamp a candidate epoch-ms value to a sane range (else None) so the
    jsonl adapter's ISO backfill can never raise on a garbage timestamp."""
    try:
        ms = float(ms)
    except Exception:
        return None
    if not (0 < ms < _MAX_TS_MS):
        return None
    return ms


def _cstr(b: bytes) -> str:
    """NUL-terminated fixed-width C string → str (never raises)."""
    return b.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _ip4(v: int) -> str:
    return "%d.%d.%d.%d" % ((v >> 24) & 255, (v >> 16) & 255, (v >> 8) & 255, v & 255)


def _read_span(path: str, max_offset: int, cap: int = _MAX_BYTES) -> bytes:
    """Bytes [0 .. min(size, max_offset)], capped at `cap`. Never raises."""
    try:
        size = os.path.getsize(path)
        end = min(size, max_offset) if max_offset > 0 else size
        with open(path, "rb") as f:
            return f.read(min(end, cap))
    except Exception:
        return b""


def _tail_span(path: str, max_offset: int, tail_bytes: int,
               recsize: int) -> tuple[bytes, int]:
    """Aligned tail read for FIXED-size-record files. Returns (bytes, start
    offset); start is a multiple of recsize so records stay aligned (lastlog
    derives the UID from the absolute offset). Never raises."""
    try:
        size = os.path.getsize(path)
        end = min(size, max_offset) if max_offset > 0 else size
        end -= end % recsize                      # drop a trailing partial record
        start = max(0, end - max(tail_bytes, recsize))
        start -= start % recsize                  # record-aligned start
        if end <= start:
            return b"", 0
        with open(path, "rb") as f:
            f.seek(start)
            return f.read(end - start), start
    except Exception:
        return b"", 0


# ═══════════════════════════════════════════════════════════════════════════════
#  utmp / wtmp / btmp  (glibc struct utmp, 384 bytes)
# ═══════════════════════════════════════════════════════════════════════════════

# bits/utmp.h layout as compiled by glibc on x86-64/aarch64 Linux (the layout
# every /var/log/wtmp people actually copy around uses): int32 tv_sec/tv_usec
# even on 64-bit, so the record is 384 bytes on both 32- and 64-bit LE arches.
# CROSS-PLATFORM NOTE: we decode LITTLE-ENDIAN records. Files written on a
# big-endian host (rare today: s390x, older MIPS/SPARC) will fail the ut_type
# sanity gate and decode to [] rather than to wrong events — documented
# behavior, not silent corruption. macOS utmpx is a different format (its text
# forms route via `last`/adapters).
_UTMP_FMT = "<h2xi32s4s32s256s2hi2i16s20x"
_UTMP_SIZE = struct.calcsize(_UTMP_FMT)           # == 384
_UT_TYPES = {0: "EMPTY", 1: "RUN_LVL", 2: "BOOT_TIME", 3: "NEW_TIME",
             4: "OLD_TIME", 5: "INIT_PROCESS", 6: "LOGIN_PROCESS",
             7: "USER_PROCESS", 8: "DEAD_PROCESS", 9: "ACCOUNTING"}


class UtmpReader:
    """Linux utmp/wtmp/btmp login-accounting records (catalog: utmp-wtmp-btmp,
    utmp_wtmp_last). One event per non-EMPTY record. If the file basename
    contains 'btmp' the records are FAILED logins → data.level=WARN."""
    kind = "utmp"

    def read_records(self, path, *, max_offset=0, tail_bytes=1_048_576):
        out: deque = deque(maxlen=_MAX_RECORDS)
        try:
            raw, _ = _tail_span(path, max_offset, tail_bytes, _UTMP_SIZE)
            failed = "btmp" in os.path.basename(path).lower()
            for off in range(0, len(raw) - _UTMP_SIZE + 1, _UTMP_SIZE):
                (ut_type, pid, line, ut_id, user, host, e_term, e_exit,
                 session, tv_sec, tv_usec, addr) = struct.unpack_from(
                    _UTMP_FMT, raw, off)
                if ut_type not in _UT_TYPES or ut_type == 0:   # sanity gate
                    continue
                tname = _UT_TYPES[ut_type]
                user_s, line_s, host_s = _cstr(user), _cstr(line), _cstr(host)
                if not host_s and addr[:4] != b"\x00\x00\x00\x00" \
                        and addr[4:] == b"\x00" * 12:
                    host_s = "%d.%d.%d.%d" % tuple(addr[:4])
                usec = tv_usec if 0 <= tv_usec < 1_000_000 else 0
                verb = "failed login" if failed else tname
                msg = f"{verb} user={user_s or '-'} line={line_s or '-'}" \
                      f" host={host_s or '-'} pid={pid}"
                data = {"message": msg, "type": ut_type, "type_name": tname,
                        "user": user_s, "line": line_s, "id": _cstr(ut_id),
                        "host": host_s, "pid": pid, "session": session}
                if ut_type == 8:                    # DEAD_PROCESS: exit status
                    data["exit_status"] = e_exit
                    data["termination"] = e_term
                if failed:
                    data["level"] = "WARN"
                out.append({
                    "ts_ms": _ms_or_none(tv_sec * 1000.0 + usec // 1000),
                    "category": "auth", "source": "utmp", "data": data,
                })
        except Exception:
            pass
        return list(out)


# ═══════════════════════════════════════════════════════════════════════════════
#  lastlog  (292-byte per-UID records, sparse)
# ═══════════════════════════════════════════════════════════════════════════════

_LASTLOG_FMT = "<i32s256s"                        # ll_time, ll_line, ll_host
_LASTLOG_SIZE = struct.calcsize(_LASTLOG_FMT)     # == 292


class LastlogReader:
    """/var/log/lastlog last-login database (catalog: lastlog-binary). The file
    is indexed by UID (record N = UID N, sparse); empty slots (ll_time==0) are
    skipped, and the UID is recovered from the absolute record offset."""
    kind = "lastlog"

    def read_records(self, path, *, max_offset=0, tail_bytes=1_048_576):
        out: deque = deque(maxlen=_MAX_RECORDS)
        try:
            raw, start = _tail_span(path, max_offset, tail_bytes, _LASTLOG_SIZE)
            for off in range(0, len(raw) - _LASTLOG_SIZE + 1, _LASTLOG_SIZE):
                ll_time, line, host = struct.unpack_from(_LASTLOG_FMT, raw, off)
                if ll_time <= 0:                  # sparse slot / garbage gate
                    continue
                uid = (start + off) // _LASTLOG_SIZE
                line_s, host_s = _cstr(line), _cstr(host)
                out.append({
                    "ts_ms": _ms_or_none(ll_time * 1000.0),
                    "category": "auth", "source": "lastlog",
                    "data": {"message": f"last login uid={uid}"
                                        f" line={line_s or '-'} host={host_s or '-'}",
                             "uid": uid, "line": line_s, "host": host_s},
                })
        except Exception:
            pass
        return list(out)


# ═══════════════════════════════════════════════════════════════════════════════
#  pam_faillock tally files  (64-byte struct tally, one file per user)
# ═══════════════════════════════════════════════════════════════════════════════

# linux-pam modules/pam_faillock/faillock.h:
#   struct tally { char source[52]; uint16_t reserved; uint16_t status;
#                  uint64_t time; };            /* 64 bytes, little-endian */
_FAILLOCK_FMT = "<52sHHQ"
_FAILLOCK_SIZE = struct.calcsize(_FAILLOCK_FMT)   # == 64
_TALLY_VALID, _TALLY_RHOST, _TALLY_RUSER = 0x1, 0x2, 0x4


class FaillockReader:
    """pam_faillock per-user failure tallies (catalog: faillock-records).
    /var/run/faillock/<user> — the username is the file basename. Only records
    with the VALID status bit are emitted; each is a failed login (WARN)."""
    kind = "faillock"

    def read_records(self, path, *, max_offset=0, tail_bytes=1_048_576):
        out: deque = deque(maxlen=_MAX_RECORDS)
        try:
            raw, _ = _tail_span(path, max_offset, tail_bytes, _FAILLOCK_SIZE)
            user = os.path.basename(path)
            for off in range(0, len(raw) - _FAILLOCK_SIZE + 1, _FAILLOCK_SIZE):
                src, _rsv, status, t = struct.unpack_from(_FAILLOCK_FMT, raw, off)
                if not (status & _TALLY_VALID) or not (1e9 < t < 4.1e9):
                    continue                       # cleared slot / garbage gate
                src_s = _cstr(src)
                src_kind = ("rhost" if status & _TALLY_RHOST
                            else "ruser" if status & _TALLY_RUSER else "tty")
                out.append({
                    "ts_ms": _ms_or_none(t * 1000.0),
                    "category": "auth", "source": "faillock",
                    "data": {"message": f"failed login user={user}"
                                        f" {src_kind}={src_s or '-'}",
                             "user": user, "src": src_s, "src_kind": src_kind,
                             "level": "WARN"},
                })
        except Exception:
            pass
        return list(out)


# ═══════════════════════════════════════════════════════════════════════════════
#  wtmpdb / lastlog2  (Y2038-safe sqlite login records)
# ═══════════════════════════════════════════════════════════════════════════════

def _flex_epoch_ms(v) -> float | None:
    """wtmpdb stores µs epochs; lastlog2 seconds. Accept s/ms/µs by magnitude."""
    try:
        v = float(v)
    except Exception:
        return None
    if v <= 0:
        return None
    if v > 1e14:                                   # microseconds
        return _ms_or_none(v / 1000.0)
    if v > 1e11:                                   # already milliseconds
        return _ms_or_none(v)
    return _ms_or_none(v * 1000.0)                 # seconds


class WtmpdbReader:
    """wtmpdb `wtmp` table (/var/lib/wtmpdb/wtmp.db) and lastlog2 `lastlog2`
    table (catalog: wtmpdb-sqlite) via stdlib sqlite3, opened read-only/
    immutable. Non-sqlite or schema-less files return []."""
    kind = "wtmpdb"
    _TYPES = {0: "EMPTY", 1: "BOOT_TIME", 2: "RUNLEVEL", 3: "USER_PROCESS"}

    def read_records(self, path, *, max_offset=0, tail_bytes=1_048_576):
        out: list = []
        try:
            with open(path, "rb") as f:            # cheap magic gate
                if f.read(16) != b"SQLite format 3\x00":
                    return []
            import sqlite3
            con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            try:
                rows, table = [], ""
                for table, sql in (
                        ("wtmp", "SELECT Type, User, Login, Logout, TTY,"
                                 " RemoteHost, Service FROM wtmp"
                                 " ORDER BY Login DESC LIMIT ?"),
                        ("lastlog2", "SELECT 3, Name, Time, NULL, TTY,"
                                     " RemoteHost, PamService FROM lastlog2"
                                     " ORDER BY Time DESC LIMIT ?")):
                    try:
                        rows = con.execute(sql, (_MAX_RECORDS,)).fetchall()
                        break
                    except Exception:
                        rows = []
                        continue
            finally:
                con.close()
            for typ, user, login, logout, tty, rhost, service in reversed(rows):
                tname = self._TYPES.get(typ, str(typ))
                login_ms, logout_ms = _flex_epoch_ms(login), _flex_epoch_ms(logout)
                data = {"message": f"{tname} user={user or '-'}"
                                   f" tty={tty or '-'} host={rhost or '-'}",
                        "type": typ, "type_name": tname, "user": user or "",
                        "tty": tty or "", "host": rhost or "",
                        "service": service or "", "table": table}
                if logout_ms:
                    data["logout_ms"] = logout_ms
                out.append({"ts_ms": login_ms, "category": "auth",
                            "source": "wtmpdb", "data": data})
        except Exception:
            pass
        return out[-_MAX_RECORDS:]


# ═══════════════════════════════════════════════════════════════════════════════
#  NetFlow v5  (24-byte BE header + N × 48-byte flow records)
# ═══════════════════════════════════════════════════════════════════════════════

_NF5_HDR = ">HHIIIIBBH"                            # 24 bytes
_NF5_REC = ">IIIHHIIIIHHBBBBHHBBH"                 # 48 bytes
_NF5_HDR_SIZE = struct.calcsize(_NF5_HDR)
_NF5_REC_SIZE = struct.calcsize(_NF5_REC)
_PROTO_NAMES = {1: "icmp", 6: "tcp", 17: "udp", 47: "gre", 50: "esp", 58: "icmpv6"}


class NetflowV5Reader:
    """Cisco NetFlow v5 export datagrams (catalog: netflow-v5-datagram), as
    captured to a file — handles one datagram or many concatenated. Each flow
    record becomes one event; ts = header wallclock mapped through sysuptime
    to the flow's `first` ms (flow start; flow end in data.last_ms)."""
    kind = "netflow_v5"

    def read_records(self, path, *, max_offset=0, tail_bytes=1_048_576):
        out: deque = deque(maxlen=_MAX_RECORDS)
        try:
            raw = _read_span(path, max_offset)
            off, n = 0, len(raw)
            while off + _NF5_HDR_SIZE <= n:
                (ver, count, uptime, secs, nsecs, seq, etype, eid,
                 sampling) = struct.unpack_from(_NF5_HDR, raw, off)
                if ver != 5 or not (1 <= count <= 30):
                    break                          # magic-less: sanity gate
                off += _NF5_HDR_SIZE
                boot_ms = secs * 1000.0 + nsecs / 1e6 - uptime
                for _ in range(count):
                    if off + _NF5_REC_SIZE > n:    # truncated datagram
                        off = n
                        break
                    (sa, da, nh, inp, outp, pkts, octets, first, last, sp, dp,
                     _p1, flags, proto, tos, sas, das, smask, dmask,
                     _p2) = struct.unpack_from(_NF5_REC, raw, off)
                    off += _NF5_REC_SIZE
                    pname = _PROTO_NAMES.get(proto, str(proto))
                    src, dst = _ip4(sa), _ip4(da)
                    out.append({
                        "ts_ms": _ms_or_none(boot_ms + first),
                        "category": "netflow", "source": "netflow.v5",
                        "data": {"message": f"flow {src}:{sp} -> {dst}:{dp}"
                                            f" {pname} pkts={pkts} bytes={octets}",
                                 "src": src, "dst": dst, "src_port": sp,
                                 "dst_port": dp, "proto": proto,
                                 "proto_name": pname, "packets": pkts,
                                 "bytes": octets, "tcp_flags": flags, "tos": tos,
                                 "src_as": sas, "dst_as": das,
                                 "if_in": inp, "if_out": outp,
                                 "first_ms": _ms_or_none(boot_ms + first),
                                 "last_ms": _ms_or_none(boot_ms + last),
                                 "engine": f"{etype}/{eid}", "sequence": seq,
                                 "sampling": sampling},
                    })
        except Exception:
            pass
        return list(out)


# ═══════════════════════════════════════════════════════════════════════════════
#  pcap  (classic libpcap capture container)
# ═══════════════════════════════════════════════════════════════════════════════

# magic (read little-endian) → (byte order, ts fraction unit per second)
_PCAP_MAGICS = {0xA1B2C3D4: ("<", 1_000_000),      # native LE, µs
                0xD4C3B2A1: (">", 1_000_000),      # swapped (BE file), µs
                0xA1B23C4D: ("<", 1_000_000_000),  # LE, ns
                0x4D3CB2A1: (">", 1_000_000_000)}  # BE, ns
_DLT_NAMES = {0: "NULL", 1: "EN10MB", 8: "SLIP", 9: "PPP", 12: "RAW",
              101: "RAW", 105: "IEEE802_11", 108: "LOOP", 113: "LINUX_SLL",
              117: "PFLOG", 127: "IEEE802_11_RADIOTAP", 228: "IPV4",
              229: "IPV6", 276: "LINUX_SLL2"}


class PcapReader:
    """Classic pcap capture files — all four magics (µs/ns × LE/BE). One event
    per packet: real capture ts + caplen/origlen + linktype (117 = PFLOG →
    catalog pflog-pcap-dlt117 at container level). Packet payloads are NOT
    deep-parsed. pcapng (magic 0x0A0D0D0A) is a different block container and
    is not handled here (see module DEFERRED roadmap)."""
    kind = "pcap"

    def read_records(self, path, *, max_offset=0, tail_bytes=1_048_576):
        out: deque = deque(maxlen=_MAX_RECORDS)
        try:
            raw = _read_span(path, max_offset)
            if len(raw) < 24:
                return []
            magic = struct.unpack_from("<I", raw, 0)[0]
            if magic not in _PCAP_MAGICS:
                return []
            endian, frac_per_s = _PCAP_MAGICS[magic]
            vmaj, vmin, _tz, _sig, snaplen, network = struct.unpack_from(
                endian + "HHiIII", raw, 4)
            cap_limit = max(snaplen if 0 < snaplen < 0x7FFFFFFF else 0, 0x40000)
            lname = _DLT_NAMES.get(network, str(network))
            off, n, idx = 24, len(raw), 0
            while off + 16 <= n:
                ts_sec, ts_frac, caplen, origlen = struct.unpack_from(
                    endian + "IIII", raw, off)
                if caplen > cap_limit:             # framing lost / garbage
                    break
                off += 16 + caplen                 # payload not parsed
                if off > n:                        # truncated final packet
                    break
                idx += 1
                out.append({
                    "ts_ms": _ms_or_none(ts_sec * 1000.0
                                         + ts_frac * 1000.0 / frac_per_s),
                    "category": "packet", "source": "pcap",
                    "data": {"message": f"packet #{idx} caplen={caplen}"
                                        f" origlen={origlen} linktype={lname}",
                             "index": idx, "caplen": caplen, "origlen": origlen,
                             "linktype": network, "linktype_name": lname,
                             "pcap_version": f"{vmaj}.{vmin}"},
                })
        except Exception:
            pass
        return list(out)


# ═══════════════════════════════════════════════════════════════════════════════
#  MRT routing archives  (RFC 6396 — RouteViews / RIPE RIS / FRR dumps)
# ═══════════════════════════════════════════════════════════════════════════════

_MRT_HDR = ">IHHI"                                 # ts, type, subtype, length
_MRT_HDR_SIZE = struct.calcsize(_MRT_HDR)          # == 12
_MRT_TYPES = {11: "OSPFv2", 12: "TABLE_DUMP", 13: "TABLE_DUMP_V2",
              16: "BGP4MP", 17: "BGP4MP_ET", 32: "ISIS", 33: "ISIS_ET",
              48: "OSPFv3", 49: "OSPFv3_ET"}
_MRT_ET_TYPES = {17, 33, 49}                       # extended: +µs field
_MRT_SUBTYPES = {
    16: {0: "STATE_CHANGE", 1: "MESSAGE", 4: "MESSAGE_AS4",
         5: "STATE_CHANGE_AS4", 6: "MESSAGE_LOCAL", 7: "MESSAGE_AS4_LOCAL"},
    12: {1: "AFI_IPv4", 2: "AFI_IPv6"},
    13: {1: "PEER_INDEX_TABLE", 2: "RIB_IPV4_UNICAST", 3: "RIB_IPV4_MULTICAST",
         4: "RIB_IPV6_UNICAST", 5: "RIB_IPV6_MULTICAST", 6: "RIB_GENERIC"},
}
_MRT_SUBTYPES[17] = _MRT_SUBTYPES[16]


class MrtReader:
    """MRT routing archive records (catalog: mrt-bgp-dump), RECORD level: one
    event per MRT record with ts (µs-precise for _ET types), type/subtype
    names, and payload length. The BGP message / RIB entry payload itself is
    NOT decoded (see module docstring)."""
    kind = "mrt"

    def read_records(self, path, *, max_offset=0, tail_bytes=1_048_576):
        out: deque = deque(maxlen=_MAX_RECORDS)
        try:
            raw = _read_span(path, max_offset)
            off, n = 0, len(raw)
            while off + _MRT_HDR_SIZE <= n:
                ts, mtype, subtype, length = struct.unpack_from(_MRT_HDR, raw, off)
                if mtype not in _MRT_TYPES or length > _MAX_BYTES:
                    break                          # magic-less: sanity gate
                body = off + _MRT_HDR_SIZE
                usec = 0
                if mtype in _MRT_ET_TYPES and body + 4 <= n and length >= 4:
                    usec = struct.unpack_from(">I", raw, body)[0]
                    if usec >= 1_000_000:
                        usec = 0
                off = body + length
                if off > n:                        # truncated final record
                    break
                tname = _MRT_TYPES[mtype]
                sname = _MRT_SUBTYPES.get(mtype, {}).get(subtype, str(subtype))
                out.append({
                    "ts_ms": _ms_or_none(ts * 1000.0 + usec // 1000),
                    "category": "bgp", "source": "mrt",
                    "data": {"message": f"{tname}/{sname} len={length}",
                             "type": mtype, "type_name": tname,
                             "subtype": subtype, "subtype_name": sname,
                             "length": length},
                })
        except Exception:
            pass
        return list(out)


# ═══════════════════════════════════════════════════════════════════════════════
#  Snort unified2  (BE type/length framing + IDS event structs)
# ═══════════════════════════════════════════════════════════════════════════════

_U2_TYPES = {2: "PACKET", 7: "IDS_EVENT", 72: "EXTRA_DATA",
             104: "IDS_EVENT_VLAN", 105: "IDS_EVENT_IPV6_VLAN",
             110: "IDS_EVENT_APPID", 111: "IDS_EVENT_APPID_IPV6"}
_U2_EVENT_FMT = ">IIIIIIIIIIIHHBBBB"               # 52-byte v1 IDS event
_U2_EVENT_SIZE = struct.calcsize(_U2_EVENT_FMT)
_U2_PRIORITY_LEVEL = {1: "ERROR", 2: "WARN"}       # snort priority → level


class Unified2Reader:
    """Snort unified2 alert/packet logs (catalog: snort-unified2). Frames are
    big-endian (type, length) + body. IDS events (types 7 and 104) decode all
    catalog-listed fields; PACKET (2) and the other event types decode to a
    summary with ts; unknown-but-sane frames are skipped."""
    kind = "unified2"

    def read_records(self, path, *, max_offset=0, tail_bytes=1_048_576):
        out: deque = deque(maxlen=_MAX_RECORDS)
        try:
            raw = _read_span(path, max_offset)
            off, n = 0, len(raw)
            while off + 8 <= n:
                rtype, length = struct.unpack_from(">II", raw, off)
                if rtype not in _U2_TYPES or length > 1_048_576:
                    break                          # magic-less: sanity gate
                body = off + 8
                off = body + length
                if off > n:                        # truncated final record
                    break
                tname = _U2_TYPES[rtype]
                if rtype in (7, 104) and length >= _U2_EVENT_SIZE:
                    (sensor, event_id, sec, usec, sig, gen, rev, cls, prio,
                     ip_src, ip_dst, sport, dport, proto, _impf, _imp,
                     blocked) = struct.unpack_from(_U2_EVENT_FMT, raw, body)
                    src, dst = _ip4(ip_src), _ip4(ip_dst)
                    pname = _PROTO_NAMES.get(proto, str(proto))
                    data = {"message": f"alert gid={gen} sid={sig} rev={rev}"
                                       f" prio={prio} {src}:{sport} ->"
                                       f" {dst}:{dport} {pname}"
                                       + (" [blocked]" if blocked else ""),
                            "record_type": tname, "sensor_id": sensor,
                            "event_id": event_id, "signature_id": sig,
                            "generator_id": gen, "signature_revision": rev,
                            "classification_id": cls, "priority": prio,
                            "src": src, "dst": dst, "src_port": sport,
                            "dst_port": dport, "proto": proto,
                            "proto_name": pname, "blocked": blocked,
                            "level": _U2_PRIORITY_LEVEL.get(prio, "INFO")}
                    if rtype == 104 and length >= _U2_EVENT_SIZE + 6:
                        mpls, vlan = struct.unpack_from(
                            ">IH", raw, body + _U2_EVENT_SIZE)
                        data["mpls_label"], data["vlan_id"] = mpls, vlan
                    ts_ms = _ms_or_none(
                        sec * 1000.0 + (usec // 1000 if usec < 1_000_000 else 0))
                elif rtype == 2 and length >= 28:
                    (sensor, event_id, _esec, psec, pusec, linktype,
                     plen) = struct.unpack_from(">IIIIIII", raw, body)
                    data = {"message": f"packet event_id={event_id}"
                                       f" len={plen} linktype={linktype}",
                            "record_type": tname, "sensor_id": sensor,
                            "event_id": event_id, "packet_length": plen,
                            "linktype": linktype}
                    ts_ms = _ms_or_none(
                        psec * 1000.0 + (pusec // 1000 if pusec < 1_000_000 else 0))
                elif rtype in (105, 110, 111) and length >= 16:
                    sensor, event_id, sec, usec = struct.unpack_from(
                        ">IIII", raw, body)
                    data = {"message": f"{tname} event_id={event_id}",
                            "record_type": tname, "sensor_id": sensor,
                            "event_id": event_id}
                    ts_ms = _ms_or_none(
                        sec * 1000.0 + (usec // 1000 if usec < 1_000_000 else 0))
                else:                              # EXTRA_DATA / short frames
                    continue
                out.append({"ts_ms": ts_ms, "category": "ids",
                            "source": "unified2", "data": data})
        except Exception:
            pass
        return list(out)


# ═══════════════════════════════════════════════════════════════════════════════
#  Registry hand-off  (log_sources.py imports this and registers each reader)
# ═══════════════════════════════════════════════════════════════════════════════

BINARY_READERS = {
    "utmp": UtmpReader,
    "lastlog": LastlogReader,
    "faillock": FaillockReader,
    "wtmpdb": WtmpdbReader,
    "netflow_v5": NetflowV5Reader,
    "pcap": PcapReader,
    "mrt": MrtReader,
    "unified2": Unified2Reader,
}
