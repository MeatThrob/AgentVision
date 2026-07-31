"""
Family-module log adapters
================================================================================
The long tail of named log formats lives here, one module per family, so the
core `log_adapters.py` stays readable and families can grow in parallel without
merge collisions. Every adapter in these modules subclasses
`log_adapters.LogAdapter` and calls `log_adapters.register_adapter()` at import
time, wiring itself into the shared REGISTRY.

Import ORDER matters for the rare 1.0-confidence ties between a specific format
and the generic `syslog`/`systemd` fallbacks: adapters that register
`before="syslog"` (the earliest slot) must be registered before adapters that
register `before="systemd"`. We therefore load `security` first (its sshd/dhcp/
dnsmasq adapters must outrank the ISO `rsyslog` envelope adapter in `kernel`),
then `console`, then `kernel`. Every module is imported defensively so one bad
module can never take down the others or the core framework.
"""
from __future__ import annotations

for _mod in ("security", "console", "kernel",
             # `browser` must load BEFORE webserver: its browser_network adapter
             # matches `GET https://host/p 502 (Bad Gateway)`, which the
             # server-side access-log adapters would otherwise claim on a 1.0
             # tie. browser_network is deliberately strict (absolute URL AND a
             # parenthesised reason) so it cannot steal a real access-log line.
             "browser",
             "database", "webserver", "cloud", "runtime", "cicd", "messaging",
             # batch 3 — network must load after security (its cisco_iosxr
             # registers before=cisco_ios); the rest are order-independent.
             "devtools", "observability", "network", "os_platform",
             "virt", "bigdata",
             # batch 4 — backup must load BEFORE mainframe: spectrum_protect's
             # ANS/ANR message ids also fit the generic MVS message-id grammar
             # (mvs_message), and earlier registration wins the 1.0 tie.
             "telecom", "backup", "mainframe",
             # batch 5 — profiling (flamegraph/perf/bpftrace/gdb-mi) and
             # industrial/SCADA formats; order-independent of the others.
             "profiling", "industrial",
             # batch 6 — android (logcat-long/ART-GC/instrumentation/dropbox)
             # and apple (log-show-compact/ASL/system.log/legacy .crash);
             # apple's macos_asl registers before="syslog" like security's
             # 3164-shaped adapters — order-independent of the others.
             "android", "apple",
             # batch 8b — medium-tier stragglers; loaded near-LAST so its
             # additions never disturb an earlier module's 1.0-confidence
             # `before=` ties.
             "batch8b",
             # batch 9 — low-tier stragglers; loaded LAST of all for the same
             # reason (tie-break by REGISTRY order means it can only gain
             # structural/generic_ts fallthroughs, never steal a named sample).
             "batch9",
             # user_adapters — RUNTIME-added adapters persisted to
             # user_adapters.json. Loaded ABSOLUTELY LAST (after batch9) so a
             # user-supplied adapter can never disturb a built-in on a 1.0
             # `before=` tie; it can only claim a sample that would otherwise
             # fall to structural/generic_ts/raw (the coverage gap it closes).
             "user_adapters"):
    if _mod == "user_adapters":
        # Freeze the BUILT-IN name set before any user adapter registers, so a
        # user spec can never silently shadow a built-in (register_adapter is
        # idempotent by name and would otherwise REPLACE it). See
        # log_adapters.snapshot_builtin_names().
        try:
            from .. import log_adapters as _la
            _la.snapshot_builtin_names()
        except Exception as _exc:   # pragma: no cover - defensive
            import sys
            print(f"[log_adapters] could not snapshot built-in names: {_exc}",
                  file=sys.stderr)
    try:
        __import__(f"{__name__}.{_mod}")
    except Exception as _exc:   # pragma: no cover - defensive
        import sys
        print(f"[log_adapters] family module {_mod!r} failed to load: {_exc}",
              file=sys.stderr)
