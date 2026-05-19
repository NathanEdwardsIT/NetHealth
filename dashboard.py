"""Tkinter GUI dashboard for NetHealth."""

from __future__ import annotations

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

from db import NetHealthDB
from network_tests import bufferbloat_test, run_traceroute


WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class DashboardApp:
    def __init__(
        self,
        db: NetHealthDB,
        reports_dir: Path,
        targets: dict[str, str],
        on_run_traceroute: Callable[[], None] | None = None,
        on_run_bufferbloat: Callable[[], None] | None = None,
        live_stats: Callable[[], dict] | None = None,
    ) -> None:
        self.db = db
        self.reports_dir = reports_dir
        self.targets = targets
        self.on_run_traceroute = on_run_traceroute
        self.on_run_bufferbloat = on_run_bufferbloat
        self.live_stats = live_stats

        self.root = tk.Tk()
        self.root.title("NetHealth Dashboard")
        self.root.geometry("1100x720")
        self.root.configure(bg="#0d1117")
        self._style()
        self._build_ui()
        self.refresh()

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
        style.configure("TButton", padding=6)

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root)
        header.pack(fill=tk.X, padx=12, pady=8)
        ttk.Label(header, text="NetHealth", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Button(header, text="Refresh", command=self.refresh).pack(side=tk.RIGHT, padx=4)
        ttk.Button(header, text="Export CSV", command=self._export_csv).pack(side=tk.RIGHT, padx=4)
        ttk.Button(header, text="Export HTML", command=self._export_html).pack(side=tk.RIGHT, padx=4)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self._tab_overview()
        self._tab_heatmaps()
        self._tab_history()
        self._tab_tools()

    def _tab_overview(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Overview")

        self.metric_labels: dict[str, ttk.Label] = {}
        grid = ttk.Frame(frame)
        grid.pack(fill=tk.X, padx=16, pady=16)

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
            cell.grid(row=i // 3, column=i % 3, padx=20, pady=12, sticky="w")
            ttk.Label(cell, text=title).pack(anchor="w")
            lbl = ttk.Label(cell, text="—", style="Metric.TLabel")
            lbl.pack(anchor="w")
            self.metric_labels[key] = lbl

        self.worst_text = tk.Text(
            frame, height=8, bg="#161b22", fg="#c9d1d9", font=("Consolas", 10), relief=tk.FLAT
        )
        ttk.Label(frame, text="Most unstable hours (lowest stability)").pack(anchor="w", padx=16)
        self.worst_text.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)

    def _tab_heatmaps(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Heatmaps")

        self.fig = Figure(figsize=(10, 4), facecolor="#0d1117")
        self.ax_hour = self.fig.add_subplot(121)
        self.ax_week = self.fig.add_subplot(122)
        self.canvas = FigureCanvasTkAgg(self.fig, master=frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def _tab_history(self) -> None:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="History")

        cols = (
            "ts",
            "wifi_stability_pct",
            "router_loss_pct",
            "internet_loss_pct",
            "router_jitter_ms",
            "internet_jitter_ms",
        )
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=18)
        for c in cols:
            self.tree.heading(c, text=c.replace("_", " ").title())
            self.tree.column(c, width=140)
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)
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

    def _export_csv(self) -> None:
        path = self.db.export_csv(self.reports_dir)
        messagebox.showinfo("Export", f"CSV saved to:\n{path}")

    def _export_html(self) -> None:
        path = self.db.export_html(self.reports_dir)
        messagebox.showinfo("Export", f"HTML report saved to:\n{path}")

    def _run_bufferbloat_ui(self) -> None:
        self.tools_text.insert(tk.END, "Running bufferbloat test (may take ~30s)...\n")
        self.tools_text.see(tk.END)
        self.root.update()

        host = self.targets.get("INTERNET", "1.1.1.1")
        try:
            result = bufferbloat_test(host)
            self.db.insert_bufferbloat(
                host,
                result["baseline_ms"],
                result["loaded_ms"],
                result["grade"],
            )
            line = (
                f"[{datetime.now():%H:%M:%S}] Bufferbloat {result['grade']}: "
                f"baseline {result['baseline_ms']} ms, loaded {result['loaded_ms']} ms, "
                f"delta +{result['delta_ms']} ms\n"
            )
            self.tools_text.insert(tk.END, line)
            if self.on_run_bufferbloat:
                self.on_run_bufferbloat()
        except Exception as e:
            self.tools_text.insert(tk.END, f"Error: {e}\n")
        self.tools_text.see(tk.END)

    def _run_traceroute_ui(self) -> None:
        target = self.targets.get("INTERNET", "1.1.1.1")
        self.tools_text.insert(tk.END, f"Traceroute to {target}...\n")
        self.tools_text.see(tk.END)
        self.root.update()

        try:
            hops = run_traceroute(target)
            self.db.insert_traceroute(target, hops)
            self.tools_text.insert(tk.END, f"--- {target} ({len(hops)} hops) ---\n")
            for h in hops:
                lat = h.get("latency_ms")
                lat_s = f"{lat} ms" if lat is not None else "timeout"
                self.tools_text.insert(tk.END, f"  {h['hop']:2d}  {h['host']:<40} {lat_s}\n")
            if self.on_run_traceroute:
                self.on_run_traceroute()
        except Exception as e:
            self.tools_text.insert(tk.END, f"Error: {e}\n")
        self.tools_text.see(tk.END)

    def _plot_hourly_bar(self, hourly: list[dict]) -> None:
        ax = self.ax_hour
        ax.clear()
        ax.set_facecolor("#161b22")
        hours = list(range(24))
        stability = [100.0] * 24
        loss = [0.0] * 24
        for row in hourly:
            h = int(row["hour"])
            stability[h] = float(row["avg_stability"] or 0)
            loss[h] = float(row["avg_router_loss"] or 0) + float(row["avg_internet_loss"] or 0)

        instability = [100 - s for s in stability]
        colors = ["#3fb950" if i < 40 else "#d29922" if i < 70 else "#f85149" for i in instability]
        ax.bar(hours, instability, color=colors, edgecolor="#0d1117")
        ax.set_xlabel("Hour of day", color="#8b949e")
        ax.set_ylabel("Instability index", color="#8b949e")
        ax.set_title("Hourly instability (higher = worse)", color="#c9d1d9")
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
            stab = float(row["avg_stability"] or 0)
            grid[wd, hr] = 100 - stab

        im = ax.imshow(grid, aspect="auto", cmap="RdYlGn_r", vmin=0, vmax=80)
        ax.set_yticks(range(7))
        ax.set_yticklabels(WEEKDAY_LABELS, color="#8b949e")
        ax.set_xticks(range(0, 24, 2))
        ax.set_xlabel("Hour", color="#8b949e")
        ax.set_title("Week × hour instability heatmap", color="#c9d1d9")
        ax.tick_params(colors="#8b949e")
        self.fig.colorbar(im, ax=ax, label="Instability", fraction=0.046)

    def refresh(self) -> None:
        live = self.live_stats() if self.live_stats else {}
        if live:
            self.metric_labels["wifi_stability"].configure(
                text=f"{live.get('wifi_stability', 0):.1f}%"
            )
            self.metric_labels["router_loss"].configure(text=f"{live.get('router_loss', 0):.1f}%")
            self.metric_labels["internet_loss"].configure(
                text=f"{live.get('internet_loss', 0):.1f}%"
            )
            self.metric_labels["router_jitter"].configure(
                text=f"{live.get('router_jitter', 0):.1f} ms ({live.get('router_jitter_pct', 0):.0f}%)"
            )
            self.metric_labels["internet_jitter"].configure(
                text=f"{live.get('internet_jitter', 0):.1f} ms ({live.get('internet_jitter_pct', 0):.0f}%)"
            )
            sig = live.get("signal")
            self.metric_labels["signal"].configure(
                text=f"{sig}%" if sig is not None else "N/A"
            )
        else:
            recent = self.db.recent_summaries(1)
            if recent:
                r = recent[0]
                self.metric_labels["wifi_stability"].configure(
                    text=f"{r['wifi_stability_pct']:.1f}%"
                )
                self.metric_labels["router_loss"].configure(text=f"{r['router_loss_pct']:.1f}%")
                self.metric_labels["internet_loss"].configure(
                    text=f"{r['internet_loss_pct']:.1f}%"
                )
                self.metric_labels["router_jitter"].configure(
                    text=f"{r['router_jitter_ms']:.1f} ms"
                )
                self.metric_labels["internet_jitter"].configure(
                    text=f"{r['internet_jitter_ms']:.1f} ms"
                )
                sig = r.get("wifi_signal_pct")
                self.metric_labels["signal"].configure(
                    text=f"{sig}%" if sig is not None else "N/A"
                )

        worst = self.db.worst_hours()
        self.worst_text.delete("1.0", tk.END)
        if not worst:
            self.worst_text.insert(tk.END, "Collecting data… run the monitor for a while.\n")
        for w in worst:
            self.worst_text.insert(
                tk.END,
                f"  {w['hour']:02d}:00  —  stability {w['avg_stability']:.1f}%  |  "
                f"loss R:{w['avg_router_loss']:.1f}% I:{w.get('avg_internet_loss', 0):.1f}%  |  "
                f"jitter {w['avg_router_jitter']:.1f} ms  ({w['samples']} samples)\n",
            )

        hourly = self.db.hourly_by_clock()
        heatmap = self.db.hourly_heatmap()
        self._plot_hourly_bar(hourly)
        self._plot_week_heatmap(heatmap)
        self.canvas.draw()

        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in self.db.recent_summaries(80):
            self.tree.insert(
                "",
                tk.END,
                values=tuple(row.get(c, "") for c in self.tree["columns"]),
            )

        self.tools_text.delete("1.0", tk.END)
        for b in self.db.recent_bufferbloat(5):
            self.tools_text.insert(
                tk.END,
                f"Bufferbloat {b['ts']}: {b['grade']} "
                f"(+{b['delta_ms']:.0f} ms under load)\n",
            )
        for t in self.db.recent_traceroutes(3):
            self.tools_text.insert(
                tk.END, f"Traceroute {t['ts']} → {t['target']} ({t['hop_count']} hops)\n"
            )

    def run(self) -> None:
        self.root.mainloop()


def open_dashboard(**kwargs) -> None:
    app = DashboardApp(**kwargs)
    app.run()
