"""On-demand timed network stability test."""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from db import quality_label
from network_tests import (
    compute_metrics,
    jitter_percent,
    ping,
    wifi_signal,
    wifi_stability_percent,
)


@dataclass
class TimedTestTick:
    elapsed_sec: float
    remaining_sec: float
    progress: float
    wifi_stability_pct: float
    instability_pct: float
    wifi_quality: str
    wifi_signal_pct: int | None
    router_avg_ms: float
    router_jitter_ms: float
    router_loss_pct: float
    router_jitter_pct: float
    internet_avg_ms: float
    internet_jitter_ms: float
    internet_loss_pct: float
    internet_jitter_pct: float
    router_last_ms: float | None
    internet_last_ms: float | None
    router_ok: bool
    internet_ok: bool


@dataclass
class TimedTestResult:
    requested_sec: int
    actual_sec: float
    started_at: datetime
    ended_at: datetime
    stopped_early: bool
    samples: list[TimedTestTick] = field(default_factory=list)

    @property
    def avg_stability(self) -> float:
        return statistics.mean(s.wifi_stability_pct for s in self.samples) if self.samples else 0.0

    @property
    def min_stability(self) -> float:
        return min(s.wifi_stability_pct for s in self.samples) if self.samples else 0.0

    @property
    def max_stability(self) -> float:
        return max(s.wifi_stability_pct for s in self.samples) if self.samples else 0.0

    @property
    def avg_router_loss(self) -> float:
        return statistics.mean(s.router_loss_pct for s in self.samples) if self.samples else 0.0

    @property
    def avg_internet_loss(self) -> float:
        return statistics.mean(s.internet_loss_pct for s in self.samples) if self.samples else 0.0

    @property
    def avg_router_ping(self) -> float:
        return statistics.mean(s.router_avg_ms for s in self.samples) if self.samples else 0.0

    @property
    def avg_internet_ping(self) -> float:
        return statistics.mean(s.internet_avg_ms for s in self.samples) if self.samples else 0.0

    @property
    def avg_router_jitter(self) -> float:
        return statistics.mean(s.router_jitter_ms for s in self.samples) if self.samples else 0.0

    @property
    def avg_internet_jitter(self) -> float:
        return statistics.mean(s.internet_jitter_ms for s in self.samples) if self.samples else 0.0

    @property
    def final_quality(self) -> str:
        return self.samples[-1].wifi_quality if self.samples else "Unknown"

    def summary_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "ended_at": self.ended_at.isoformat(timespec="seconds"),
            "requested_sec": self.requested_sec,
            "actual_sec": round(self.actual_sec, 1),
            "stopped_early": self.stopped_early,
            "sample_count": len(self.samples),
            "avg_stability": round(self.avg_stability, 1),
            "min_stability": round(self.min_stability, 1),
            "max_stability": round(self.max_stability, 1),
            "avg_router_loss": round(self.avg_router_loss, 1),
            "avg_internet_loss": round(self.avg_internet_loss, 1),
            "avg_router_ping": round(self.avg_router_ping, 1),
            "avg_internet_ping": round(self.avg_internet_ping, 1),
            "avg_router_jitter": round(self.avg_router_jitter, 1),
            "avg_internet_jitter": round(self.avg_internet_jitter, 1),
            "final_quality": self.final_quality,
        }


def run_timed_test(
    targets: dict[str, str],
    duration_sec: int,
    *,
    ping_interval: float = 1.0,
    stop_event: Any = None,
    on_tick: Callable[[TimedTestTick], None] | None = None,
) -> TimedTestResult:
    duration_sec = max(10, min(3600, int(duration_sec)))
    started = datetime.now()
    start_mono = time.monotonic()
    samples: list[TimedTestTick] = []

    stats = {name: {"latencies": [], "sent": 0, "loss": 0} for name in targets}
    names = list(targets.keys())
    router_key = "ROUTER" if "ROUTER" in targets else names[0]
    internet_key = "INTERNET" if "INTERNET" in targets else (names[1] if len(names) > 1 else router_key)

    while True:
        if stop_event is not None and stop_event.is_set():
            break
        elapsed = time.monotonic() - start_mono
        if elapsed >= duration_sec:
            break

        ping_results: dict[str, tuple[float | None, bool]] = {}
        for name, host in targets.items():
            stats[name]["sent"] += 1
            rtt = ping(host)
            ok = rtt is not None
            if not ok:
                stats[name]["loss"] += 1
            else:
                stats[name]["latencies"].append(rtt)
            ping_results[name] = (rtt, ok)

        r = stats[router_key]
        i = stats[internet_key]
        r_avg, r_jit, r_loss = compute_metrics(r["latencies"], r["sent"], r["loss"])
        i_avg, i_jit, i_loss = compute_metrics(i["latencies"], i["sent"], i["loss"])
        signal = wifi_signal()
        stability = wifi_stability_percent(signal, r_jit, r_loss)
        instability = max(0.0, min(100.0, 100.0 - stability))
        remaining = max(0.0, duration_sec - elapsed)

        tick = TimedTestTick(
            elapsed_sec=round(elapsed, 1),
            remaining_sec=round(remaining, 1),
            progress=min(1.0, elapsed / duration_sec),
            wifi_stability_pct=round(stability, 1),
            instability_pct=round(instability, 1),
            wifi_quality=quality_label(stability),
            wifi_signal_pct=signal,
            router_avg_ms=round(r_avg, 1),
            router_jitter_ms=round(r_jit, 1),
            router_loss_pct=round(r_loss, 1),
            router_jitter_pct=round(jitter_percent(r_jit, r_avg), 1),
            internet_avg_ms=round(i_avg, 1),
            internet_jitter_ms=round(i_jit, 1),
            internet_loss_pct=round(i_loss, 1),
            internet_jitter_pct=round(jitter_percent(i_jit, i_avg), 1),
            router_last_ms=ping_results.get(router_key, (None, False))[0],
            internet_last_ms=ping_results.get(internet_key, (None, False))[0],
            router_ok=ping_results.get(router_key, (None, False))[1],
            internet_ok=ping_results.get(internet_key, (None, False))[1],
        )
        samples.append(tick)
        if on_tick:
            on_tick(tick)

        if elapsed + ping_interval >= duration_sec:
            break
        time.sleep(ping_interval)

    actual = time.monotonic() - start_mono
    stopped_early = (
        stop_event is not None and stop_event.is_set() and actual < duration_sec - 0.5
    )
    return TimedTestResult(
        requested_sec=duration_sec,
        actual_sec=actual,
        started_at=started,
        ended_at=datetime.now(),
        stopped_early=stopped_early,
        samples=samples,
    )
