"""
NetHealth — Wi-Fi stability monitor with database logging and dashboard.

Features:
- Monitors router + internet latency, jitter, packet loss
- Wi-Fi stability % from signal + router quality
- SQLite database for long-term trends and hourly heatmaps
- Bufferbloat and traceroute tests (scheduled + on-demand from dashboard)
- System tray background monitor
- GUI dashboard (heatmaps, exports, history)

Dependencies:
    pip install pystray pillow matplotlib numpy

Windows only (ping, tracert, netsh wlan).
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from pystray import MenuItem as item

from dashboard import DashboardApp
from db import NetHealthDB, SummaryRow
from network_tests import (
    bufferbloat_test,
    compute_metrics,
    jitter_percent,
    ping,
    run_traceroute,
    wifi_signal,
    wifi_stability_percent,
)

# =========================
# FORCE CONSOLE (optional)
# =========================
try:
    ctypes.windll.kernel32.AllocConsole()
except OSError:
    pass

# =========================
# CONFIG
# =========================
TARGETS = {
    "ROUTER": "192.168.1.1",
    "INTERNET": "1.1.1.1",
}

TEST_DURATION = 300
IDLE_DURATION = 1500
PING_INTERVAL = 1.0
SUMMARY_INTERVAL = 20

# Run bufferbloat + traceroute once per idle cycle (after each 5-min test)
RUN_EXTENDED_TESTS_ON_IDLE = True

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
DB_PATH = DATA_DIR / "nethealth.db"

# =========================
# STATE
# =========================
stats = {
    name: {"latencies": [], "sent": 0, "loss": 0}
    for name in TARGETS
}

running = True
last_status = "NetHealth: starting…"
live_snapshot: dict = {}
db = NetHealthDB(DB_PATH)
_dashboard_thread: threading.Thread | None = None
_icon: pystray.Icon | None = None


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def update_live_snapshot(
    stability: float,
    signal: int | None,
    r_avg: float,
    r_jit: float,
    r_loss: float,
    i_avg: float,
    i_jit: float,
    i_loss: float,
) -> None:
    global last_status, live_snapshot
    live_snapshot = {
        "wifi_stability": stability,
        "signal": signal,
        "router_loss": r_loss,
        "internet_loss": i_loss,
        "router_jitter": r_jit,
        "internet_jitter": i_jit,
        "router_jitter_pct": jitter_percent(r_jit, r_avg),
        "internet_jitter_pct": jitter_percent(i_jit, i_avg),
        "router_avg": r_avg,
        "internet_avg": i_avg,
    }
    last_status = (
        f"Wi-Fi {stability:.0f}% | Loss R:{r_loss:.0f}% I:{i_loss:.0f}% | "
        f"Jitter R:{r_jit:.0f}ms I:{i_jit:.0f}ms"
    )


def record_summary() -> None:
    r = stats["ROUTER"]
    i = stats["INTERNET"]
    r_avg, r_jit, r_loss = compute_metrics(r["latencies"], r["sent"], r["loss"])
    i_avg, i_jit, i_loss = compute_metrics(i["latencies"], i["sent"], i["loss"])
    signal = wifi_signal()
    stability = wifi_stability_percent(signal, r_jit, r_loss)

    db.insert_summary(
        SummaryRow(
            timestamp=datetime.now(),
            router_avg_ms=r_avg,
            router_jitter_ms=r_jit,
            router_loss_pct=r_loss,
            internet_avg_ms=i_avg,
            internet_jitter_ms=i_jit,
            internet_loss_pct=i_loss,
            wifi_stability_pct=stability,
            wifi_signal_pct=signal,
        )
    )
    update_live_snapshot(stability, signal, r_avg, r_jit, r_loss, i_avg, i_jit, i_loss)

    log("================ SUMMARY ================")
    log(f"ROUTER   | Ping {r_avg:5.1f} ms | Jitter {r_jit:4.1f} ms ({jitter_percent(r_jit, r_avg):.0f}%) | Loss {r_loss:4.1f}%")
    log(f"INTERNET | Ping {i_avg:5.1f} ms | Jitter {i_jit:4.1f} ms ({jitter_percent(i_jit, i_avg):.0f}%) | Loss {i_loss:4.1f}%")
    log(f"WIFI STABILITY: {stability:.1f}% | Signal: {signal if signal else 'N/A'}%")
    log("========================================")


def run_scheduled_extended_tests() -> None:
    if not RUN_EXTENDED_TESTS_ON_IDLE or not running:
        return

    host = TARGETS["INTERNET"]
    log("Running scheduled bufferbloat test…")
    try:
        result = bufferbloat_test(host)
        db.insert_bufferbloat(
            host, result["baseline_ms"], result["loaded_ms"], result["grade"]
        )
        log(
            f"Bufferbloat grade {result['grade']}: "
            f"+{result['delta_ms']} ms under load "
            f"({result['baseline_ms']} → {result['loaded_ms']} ms)"
        )
    except Exception as e:
        log(f"Bufferbloat test failed: {e}")

    log(f"Running scheduled traceroute to {host}…")
    try:
        hops = run_traceroute(host)
        db.insert_traceroute(host, hops)
        log(f"Traceroute saved ({len(hops)} hops)")
    except Exception as e:
        log(f"Traceroute failed: {e}")


def monitor() -> None:
    global running

    log("NetHealth monitor started")
    log(f"Database: {DB_PATH}")
    log(f"Reports: {REPORTS_DIR}")
    log(f"Targets: ROUTER={TARGETS['ROUTER']} INTERNET={TARGETS['INTERNET']}")

    while running:
        for k in stats:
            stats[k]["latencies"].clear()
            stats[k]["sent"] = 0
            stats[k]["loss"] = 0

        start = time.time()
        last_summary = time.time()
        log("Starting 5-minute test cycle")

        while time.time() - start < TEST_DURATION and running:
            for name, host in TARGETS.items():
                stats[name]["sent"] += 1
                rtt = ping(host)
                timed_out = rtt is None
                if timed_out:
                    stats[name]["loss"] += 1
                else:
                    stats[name]["latencies"].append(rtt)
                db.insert_ping(name, host, rtt, timed_out)

            if time.time() - last_summary >= SUMMARY_INTERVAL:
                record_summary()
                last_summary = time.time()

            time.sleep(PING_INTERVAL)

        log("Cycle complete. Idle period.")
        if RUN_EXTENDED_TESTS_ON_IDLE:
            threading.Thread(target=run_scheduled_extended_tests, daemon=True).start()

        idle_end = time.time() + IDLE_DURATION
        while time.time() < idle_end and running:
            time.sleep(5)


# =========================
# TRAY
# =========================
def icon_img(color: str = "green") -> Image.Image:
    img = Image.new("RGB", (64, 64), "black")
    d = ImageDraw.Draw(img)
    d.rectangle((16, 16, 48, 48), fill=color)
    return img


def show_status(icon: pystray.Icon, _item: object) -> None:
    icon.notify(last_status, "NetHealth")


def open_dashboard_action(icon: pystray.Icon, _item: object) -> None:
    global _dashboard_thread

    def _run() -> None:
        app = DashboardApp(
            db=db,
            reports_dir=REPORTS_DIR,
            targets=TARGETS,
            live_stats=lambda: dict(live_snapshot),
        )
        app.run()

    if _dashboard_thread and _dashboard_thread.is_alive():
        log("Dashboard already open")
        return
    _dashboard_thread = threading.Thread(target=_run, daemon=True)
    _dashboard_thread.start()


def export_reports(icon: pystray.Icon, _item: object) -> None:
    csv_path = db.export_csv(REPORTS_DIR)
    html_path = db.export_html(REPORTS_DIR)
    icon.notify(f"CSV + HTML saved to reports/", "NetHealth Export")
    log(f"Exported: {csv_path.name}, {html_path.name}")


def exit_app(icon: pystray.Icon, _item: object) -> None:
    global running
    running = False
    log("Shutting down")
    icon.stop()
    sys.exit(0)


def start() -> None:
    global _icon

    menu = pystray.Menu(
        item("Show Status", show_status),
        item("Open Dashboard", open_dashboard_action),
        item("Export Reports", export_reports),
        item("Exit", exit_app),
    )

    _icon = pystray.Icon("NetHealth", icon_img(), "NetHealth", menu=menu)

    t = threading.Thread(target=monitor, daemon=True)
    t.start()

    time.sleep(1)
    log("Tray monitor running — right-click icon for dashboard")

    _icon.run()


if __name__ == "__main__":
    start()
