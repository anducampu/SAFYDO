"""CPU, RAM, disk and temperature for Settings → System.

Read only on request (the tab polls while it is open); nothing here runs in
the background. Everything comes from /proc and /sys except the firmware's
throttle flags, which only vcgencmd can tell.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path


def _cpu_times() -> tuple[int, int]:
    """(idle, total) jiffies over all cores."""
    fields = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
    return idle, sum(fields[:8])


def _cpu_percent(window: float = 0.25) -> float | None:
    try:
        i0, t0 = _cpu_times()
        time.sleep(window)
        i1, t1 = _cpu_times()
    except (OSError, ValueError, IndexError):
        return None
    total = t1 - t0
    return round(100.0 * (1 - (i1 - i0) / total), 1) if total > 0 else None


def _meminfo() -> dict[str, int]:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            out[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


def _read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _disk(path: str, label: str) -> dict | None:
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return {"label": label, "path": path, "total": u.total, "used": u.used, "free": u.free}


def _throttled() -> dict | None:
    """Pi firmware flags: under-voltage / throttling now and since boot."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, errors="replace", timeout=3).stdout
        value = int(out.strip().split("=", 1)[1], 16)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    return {
        "raw": hex(value),
        "under_voltage": bool(value & 0x1),
        "throttled": bool(value & 0x4),
        "under_voltage_since_boot": bool(value & 0x10000),
        "throttled_since_boot": bool(value & 0x40000),
    }


def snapshot(usb_mount: str = "/media/usb") -> dict:
    mem = _meminfo()
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)
    temp = _read_int("/sys/class/thermal/thermal_zone0/temp")
    freq = _read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    freq_max = _read_int("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")
    try:
        load = list(os.getloadavg())
    except OSError:
        load = None
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        uptime = None
    disks = [d for d in (_disk("/", "SD card"),) if d]
    if os.path.ismount(usb_mount):
        usb = _disk(usb_mount, "USB drive")
        if usb:
            disks.append(usb)
    rss = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return {
        "cpu": {
            "percent": _cpu_percent(),
            "cores": os.cpu_count(),
            "load": load,
            "freq_mhz": freq // 1000 if freq else None,
            "freq_max_mhz": freq_max // 1000 if freq_max else None,
        },
        "temperature_c": round(temp / 1000, 1) if temp is not None else None,
        "memory": {
            "total": total,
            "available": available,
            "used": max(0, total - available),
            "swap_total": mem.get("SwapTotal", 0),
            "swap_used": max(0, mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)),
            "backend_rss": rss,
        },
        "disks": disks,
        "throttle": _throttled(),
        "uptime_s": uptime,
    }
