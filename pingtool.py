"""
Wi-Fi Stability Tray Monitor (v3 - IMPROVED + TIME-STAMPED LOGS)
---------------------------------------------------------------
FEATURES:
- Monitors:
    * Router: 192.168.1.1
    * Internet: 1.1.1.1
- Computes:
    * latency
    * jitter
    * packet loss
    * Wi-Fi stability % (like WiFi Analyzer)
- Every 20 seconds:
    * prints per-target stats
    * prints Wi-Fi stability %
- Creates NEW log file on every run:
    wifi_log_YYYY-MM-DD_HH-MM-SS.txt
- System tray background monitor

Dependencies:
- pip install pystray pillow

Windows only
"""

import threading
import time
import subprocess
import statistics
import sys
import os
from datetime import datetime
import ctypes

import pystray
from pystray import MenuItem as item
from PIL import Image, ImageDraw

# =========================
# FORCE CONSOLE
# =========================
try:
    ctypes.windll.kernel32.AllocConsole()
except:
    pass

# =========================
# CONFIG
# =========================
TARGETS = {
    "ROUTER": "192.168.1.1",
    "INTERNET": "1.1.1.1"
}

TEST_DURATION = 300
IDLE_DURATION = 1500
PING_INTERVAL = 1.0
SUMMARY_INTERVAL = 20

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# NEW: timestamped log file per run
RUN_ID = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
LOG_FILE = os.path.join(BASE_DIR, f"wifi_log_{RUN_ID}.txt")

# =========================
# STATE
# =========================
stats = {
    name: {
        "latencies": [],
        "sent": 0,
        "loss": 0
    } for name in TARGETS
}

running = True
last_score = "N/A"

# =========================
# LOGGING
# =========================
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"

    print(line, flush=True)

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"LOG ERROR: {e}")

# =========================
# PING
# =========================
def ping(host):
    try:
        r = subprocess.run(
            ["ping", "-n", "1", "-w", "1000", host],
            capture_output=True,
            text=True
        )

        if "timed out" in r.stdout or "unreachable" in r.stdout:
            return None

        for line in r.stdout.splitlines():
            if "time=" in line:
                try:
                    return float(line.split("time=")[1].split("ms")[0])
                except:
                    pass
        return None
    except:
        return None

# =========================
# WIFI SIGNAL
# =========================
def wifi_signal():
    try:
        out = subprocess.check_output(["netsh", "wlan", "show", "interfaces"], text=True)
        for line in out.splitlines():
            if "Signal" in line:
                return int(line.split(":")[1].strip().replace("%", ""))
    except:
        return None

# =========================
# METRICS
# =========================
def compute(latencies, sent, loss):
    if len(latencies) > 1:
        jitter = statistics.stdev(latencies)
        avg = sum(latencies) / len(latencies)
    else:
        jitter = 0
        avg = 0

    loss_pct = (loss / sent) * 100 if sent else 0
    return avg, jitter, loss_pct

# =========================
# WIFI STABILITY (NEW)
# =========================
def wifi_stability_percent(signal, router_jitter, router_loss):
    signal = signal if signal is not None else 50

    jitter_penalty = min(router_jitter * 2, 100)
    loss_penalty = router_loss

    stability = (
        signal * 0.7 +
        (100 - jitter_penalty - loss_penalty) * 0.3
    )

    return max(0, min(100, stability))

# =========================
# FORMAT
# =========================
def format_summary(name):
    d = stats[name]
    avg, jitter, loss = compute(d["latencies"], d["sent"], d["loss"])

    return avg, jitter, loss

# =========================
# MONITOR LOOP
# =========================
def monitor():
    global running

    log("WiFi Monitor started")
    log(f"Log file: {LOG_FILE}")
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

                if rtt is None:
                    stats[name]["loss"] += 1
                    log(f"{name}: TIMEOUT ({host})")
                else:
                    stats[name]["latencies"].append(rtt)
                    log(f"{name}: {rtt:.1f} ms")

            # =========================
            # 20 SECOND SUMMARY
            # =========================
            if time.time() - last_summary >= SUMMARY_INTERVAL:

                r_avg, r_jit, r_loss = compute(
                    stats["ROUTER"]["latencies"],
                    stats["ROUTER"]["sent"],
                    stats["ROUTER"]["loss"]
                )

                i_avg, i_jit, i_loss = compute(
                    stats["INTERNET"]["latencies"],
                    stats["INTERNET"]["sent"],
                    stats["INTERNET"]["loss"]
                )

                signal = wifi_signal()
                stability = wifi_stability_percent(signal, r_jit, r_loss)

                log("================ SUMMARY ================")
                log(f"ROUTER   | Ping {r_avg:5.1f} ms | Jitter {r_jit:4.1f} ms | Loss {r_loss:4.1f}%")
                log(f"INTERNET | Ping {i_avg:5.1f} ms | Jitter {i_jit:4.1f} ms | Loss {i_loss:4.1f}%")
                log(f"WIFI STABILITY: {stability:.1f}% | Signal: {signal if signal else 'N/A'}%")
                log("========================================")

                last_summary = time.time()

            time.sleep(PING_INTERVAL)

        log("Cycle complete. Idle 25 minutes.")
        time.sleep(IDLE_DURATION)

# =========================
# TRAY
# =========================
def icon_img():
    img = Image.new('RGB', (64, 64), "black")
    d = ImageDraw.Draw(img)
    d.rectangle((16, 16, 48, 48), fill="green")
    return img


def exit_app(icon, item):
    global running
    running = False
    log("Shutting down")
    icon.stop()
    sys.exit()


def show(icon, item):
    icon.notify(last_score)

# =========================
# START
# =========================
def start():
    icon = pystray.Icon(
        "WiFi Monitor",
        icon_img(),
        menu=pystray.Menu(
            item("Show Status", show),
            item("Exit", exit_app)
        )
    )

    t = threading.Thread(target=monitor, daemon=True)
    t.start()

    time.sleep(1)
    log("Main thread started")

    icon.run()

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    start()