"""
Module 1: Diagnostic Tools Layer
Collects live system health: CPU, RAM, disk, processes, network.
"""
import psutil
import platform
import subprocess
import shutil
from shared.schema.snapshot_schema import PerfInfo


def collect_perf(save_folder: str = "/") -> PerfInfo:
    info = PerfInfo()

    # CPU
    info.cpu_percent = psutil.cpu_percent(interval=0.1)

    # RAM
    vm = psutil.virtual_memory()
    info.ram_gb = round(vm.used / 1_073_741_824, 2)

    # Disk free on save folder's volume
    try:
        usage = shutil.disk_usage(save_folder)
        info.disk_free_gb = round(usage.free / 1_073_741_824, 2)
    except Exception:
        info.disk_free_gb = 0.0

    # GPU (macOS — try powermetrics; fall back gracefully)
    try:
        result = subprocess.run(
            ["sudo", "powermetrics", "-n", "1", "--samplers", "gpu_power", "-i", "100"],
            capture_output=True, text=True, timeout=2)
        for line in result.stdout.splitlines():
            if "GPU Active Residency" in line:
                pct = float(line.split(":")[1].strip().replace("%", ""))
                info.gpu_percent = round(pct, 1)
                break
    except Exception:
        info.gpu_percent = 0.0

    # Network latency (ping localhost as baseline)
    try:
        result = subprocess.run(["ping", "-c", "1", "-W", "200", "8.8.8.8"],
                                 capture_output=True, text=True, timeout=2)
        for line in result.stdout.splitlines():
            if "min/avg/max" in line:
                avg = float(line.split("/")[4])
                info.net_latency_ms = round(avg, 1)
                break
    except Exception:
        info.net_latency_ms = 0.0

    return info


def get_running_processes(limit: int = 10) -> list[dict]:
    """Top N processes by CPU usage — for diagnostics sidebar."""
    procs = []
    for p in sorted(psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]),
                    key=lambda x: x.info["cpu_percent"] or 0,
                    reverse=True)[:limit]:
        procs.append({
            "pid":  p.info["pid"],
            "name": p.info["name"],
            "cpu":  round(p.info["cpu_percent"] or 0, 1),
            "ram_mb": round((p.info["memory_info"].rss if p.info["memory_info"] else 0)
                            / 1_048_576, 1),
        })
    return procs
