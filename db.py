"""SQLite persistence for NetHealth — human-readable views and analytics queries."""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

# Friendly labels used in the DB, exports, and dashboard
WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

QUALITY_BANDS: list[tuple[float, str]] = [
    (80, "Excellent"),
    (60, "Good"),
    (40, "Fair"),
    (0, "Poor"),
]


def quality_label(stability_pct: float) -> str:
    for threshold, label in QUALITY_BANDS:
        if stability_pct >= threshold:
            return label
    return "Poor"


def format_time_slot(hour: int) -> str:
    return f"{hour:02d}:00-{(hour + 1) % 24:02d}:00"


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


@dataclass
class SummaryFilters:
    """Filter options for searching network snapshot history."""

    days: int | None = 30
    date_from: datetime | None = None
    date_to: datetime | None = None
    hour: int | None = None
    weekday: int | None = None
    quality: str | None = None  # Excellent | Good | Fair | Poor
    min_stability: float | None = None
    max_stability: float | None = None
    min_router_loss: float | None = None
    min_internet_loss: float | None = None
    search_text: str | None = None
    limit: int = 500
    order_desc: bool = True


READABLE_SUMMARY_COLUMNS = [
    "recorded_at",
    "day_name",
    "time_slot",
    "wifi_quality",
    "wifi_stability_pct",
    "wifi_signal_pct",
    "router_ping_ms",
    "router_jitter_ms",
    "router_loss_pct",
    "internet_ping_ms",
    "internet_jitter_ms",
    "internet_loss_pct",
]


class NetHealthDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
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
                    day_name TEXT,
                    time_slot TEXT,
                    wifi_quality TEXT,
                    router_jitter_pct REAL,
                    internet_jitter_pct REAL,
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
                    grade TEXT NOT NULL,
                    verdict TEXT
                );

                CREATE TABLE IF NOT EXISTS traceroutes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    target TEXT NOT NULL,
                    hop_count INTEGER NOT NULL,
                    hops_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS traceroute_hops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    traceroute_id INTEGER NOT NULL,
                    hop_number INTEGER NOT NULL,
                    host TEXT NOT NULL,
                    latency_ms REAL,
                    FOREIGN KEY (traceroute_id) REFERENCES traceroutes(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_summaries_ts ON summaries(ts);
                CREATE INDEX IF NOT EXISTS idx_summaries_hour ON summaries(hour);
                CREATE INDEX IF NOT EXISTS idx_summaries_weekday_hour
                    ON summaries(weekday, hour);
                CREATE INDEX IF NOT EXISTS idx_ping_samples_ts ON ping_samples(ts);
                CREATE INDEX IF NOT EXISTS idx_tr_hops_route ON traceroute_hops(traceroute_id);
                """
            )
            self._migrate_columns(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_summaries_quality ON summaries(wifi_quality)"
            )
            self._create_views(conn)
            self._backfill_readable_columns(conn)
            self._backfill_traceroute_hops(conn)

    def _migrate_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(summaries)")}
        additions = [
            ("day_name", "TEXT"),
            ("time_slot", "TEXT"),
            ("wifi_quality", "TEXT"),
            ("router_jitter_pct", "REAL"),
            ("internet_jitter_pct", "REAL"),
        ]
        for col, typ in additions:
            if col not in existing:
                conn.execute(f"ALTER TABLE summaries ADD COLUMN {col} {typ}")

        bloat_cols = {row[1] for row in conn.execute("PRAGMA table_info(bufferbloat_tests)")}
        if "verdict" not in bloat_cols:
            conn.execute("ALTER TABLE bufferbloat_tests ADD COLUMN verdict TEXT")

    def _create_views(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            DROP VIEW IF EXISTS v_network_snapshots;
            DROP VIEW IF EXISTS v_ping_events;
            DROP VIEW IF EXISTS v_bufferbloat_results;
            DROP VIEW IF EXISTS v_hourly_patterns;
            DROP VIEW IF EXISTS v_daily_rollups;

            -- Main analytics view: one row per 20-second summary, plain English columns
            CREATE VIEW v_network_snapshots AS
            SELECT
                id,
                ts AS recorded_at_iso,
                strftime('%Y-%m-%d %H:%M:%S', ts) AS recorded_at,
                COALESCE(day_name,
                    CASE weekday
                        WHEN 0 THEN 'Monday' WHEN 1 THEN 'Tuesday' WHEN 2 THEN 'Wednesday'
                        WHEN 3 THEN 'Thursday' WHEN 4 THEN 'Friday' WHEN 5 THEN 'Saturday'
                        WHEN 6 THEN 'Sunday'
                    END) AS day_name,
                weekday,
                hour,
                COALESCE(time_slot, printf('%02d:00', hour) || '-' ||
                    printf('%02d:00', (hour + 1) % 24)) AS time_slot,
                COALESCE(wifi_quality,
                    CASE
                        WHEN wifi_stability_pct >= 80 THEN 'Excellent'
                        WHEN wifi_stability_pct >= 60 THEN 'Good'
                        WHEN wifi_stability_pct >= 40 THEN 'Fair'
                        ELSE 'Poor'
                    END) AS wifi_quality,
                ROUND(wifi_stability_pct, 1) AS wifi_stability_pct,
                wifi_signal_pct,
                ROUND(router_avg_ms, 1) AS router_ping_ms,
                ROUND(router_jitter_ms, 1) AS router_jitter_ms,
                ROUND(router_loss_pct, 1) AS router_loss_pct,
                ROUND(COALESCE(router_jitter_pct,
                    CASE WHEN router_avg_ms > 0
                        THEN (router_jitter_ms / router_avg_ms) * 100 ELSE 0 END), 1)
                    AS router_jitter_pct,
                ROUND(internet_avg_ms, 1) AS internet_ping_ms,
                ROUND(internet_jitter_ms, 1) AS internet_jitter_ms,
                ROUND(internet_loss_pct, 1) AS internet_loss_pct,
                ROUND(COALESCE(internet_jitter_pct,
                    CASE WHEN internet_avg_ms > 0
                        THEN (internet_jitter_ms / internet_avg_ms) * 100 ELSE 0 END), 1)
                    AS internet_jitter_pct,
                ROUND(router_loss_pct + internet_loss_pct, 1) AS combined_loss_pct,
                ROUND(100 - wifi_stability_pct, 1) AS instability_index
            FROM summaries;

            CREATE VIEW v_ping_events AS
            SELECT
                id,
                ts AS recorded_at_iso,
                strftime('%Y-%m-%d %H:%M:%S', ts) AS recorded_at,
                target_name AS target,
                host,
                CASE timed_out WHEN 1 THEN 'timeout' ELSE 'ok' END AS status,
                ROUND(latency_ms, 1) AS latency_ms
            FROM ping_samples;

            CREATE VIEW v_bufferbloat_results AS
            SELECT
                id,
                ts AS recorded_at_iso,
                strftime('%Y-%m-%d %H:%M:%S', ts) AS recorded_at,
                host,
                ROUND(baseline_ms, 1) AS idle_latency_ms,
                ROUND(loaded_ms, 1) AS loaded_latency_ms,
                ROUND(delta_ms, 1) AS latency_increase_ms,
                grade,
                COALESCE(verdict,
                    CASE
                        WHEN grade IN ('A+', 'A') THEN 'Minimal bufferbloat'
                        WHEN grade = 'B' THEN 'Moderate bufferbloat'
                        WHEN grade = 'C' THEN 'Noticeable bufferbloat'
                        ELSE 'Severe bufferbloat'
                    END) AS verdict
            FROM bufferbloat_tests;

            CREATE VIEW v_hourly_patterns AS
            SELECT
                hour,
                printf('%02d:00', hour) AS time_slot,
                COUNT(*) AS sample_count,
                ROUND(AVG(wifi_stability_pct), 1) AS avg_wifi_stability_pct,
                ROUND(AVG(router_loss_pct), 1) AS avg_router_loss_pct,
                ROUND(AVG(internet_loss_pct), 1) AS avg_internet_loss_pct,
                ROUND(AVG(router_jitter_ms), 1) AS avg_router_jitter_ms,
                ROUND(AVG(internet_jitter_ms), 1) AS avg_internet_jitter_ms,
                ROUND(AVG(100 - wifi_stability_pct), 1) AS avg_instability_index
            FROM summaries
            GROUP BY hour;

            CREATE VIEW v_daily_rollups AS
            SELECT
                date(ts) AS calendar_date,
                COUNT(*) AS snapshots,
                ROUND(AVG(wifi_stability_pct), 1) AS avg_wifi_stability_pct,
                ROUND(MIN(wifi_stability_pct), 1) AS min_wifi_stability_pct,
                ROUND(MAX(router_loss_pct), 1) AS max_router_loss_pct,
                ROUND(MAX(internet_loss_pct), 1) AS max_internet_loss_pct,
                SUM(CASE WHEN wifi_stability_pct < 40 THEN 1 ELSE 0 END) AS poor_quality_count
            FROM summaries
            GROUP BY date(ts)
            ORDER BY calendar_date DESC;
            """
        )

    def _backfill_readable_columns(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE summaries SET
                day_name = CASE weekday
                    WHEN 0 THEN 'Monday' WHEN 1 THEN 'Tuesday' WHEN 2 THEN 'Wednesday'
                    WHEN 3 THEN 'Thursday' WHEN 4 THEN 'Friday' WHEN 5 THEN 'Saturday'
                    ELSE 'Sunday' END,
                time_slot = printf('%02d:00', hour) || '-' ||
                    printf('%02d:00', (hour + 1) % 24),
                wifi_quality = CASE
                    WHEN wifi_stability_pct >= 80 THEN 'Excellent'
                    WHEN wifi_stability_pct >= 60 THEN 'Good'
                    WHEN wifi_stability_pct >= 40 THEN 'Fair'
                    ELSE 'Poor' END,
                router_jitter_pct = CASE WHEN router_avg_ms > 0
                    THEN (router_jitter_ms / router_avg_ms) * 100 ELSE 0 END,
                internet_jitter_pct = CASE WHEN internet_avg_ms > 0
                    THEN (internet_jitter_ms / internet_avg_ms) * 100 ELSE 0 END
            WHERE day_name IS NULL OR wifi_quality IS NULL
            """
        )
        conn.execute(
            """
            UPDATE bufferbloat_tests SET verdict = CASE
                WHEN grade IN ('A+', 'A') THEN 'Minimal bufferbloat'
                WHEN grade = 'B' THEN 'Moderate bufferbloat'
                WHEN grade = 'C' THEN 'Noticeable bufferbloat'
                ELSE 'Severe bufferbloat'
            END
            WHERE verdict IS NULL
            """
        )

    def _backfill_traceroute_hops(self, conn: sqlite3.Connection) -> None:
        routes = conn.execute(
            """
            SELECT t.id, t.hops_json FROM traceroutes t
            WHERE NOT EXISTS (
                SELECT 1 FROM traceroute_hops h WHERE h.traceroute_id = t.id
            )
            """
        ).fetchall()
        for route_id, hops_json in routes:
            self._insert_hops(conn, route_id, json.loads(hops_json))

    @staticmethod
    def _jitter_pct(jitter_ms: float, avg_ms: float) -> float:
        return (jitter_ms / avg_ms * 100) if avg_ms > 0 else 0.0

    @staticmethod
    def _bloat_verdict(grade: str) -> str:
        if grade in ("A+", "A"):
            return "Minimal bufferbloat"
        if grade == "B":
            return "Moderate bufferbloat"
        if grade == "C":
            return "Noticeable bufferbloat"
        return "Severe bufferbloat"

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
        quality = quality_label(row.wifi_stability_pct)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO summaries (
                    ts, hour, weekday, day_name, time_slot, wifi_quality,
                    router_jitter_pct, internet_jitter_pct,
                    router_avg_ms, router_jitter_ms, router_loss_pct,
                    internet_avg_ms, internet_jitter_ms, internet_loss_pct,
                    wifi_stability_pct, wifi_signal_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._ts(ts),
                    ts.hour,
                    ts.weekday(),
                    WEEKDAY_NAMES[ts.weekday()],
                    format_time_slot(ts.hour),
                    quality,
                    self._jitter_pct(row.router_jitter_ms, row.router_avg_ms),
                    self._jitter_pct(row.internet_jitter_ms, row.internet_avg_ms),
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
                INSERT INTO bufferbloat_tests
                    (ts, host, baseline_ms, loaded_ms, delta_ms, grade, verdict)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._ts(ts),
                    host,
                    baseline_ms,
                    loaded_ms,
                    delta,
                    grade,
                    self._bloat_verdict(grade),
                ),
            )

    @staticmethod
    def _insert_hops(
        conn: sqlite3.Connection, traceroute_id: int, hops: list[dict[str, Any]]
    ) -> None:
        for hop in hops:
            conn.execute(
                """
                INSERT INTO traceroute_hops (traceroute_id, hop_number, host, latency_ms)
                VALUES (?, ?, ?, ?)
                """,
                (
                    traceroute_id,
                    hop.get("hop", 0),
                    hop.get("host", "?"),
                    hop.get("latency_ms"),
                ),
            )

    def insert_traceroute(
        self,
        target: str,
        hops: list[dict[str, Any]],
        ts: datetime | None = None,
    ) -> None:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO traceroutes (ts, target, hop_count, hops_json)
                VALUES (?, ?, ?, ?)
                """,
                (self._ts(ts), target, len(hops), json.dumps(hops)),
            )
            self._insert_hops(conn, cur.lastrowid, hops)

    def _cutoff_iso(self, days: int | None) -> str | None:
        if days is None:
            return None
        return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")

    def query_snapshots(self, filters: SummaryFilters | None = None) -> list[dict[str, Any]]:
        """Search/filter readable network snapshots (v_network_snapshots)."""
        f = filters or SummaryFilters()
        clauses: list[str] = []
        params: list[Any] = []

        cutoff = self._cutoff_iso(f.days)
        if cutoff and not f.date_from:
            clauses.append("recorded_at_iso >= ?")
            params.append(cutoff)
        if f.date_from:
            clauses.append("recorded_at_iso >= ?")
            params.append(f.date_from.isoformat(timespec="seconds"))
        if f.date_to:
            clauses.append("recorded_at_iso <= ?")
            params.append(f.date_to.isoformat(timespec="seconds"))
        if f.hour is not None:
            clauses.append("hour = ?")
            params.append(f.hour)
        if f.weekday is not None:
            clauses.append("weekday = ?")
            params.append(f.weekday)
        if f.quality:
            clauses.append("wifi_quality = ?")
            params.append(f.quality)
        if f.min_stability is not None:
            clauses.append("wifi_stability_pct >= ?")
            params.append(f.min_stability)
        if f.max_stability is not None:
            clauses.append("wifi_stability_pct <= ?")
            params.append(f.max_stability)
        if f.min_router_loss is not None:
            clauses.append("router_loss_pct >= ?")
            params.append(f.min_router_loss)
        if f.min_internet_loss is not None:
            clauses.append("internet_loss_pct >= ?")
            params.append(f.min_internet_loss)
        if f.search_text:
            q = f"%{f.search_text.strip()}%"
            clauses.append(
                "(recorded_at LIKE ? OR day_name LIKE ? OR time_slot LIKE ? "
                "OR wifi_quality LIKE ?)"
            )
            params.extend([q, q, q, q])

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        order = "DESC" if f.order_desc else "ASC"
        sql = f"""
            SELECT * FROM v_network_snapshots
            {where}
            ORDER BY recorded_at_iso {order}
            LIMIT ?
        """
        params.append(f.limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def analytics_overview(self, days: int = 30) -> dict[str, Any]:
        """High-level stats for the Analytics tab."""
        cutoff = self._cutoff_iso(days)
        with self._conn() as conn:
            snap = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_snapshots,
                    MIN(recorded_at_iso) AS first_seen,
                    MAX(recorded_at_iso) AS last_seen,
                    ROUND(AVG(wifi_stability_pct), 1) AS avg_stability,
                    ROUND(AVG(instability_index), 1) AS avg_instability,
                    ROUND(AVG(router_loss_pct), 1) AS avg_router_loss,
                    ROUND(AVG(internet_loss_pct), 1) AS avg_internet_loss,
                    SUM(CASE WHEN wifi_quality = 'Poor' THEN 1 ELSE 0 END) AS poor_count,
                    SUM(CASE WHEN wifi_quality = 'Excellent' THEN 1 ELSE 0 END) AS excellent_count
                FROM v_network_snapshots
                WHERE recorded_at_iso >= ?
                """,
                (cutoff,),
            ).fetchone()

            pings = conn.execute(
                "SELECT COUNT(*) AS n FROM ping_samples WHERE ts >= ?", (cutoff,)
            ).fetchone()

            worst = conn.execute(
                """
                SELECT time_slot, AVG(instability_index) AS avg_instab, COUNT(*) AS n
                FROM v_network_snapshots
                WHERE recorded_at_iso >= ?
                GROUP BY hour
                HAVING n >= 3
                ORDER BY avg_instab DESC
                LIMIT 1
                """,
                (cutoff,),
            ).fetchone()

            best = conn.execute(
                """
                SELECT time_slot, AVG(wifi_stability_pct) AS avg_stab, COUNT(*) AS n
                FROM v_network_snapshots
                WHERE recorded_at_iso >= ?
                GROUP BY hour
                HAVING n >= 3
                ORDER BY avg_stab DESC
                LIMIT 1
                """,
                (cutoff,),
            ).fetchone()

            bloat = conn.execute(
                "SELECT COUNT(*) AS n FROM bufferbloat_tests WHERE ts >= ?", (cutoff,)
            ).fetchone()

        total = snap["total_snapshots"] or 0
        poor = snap["poor_count"] or 0
        return {
            "days": days,
            "total_snapshots": total,
            "total_pings": pings["n"] or 0,
            "bufferbloat_tests": bloat["n"] or 0,
            "first_seen": snap["first_seen"],
            "last_seen": snap["last_seen"],
            "avg_stability": snap["avg_stability"],
            "avg_instability": snap["avg_instability"],
            "avg_router_loss": snap["avg_router_loss"],
            "avg_internet_loss": snap["avg_internet_loss"],
            "poor_pct": round(100 * poor / total, 1) if total else 0,
            "excellent_pct": round(100 * (snap["excellent_count"] or 0) / total, 1) if total else 0,
            "worst_time_slot": worst["time_slot"] if worst else None,
            "worst_time_instability": round(worst["avg_instab"], 1) if worst else None,
            "best_time_slot": best["time_slot"] if best else None,
            "best_time_stability": round(best["avg_stab"], 1) if best else None,
        }

    def quality_breakdown(self, days: int = 30) -> list[dict[str, Any]]:
        cutoff = self._cutoff_iso(days)
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT wifi_quality, COUNT(*) AS count,
                    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
                FROM v_network_snapshots
                WHERE recorded_at_iso >= ?
                GROUP BY wifi_quality
                ORDER BY
                    CASE wifi_quality
                        WHEN 'Excellent' THEN 1 WHEN 'Good' THEN 2
                        WHEN 'Fair' THEN 3 ELSE 4
                    END
                """,
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def daily_rollups(self, days: int = 30) -> list[dict[str, Any]]:
        cutoff = self._cutoff_iso(days)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM v_daily_rollups WHERE calendar_date >= date(?) LIMIT ?",
                (cutoff, days + 5),
            ).fetchall()
        return [dict(r) for r in rows]

    def hourly_heatmap(self, days: int = 30) -> list[dict[str, Any]]:
        cutoff = self._cutoff_iso(days)
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
        cutoff = self._cutoff_iso(days)
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    hour,
                    printf('%02d:00', hour) AS time_slot,
                    COUNT(*) AS sample_count,
                    ROUND(AVG(wifi_stability_pct), 1) AS avg_wifi_stability_pct,
                    ROUND(AVG(router_loss_pct), 1) AS avg_router_loss_pct,
                    ROUND(AVG(internet_loss_pct), 1) AS avg_internet_loss_pct,
                    ROUND(AVG(router_jitter_ms), 1) AS avg_router_jitter_ms,
                    ROUND(AVG(internet_jitter_ms), 1) AS avg_internet_jitter_ms,
                    ROUND(AVG(100 - wifi_stability_pct), 1) AS avg_instability_index
                FROM summaries
                WHERE ts >= ?
                GROUP BY hour
                ORDER BY hour
                """,
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_summaries(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.query_snapshots(SummaryFilters(days=None, limit=limit))

    def recent_bufferbloat(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM v_bufferbloat_results ORDER BY recorded_at DESC LIMIT {int(limit)}"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_traceroutes(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, strftime('%Y-%m-%d %H:%M:%S', ts) AS recorded_at,
                    target, hop_count, hops_json
                FROM traceroutes ORDER BY ts DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["hops"] = json.loads(d.pop("hops_json"))
            result.append(d)
        return result

    def traceroute_hops_for(self, traceroute_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT hop_number, host, latency_ms
                FROM traceroute_hops
                WHERE traceroute_id = ?
                ORDER BY hop_number
                """,
                (traceroute_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def worst_hours(self, days: int = 30, limit: int = 5) -> list[dict[str, Any]]:
        cutoff = self._cutoff_iso(days)
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    hour,
                    time_slot,
                    AVG(wifi_stability_pct) AS avg_stability,
                    AVG(router_loss_pct) AS avg_router_loss,
                    AVG(internet_loss_pct) AS avg_internet_loss,
                    AVG(router_jitter_ms) AS avg_router_jitter,
                    COUNT(*) AS samples
                FROM v_network_snapshots
                WHERE recorded_at_iso >= ?
                GROUP BY hour
                HAVING samples >= 3
                ORDER BY avg_stability ASC
                LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def table_counts(self) -> dict[str, int]:
        with self._conn() as conn:
            counts = {}
            for table in (
                "summaries",
                "ping_samples",
                "bufferbloat_tests",
                "traceroutes",
                "traceroute_hops",
            ):
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
        return counts

    def export_csv(self, out_dir: Path, days: int = 30) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = out_dir / f"nethealth_report_{stamp}.csv"
        cutoff = self._cutoff_iso(days)

        exports = [
            (
                "network_snapshots",
                "SELECT * FROM v_network_snapshots WHERE recorded_at_iso >= ? ORDER BY recorded_at_iso",
            ),
            (
                "hourly_patterns",
                "SELECT * FROM v_hourly_patterns ORDER BY hour",
            ),
            (
                "daily_rollups",
                "SELECT * FROM v_daily_rollups WHERE calendar_date >= date(?) ORDER BY calendar_date",
            ),
            (
                "bufferbloat_results",
                "SELECT * FROM v_bufferbloat_results WHERE recorded_at_iso >= ? ORDER BY recorded_at_iso",
            ),
            (
                "ping_events_sample",
                "SELECT * FROM v_ping_events WHERE recorded_at_iso >= ? ORDER BY recorded_at_iso LIMIT 5000",
            ),
        ]

        with self._conn() as conn, path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["NetHealth export", f"last {days} days", f"generated {stamp}"])
            writer.writerow([])

            for section, query in exports:
                writer.writerow([f"=== {section} ==="])
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

        overview = self.analytics_overview(days)
        hourly = self.hourly_by_clock(days)
        worst = self.worst_hours(days)
        quality = self.quality_breakdown(days)
        recent = self.query_snapshots(SummaryFilters(days=days, limit=30))
        bloat = self.recent_bufferbloat(5)

        def fmt_rows(rows: list[dict[str, Any]], keys: list[str]) -> str:
            if not rows:
                return "<p>No data yet.</p>"
            head = "".join(f"<th>{k.replace('_', ' ').title()}</th>" for k in keys)
            body = ""
            for r in rows:
                body += "<tr>" + "".join(f"<td>{r.get(k, '')}</td>" for k in keys) + "</tr>"
            return f"<table><tr>{head}</tr>{body}</table>"

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>NetHealth Report {stamp}</title>
<style>
body {{ font-family: Segoe UI, sans-serif; margin: 2rem; background: #0f1419; color: #e6edf3; }}
table {{ border-collapse: collapse; margin: 1rem 0; width: 100%; max-width: 1100px; }}
th, td {{ border: 1px solid #30363d; padding: 8px; text-align: left; }}
th {{ background: #161b22; }}
h1, h2 {{ color: #58a6ff; }}
.stat {{ display: inline-block; margin: 0.5rem 1.5rem 0.5rem 0; }}
</style></head><body>
<h1>NetHealth Analytics Report</h1>
<p>Generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} — last {days} days</p>

<h2>Overview</h2>
<p class="stat"><b>{overview.get('total_snapshots', 0)}</b> snapshots</p>
<p class="stat"><b>{overview.get('avg_stability') or '—'}%</b> avg Wi-Fi stability</p>
<p class="stat"><b>{overview.get('poor_pct', 0)}%</b> poor quality time</p>
<p class="stat">Worst hour: <b>{overview.get('worst_time_slot') or '—'}</b></p>
<p class="stat">Best hour: <b>{overview.get('best_time_slot') or '—'}</b></p>

<h2>Quality breakdown</h2>
{fmt_rows(quality, ["wifi_quality", "count", "pct"])}

<h2>Worst hours</h2>
{fmt_rows(worst, ["time_slot", "avg_stability", "avg_router_loss", "avg_internet_loss", "samples"])}

<h2>Hourly patterns</h2>
{fmt_rows(hourly, ["time_slot", "avg_wifi_stability_pct", "avg_instability_index", "sample_count"])}

<h2>Recent snapshots</h2>
{fmt_rows(recent, READABLE_SUMMARY_COLUMNS[:10])}

<h2>Bufferbloat</h2>
{fmt_rows(bloat, ["recorded_at", "grade", "verdict", "latency_increase_ms", "idle_latency_ms", "loaded_latency_ms"])}

</body></html>"""
        path.write_text(html, encoding="utf-8")
        return path
