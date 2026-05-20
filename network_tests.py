"""Ping, Wi-Fi signal, traceroute, and bufferbloat measurements."""

from __future__ import annotations

import re
import statistics
import subprocess
import threading
import time
import urllib.request
from typing import Any

# Bufferbloat grading (loaded - baseline latency delta in ms)
BLOAT_GRADES = [
    (5, "A+"),
    (15, "A"),
    (30, "B"),
    (50, "C"),
    (100, "D"),
    (9999, "F"),
]

LOAD_URL = "https://speed.cloudflare.com/__down?bytes=25000000"


def ping(host: str, timeout_ms: int = 1000) -> float | None:
    try:
        r = subprocess.run(
            ["ping", "-n", "1", "-w", str(timeout_ms), host],
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000) + 2,
        )
        if "timed out" in r.stdout or "unreachable" in r.stdout:
            return None
        for line in r.stdout.splitlines():
            if "time=" in line:
                try:
                    return float(line.split("time=")[1].split("ms")[0])
                except (IndexError, ValueError):
                    pass
        return None
    except (subprocess.TimeoutExpired, OSError):
        return None


def wifi_signal() -> int | None:
    try:
        out = subprocess.check_output(
            ["netsh", "wlan", "show", "interfaces"],
            text=True,
            timeout=5,
        )
        for line in out.splitlines():
            if "Signal" in line:
                return int(line.split(":")[1].strip().replace("%", ""))
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return None


def compute_metrics(latencies: list[float], sent: int, loss: int) -> tuple[float, float, float]:
    if len(latencies) > 1:
        jitter = statistics.stdev(latencies)
        avg = sum(latencies) / len(latencies)
    elif len(latencies) == 1:
        jitter = 0.0
        avg = latencies[0]
    else:
        jitter = 0.0
        avg = 0.0
    loss_pct = (loss / sent) * 100 if sent else 0.0
    return avg, jitter, loss_pct


def wifi_stability_percent(
    signal: int | None,
    router_jitter: float,
    router_loss: float,
) -> float:
    sig = signal if signal is not None else 50
    jitter_penalty = min(router_jitter * 2, 100)
    loss_penalty = router_loss
    stability = sig * 0.7 + (100 - jitter_penalty - loss_penalty) * 0.3
    return max(0.0, min(100.0, stability))


def jitter_percent(jitter_ms: float, avg_ms: float) -> float:
    """Jitter as % of average latency (0 if no baseline)."""
    if avg_ms <= 0:
        return 0.0
    return min(100.0, (jitter_ms / avg_ms) * 100)


def grade_bufferbloat(delta_ms: float) -> str:
    for threshold, grade in BLOAT_GRADES:
        if delta_ms <= threshold:
            return grade
    return "F"


def _download_worker(stop: threading.Event) -> None:
    try:
        req = urllib.request.Request(LOAD_URL, headers={"User-Agent": "NetHealth/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            while not stop.is_set():
                chunk = resp.read(65536)
                if not chunk:
                    break
    except OSError:
        pass


def bufferbloat_test(
    host: str = "1.1.1.1",
    baseline_pings: int = 12,
    loaded_pings: int = 12,
) -> dict[str, Any]:
    """
    Measure latency idle vs under download load.
    Returns baseline/loaded averages, delta, and letter grade.
    """
    baseline_samples: list[float] = []
    for _ in range(baseline_pings):
        rtt = ping(host)
        if rtt is not None:
            baseline_samples.append(rtt)
        time.sleep(0.25)

    baseline = statistics.mean(baseline_samples) if baseline_samples else 0.0

    stop = threading.Event()
    loader = threading.Thread(target=_download_worker, args=(stop,), daemon=True)
    loader.start()
    time.sleep(0.5)

    loaded_samples: list[float] = []
    for _ in range(loaded_pings):
        rtt = ping(host)
        if rtt is not None:
            loaded_samples.append(rtt)
        time.sleep(0.25)

    stop.set()
    loader.join(timeout=2)

    loaded = statistics.mean(loaded_samples) if loaded_samples else baseline
    delta = max(0.0, loaded - baseline)
    return {
        "host": host,
        "baseline_ms": round(baseline, 2),
        "loaded_ms": round(loaded, 2),
        "delta_ms": round(delta, 2),
        "grade": grade_bufferbloat(delta),
    }


def run_traceroute(target: str, max_hops: int = 30) -> list[dict[str, Any]]:
    """Parse Windows tracert output into hop list."""
    hops: list[dict[str, Any]] = []
    try:
        r = subprocess.run(
            ["tracert", "-d", "-h", str(max_hops), target],
            capture_output=True,
            text=True,
            timeout=120,
        )
        stdout = r.stdout
    except (subprocess.TimeoutExpired, OSError):
        return hops

    hop_re = re.compile(
        r"^\s*(\d+)\s+(?:(\d+)\s+ms|\*)\s+(?:(\d+)\s+ms|\*)\s+(?:(\d+)\s+ms|\*)\s+(.+)$"
    )
    for line in stdout.splitlines():
        m = hop_re.match(line)
        if not m:
            continue
        hop_num = int(m.group(1))
        latencies = []
        for g in (m.group(2), m.group(3), m.group(4)):
            if g:
                latencies.append(float(g))
        host = m.group(5).strip()
        avg_lat = statistics.mean(latencies) if latencies else None
        hops.append(
            {
                "hop": hop_num,
                "host": host,
                "latency_ms": round(avg_lat, 2) if avg_lat is not None else None,
            }
        )
    return hops