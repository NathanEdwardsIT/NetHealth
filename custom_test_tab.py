"""Styled 'Custom Test' tab — timed ping/stability sessions with live charts."""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from timed_test import TimedTestResult, TimedTestTick, run_timed_test

if TYPE_CHECKING:
    from db import NetHealthDB

QUALITY_COLORS = {
    "Excellent": "#3fb950",
    "Good": "#58a6ff",
    "Fair": "#d29922",
    "Poor": "#f85149",
    "Unknown": "#8b949e",
}

TT = {
    "bg": "#0d1117",
    "card": "#161b22",
    "elevated": "#1c2128",
    "border": "#30363d",
    "muted": "#8b949e",
    "text": "#e6edf3",
    "accent": "#58a6ff",
    "green": "#238636",
    "green_hi": "#3fb950",
    "red": "#b62324",
    "red_hi": "#f85149",
    "pill_active": "#388bfd",
    "pill_idle": "#21262d",
}


class CustomTestTab:
    """Builds and manages the Custom Test notebook tab."""

    def __init__(
        self,
        notebook: ttk.Notebook,
        root: tk.Misc,
        db: NetHealthDB,
        targets: dict[str, str],
    ) -> None:
        self.notebook = notebook
        self.root = root
        self.db = db
        self.targets = targets
        self.running = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[TimedTestTick] = []
        self._pills: list[tuple[int, tk.Label]] = []

        self.frame = tk.Frame(notebook, bg=TT["bg"])
        notebook.add(self.frame, text="Custom Test")
        self._build()

    def _card(self, parent: tk.Misc, **pack) -> tk.Frame:
        card = tk.Frame(
            parent, bg=TT["card"], highlightbackground=TT["border"], highlightthickness=1
        )
        if pack:
            card.pack(**pack)
        return card

    def _build(self) -> None:
        outer = tk.Frame(self.frame, bg=TT["bg"])
        outer.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        # Header
        hdr = self._card(outer, fill=tk.X, pady=(0, 10))
        hi = tk.Frame(hdr, bg=TT["card"])
        hi.pack(fill=tk.X, padx=20, pady=16)
        tk.Label(
            hi, text="Custom Network Test", bg=TT["card"], fg=TT["accent"],
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            hi,
            text="Measure Wi-Fi stability, ping, jitter, and packet loss over your chosen duration.",
            bg=TT["card"], fg=TT["muted"], font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(4, 0))

        # Duration + controls
        ctrl = self._card(outer, fill=tk.X, pady=(0, 10))
        ci = tk.Frame(ctrl, bg=TT["card"])
        ci.pack(fill=tk.X, padx=20, pady=16)
        tk.Label(ci, text="DURATION", bg=TT["card"], fg=TT["muted"], font=("Segoe UI", 9, "bold")).pack(
            anchor="w"
        )
        pill_row = tk.Frame(ci, bg=TT["card"])
        pill_row.pack(fill=tk.X, pady=(8, 12))
        self._duration_var = tk.IntVar(value=60)
        self._custom_var = tk.StringVar()
        for sec, label in ((30, "30s"), (60, "1 min"), (120, "2 min"), (300, "5 min"), (600, "10 min")):
            pill = tk.Label(
                pill_row, text=label, bg=TT["pill_idle"], fg=TT["muted"],
                font=("Segoe UI", 10, "bold"), padx=14, pady=7, cursor="hand2",
            )
            pill.bind("<Button-1>", lambda e, s=sec: self._pick_duration(s))
            pill.bind("<Enter>", lambda e, p=pill: self._pill_hover(p, True))
            pill.bind("<Leave>", lambda e, p=pill, s=sec: self._pill_hover(p, False, s))
            pill.pack(side=tk.LEFT, padx=(0, 8))
            self._pills.append((sec, pill))

        cr = tk.Frame(ci, bg=TT["card"])
        cr.pack(fill=tk.X, pady=(0, 14))
        tk.Label(cr, text="Custom", bg=TT["card"], fg=TT["muted"], font=("Segoe UI", 9)).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ew = tk.Frame(cr, bg=TT["elevated"], highlightbackground=TT["border"], highlightthickness=1)
        ew.pack(side=tk.LEFT)
        self._custom_entry = tk.Entry(
            ew, textvariable=self._custom_var, width=8, bg=TT["elevated"], fg=TT["text"],
            insertbackground=TT["text"], relief=tk.FLAT, font=("Segoe UI", 11), justify=tk.CENTER,
        )
        self._custom_entry.pack(ipadx=8, ipady=6)
        tk.Label(cr, text="seconds (10–3600)", bg=TT["card"], fg=TT["muted"], font=("Segoe UI", 9)).pack(
            side=tk.LEFT, padx=10
        )
        self._pick_duration(60)

        br = tk.Frame(ci, bg=TT["card"])
        br.pack(fill=tk.X)
        self._btn_start = self._action_btn(br, "▶  Run test", self._start, TT["green"], TT["green_hi"])
        self._btn_stop = self._action_btn(
            br, "■  Stop", self._stop_test, TT["elevated"], TT["red_hi"], enabled=False
        )
        self._save_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            br, text="Save to database", variable=self._save_var,
            bg=TT["card"], fg=TT["text"], selectcolor=TT["elevated"],
            activebackground=TT["card"], font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=(18, 0))

        # Progress
        prog = self._card(outer, fill=tk.X, pady=(0, 10))
        pi = tk.Frame(prog, bg=TT["card"])
        pi.pack(fill=tk.X, padx=20, pady=14)
        pt = tk.Frame(pi, bg=TT["card"])
        pt.pack(fill=tk.X)
        tk.Label(pt, text="PROGRESS", bg=TT["card"], fg=TT["muted"], font=("Segoe UI", 9, "bold")).pack(
            side=tk.LEFT
        )
        self._timer_lbl = tk.Label(
            pt, text="Ready", bg=TT["card"], fg=TT["text"], font=("Segoe UI", 10, "bold")
        )
        self._timer_lbl.pack(side=tk.RIGHT)
        self._prog_canvas = tk.Canvas(pi, height=12, bg=TT["card"], highlightthickness=0)
        self._prog_canvas.pack(fill=tk.X, pady=(10, 0))
        self._draw_progress(0)

        # Live metrics
        live = self._card(outer, fill=tk.X, pady=(0, 10))
        li = tk.Frame(live, bg=TT["card"])
        li.pack(fill=tk.X, padx=20, pady=16)

        hero = tk.Frame(li, bg=TT["card"])
        hero.pack(fill=tk.X)
        gc = tk.Frame(hero, bg=TT["card"])
        gc.pack(side=tk.LEFT)
        self._gauge = tk.Canvas(gc, bg=TT["card"], highlightthickness=0)
        self._gauge.pack()
        self._draw_gauge(0, TT["muted"])

        tc = tk.Frame(hero, bg=TT["card"])
        tc.pack(side=tk.LEFT, padx=(4, 20))
        self._qual_lbl = tk.Label(
            tc, text="READY", bg=TT["card"], fg=TT["muted"], font=("Segoe UI", 10, "bold")
        )
        self._qual_lbl.pack(anchor="w")
        self._stab_lbl = tk.Label(
            tc, text="—", bg=TT["card"], fg=TT["green_hi"], font=("Segoe UI", 40, "bold")
        )
        self._stab_lbl.pack(anchor="w")
        tk.Label(tc, text="Wi-Fi stability", bg=TT["card"], fg=TT["muted"], font=("Segoe UI", 10)).pack(
            anchor="w"
        )

        grid = tk.Frame(li, bg=TT["card"])
        grid.pack(fill=tk.X, pady=(14, 0))
        self._metric_lbls: dict[str, tk.Label] = {}
        specs = (
            ("wifi", "Wi-Fi %", "#3fb950"),
            ("router_ping", "Router ping", "#58a6ff"),
            ("router_jitter", "Router jitter", "#79c0ff"),
            ("router_loss", "Router loss", "#f85149"),
            ("isp_ping", "ISP ping", "#d2a8ff"),
            ("isp_jitter", "ISP jitter", "#e6edf3"),
            ("isp_loss", "ISP loss", "#ffa657"),
            ("instability", "Instability", "#d29922"),
        )
        for i, (key, title, accent) in enumerate(specs):
            box = tk.Frame(
                grid, bg=TT["elevated"], highlightbackground=TT["border"], highlightthickness=1
            )
            box.grid(row=i // 4, column=i % 4, padx=4, pady=4, sticky="nsew")
            grid.columnconfigure(i % 4, weight=1)
            tk.Frame(box, bg=accent, width=4).pack(side=tk.LEFT, fill=tk.Y)
            inn = tk.Frame(box, bg=TT["elevated"])
            inn.pack(side=tk.LEFT, padx=10, pady=8)
            tk.Label(inn, text=title.upper(), bg=TT["elevated"], fg=TT["muted"], font=("Segoe UI", 8)).pack(
                anchor="w"
            )
            lbl = tk.Label(inn, text="—", bg=TT["elevated"], fg=TT["text"], font=("Segoe UI", 12, "bold"))
            lbl.pack(anchor="w")
            self._metric_lbls[key] = lbl

        # Charts
        chart_card = self._card(outer, fill=tk.BOTH, expand=True, pady=(0, 10))
        chi = tk.Frame(chart_card, bg=TT["card"])
        chi.pack(fill=tk.BOTH, expand=True, padx=16, pady=14)
        tk.Label(
            chi, text="LIVE METRICS", bg=TT["card"], fg=TT["muted"], font=("Segoe UI", 9, "bold")
        ).pack(anchor="w", padx=4, pady=(0, 6))
        self._fig = Figure(figsize=(10, 2.6), facecolor=TT["card"])
        self._ax_s = self._fig.add_subplot(221)
        self._ax_p = self._fig.add_subplot(222)
        self._ax_j = self._fig.add_subplot(223)
        self._ax_l = self._fig.add_subplot(224)
        self._canvas = FigureCanvasTkAgg(self._fig, master=chi)
        self._canvas.get_tk_widget().configure(bg=TT["card"])
        self._canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Report
        rep = self._card(outer, fill=tk.X)
        ri = tk.Frame(rep, bg=TT["card"])
        ri.pack(fill=tk.X, padx=20, pady=14)
        tk.Label(ri, text="RESULTS", bg=TT["card"], fg=TT["muted"], font=("Segoe UI", 9, "bold")).pack(
            anchor="w", pady=(0, 8)
        )
        self._report = tk.Text(
            ri, height=6, bg=TT["elevated"], fg=TT["text"], font=("Consolas", 10),
            relief=tk.FLAT, wrap=tk.WORD, padx=12, pady=10,
            highlightbackground=TT["border"], highlightthickness=1,
        )
        self._report.pack(fill=tk.X)
        self._report.tag_configure("h", foreground=TT["accent"], font=("Consolas", 10, "bold"))
        self._report.tag_configure("ok", foreground=TT["green_hi"])
        self._report.tag_configure("dim", foreground=TT["muted"])
        self._report.insert(tk.END, "Select a duration and press Run test.\n", "dim")

    def _action_btn(
        self, parent: tk.Misc, text: str, cmd, bg: str, hover: str, *, enabled: bool = True
    ) -> tk.Label:
        fg = TT["text"] if enabled else TT["muted"]
        btn = tk.Label(
            parent, text=text, bg=bg if enabled else TT["elevated"], fg=fg,
            font=("Segoe UI", 10, "bold"), padx=18, pady=9,
            cursor="hand2" if enabled else "arrow",
        )
        btn._cmd = cmd  # type: ignore[attr-defined]
        btn._bg = bg  # type: ignore[attr-defined]
        btn._hover = hover  # type: ignore[attr-defined]
        btn._enabled = enabled  # type: ignore[attr-defined]

        def click(_e: tk.Event, b: tk.Label = btn) -> None:
            if b._enabled:  # type: ignore[attr-defined]
                b._cmd()  # type: ignore[attr-defined]

        def enter(_e: tk.Event, b: tk.Label = btn) -> None:
            if b._enabled:  # type: ignore[attr-defined]
                b.configure(bg=b._hover)  # type: ignore[attr-defined]

        def leave(_e: tk.Event, b: tk.Label = btn) -> None:
            if b._enabled:  # type: ignore[attr-defined]
                b.configure(bg=b._bg)  # type: ignore[attr-defined]

        btn.bind("<Button-1>", click)
        btn.bind("<Enter>", enter)
        btn.bind("<Leave>", leave)
        btn.pack(side=tk.LEFT, padx=4)
        return btn

    def _set_btn(self, btn: tk.Label, enabled: bool, bg: str, hover: str) -> None:
        btn._enabled = enabled  # type: ignore[attr-defined]
        btn._bg = bg  # type: ignore[attr-defined]
        btn._hover = hover  # type: ignore[attr-defined]
        btn.configure(
            bg=bg if enabled else TT["elevated"],
            fg=TT["text"] if enabled else TT["muted"],
            cursor="hand2" if enabled else "arrow",
        )

    def _pick_duration(self, sec: int) -> None:
        if self.running:
            return
        self._duration_var.set(sec)
        if hasattr(self, "_custom_var"):
            self._custom_var.set("")
        for s, pill in self._pills:
            active = s == sec
            pill.configure(
                bg=TT["pill_active"] if active else TT["pill_idle"],
                fg=TT["text"] if active else TT["muted"],
            )

    def _pill_hover(self, pill: tk.Label, enter: bool, sec: int = 0) -> None:
        if self.running:
            return
        if enter and pill.cget("bg") != TT["pill_active"]:
            pill.configure(bg="#2d333b")
        elif not enter:
            active = sec == self._duration_var.get()
            pill.configure(
                bg=TT["pill_active"] if active else TT["pill_idle"],
                fg=TT["text"] if active else TT["muted"],
            )

    def _duration_sec(self) -> int:
        c = self._custom_var.get().strip()
        if c:
            try:
                return max(10, min(3600, int(c)))
            except ValueError:
                pass
        return int(self._duration_var.get())

    def _draw_progress(self, pct: float) -> None:
        c = self._prog_canvas
        c.delete("all")
        w = max(c.winfo_width(), 200)
        fill = int(w * max(0, min(100, pct)) / 100)
        c.create_rectangle(0, 2, w, 12, fill=TT["elevated"], outline="")
        if fill:
            c.create_rectangle(0, 2, fill, 12, fill=TT["green_hi"], outline="")

    def _draw_gauge(self, pct: float, color: str) -> None:
        c = self._gauge
        c.delete("all")
        pad, size = 10, 130
        x0, y0, x1, y1 = pad, pad, pad + size, pad + size
        c.configure(width=size + pad * 2, height=size + pad * 2)
        c.create_oval(x0, y0, x1, y1, outline=TT["elevated"], width=10)
        ext = max(0, min(100, pct)) * 3.6
        if ext:
            c.create_arc(x0, y0, x1, y1, start=90, extent=-ext, style=tk.ARC, outline=color, width=10)

    def _start(self) -> None:
        if self.running:
            return
        dur = self._duration_sec()
        if dur < 10:
            messagebox.showwarning("Custom Test", "Duration must be at least 10 seconds.")
            return

        self.running = True
        self._stop_event.clear()
        self._samples.clear()
        self._set_btn(self._btn_start, False, TT["elevated"], TT["green_hi"])
        self._set_btn(self._btn_stop, True, TT["red"], TT["red_hi"])
        self._custom_entry.configure(state=tk.DISABLED)
        self._draw_progress(0)
        self._report.delete("1.0", tk.END)
        self._report.insert(tk.END, f"Running {dur}s test…\n", "dim")
        self._timer_lbl.configure(text=f"0s / {dur}s", fg=TT["accent"])
        self.notebook.select(self.frame)

        def worker() -> None:
            try:
                result = run_timed_test(
                    self.targets,
                    dur,
                    stop_event=self._stop_event,
                    on_tick=lambda t: self.root.after(0, lambda tick=t: self._on_tick(tick)),
                )
                self.root.after(0, lambda r=result: self._on_done(r))
            except Exception as exc:
                self.root.after(0, lambda e=exc: self._on_err(e))

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def _stop_test(self) -> None:
        if self.running:
            self._stop_event.set()
            self._timer_lbl.configure(text="Stopping…", fg=TT["red_hi"])

    def _on_tick(self, tick: TimedTestTick) -> None:
        self._samples.append(tick)
        color = QUALITY_COLORS.get(tick.wifi_quality, TT["muted"])
        total = tick.elapsed_sec + tick.remaining_sec

        self._qual_lbl.configure(text=tick.wifi_quality.upper(), fg=color)
        self._stab_lbl.configure(text=f"{tick.wifi_stability_pct:.0f}%", fg=color)
        self._draw_progress(tick.progress * 100)
        self._draw_gauge(tick.wifi_stability_pct, color)
        self._timer_lbl.configure(
            text=f"{tick.elapsed_sec:.0f}s / {total:.0f}s · {tick.remaining_sec:.0f}s left"
        )

        sig = tick.wifi_signal_pct
        self._metric_lbls["wifi"].configure(text=f"{sig}%" if sig is not None else "N/A")
        self._metric_lbls["instability"].configure(text=f"{tick.instability_pct:.1f}%")
        self._metric_lbls["router_ping"].configure(
            text=f"{tick.router_last_ms:.0f} ms" if tick.router_last_ms else "timeout"
        )
        self._metric_lbls["router_jitter"].configure(text=f"{tick.router_jitter_ms:.1f} ms")
        self._metric_lbls["router_loss"].configure(text=f"{tick.router_loss_pct:.1f}%")
        self._metric_lbls["isp_ping"].configure(
            text=f"{tick.internet_last_ms:.0f} ms" if tick.internet_last_ms else "timeout"
        )
        self._metric_lbls["isp_jitter"].configure(text=f"{tick.internet_jitter_ms:.1f} ms")
        self._metric_lbls["isp_loss"].configure(text=f"{tick.internet_loss_pct:.1f}%")

        self._plot()

    def _plot(self) -> None:
        if not self._samples:
            return
        t = [s.elapsed_sec for s in self._samples]
        for ax in (self._ax_s, self._ax_p, self._ax_j, self._ax_l):
            ax.clear()
            ax.set_facecolor(TT["elevated"])
            ax.tick_params(colors=TT["muted"], labelsize=7)
            for sp in ax.spines.values():
                sp.set_color(TT["border"])

        s = [x.wifi_stability_pct for x in self._samples]
        self._ax_s.plot(t, s, color=TT["green_hi"], lw=2)
        self._ax_s.fill_between(t, s, alpha=0.2, color=TT["green_hi"])
        self._ax_s.set_ylim(0, 100)
        self._ax_s.set_title("Wi-Fi %", color=TT["text"], fontsize=9)

        self._ax_p.plot(t, [x.router_avg_ms for x in self._samples], color="#58a6ff", lw=1.5, label="Router")
        self._ax_p.plot(t, [x.internet_avg_ms for x in self._samples], color="#d2a8ff", lw=1.5, label="ISP")
        self._ax_p.set_title("Ping (ms)", color=TT["text"], fontsize=9)
        self._ax_p.legend(fontsize=6, facecolor=TT["card"], edgecolor=TT["border"])

        self._ax_j.plot(t, [x.router_jitter_ms for x in self._samples], color="#79c0ff", lw=1.5, label="Router")
        self._ax_j.plot(t, [x.internet_jitter_ms for x in self._samples], color="#e6edf3", lw=1.5, label="ISP")
        self._ax_j.set_title("Jitter (ms)", color=TT["text"], fontsize=9)
        self._ax_j.legend(fontsize=6, facecolor=TT["card"], edgecolor=TT["border"])

        self._ax_l.plot(t, [x.router_loss_pct for x in self._samples], color="#f85149", lw=1.5, label="Router")
        self._ax_l.plot(t, [x.internet_loss_pct for x in self._samples], color="#ffa657", lw=1.5, label="ISP")
        self._ax_l.set_title("Packet loss %", color=TT["text"], fontsize=9)
        self._ax_l.legend(fontsize=6, facecolor=TT["card"], edgecolor=TT["border"])

        for ax in (self._ax_s, self._ax_p, self._ax_j, self._ax_l):
            ax.set_xlabel("s", color=TT["muted"], fontsize=8)
        self._fig.tight_layout(padding=0.8)
        self._canvas.draw_idle()

    def _on_done(self, result: TimedTestResult) -> None:
        self.running = False
        self._set_btn(self._btn_start, True, TT["green"], TT["green_hi"])
        self._set_btn(self._btn_stop, False, TT["elevated"], TT["red_hi"])
        self._custom_entry.configure(state=tk.NORMAL)
        self._draw_progress(100)
        color = QUALITY_COLORS.get(result.final_quality, TT["muted"])
        self._draw_gauge(result.avg_stability, color)
        self._timer_lbl.configure(text="Complete", fg=TT["green_hi"])

        test_id = None
        if self._save_var.get():
            try:
                test_id = self.db.insert_custom_test(result)
            except Exception as exc:
                messagebox.showwarning("Save failed", str(exc))

        status = "stopped early" if result.stopped_early else "finished"
        self._report.delete("1.0", tk.END)
        self._report.insert(tk.END, f"Test {status}\n\n", "h")
        self._report.insert(
            tk.END,
            f"Duration: {result.actual_sec:.1f}s / {result.requested_sec}s\n"
            f"Samples:  {len(result.samples)}\n\n",
            "dim",
        )
        self._report.insert(tk.END, "Wi-Fi stability\n", "h")
        self._report.insert(
            tk.END,
            f"  Avg {result.avg_stability:.1f}%  (min {result.min_stability:.1f}% · "
            f"max {result.max_stability:.1f}%)  —  {result.final_quality}\n\n",
            "ok",
        )
        self._report.insert(tk.END, "Router\n", "h")
        self._report.insert(
            tk.END,
            f"  Ping {result.avg_router_ping:.1f} ms · Jitter {result.avg_router_jitter:.1f} ms · "
            f"Loss {result.avg_router_loss:.1f}%\n\n",
            "dim",
        )
        self._report.insert(tk.END, "ISP / Internet\n", "h")
        self._report.insert(
            tk.END,
            f"  Ping {result.avg_internet_ping:.1f} ms · Jitter {result.avg_internet_jitter:.1f} ms · "
            f"Loss {result.avg_internet_loss:.1f}%\n",
            "dim",
        )
        if test_id:
            self._report.insert(tk.END, f"\nSaved as custom test #{test_id}.\n", "ok")
        self._plot()

    def _on_err(self, exc: Exception) -> None:
        self.running = False
        self._set_btn(self._btn_start, True, TT["green"], TT["green_hi"])
        self._set_btn(self._btn_stop, False, TT["elevated"], TT["red_hi"])
        self._custom_entry.configure(state=tk.NORMAL)
        self._report.insert(tk.END, f"\nError: {exc}\n", "dim")
        messagebox.showerror("Custom Test", str(exc))
