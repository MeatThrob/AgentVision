"""
Tests for the universal structural auto-normalizer + BATCH-1 family adapters.

Pure stdlib (no pytest). Run:
    python3 python_backend/connectors/test_adapters_batch1.py
Exits non-zero on any failure so it gates CI / packaging.

Covers, for every new adapter:
  • detect()  — the intended sample resolves to the intended adapter, and
  • parse_line() — level/category/source/fields land in the unified schema, and
  • a collision guard — no generic fallback (structural/generic_ts/raw) is ever
    tied at the top, and the intended adapter beats the generic syslog-shape
    adapters it overlaps with.
Plus a dedicated section for the structural normalizer's shapes + guarantees.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from connectors import log_adapters as la  # noqa: E402

_fails = 0


def check(name: str, cond: bool, detail: str = ""):
    global _fails
    status = "ok  " if cond else "FAIL"
    if not cond:
        _fails += 1
    print(f"  [{status}] {name}{'  — ' + detail if detail and not cond else ''}")


# ── One representative sample per batch-1 adapter (the intended winner) ──────────
BATCH1 = {
    "ps5_orbis_klog":   "<134>[SceLncSysServiceProcess] app launch requested, titleId=CUSA00000",
    "orbis_module_tag": "[SceShellCore] NetworkService: connection failed r0=0x8002",
    "orbis_fatal_trap": "Fatal trap 12: page fault while in kernel mode",
    "freebsd_messages": "Jul 20 14:03:11 fbsdhost kernel: ada0: <Samsung SSD 870> ACS-4 device",
    "linux_dmesg":      "[    3.123456] usb 1-1: new high-speed USB device number 2 using ci_hdrc",
    "linux_dmesg_ctime":"[Sun Jul 20 14:03:11 2026] usb 1-1: new high-speed USB device number 4",
    "linux_devkmsg":    "6,339,5140900,-;usb 1-1: USB disconnect, device number 4",
    "kernel_panic_oops":"[  345.678901] BUG: unable to handle page fault for address: 00000000",
    "netfilter_klog":   ("Dec  4 08:25:00 host kernel: [1659.916] [UFW BLOCK] IN=eth0 OUT= "
                         "MAC=8a:aa SRC=184.91.129.123 DST=172.86.75.134 LEN=52 PROTO=TCP "
                         "SPT=60562 DPT=3389 WINDOW=64240 SYN URGP=0"),
    "esp32_panic":      "Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.",
    "esp_idf_log":      "I (1523) wifi:connected with MyAP, aid = 1, channel 6",
    "zephyr_log":       "[00:00:00.000,274] <inf> sample_instance.inst1: logging message",
    "zephyr_fatal":     "E: >>> ZEPHYR FATAL ERROR 3: Kernel oops on CPU 0",
    "uboot_boot_log":   "U-Boot 2022.04 (Apr 20 2022 - 10:14:33 +0000)",
    "freertos_logging": "   12.045.678 [Tmr Svc      ] MQTT connection established",
    "macos_unified_log":("2026-07-20 14:03:11.248693-0700 0x7c393    Default     0x0"
                         "                  10371  0    kernel: (AppleACPIPlatform) foo"),
    "rsyslog":          "2026-07-20T14:03:11.123456+00:00 myhost myapp[1234]: config reloaded",
    "ftrace":           "          <idle>-0     [000] d.h. 12345.678901: sched_switch: prev_comm=swapper/0 prev_pid=0",
    "cef":              ("Jan 24 12:32:10 host CEF:0|Security|threatmanager|1.0|100|worm "
                         "successfully stopped|10|src=10.0.0.1 dst=2.1.2.2 spt=1232 act=blocked dpt=443"),
    "leef":             ("<134>May  1 12:00:00 gw LEEF:2.0|Lancope|StealthWatch|1.0|41|^|"
                         "src=10.0.0.5^dst=8.8.8.8^sev=8^srcPort=51044^dstPort=80"),
    "cisco_asa":        ("<166>Jun 27 2018 12:17:46 asa-fw : %ASA-3-106023: Deny tcp src "
                         "outside:1.2.3.4/1 dst inside:5.6.7.8/2 by access-group"),
    "cisco_ios":        ("Jan 24 12:20:11 sw1 123456: *Jan 24 12:20:10.512: %LINK-3-UPDOWN: "
                         "Interface GigabitEthernet0/1, changed state to down"),
    "fortigate":        ('date=2019-05-13 time=14:29:12 logid="0100032002" type="event" '
                         'subtype="system" level="alert" logdesc="Admin login failed" '
                         'action="login" status="failed"'),
    "checkpoint":       ('<134>1 2024-03-21T17:32:32Z gw-da58d8 CheckPoint 18160 - '
                         '[action:"Drop"; ifdir:"inbound"; origin:"192.168.96.80"; '
                         'time:"1521649952"; dst:"192.168.96.27"; proto:"6"; s_port:"45325"; '
                         'service:"443"; src:"192.168.96.80"; product:"VPN-1 & FireWall-1"]'),
    "pfsense_filterlog":("Mar  2 10:15:23 fw01 filterlog: 4,,,1000000103,pppoe0,match,block,in,"
                         "4,0x0,,111,23330,0,DF,6,tcp,52,115.73.209.120,125.229.96.130,56735,"
                         "445,0,S,2152994813,,8192,,mss;nop;wscale;nop;nop;sackOK"),
    "paloalto_panos":   ("Jan 24 12:28:00 pa1 1,2026/01/24 12:28:00,001801000001,TRAFFIC,end,"
                         "2561,2026/01/24 12:27:59,10.0.0.5,198.51.100.9,203.0.113.4,"
                         "198.51.100.9,rule-web,user1,,ssl,vsys1,trust,untrust,ae1,ae2,fwd,x,"
                         "20134,1,443,51920,443,51920,0x400000,tcp,allow,182"),
    "suricata_fast":    ("07/16/2015-01:32:12.275324  [**] [1:2008983:6] ET USER_AGENTS "
                         "Suspicious User Agent (BlackSun) [**] [Classification: A Network "
                         "Trojan was detected] [Priority: 1] {TCP} 10.0.2.15:49779 -> 74.125.28.99:80"),
    "snort_fast":       ('07/16-09:23:39.153899  [**] [1:1000000:0] "SERVER-WEBAPP Apache Log4j '
                         'attempt" [**] [Classification: Attempted User Privilege Gain] '
                         '[Priority: 1] {TCP} 192.168.1.2:50284 -> 192.168.2.3:80'),
    "sshd_auth":        ("May  9 06:11:22 host sshd[2843]: Failed password for invalid user "
                         "admin from 10.0.0.5 port 55221 ssh2"),
    "dnsmasq":          "Jul  7 20:19:36 dnsmasq[536]: query[A] dnl-14.geo.kaspersky.com from 10.0.10.128",
    "isc_dhcpd":        "Mar  2 09:15:23 server dhcpd: DHCPREQUEST for 192.168.1.130 from aa:bb:cc:dd:ee:05 via eth0",
    "zeek_tsv":         "1747147647.668533\tCgnovV3tXhiyU385S\t192.168.1.8\t52917\t192.0.78.212\t80\ttcp\thttp\t0.098478\t71\t377\tREJ",
    "ossec_wazuh_alerts":"** Alert 1618409999.12345: - syslog,sshd,authentication_failed,",
    "selinux_avc":      ('type=AVC msg=audit(1455805464.059:137): avc:  denied  { append } for  '
                         'pid=861 comm="httpd" name="error_log" scontext=system_u:system_r:httpd_t:s0 '
                         'tcontext=system_u:object_r:var_run_t:s0 tclass=file permissive=0'),
}


def test_batch1_detection():
    print("batch-1 detection (each sample resolves to its intended adapter):")
    for want, line in BATCH1.items():
        adapter, conf, scores = la.detect_adapter([line])
        check(f"{want} detected (got {adapter.name})", adapter.name == want,
              f"conf={conf} scores={ {k:v for k,v in scores.items() if v>0} }")


def test_batch1_collision_guard():
    """The core collision requirement: on every batch-1 sample the intended
    (specific) adapter wins, and NO generic fallback (structural/generic_ts/raw)
    is ever tied at the top confidence. (Ties with the generic syslog/systemd/
    logfmt/auditd adapters are expected and resolved by registry ordering — the
    same pre-existing pattern as haproxy/kafka/springboot.)"""
    print("batch-1 collision guard (no fallback ever tied at the top):")
    FALLBACKS = {"structural", "generic_ts", "raw"}
    for want, line in BATCH1.items():
        adapter, conf, scores = la.detect_adapter([line])
        top = [k for k, v in scores.items() if abs(v - conf) < 1e-9]
        fb_in_tie = FALLBACKS & set(top)
        check(f"{want}: winner is intended + no fallback in tie",
              adapter.name == want and not fb_in_tie,
              f"winner={adapter.name} top={top}")


def test_batch1_parse():
    print("batch-1 parse → unified schema (level/category/source/fields):")

    def ev(name):
        return la.get_adapter(name).parse_line(BATCH1[name])

    e = ev("ps5_orbis_klog")
    check("ps5_orbis_klog source=module", e["source"] == "SceLncSysServiceProcess")
    check("ps5_orbis_klog titleId field", e["data"].get("titleId") == "CUSA00000", str(e["data"]))

    e = ev("orbis_module_tag")
    check("orbis_module_tag error+ret", e["level"] == "ERROR"
          and e["data"].get("r0") == "0x8002", str(e["data"]))

    e = ev("orbis_fatal_trap")
    check("orbis_fatal_trap FATAL", e["level"] == "FATAL" and e["category"] == "error"
          and e["data"].get("trap_number") == 12, str(e["data"]))

    e = ev("freebsd_messages")
    check("freebsd_messages source=kernel", e["source"] == "kernel"
          and e["data"].get("device") == "ada0", str(e["data"]))

    e = ev("linux_dmesg")
    check("linux_dmesg monotonic (no ts_ms) + uptime", e["ts_ms"] is None
          and e["data"].get("uptime") == 3.123456, str(e["data"]))

    e = ev("linux_dmesg_ctime")
    check("linux_dmesg_ctime wallclock ts", isinstance(e["ts_ms"], float) and e["ts_ms"] > 1e12)

    e = ev("linux_devkmsg")
    check("linux_devkmsg facility/level", e["level"] in ("INFO",) and e["data"].get("facility") == 0,
          str(e["data"]))

    e = ev("kernel_panic_oops")
    check("kernel_panic_oops FATAL+error", e["level"] == "FATAL" and e["category"] == "error")

    e = ev("netfilter_klog")
    check("netfilter WARN + tuple", e["level"] == "WARN" and e["data"].get("src") == "184.91.129.123"
          and e["data"].get("dpt") == "3389" and e["data"].get("tcp_flags") == "SYN", str(e["data"]))
    check("netfilter log_prefix", e["data"].get("log_prefix") == "[UFW BLOCK]", str(e["data"]))

    e = ev("esp32_panic")
    check("esp32_panic FATAL+cause", e["level"] == "FATAL"
          and e["data"].get("cause") == "LoadProhibited", str(e["data"]))

    e = ev("esp_idf_log")
    check("esp_idf INFO+tag", e["level"] == "INFO" and e["source"] == "wifi"
          and e["data"].get("uptime_ms") == 1523, str(e["data"]))

    e = ev("zephyr_log")
    check("zephyr_log INFO+module", e["level"] == "INFO" and e["source"] == "sample_instance.inst1")

    e = ev("zephyr_fatal")
    check("zephyr_fatal FATAL+num", e["level"] == "FATAL" and e["data"].get("error_number") == 3)

    e = ev("uboot_boot_log")
    check("uboot version", e["data"].get("version") == "2022.04", str(e["data"]))

    e = ev("freertos_logging")
    check("freertos task source", e["source"] == "Tmr Svc" and e["ts_ms"] is None, str(e["data"]))

    e = ev("macos_unified_log")
    check("macos Default→info", e["level"] == "INFO" and e["source"] == "kernel"
          and e["data"].get("type") == "Default", str(e["data"]))
    e2 = la.get_adapter("macos_unified_log").parse_line(
        BATCH1["macos_unified_log"].replace("Default", "Error"))
    check("macos Error→error", e2["level"] == "ERROR" and e2["category"] == "error")

    e = ev("rsyslog")
    check("rsyslog ts+tag", isinstance(e["ts_ms"], float) and e["source"] == "myapp"
          and e["data"].get("pid") == "1234", str(e["data"]))

    e = ev("ftrace")
    check("ftrace event", e["source"] == "sched_switch" and e["data"].get("cpu") == 0, str(e["data"]))

    e = ev("cef")
    check("cef sev10→fatal", e["level"] == "FATAL" and e["data"].get("act") == "blocked"
          and e["data"].get("src") == "10.0.0.1", str(e["data"]))

    e = ev("leef")
    check("leef sev8→error + attrs", e["level"] == "ERROR" and e["data"].get("dst") == "8.8.8.8"
          and e["data"].get("dstPort") == "80", str(e["data"]))

    e = ev("cisco_asa")
    check("cisco_asa sev3→error+id", e["level"] == "ERROR" and e["data"].get("message_id") == "106023"
          and e["source"] == "cisco.asa", str(e["data"]))

    e = ev("cisco_ios")
    check("cisco_ios sev3→error+mnem", e["level"] == "ERROR"
          and e["data"].get("mnemonic") == "UPDOWN", str(e["data"]))

    e = ev("fortigate")
    check("fortigate alert→fatal+desc", e["level"] == "FATAL"
          and e["data"].get("message") == "Admin login failed", str(e["data"]))

    e = ev("checkpoint")
    check("checkpoint Drop→warn", e["level"] == "WARN" and e["data"].get("src") == "192.168.96.80",
          str(e["data"]))

    e = ev("pfsense_filterlog")
    check("pfsense block→warn+tuple", e["level"] == "WARN" and e["data"].get("action") == "block"
          and e["data"].get("dstport") == "445", str(e["data"]))

    e = ev("paloalto_panos")
    check("panos traffic src/dst/action", e["data"].get("src") == "10.0.0.5"
          and e["data"].get("dst") == "198.51.100.9" and e["data"].get("action") == "allow",
          str(e["data"]))
    e2 = la.get_adapter("paloalto_panos").parse_line(BATCH1["paloalto_panos"]
        .replace(",TRAFFIC,end,", ",THREAT,vulnerability,").replace(",allow,182", ",alert,critical"))
    check("panos threat critical→fatal", e2["level"] == "FATAL", str(e2["data"]))

    e = ev("suricata_fast")
    check("suricata pri1→error+sid", e["level"] == "ERROR" and e["data"].get("sid") == "2008983"
          and e["data"].get("dst") == "74.125.28.99:80", str(e["data"]))

    e = ev("snort_fast")
    check("snort pri1→error+proto", e["level"] == "ERROR" and e["data"].get("proto") == "TCP"
          and e["ts_ms"] is not None, str(e["data"]))

    e = ev("sshd_auth")
    check("sshd failed→warn+user", e["level"] == "WARN" and e["data"].get("user") == "admin"
          and e["data"].get("src_ip") == "10.0.0.5", str(e["data"]))

    e = ev("dnsmasq")
    check("dnsmasq query fields", e["data"].get("verb") == "query"
          and e["data"].get("qtype") == "A" and e["data"].get("name") == "dnl-14.geo.kaspersky.com",
          str(e["data"]))

    e = ev("isc_dhcpd")
    check("dhcpd type+ip+iface", e["data"].get("msg_type") == "DHCPREQUEST"
          and e["data"].get("ip") == "192.168.1.130" and e["data"].get("interface") == "eth0",
          str(e["data"]))

    e = ev("zeek_tsv")
    check("zeek REJ→warn+uid", e["level"] == "WARN" and e["trace_id"] == "CgnovV3tXhiyU385S"
          and e["data"].get("conn_state") == "REJ", str(e["data"]))

    e = la.get_adapter("ossec_wazuh_alerts").parse_line("Rule: 5716 (level 12) -> 'sshd: authentication failed.'")
    check("wazuh level12→error", e["level"] == "ERROR" and e["data"].get("rule_id") == "5716", str(e["data"]))

    e = ev("selinux_avc")
    check("selinux denied→warn+ctx", e["level"] == "WARN" and e["data"].get("verdict") == "denied"
          and e["data"].get("comm") == "httpd" and e["data"].get("tclass") == "file", str(e["data"]))


def test_no_collision_with_existing():
    """Feed the ORIGINAL test-suite samples through detection and confirm the
    batch-1 adapters did not steal any of them (regression on the 41 built-ins)."""
    print("no regression: existing samples still resolve to their own adapter:")
    try:
        import test_log_adapters as base
    except Exception as exc:   # pragma: no cover
        check("import base samples", False, str(exc))
        return
    for expected, lines in base.SAMPLES.items():
        adapter, _c, _s = la.detect_adapter(lines)
        check(f"{expected} still wins (got {adapter.name})", adapter.name == expected)


def test_full_crossset_no_fallback_ties():
    """Cross-set audit: across EVERY batch-1 + existing sample, the winner is
    never a fallback (structural/generic_ts/raw), and structural/raw stay last."""
    print("cross-set audit (fallbacks never win a real sample; tail pinned):")
    import test_log_adapters as base
    allsamples = {f"b1:{k}": [v] for k, v in BATCH1.items()}
    allsamples.update({f"ex:{k}": v for k, v in base.SAMPLES.items()})
    bad = 0
    for label, lines in allsamples.items():
        adapter, _c, _s = la.detect_adapter(lines)
        if adapter.name in ("structural", "generic_ts", "raw"):
            bad += 1
            print(f"    fallback {adapter.name} won {label}")
    check("no fallback won any real sample", bad == 0, f"{bad} fallback wins")
    names = [a.name for a in la.REGISTRY]
    check("structural + raw are the last two, in order", names[-2:] == ["structural", "raw"], str(names[-2:]))


# ── Structural normalizer ────────────────────────────────────────────────────
def test_structural_shapes():
    print("structural normalizer — every shape yields a usable event:")
    st = la.get_adapter("structural")
    cases = {
        "kmsg_reltime":  ("[   12.345678] mydrv: init done", "kmsg_reltime", None),
        "kmsg_ctime":    ("[Mon Jul 20 12:00:00 2026] subsys: ok", "kmsg_ctime", True),
        "devkmsg":       ("6,339,5140900,-;widget up", "devkmsg", None),
        "syslog5424":    ("<34>1 2026-07-20T12:00:00Z h app 1 ID - disk full", "syslog5424", True),
        "syslog3164":    ("<13>Jul 20 12:00:00 h weird[9]: mystery", "syslog3164", True),
        "bsd_syslog":    ("Jul 20 12:00:00 host customd[55]: status ok", "bsd_syslog", True),
        "iso_prefix":    ("2026-07-20T12:00:00.5Z totally custom line", "iso_prefix", True),
        "level_bracket": ("[ERROR] boom code=17", "level_bracket", None),
        "level_prefix":  ("WARNING: disk high", "level_prefix", None),
        "logfmt":        ("svc=auth outcome=deny reason=locked", "logfmt", None),
        "embedded_json": ('done result={"ok": false, "code": 500}', "embedded_json", None),
        "clf":           ('10.0.0.9 - - [20/Jul/2026:12:00:00 +0000] "GET /x HTTP/1.1" 503 12', "clf", True),
        "text_keyword":  ("the process aborted with a fatal panic", "text", None),
        "text_unknown":  ("asd zzz 123 ~~~ nothing", "text", None),
    }
    for label, (line, shape, has_ts) in cases.items():
        ev = st.parse_line(line)
        ok = ev is not None and ev["data"].get("shape") == shape
        if has_ts is True:
            ok = ok and isinstance(ev["ts_ms"], float)
        check(f"{label} → shape={shape}", ok,
              f"got shape={ev and ev['data'].get('shape')} ts={ev and ev['ts_ms']}")
    # level inference / category mapping on the fallbacks
    check("structural [ERROR]→error cat", st.parse_line("[ERROR] x")["category"] == "error")
    check("structural WARNING:→warn cat", st.parse_line("WARNING: x")["category"] == "warn")
    check("structural keyword panic→error", st.parse_line("a fatal panic here")["category"] == "error")
    check("structural CLF 503→error", st.parse_line(
        '1.1.1.1 - - [20/Jul/2026:12:00:00 +0000] "GET / HTTP/1.1" 503 1')["category"] == "error")
    # logfmt captures all pairs
    ev = st.parse_line("svc=auth outcome=deny reason=locked")
    check("structural logfmt keeps pairs", ev["data"].get("svc") == "auth"
          and ev["data"].get("outcome") == "deny", str(ev["data"]))
    # embedded json merged into fields
    ev = st.parse_line('done result={"ok": false, "code": 500}')
    check("structural embedded-json merged", ev["data"].get("code") == 500, str(ev["data"]))


def test_structural_guarantees():
    print("structural normalizer — ranking + robustness guarantees:")
    st = la.get_adapter("structural")
    import test_log_adapters as base
    # never outranks a named adapter on that adapter's own sample
    viol = 0
    for nm, lines in base.SAMPLES.items():
        _, _, sc = la.detect_adapter(lines)
        if sc.get(nm, 0) > 0 and sc.get("structural", 0) >= sc.get(nm, 0):
            viol += 1
    check("structural never outranks a named adapter", viol == 0, f"{viol} violations")
    # confidence is capped in (raw, generic_ts)
    allconf = [st.detect([l]) for lines in list(base.SAMPLES.values()) + [[v] for v in BATCH1.values()]
               for l in lines]
    check("structural detect() capped ≤ 0.3", all(c <= 0.3001 for c in allconf), f"max={max(allconf):.3f}")
    check("structural floor (0.02) beats raw (0.01)", st.detect(["utterly unknown gibberish"]) > 0.01)
    # never raises, always returns for non-blank
    weird = ["", "   ", "\x00\x01\x02", "{", "}{}{", "=", "<<<>>>", "a" * 5000,
             "\t\t\t", "[]", "<>", "1,2,3", "\\", "%%%"]
    ok = True
    for w in weird:
        try:
            st.parse_line(w)
            st.detect([w])
        except Exception as e:   # pragma: no cover
            ok = False
            print("    RAISED on", repr(w), e)
    check("structural never raises", ok)
    check("blank line → None", st.parse_line("   ") is None)
    check("non-blank always returns event", st.parse_line("x") is not None)


def test_never_raises_autodetect():
    print("auto-detect path never raises on hostile input:")
    for w in ["", "   ", "\x00garbage", "{bad", "<999>x", "CEF:", "LEEF:",
              "date=", "type=AVC", "%ASA-", "filterlog:", "[", "]"]:
        try:
            la.parse_line(w)
            check(f"parse_line({w!r}) no raise", True)
        except Exception as e:   # pragma: no cover
            check(f"parse_line({w!r}) no raise", False, str(e))


if __name__ == "__main__":
    print("=" * 70)
    print("batch-1 adapters + structural normalizer test suite")
    print("=" * 70)
    test_batch1_detection()
    test_batch1_collision_guard()
    test_batch1_parse()
    test_no_collision_with_existing()
    test_full_crossset_no_fallback_ties()
    test_structural_shapes()
    test_structural_guarantees()
    test_never_raises_autodetect()
    print("=" * 70)
    if _fails:
        print(f"RESULT: {_fails} FAILURE(S)")
        sys.exit(1)
    print("RESULT: all tests passed")
    sys.exit(0)
