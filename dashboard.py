"""Tkinter GUI dashboard — live stability view with reliable auto-refresh."""

from __future__ import annotations

import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import numpy as np

from db import READABLE_SUMMARY_COLUMNS, NetHealthDB, SummaryFilters, WEEKDAY_NAMES
from network_tests import bufferbloat_test, run_traceroute


WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEFAULT_LIVE_REFRESH_SEC = 1
DEFAULT_FULL_REFRESH_SEC = 5
QUALITY_OPTIONS = ["", "Excellent", "Good", "Fair", "Poor"]

QUALITY_COLORS = {
    "Excellent": "#3fb950",
    "Good": "#58a6ff",
    "Fair": "#d29922",
    "Poor": "#f85149",
    "Unknown": "#8b949e",
}


class DashboardApp:
    def __init__(
        self,
        db: NetHealthDB,
        reports_dir: Path,
        targets: dict[str, str],
        master: tk.Misc | None = None,
        live_refresh_seconds: int = DEFAULT_LIVE_REFRESH_SEC,
        full_refresh_seconds: int = DEFAULT_FULL_REFRESH_SEC,
        on_run_traceroute: Callable[[], None] | None = None,
        on_run_bufferbloat: Callable[[], None] | None = None,
        live_stats: Callable[[], dict] | None = None,
    ) -> None:
        self.db = db
        self.reports_dir = reports_dir
        self.targets = targets
        self.live_refresh_seconds = max(1, live_refresh_seconds)
        self.full_refresh_seconds = max(self.live_refresh_seconds, full_refresh_seconds)
        self.on_run_traceroute = on_run_traceroute
        self.on_run_bufferbloat = on_run_bufferbloat
        self.live_stats = live_stats

        self._embedded = master is not None
        self._tools_busy = False
        self._heatmap_days = 30
        self._colorbar = None
        self._last_full_refresh = 0.0
        self._refresh_count = 0

        if master is not None:
            self.root = tk.Toplevel(master)
        else:
            self.root = tk.Tk()

        self.root.title("NetHealth Dashboard")
        self.root.geometry("1200x820")
        self.root.configure(bg="#0d1117")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.auto_refresh_var = tk.BooleanVar(value=True)
        self._style()
        self._build_ui()
        self.refresh_live()
        self.refresh_full()

    def is_open(self) -> bool:
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def raise_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background="#0d1117")
        style.configure("TNotebook.Tab", padding=[12, 6], background="#161b22", foreground="#c9d1d9")
        style.map("TNotebook.Tab", background=[("selected", "#21262d")])
        style.configure("TFrame", background="#0d1117")
        style.configure("TLabel", background="#0d1117", foreground="#c9d1d9", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"), foreground="#58a6ff")
        style.configure("Metric.TLabel", font=("Segoe UI", 18, "bold"), foreground="#3fb950")
        style.configure("LiveBig.TLabel", font=("Segoe UI", 28, "bold"), foreground="#3fb950")
        style.configure("LiveSub.TLabel", font=("Segoe UI", 11), foreground="#8b949e")
        style.configure("Subtle.TLabel", foreground="#8b949e", font=("Segoe UI", 9))
        style.configure("TButton", padding=6)

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root)
        header.pack(fill=tk.X, padx=12, pady=8)
        ttk.Label(header, text="NetHealth", style="Header.TLabel").pack(side=tk.LEFT)

        ttk.Checkbutton(
            header,
            text="Auto-refresh",
            variable=self.auto_refresh_var,
        ).pack(side=tk.RIGHT, padx=8)
        ttk.Button(header, text="Refresh now", command=self._manual_refresh).pack(side=tk.RIGHT, padx=4)
        ttk.Button(header, text="Export CSV", command=self._export_csv).pack(side=tk.RIGHT, padx=4)
        ttk.Button(header, text="Export HTML", command=self._export_html).pack(side=tk.RIGHT, padx=4)

        self.status_label = ttk.Label(header, text="", style="Subtle.TLabel")
        self.status_label.pack(side=tk.RIGHT, padx=12)

        self._build_live_banner()

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        self._tab_overview()
        self._tab_analytics()
        self._tab_heatmaps()
        self._tab_explorer()
        self._tab_tools()

    def _build_live_banner(self) -> None:
        """Always-visible live stability panel at the top."""
        banner = tk.Frame(self.root, bg="#161b22", padx=16, pady=12)
        banner.pack(fill=tk.X, padx=8, pady=(0, 4))

        left = tk.Frame(banner, bg="#161b22")
        left.pack(side=tk.LEFT, fill=tk.Y)

        tk.Label(left, text="RIGHT NOW", bg="#161b22", fg="#8b949e", font=("Segoe UI", 9)).pack(anchor="w")
        self.live_quality_label = tk.Label(
            left, text="—", bg="#161b22", fg="#3fb950", font=("Segoe UI", 11, "bold")
        )
        self.live_quality_label.pack(anchor="w")

        stab_row = tk.Frame(left, bg="#161b22")
        stab_row.pack(anchor="w", pady=(4, 0))
        self.live_stability_label = tk.Label(
            stab_row, text="—%", bg="#161b22", fg="#3fb950", font=("Segoe UI", 32, "bold")
        )
        self.live_stability_label.pack(side=tk.LEFT)
        inst_col = tk.Frame(stab_row, bg="#161b22")
        inst_col.pack(side=tk.LEFT, padx=(12, 0))
        tk.Label(inst_col, text="unstable", bg="#161b22", fg="#8b949e", font=("Segoe UI", 9)).pack(anchor="w")
        self.live_instability_label = tk.Label(
            inst_col, text="—%", bg="#161b22", fg="#f85149", font=("Segoe UI", 16, "bold")
        )
        self.live_instability_label.pack(anchor="w")

        self.live_age_label = tk.Label(
            left, text="Waiting for monitor…", bg="#161b22", fg="#8b949e", font=("Segoe UI", 9)
        )
        self.live_age_label.pack(anchor="w", pady=(6, 0))
        self.live_phase_label = tk.Label(
            left, text="", bg="#161b22", fg="#58a6ff", font=("Segoe UI", 9)
        )
        self.live_phase_label.pack(anchor="w")

        mid = tk.Frame(banner, bg="#161b22")
        mid.pack(side=tk.LEFT, fill=tk.Y, padx=40)

        for key, title in (("router", "Router (local)"), ("internet", "ISP / Internet")):
            box = tk.Frame(mid, bg="#21262d", padx=12, pady=8)
            box.pack(side=tk.LEFT, padx=8)
            tk.Label(box, text=title, bg="#21262d", fg="#8b949e", font=("Segoe UI", 9)).pack(anchor="w")
            lbl_ping = tk.Label(box, text="— ms", bg="#21262d", fg="#c9d1d9", font=("Segoe UI", 14, "bold"))
            lbl_ping.pack(anchor="w")
            lbl_sub = tk.Label(box, text="", bg="#21262d", fg="#8b949e", font=("Segoe UI", 9))
            lbl_sub.pack(anchor="w")
            lbl_dot = tk.Label(box, text="●", bg="#21262d", fg="#8b949e", font=("Segoe UI", 12))
            lbl_dot.pack(anchor="e")
            setattr(self, f"live_{key}_ping", lbl_ping)
            setattr(self, f"live_{key}_sub", lbl_sub)
            setattr(self, f"live_{key}_dot", lbl_dot)

        right = tk.Frame(banner, bg="#161b22")
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        tk.Label(
            right, text="Live trend (rolling window)", bg="#161b22", fg="#8b949e", font=("Segoe UI", 9)
        ).pack(anchor="e")
        self.live_fig = Figure(figsize=(4.2, 1.2), facecolor="#161b22")
        self.live_ax = self.live_fig.add_subplot(111)
        self.live_canvas = FigureCanvasTkAgg(self.live_fig, master=right)
        self.live_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self.stability_bar = ttk.Progressbar(banner, length=200, mode="determinate", maximum=100)
        self.stability_bar.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))

    def _tab_overview(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Overview")

        ttk.Label(
            frame,
            text="Rolling window metrics (same data as Live banner above)",
            style="Subtle.TLabel",
        ).pack(anchor="w", padx=16, pady=(8, 0))

        self.metric_labels: dict[str, ttk.Label] = {}
        grid = ttk.Frame(frame)
        grid.pack(fill=tk.X, padx=16, pady=8)

        metrics = [
            ("wifi_stability", "Wi-Fi Stability"),
            ("router_loss", "Router Packet Loss"),
            ("internet_loss", "Internet Packet Loss"),
            ("router_jitter", "Router Jitter"),
            ("internet_jitter", "Internet Jitter"),
            ("signal", "Wi-Fi Signal"),
        ]
        for i, (key, title) in enumerate(metrics):
            cell = ttk.Frame(grid)
            cell.grid(row=i // 3, column=i % 3, padx=20, pady=8, sticky="w")
            ttk.Label(cell, text=title).pack(anchor="w")
            lbl = ttk.Label(cell, text="—", style="Metric.TLabel")
            lbl.pack(anchor="w")
            self.metric_labels[key] = lbl

        self.worst_text = tk.Text(
            frame, height=7, bg="#161b22", fg="#c9d1d9", font=("Consolas", 10), relief=tk.FLAT
        )
        ttk.Label(frame, text="Historical: most unstable times of day").pack(anchor="w", padx=16)
        self.worst_text.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)

    def _tab_analytics(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Analytics")

        self.analytics_text = tk.Text(
            frame, height=14, bg="#161b22", fg="#c9d1d9", font=("Consolas", 10), relief=tk.FLAT
        )
        self.analytics_text.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)

        ttk.Label(frame, text="Daily rollups", style="Subtle.TLabel").pack(anchor="w", padx=16)
        daily_cols = (
            "calendar_date",
            "snapshots",
            "avg_wifi_stability_pct",
            "min_wifi_stability_pct",
            "poor_quality_count",
        )
        self.daily_tree = ttk.Treeview(frame, columns=daily_cols, show="headings", height=8)
        for c in daily_cols:
            self.daily_tree.heading(c, text=c.replace("_", " ").title())
            self.daily_tree.column(c, width=130)
        self.daily_tree.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)

    def _tab_heatmaps(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Heatmaps")

        ctrl = ttk.Frame(frame)
        ctrl.pack(fill=tk.X, padx=16, pady=4)
        ttk.Label(ctrl, text="Lookback (days):").pack(side=tk.LEFT)
        self.heatmap_days_var = tk.StringVar(value="30")
        ttk.Combobox(
            ctrl, textvariable=self.heatmap_days_var, values=["7", "14", "30", "90"], width=6
        ).pack(side=tk.LEFT, padx=8)
        ttk.Button(ctrl, text="Apply", command=self._apply_heatmap_days).pack(side=tk.LEFT)

        self.fig = Figure(figsize=(10, 4), facecolor="#0d1117")
        self.ax_hour = self.fig.add_subplot(121)
        self.ax_week = self.fig.add_subplot(122)
        self.canvas = FigureCanvasTkAgg(self.fig, master=frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def _tab_explorer(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Data Explorer")

        filters = ttk.LabelFrame(frame, text="Filters")
        filters.pack(fill=tk.X, padx=12, pady=8)

        row1 = ttk.Frame(filters)
        row1.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row1, text="Days:").pack(side=tk.LEFT)
        self.filter_days = tk.StringVar(value="7")
        ttk.Combobox(
            row1, textvariable=self.filter_days, values=["1", "7", "14", "30", "90", "365"], width=6
        ).pack(side=tk.LEFT, padx=4)
        ttk.Label(row1, text="Quality:").pack(side=tk.LEFT, padx=(12, 0))
        self.filter_quality = tk.StringVar(value="")
        ttk.Combobox(row1, textvariable=self.filter_quality, values=QUALITY_OPTIONS, width=10).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Label(row1, text="Hour:").pack(side=tk.LEFT, padx=(12, 0))
        self.filter_hour = tk.StringVar(value="")
        ttk.Entry(row1, textvariable=self.filter_hour, width=5).pack(side=tk.LEFT, padx=4)
        ttk.Label(row1, text="Weekday:").pack(side=tk.LEFT, padx=(12, 0))
        self.filter_weekday = tk.StringVar(value="")
        ttk.Combobox(
            row1, textvariable=self.filter_weekday, values=[""] + WEEKDAY_NAMES, width=12
        ).pack(side=tk.LEFT, padx=4)

        row2 = ttk.Frame(filters)
        row2.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row2, text="Min stability %:").pack(side=tk.LEFT)
        self.filter_min_stab = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.filter_min_stab, width=6).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="Min router loss %:").pack(side=tk.LEFT, padx=(12, 0))
        self.filter_min_loss = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.filter_min_loss, width=6).pack(side=tk.LEFT, padx=4)
        ttk.Label(row2, text="Search:").pack(side=tk.LEFT, padx=(12, 0))
        self.filter_search = tk.StringVar(value="")
        ttk.Entry(row2, textvariable=self.filter_search, width=24).pack(side=tk.LEFT, padx=4)
        ttk.Button(row2, text="Apply filters", command=self._refresh_explorer).pack(side=tk.LEFT, padx=8)
        ttk.Button(row2, text="Clear", command=self._clear_explorer_filters).pack(side=tk.LEFT)

        self.explorer_summary = ttk.Label(frame, text="", style="Subtle.TLabel")
        self.explorer_summary.pack(anchor="w", padx=16)

        self.explorer_tree = ttk.Treeview(frame, columns=READABLE_SUMMARY_COLUMNS, show="headings", height=14)
        col_widths = {"recorded_at": 150, "day_name": 90, "time_slot": 100, "wifi_quality": 80}
        for c in READABLE_SUMMARY_COLUMNS:
            self.explorer_tree.heading(c, text=c.replace("_", " ").title())
            self.explorer_tree.column(c, width=col_widths.get(c, 85))
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.explorer_tree.yview)
        self.explorer_tree.configure(yscrollcommand=scroll.set)
        self.explorer_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=12, pady=8)
        scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=8)

    def _tab_tools(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Tools")

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=tk.X, padx=16, pady=12)
        ttk.Button(btn_row, text="Run Bufferbloat Test", command=self._run_bufferbloat_ui).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(btn_row, text="Run Traceroute (Internet)", command=self._run_traceroute_ui).pack(
            side=tk.LEFT, padx=4
        )

        self.tools_text = tk.Text(
            frame, height=22, bg="#161b22", fg="#c9d1d9", font=("Consolas", 10), relief=tk.FLAT
        )
        self.tools_text.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)

    def pump(self) -> None:
        """Called from main-thread Tk pump (~4x/sec). Drives auto-refresh."""
        if not self.is_open():
            return
        if not self.auto_refresh_var.get():
            return

        now = time.monotonic()
        self.refresh_live()
        if now - self._last_full_refresh >= self.full_refresh_seconds:
            self.refresh_full()

    def _manual_refresh(self) -> None:
        self.refresh_live()
        self.refresh_full()

    def _live_age_seconds(self, live: dict) -> float | None:
        epoch = live.get("updated_epoch")
        if epoch is None:
            return None
        return max(0.0, time.time() - float(epoch))

    def _apply_live_banner(self, live: dict) -> None:
        if not live:
            self.live_age_label.configure(text="No live data — is pingtool.py running?")
            return

        stability = float(live.get("wifi_stability", 0))
        instability = float(live.get("instability", 100 - stability))
        quality = live.get("wifi_quality", "Unknown")
        color = QUALITY_COLORS.get(str(quality), QUALITY_COLORS["Unknown"])

        self.live_quality_label.configure(text=str(quality), fg=color)
        self.live_stability_label.configure(text=f"{stability:.0f}%", fg=color)
        self.live_instability_label.configure(text=f"{instability:.0f}%")
        self.stability_bar["value"] = stability

        age = self._live_age_seconds(live)
        if age is not None:
            if age < 3:
                age_txt = "just now"
            elif age < 60:
                age_txt = f"{age:.0f}s ago"
            else:
                age_txt = f"{age / 60:.0f}m ago"
            self.live_age_label.configure(
                text=f"Updated {live.get('updated_at', '?')} ({age_txt}) · "
                f"{live.get('window_pings', 0)} pings in window"
            )
        else:
            self.live_age_label.configure(text="Updated recently")

        phase = live.get("monitor_phase", "unknown")
        phase_txt = {
            "active": "● Monitoring active (pinging every second)",
            "idle": "○ Idle between test cycles",
            "starting": "… Starting monitor",
        }.get(phase, phase)
        self.live_phase_label.configure(text=phase_txt)

        self._update_link_panel(
            "router",
            live.get("router_last_ms"),
            live.get("router_avg"),
            live.get("router_loss"),
            live.get("router_jitter"),
            live.get("router_last_ok", True),
        )
        self._update_link_panel(
            "internet",
            live.get("internet_last_ms"),
            live.get("internet_avg"),
            live.get("internet_loss"),
            live.get("internet_jitter"),
            live.get("internet_last_ok", True),
        )

        self._plot_live_sparkline(live.get("history") or [])

    def _update_link_panel(
        self,
        key: str,
        last_ms: float | None,
        avg_ms: float | None,
        loss: float | None,
        jitter: float | None,
        ok: bool,
    ) -> None:
        ping_lbl = getattr(self, f"live_{key}_ping")
        sub_lbl = getattr(self, f"live_{key}_sub")
        dot_lbl = getattr(self, f"live_{key}_dot")

        if last_ms is not None:
            ping_lbl.configure(text=f"{last_ms:.0f} ms")
        elif not ok:
            ping_lbl.configure(text="timeout")
        else:
            ping_lbl.configure(text="— ms")

        sub_lbl.configure(
            text=f"avg {avg_ms or 0:.0f} ms · loss {loss or 0:.0f}% · jitter {jitter or 0:.0f} ms"
        )
        dot_lbl.configure(fg="#3fb950" if ok else "#f85149")

    def _plot_live_sparkline(self, history: list[dict]) -> None:
        ax = self.live_ax
        ax.clear()
        ax.set_facecolor("#161b22")

        if len(history) < 2:
            ax.text(
                0.5, 0.5, "Collecting live samples…",
                ha="center", va="center", color="#8b949e", transform=ax.transAxes,
            )
            ax.set_xticks([])
            ax.set_yticks([])
            self.live_canvas.draw_idle()
            return

        stab = [float(h.get("stability", 0)) for h in history]
        x = list(range(len(stab)))
        ax.fill_between(x, stab, alpha=0.25, color="#3fb950")
        ax.plot(x, stab, color="#3fb950", linewidth=1.5, label="stability")
        ax.plot(x, [100 - s for s in stab], color="#f85149", linewidth=1, alpha=0.6, label="instability")
        ax.set_ylim(0, 100)
        ax.set_xlim(0, max(len(stab) - 1, 1))
        ax.tick_params(colors="#8b949e", labelsize=7)
        ax.set_ylabel("%", color="#8b949e", fontsize=7)
        for spine in ax.spines.values():
            spine.set_color("#30363d")
        ax.legend(loc="upper right", fontsize=6, facecolor="#161b22", edgecolor="#30363d")
        self.live_fig.tight_layout(padding=0.4)
        self.live_canvas.draw_idle()

    def refresh_live(self) -> None:
        """Fast refresh: live banner + overview metric tiles only."""
        try:
            live = self.live_stats() if self.live_stats else {}
            self._apply_live_banner(live)

            if live:
                self.metric_labels["wifi_stability"].configure(
                    text=f"{live.get('wifi_stability', 0):.1f}% ({live.get('wifi_quality', '?')})"
                )
                self.metric_labels["router_loss"].configure(
                    text=f"{live.get('router_loss', 0):.1f}%"
                )
                self.metric_labels["internet_loss"].configure(
                    text=f"{live.get('internet_loss', 0):.1f}%"
                )
                self.metric_labels["router_jitter"].configure(
                    text=f"{live.get('router_jitter', 0):.1f} ms "
                    f"({live.get('router_jitter_pct', 0):.0f}%)"
                )
                self.metric_labels["internet_jitter"].configure(
                    text=f"{live.get('internet_jitter', 0):.1f} ms "
                    f"({live.get('internet_jitter_pct', 0):.0f}%)"
                )
                sig = live.get("signal")
                self.metric_labels["signal"].configure(
                    text=f"{sig}%" if sig is not None else "N/A"
                )

            self._refresh_count += 1
            now = datetime.now().strftime("%H:%M:%S")
            self.status_label.configure(
                text=f"Live #{self._refresh_count} @ {now} · "
                f"every {self.live_refresh_seconds}s · "
                f"history every {self.full_refresh_seconds}s"
            )
        except Exception as exc:
            self.status_label.configure(text=f"Live refresh error: {exc}")

    def refresh_full(self) -> None:
        """Slower refresh: charts, analytics, explorer, tools."""
        try:
            self._refresh_overview_history()
            self._refresh_analytics()
            self._refresh_charts()
            self._refresh_explorer()
            if not self._tools_busy:
                self._refresh_tools()
            self._last_full_refresh = time.monotonic()
        except Exception as exc:
            self.status_label.configure(text=f"Full refresh error: {exc}")

    def _export_csv(self) -> None:
        path = self.db.export_csv(self.reports_dir)
        messagebox.showinfo("Export", f"CSV saved to:\n{path}")

    def _export_html(self) -> None:
        path = self.db.export_html(self.reports_dir)
        messagebox.showinfo("Export", f"HTML report saved to:\n{path}")

    def _run_bufferbloat_ui(self) -> None:
        self._tools_busy = True
        self.tools_text.insert(tk.END, "Running bufferbloat test (may take ~30s)...\n")
        self.tools_text.see(tk.END)
        self.root.update_idletasks()

        host = self.targets.get("INTERNET", "1.1.1.1")
        try:
            result = bufferbloat_test(host)
            self.db.insert_bufferbloat(
                host, result["baseline_ms"], result["loaded_ms"], result["grade"]
            )
            self.tools_text.insert(
                tk.END,
                f"[{datetime.now():%H:%M:%S}] Bufferbloat {result['grade']}: "
                f"+{result['delta_ms']} ms\n",
            )
            if self.on_run_bufferbloat:
                self.on_run_bufferbloat()
            self.refresh_full()
        except Exception as e:
            self.tools_text.insert(tk.END, f"Error: {e}\n")
        finally:
            self._tools_busy = False
        self.tools_text.see(tk.END)

    def _run_traceroute_ui(self) -> None:
        self._tools_busy = True
        target = self.targets.get("INTERNET", "1.1.1.1")
        self.tools_text.insert(tk.END, f"Traceroute to {target}...\n")
        self.tools_text.see(tk.END)
        self.root.update_idletasks()

        try:
            hops = run_traceroute(target)
            self.db.insert_traceroute(target, hops)
            for h in hops:
                lat = h.get("latency_ms")
                lat_s = f"{lat} ms" if lat is not None else "timeout"
                self.tools_text.insert(
                    tk.END, f"  {h['hop']:2d}  {h['host']:<40} {lat_s}\n"
                )
            if self.on_run_traceroute:
                self.on_run_traceroute()
            self.refresh_full()
        except Exception as e:
            self.tools_text.insert(tk.END, f"Error: {e}\n")
        finally:
            self._tools_busy = False
        self.tools_text.see(tk.END)

    def _stability_from_row(self, row: dict) -> float:
        return float(row.get("wifi_stability_pct") or row.get("avg_wifi_stability_pct") or 0)

    def _plot_hourly_bar(self, hourly: list[dict]) -> None:
        ax = self.ax_hour
        ax.clear()
        ax.set_facecolor("#161b22")
        instability = [0.0] * 24
        for row in hourly:
            h = int(row["hour"])
            instability[h] = 100 - self._stability_from_row(row)

        colors = ["#3fb950" if i < 40 else "#d29922" if i < 70 else "#f85149" for i in instability]
        ax.bar(range(24), instability, color=colors, edgecolor="#0d1117")
        ax.set_xlabel("Hour of day", color="#8b949e")
        ax.set_ylabel("Instability index", color="#8b949e")
        ax.set_title("Historical hourly instability", color="#c9d1d9")
        ax.set_xticks(range(0, 24, 2))
        ax.tick_params(colors="#8b949e")
        for spine in ax.spines.values():
            spine.set_color("#30363d")

    def _plot_week_heatmap(self, heatmap: list[dict]) -> None:
        ax = self.ax_week
        ax.clear()
        ax.set_facecolor("#161b22")

        grid = np.full((7, 24), np.nan)
        for row in heatmap:
            wd = int(row["weekday"])
            hr = int(row["hour"])
            stab = float(row.get("avg_stability") or 0)
            grid[wd, hr] = 100 - stab

        im = ax.imshow(grid, aspect="auto", cmap="RdYlGn_r", vmin=0, vmax=80)
        ax.set_yticks(range(7))
        ax.set_yticklabels(WEEKDAY_LABELS, color="#8b949e")
        ax.set_xticks(range(0, 24, 2))
        ax.set_xlabel("Hour", color="#8b949e")
        ax.set_title("Week × hour instability", color="#c9d1d9")
        ax.tick_params(colors="#8b949e")
        if self._colorbar:
            self._colorbar.remove()
        self._colorbar = self.fig.colorbar(im, ax=ax, label="Instability", fraction=0.046)

    def _refresh_overview_history(self) -> None:
        worst = self.db.worst_hours(self._heatmap_days)
        self.worst_text.delete("1.0", tk.END)
        if not worst:
            self.worst_text.insert(tk.END, "Collecting historical data…\n")
        for w in worst:
            slot = w.get("time_slot") or f"{w['hour']:02d}:00"
            self.worst_text.insert(
                tk.END,
                f"  {slot}  —  stability {w['avg_stability']:.1f}%  |  "
                f"loss R:{w['avg_router_loss']:.1f}% I:{w.get('avg_internet_loss', 0):.1f}%  |  "
                f"jitter {w['avg_router_jitter']:.1f} ms  ({w['samples']} samples)\n",
            )

    def _refresh_analytics(self) -> None:
        overview = self.db.analytics_overview(self._heatmap_days)
        quality = self.db.quality_breakdown(self._heatmap_days)
        counts = self.db.table_counts()

        self.analytics_text.delete("1.0", tk.END)
        lines = [
            f"=== Historical analytics (last {self._heatmap_days} days) ===\n",
            f"Snapshots:     {overview['total_snapshots']:,}  |  Pings: {overview['total_pings']:,}",
            f"Avg stability: {overview.get('avg_stability') or '—'}%",
            f"Quality mix:   {overview.get('excellent_pct', 0)}% excellent  |  {overview.get('poor_pct', 0)}% poor",
            f"Worst hour:    {overview.get('worst_time_slot') or '—'}",
            f"Best hour:     {overview.get('best_time_slot') or '—'}",
            "\nTable row counts:",
        ]
        for table, n in counts.items():
            lines.append(f"  {table}: {n:,}")
        lines.append("\nQuality breakdown:")
        for q in quality:
            lines.append(f"  {q['wifi_quality']:10s}  {q['count']:5d}  ({q['pct']}%)")
        self.analytics_text.insert(tk.END, "\n".join(lines))

        for item in self.daily_tree.get_children():
            self.daily_tree.delete(item)
        for row in self.db.daily_rollups(self._heatmap_days):
            self.daily_tree.insert(
                "", tk.END, values=tuple(row.get(c, "") for c in self.daily_tree["columns"])
            )

    def _refresh_charts(self) -> None:
        hourly = self.db.hourly_by_clock(self._heatmap_days)
        heatmap = self.db.hourly_heatmap(self._heatmap_days)
        self._plot_hourly_bar(hourly)
        self._plot_week_heatmap(heatmap)
        self.canvas.draw_idle()

    def _build_filters(self) -> SummaryFilters:
        wd = self.filter_weekday.get().strip()
        weekday = WEEKDAY_NAMES.index(wd) if wd in WEEKDAY_NAMES else None
        quality = self.filter_quality.get().strip() or None
        return SummaryFilters(
            days=int(self.filter_days.get() or "7"),
            hour=self._parse_optional_int(self.filter_hour.get()),
            weekday=weekday,
            quality=quality,
            min_stability=self._parse_optional_float(self.filter_min_stab.get()),
            min_router_loss=self._parse_optional_float(self.filter_min_loss.get()),
            search_text=self.filter_search.get().strip() or None,
            limit=500,
        )

    def _parse_optional_float(self, value: str) -> float | None:
        value = value.strip()
        return float(value) if value else None

    def _parse_optional_int(self, value: str) -> int | None:
        value = value.strip()
        return int(value) if value else None

    def _refresh_explorer(self) -> None:
        filters = self._build_filters()
        rows = self.db.query_snapshots(filters)

        for item in self.explorer_tree.get_children():
            self.explorer_tree.delete(item)
        for row in rows:
            self.explorer_tree.insert(
                "", tk.END, values=tuple(row.get(c, "") for c in READABLE_SUMMARY_COLUMNS)
            )

        parts = [f"Showing {len(rows)} snapshot(s)"]
        if filters.quality:
            parts.append(f"quality={filters.quality}")
        self.explorer_summary.configure(text="  |  ".join(parts))

    def _clear_explorer_filters(self) -> None:
        self.filter_days.set("7")
        self.filter_quality.set("")
        self.filter_hour.set("")
        self.filter_weekday.set("")
        self.filter_min_stab.set("")
        self.filter_min_loss.set("")
        self.filter_search.set("")
        self._refresh_explorer()

    def _apply_heatmap_days(self) -> None:
        try:
            self._heatmap_days = int(self.heatmap_days_var.get())
        except ValueError:
            self._heatmap_days = 30
        self.refresh_full()

    def _refresh_tools(self) -> None:
        self.tools_text.delete("1.0", tk.END)
        for b in self.db.recent_bufferbloat(5):
            self.tools_text.insert(
                tk.END,
                f"{b['recorded_at']}  {b['grade']} — {b['verdict']}  "
                f"(+{b['latency_increase_ms']:.0f} ms)\n",
            )
        for t in self.db.recent_traceroutes(3):
            self.tools_text.insert(
                tk.END,
                f"Traceroute {t.get('recorded_at', '')} → {t['target']} ({t['hop_count']} hops)\n",
            )

    def _on_close(self) -> None:
        self.root.destroy()

    def run(self) -> None:
        """Standalone mode (dashboard opened directly, not from tray)."""
        if self._embedded:
            self.raise_window()
            return

        def standalone_pump() -> None:
            if self.is_open():
                self.pump()
                self.root.after(250, standalone_pump)

        standalone_pump()
        self.root.mainloop()


def open_dashboard(**kwargs) -> None:
    app = DashboardApp(**kwargs)
    app.run()
