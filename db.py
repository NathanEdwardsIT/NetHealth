"""SQLite persistence for NetHealth metrics and reports."""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


@dataclass
class SummaryRow:
    timestamp: datetime
    router_avg_ms: float
    router_jitter_ms: float
    router_loss_pct: float
    internet_avg_ms: float
    internet_jitter_ms: float
    internet_loss_pct: float
    wifi_stability_pct: float
    wifi_signal_pct: int | None


class NetHealthDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ping_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    latency_ms REAL,
                    timed_out INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    hour INTEGER NOT NULL,
                    weekday INTEGER NOT NULL,
                    router_avg_ms REAL NOT NULL,
                    router_jitter_ms REAL NOT NULL,
                    router_loss_pct REAL NOT NULL,
                    internet_avg_ms REAL NOT NULL,
                    internet_jitter_ms REAL NOT NULL,
                    internet_loss_pct REAL NOT NULL,
                    wifi_stability_pct REAL NOT NULL,
                    wifi_signal_pct INTEGER
                );

                CREATE TABLE IF NOT EXISTS bufferbloat_tests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    host TEXT NOT NULL,
                    baseline_ms REAL NOT NULL,
                    loaded_ms REAL NOT NULL,
                    delta_ms REAL NOT NULL,
                    grade TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS traceroutes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    target TEXT NOT NULL,
                    hop_count INTEGER NOT NULL,
                    hops_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_summaries_ts ON summaries(ts);
                CREATE INDEX IF NOT EXISTS idx_summaries_hour ON summaries(hour);
                CREATE INDEX IF NOT EXISTS idx_summaries_weekday_hour
                    ON summaries(weekday, hour);
                CREATE INDEX IF NOT EXISTS idx_ping_samples_ts ON ping_samples(ts);
                """
            )

    @staticmethod
    def _ts(dt: datetime | None = None) -> str:
        return (dt or datetime.now()).isoformat(timespec="seconds")

    def insert_ping(
        self,
        target_name: str,
        host: str,
        latency_ms: float | None,
        timed_out: bool,
        ts: datetime | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO ping_samples (ts, target_name, host, latency_ms, timed_out)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self._ts(ts), target_name, host, latency_ms, int(timed_out)),
            )

    def insert_summary(self, row: SummaryRow) -> None:
        ts = row.timestamp
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO summaries (
                    ts, hour, weekday,
                    router_avg_ms, router_jitter_ms, router_loss_pct,
                    internet_avg_ms, internet_jitter_ms, internet_loss_pct,
                    wifi_stability_pct, wifi_signal_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._ts(ts),
                    ts.hour,
                    ts.weekday(),
                    row.router_avg_ms,
                    row.router_jitter_ms,
                    row.router_loss_pct,
                    row.internet_avg_ms,
                    row.internet_jitter_ms,
                    row.internet_loss_pct,
                    row.wifi_stability_pct,
                    row.wifi_signal_pct,
                ),
            )

    def insert_bufferbloat(
        self,
        host: str,
        baseline_ms: float,
        loaded_ms: float,
        grade: str,
        ts: datetime | None = None,
    ) -> None:
        delta = loaded_ms - baseline_ms
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO bufferbloat_tests (ts, host, baseline_ms, loaded_ms, delta_ms, grade)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self._ts(ts), host, baseline_ms, loaded_ms, delta, grade),
            )

    def insert_traceroute(
        self,
        target: str,
        hops: list[dict[str, Any]],
        ts: datetime | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO traceroutes (ts, target, hop_count, hops_json)
                VALUES (?, ?, ?, ?)
                """,
                (self._ts(ts), target, len(hops), json.dumps(hops)),
            )

    def hourly_heatmap(self, days: int = 30) -> list[dict[str, Any]]:
        """Average instability metrics grouped by weekday (0=Mon) and hour."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    weekday,
                    hour,
                    COUNT(*) AS samples,
                    AVG(wifi_stability_pct) AS avg_stability,
                    AVG(router_loss_pct) AS avg_router_loss,
                    AVG(internet_loss_pct) AS avg_internet_loss,
                    AVG(router_jitter_ms) AS avg_router_jitter,
                    AVG(internet_jitter_ms) AS avg_internet_jitter,
                    AVG(router_avg_ms) AS avg_router_latency,
                    AVG(internet_avg_ms) AS avg_internet_latency
                FROM summaries
                WHERE ts >= ?
                GROUP BY weekday, hour
                ORDER BY weekday, hour
                """,
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def hourly_by_clock(self, days: int = 30) -> list[dict[str, Any]]:
        """Aggregate by hour-of-day only (0-23) for simple daily pattern view."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    hour,
                    COUNT(*) AS samples,
                    AVG(wifi_stability_pct) AS avg_stability,
                    AVG(router_loss_pct) AS avg_router_loss,
                    AVG(internet_loss_pct) AS avg_internet_loss,
                    AVG(router_jitter_ms) AS avg_router_jitter,
                    AVG(internet_jitter_ms) AS avg_internet_jitter
                FROM summaries
                WHERE ts >= ?
                GROUP BY hour
                ORDER BY hour
                """,
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_summaries(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM summaries
                ORDER BY ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_bufferbloat(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM bufferbloat_tests
                ORDER BY ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_traceroutes(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM traceroutes
                ORDER BY ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["hops"] = json.loads(d.pop("hops_json"))
            result.append(d)
        return result

    def worst_hours(self, days: int = 30, limit: int = 5) -> list[dict[str, Any]]:
        """Hours with lowest Wi-Fi stability (most unstable)."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    hour,
                    AVG(wifi_stability_pct) AS avg_stability,
                    AVG(router_loss_pct) AS avg_router_loss,
                    AVG(internet_loss_pct) AS avg_internet_loss,
                    AVG(router_jitter_ms) AS avg_router_jitter,
                    COUNT(*) AS samples
                FROM summaries
                WHERE ts >= ?
                GROUP BY hour
                HAVING samples >= 3
                ORDER BY avg_stability ASC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def export_csv(self, out_dir: Path, days: int = 30) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = out_dir / f"nethealth_report_{stamp}.csv"
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")

        with self._conn() as conn, path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["NetHealth export", f"since {cutoff}"])
            writer.writerow([])

            for table, query in [
                (
                    "summaries",
                    "SELECT * FROM summaries WHERE ts >= ? ORDER BY ts",
                ),
                (
                    "bufferbloat_tests",
                    "SELECT * FROM bufferbloat_tests WHERE ts >= ? ORDER BY ts",
                ),
                (
                    "traceroutes",
                    "SELECT id, ts, target, hop_count, hops_json FROM traceroutes "
                    "WHERE ts >= ? ORDER BY ts",
                ),
            ]:
                writer.writerow([f"=== {table} ==="])
                rows = conn.execute(query, (cutoff,)).fetchall()
                if rows:
                    writer.writerow(rows[0].keys())
                    for row in rows:
                        writer.writerow(tuple(row))
                writer.writerow([])

        return path

    def export_html(self, out_dir: Path, days: int = 30) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = out_dir / f"nethealth_report_{stamp}.html"

        hourly = self.hourly_by_clock(days)
        worst = self.worst_hours(days)
        recent = self.recent_summaries(20)
        bloat = self.recent_bufferbloat(5)
        traces = self.recent_traceroutes(3)

        def fmt_rows(rows: list[dict[str, Any]], keys: list[str]) -> str:
            if not rows:
                return "<p>No data yet.</p>"
            head = "".join(f"<th>{k}</th>" for k in keys)
            body = ""
            for r in rows:
                body += "<tr>" + "".join(f"<td>{r.get(k, '')}</td>" for k in keys) + "</tr>"
            return f"<table><tr>{head}</tr>{body}</table>"

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>NetHealth Report {stamp}</title>
<style>
body {{ font-family: Segoe UI, sans-serif; margin: 2rem; background: #0f1419; color: #e6edf3; }}
table {{ border-collapse: collapse; margin: 1rem 0; width: 100%; max-width: 900px; }}
th, td {{ border: 1px solid #30363d; padding: 8px; text-align: left; }}
th {{ background: #161b22; }}
h1, h2 {{ color: #58a6ff; }}
</style></head><body>
<h1>NetHealth Report</h1>
<p>Generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} — last {days} days</p>

<h2>Worst hours (lowest Wi-Fi stability)</h2>
{fmt_rows(worst, ["hour", "avg_stability", "avg_router_loss", "avg_internet_loss", "avg_router_jitter", "samples"])}

<h2>Hourly pattern (0–23)</h2>
{fmt_rows(hourly, ["hour", "avg_stability", "avg_router_loss", "avg_internet_loss", "avg_router_jitter", "samples"])}

<h2>Recent summaries</h2>
{fmt_rows(recent, ["ts", "wifi_stability_pct", "router_loss_pct", "internet_loss_pct", "router_jitter_ms", "internet_jitter_ms"])}

<h2>Bufferbloat tests</h2>
{fmt_rows(bloat, ["ts", "baseline_ms", "loaded_ms", "delta_ms", "grade"])}

<h2>Recent traceroutes</h2>
"""
        if traces:
            for t in traces:
                html += f"<h3>{t['target']} @ {t['ts']} ({t['hop_count']} hops)</h3><ul>"
                for hop in t["hops"]:
                    html += (
                        f"<li>Hop {hop.get('hop')}: {hop.get('host', '?')} "
                        f"— {hop.get('latency_ms', 'N/A')} ms</li>"
                    )
                html += "</ul>"
        else:
            html += "<p>No traceroute data yet.</p>"

        html += "</body></html>"
        path.write_text(html, encoding="utf-8")
        return path
