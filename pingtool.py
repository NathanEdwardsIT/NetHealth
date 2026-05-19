"""
NetHealth — Wi-Fi stability monitor with database logging and dashboard.

Windows only (ping, tracert, netsh wlan).
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from pystray import MenuItem as item

from dashboard import DashboardApp
from db import NetHealthDB, SummaryRow, quality_label
from network_tests import (
    bufferbloat_test,
    compute_metrics,
    jitter_percent,
    ping,
    run_traceroute,
    wifi_signal,
    wifi_stability_percent,
)

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

RUN_EXTENDED_TESTS_ON_IDLE = True

DASHBOARD_LIVE_REFRESH_SEC = 1
DASHBOARD_FULL_REFRESH_SEC = 5

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
DB_PATH = DATA_DIR / "nethealth.db"

# =========================
# STATE
# =========================
stats = {name: {"latencies": [], "sent": 0, "loss": 0} for name in TARGETS}

running = True
monitor_phase = "starting"
last_status = "NetHealth: starting…"

_live_lock = threading.Lock()
live_snapshot: dict = {}
live_history: deque = deque(maxlen=120)

db = NetHealthDB(DB_PATH)
_dashboard: DashboardApp | None = None
_ui_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
_tk_root: tk.Tk | None = None
_icon: pystray.Icon | None = None


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _get_live_snapshot() -> dict:
    with _live_lock:
        return dict(live_snapshot)


def update_live_snapshot(
    stability: float,
    signal: int | None,
    r_avg: float,
    r_jit: float,
    r_loss: float,
    i_avg: float,
    i_jit: float,
    i_loss: float,
    *,
    router_last_ms: float | None = None,
    internet_last_ms: float | None = None,
    router_last_ok: bool = True,
    internet_last_ok: bool = True,
) -> None:
    global last_status, live_snapshot
    instability = max(0.0, min(100.0, 100.0 - stability))
    quality = quality_label(stability)
    now = datetime.now()

    entry = {
        "wifi_stability": stability,
        "instability": instability,
        "wifi_quality": quality,
        "signal": signal,
        "router_loss": r_loss,
        "internet_loss": i_loss,
        "router_jitter": r_jit,
        "internet_jitter": i_jit,
        "router_jitter_pct": jitter_percent(r_jit, r_avg),
        "internet_jitter_pct": jitter_percent(i_jit, i_avg),
        "router_avg": r_avg,
        "internet_avg": i_avg,
        "router_last_ms": router_last_ms,
        "internet_last_ms": internet_last_ms,
        "router_last_ok": router_last_ok,
        "internet_last_ok": internet_last_ok,
        "monitor_phase": monitor_phase,
        "updated_at": now.strftime("%H:%M:%S"),
        "updated_epoch": now.timestamp(),
        "window_pings": stats["ROUTER"]["sent"],
    }

    with _live_lock:
        live_snapshot = entry
        live_history.append(
            {
                "stability": stability,
                "instability": instability,
                "router_ms": r_avg,
                "internet_ms": i_avg,
            }
        )
        entry["history"] = list(live_history)

    last_status = (
        f"{quality} · Wi-Fi {stability:.0f}% · "
        f"Router {r_avg:.0f}ms · ISP {i_avg:.0f}ms · "
        f"Loss R:{r_loss:.0f}% I:{i_loss:.0f}%"
    )


def refresh_live_from_window(
    router_last_ms: float | None,
    internet_last_ms: float | None,
    router_last_ok: bool,
    internet_last_ok: bool,
) -> None:
    """Recompute rolling-window metrics (called after every ping)."""
    r = stats["ROUTER"]
    i = stats["INTERNET"]
    r_avg, r_jit, r_loss = compute_metrics(r["latencies"], r["sent"], r["loss"])
    i_avg, i_jit, i_loss = compute_metrics(i["latencies"], i["sent"], i["loss"])
    signal = wifi_signal()
    stability = wifi_stability_percent(signal, r_jit, r_loss)
    update_live_snapshot(
        stability,
        signal,
        r_avg,
        r_jit,
        r_loss,
        i_avg,
        i_jit,
        i_loss,
        router_last_ms=router_last_ms,
        internet_last_ms=internet_last_ms,
        router_last_ok=router_last_ok,
        internet_last_ok=internet_last_ok,
    )


def record_summary() -> None:
    r = stats["ROUTER"]
    i = stats["INTERNET"]
    r_avg, r_jit, r_loss = compute_metrics(r["latencies"], r["sent"], r["loss"])
    i_avg, i_jit, i_loss = compute_metrics(i["latencies"], i["sent"], i["loss"])
    signal = wifi_signal()

    router_last = r["latencies"][-1] if r["latencies"] else None
    internet_last = i["latencies"][-1] if i["latencies"] else None

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
    update_live_snapshot(
        stability,
        signal,
        r_avg,
        r_jit,
        r_loss,
        i_avg,
        i_jit,
        i_loss,
        router_last_ms=router_last,
        internet_last_ms=internet_last,
        router_last_ok=router_last is not None,
        internet_last_ok=internet_last is not None,
    )

    log("================ SUMMARY ================")
    log(
        f"ROUTER   | Ping {r_avg:5.1f} ms | Jitter {r_jit:4.1f} ms "
        f"({jitter_percent(r_jit, r_avg):.0f}%) | Loss {r_loss:4.1f}%"
    )
    log(
        f"INTERNET | Ping {i_avg:5.1f} ms | Jitter {i_jit:4.1f} ms "
        f"({jitter_percent(i_jit, i_avg):.0f}%) | Loss {i_loss:4.1f}%"
    )
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
    global running, monitor_phase

    log("NetHealth monitor started")
    log(f"Database: {DB_PATH}")
    log(f"Targets: ROUTER={TARGETS['ROUTER']} INTERNET={TARGETS['INTERNET']}")

    while running:
        for k in stats:
            stats[k]["latencies"].clear()
            stats[k]["sent"] = 0
            stats[k]["loss"] = 0

        monitor_phase = "active"
        start = time.time()
        last_summary = time.time()
        log("Starting 5-minute test cycle")

        while time.time() - start < TEST_DURATION and running:
            ping_results: dict[str, tuple[float | None, bool]] = {}

            for name, host in TARGETS.items():
                stats[name]["sent"] += 1
                rtt = ping(host)
                timed_out = rtt is None
                if timed_out:
                    stats[name]["loss"] += 1
                else:
                    stats[name]["latencies"].append(rtt)
                db.insert_ping(name, host, rtt, timed_out)
                ping_results[name] = (rtt, not timed_out)

            refresh_live_from_window(
                router_last_ms=ping_results["ROUTER"][0],
                internet_last_ms=ping_results["INTERNET"][0],
                router_last_ok=ping_results["ROUTER"][1],
                internet_last_ok=ping_results["INTERNET"][1],
            )

            if time.time() - last_summary >= SUMMARY_INTERVAL:
                record_summary()
                last_summary = time.time()

            time.sleep(PING_INTERVAL)

        monitor_phase = "idle"
        with _live_lock:
            if live_snapshot:
                live_snapshot["monitor_phase"] = "idle"
        log("Cycle complete. Idle period.")

        if RUN_EXTENDED_TESTS_ON_IDLE:
            threading.Thread(target=run_scheduled_extended_tests, daemon=True).start()

        idle_end = time.time() + IDLE_DURATION
        while time.time() < idle_end and running:
            time.sleep(5)


# =========================
# TRAY + UI (Tk on main thread)
# =========================
def icon_img(color: str = "green") -> Image.Image:
    img = Image.new("RGB", (64, 64), "black")
    d = ImageDraw.Draw(img)
    d.rectangle((16, 16, 48, 48), fill=color)
    return img


def show_status(icon: pystray.Icon, _item: object) -> None:
    icon.notify(last_status, "NetHealth")


def open_dashboard_action(icon: pystray.Icon, _item: object) -> None:
    _ui_queue.put("open")


def export_reports(icon: pystray.Icon, _item: object) -> None:
    csv_path = db.export_csv(REPORTS_DIR)
    html_path = db.export_html(REPORTS_DIR)
    icon.notify("CSV + HTML saved to reports/", "NetHealth Export")
    log(f"Exported: {csv_path.name}, {html_path.name}")


def exit_app(icon: pystray.Icon, _item: object) -> None:
    global running
    running = False
    log("Shutting down")
    icon.stop()
    if _tk_root:
        _tk_root.after(0, _tk_root.quit)
    sys.exit(0)


def _run_tray() -> None:
    menu = pystray.Menu(
        item("Show Status", show_status),
        item("Open Dashboard", open_dashboard_action),
        item("Export Reports", export_reports),
        item("Exit", exit_app),
    )
    global _icon
    _icon = pystray.Icon("NetHealth", icon_img(), "NetHealth", menu=menu)
    _icon.run()


def _open_dashboard_window() -> None:
    global _dashboard
    if _dashboard is not None and _dashboard.is_open():
        _dashboard.raise_window()
        return

    _dashboard = DashboardApp(
        master=_tk_root,
        db=db,
        reports_dir=REPORTS_DIR,
        targets=TARGETS,
        live_refresh_seconds=DASHBOARD_LIVE_REFRESH_SEC,
        full_refresh_seconds=DASHBOARD_FULL_REFRESH_SEC,
        live_stats=_get_live_snapshot,
    )


def _process_ui_queue() -> None:
    while True:
        try:
            cmd = _ui_queue.get_nowait()
        except queue.Empty:
            break
        if cmd == "open":
            _open_dashboard_window()


def _tk_pump() -> None:
    global _dashboard
    _process_ui_queue()
    if _dashboard is not None:
        if _dashboard.is_open():
            _dashboard.pump()
        else:
            _dashboard = None
    if _tk_root and running:
        _tk_root.after(250, _tk_pump)


def start() -> None:
    global _tk_root

    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=_run_tray, daemon=True).start()

    _tk_root = tk.Tk()
    _tk_root.withdraw()
    _tk_root.title("NetHealth")
    _tk_pump()

    time.sleep(0.5)
    log("Tray monitor running — right-click icon for dashboard")

    _tk_root.mainloop()


if __name__ == "__main__":
    start()
