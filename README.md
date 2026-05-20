# NetHealth
### Real-Time Network Stability & Gaming Connectivity Monitor

<img width="1479" height="952" alt="image" src="https://github.com/user-attachments/assets/c7722b60-d96f-4040-a512-74b5a79e4736" />

NetHealth is a Python-based network observability and diagnostics tool designed to monitor Wi-Fi stability, ISP routing quality, latency consistency, jitter, and packet loss in real time.

Built initially as a solution for detecting unstable gaming conditions before queueing into competitive online games, the project evolved into a lightweight network monitoring and analysis platform inspired by tools like PingPlotter and MTR.

---

## Features

### Real-Time Monitoring
- Router stability monitoring (`192.168.1.1`)
- Internet stability monitoring (`1.1.1.1`)
- Simultaneous multi-target ping diagnostics
- Live latency tracking
- Jitter calculation
- Packet loss detection

### Wi-Fi Stability Analysis
- Wi-Fi signal strength polling using Windows `netsh`
- Stability scoring system inspired by WiFi Analyzer
- Correlation between signal quality and network consistency

### Historical Data Collection
- Persistent SQLite database logging
- Timestamped monitoring sessions
- Long-term network behavior tracking
- Time-of-day stability analysis

### Smart Diagnostics
- ISP vs Local Network issue classification
- Detection of unstable routing patterns
- Jitter spike analysis
- Queue-safe / unsafe monitoring logic (planned)

### Background Monitoring
- System tray application
- Automatic monitoring cycles
- Lightweight resource usage
- Timestamped session logs

---

## Example Use Cases

- Detect unstable internet before gaming
- Identify ISP congestion periods
- Compare Wi-Fi stability throughout the day
- Monitor packet loss trends
- Diagnose whether issues originate from:
  - local Wi-Fi
  - router instability
  - ISP congestion
  - upstream routing

---

## Example Console Output

```text
================ SUMMARY ================
ROUTER   | Ping  2.1 ms | Jitter 0.8 ms | Loss 0.0%
INTERNET | Ping 18.4 ms | Jitter 6.2 ms | Loss 0.0%
WIFI STABILITY: 91.4% | Signal: 84%
========================================
```

## Technologies Used
Python
SQLite
PyStray
Windows netsh
Subprocess networking tools
Threading / background monitoring

## Planned Features
PyQt dashboard UI
Real-time latency graphs
Bufferbloat detection
Historical trend visualization
Heatmaps for network stability by hour
Queue-safe prediction system
Alert notifications
Exportable reports
Prometheus-style metrics API

## Installation

## Clone Repository
git clone https://github.com/YOUR_USERNAME/NetHealth.git
cd NetHealth

## Install Dependencies
pip install pystray pillow
## Run
python pingtool.py
