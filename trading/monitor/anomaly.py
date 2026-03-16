"""System anomaly detection for trading operations.

Uses rolling z-score statistics to detect unusual behaviour in
per-cycle metrics (signal counts, execution time, rejections, API errors).
Anomalies are logged to the action_log table for dashboard visibility.
"""

import logging
import math
from collections import deque

logger = logging.getLogger(__name__)


class AnomalyDetector:
    """Rolling-window z-score anomaly detector."""

    def __init__(self, window: int = 50):
        self.window = window
        self.metrics: dict[str, deque] = {}

    def record(self, metric: str, value: float):
        """Record a metric value into the rolling window."""
        if metric not in self.metrics:
            self.metrics[metric] = deque(maxlen=self.window)
        self.metrics[metric].append(value)

    def check(self, metric: str, value: float) -> tuple[bool, float]:
        """Check if *value* is anomalous for *metric*.

        Returns (is_anomaly, z_score).  Needs at least 10 samples
        before it can make a judgement.
        """
        if metric not in self.metrics or len(self.metrics[metric]) < 10:
            return False, 0.0

        data = list(self.metrics[metric])
        mean = sum(data) / len(data)
        variance = sum((x - mean) ** 2 for x in data) / len(data)
        std = math.sqrt(variance) if variance > 0 else 0.001

        z_score = abs(value - mean) / std
        return z_score > 2.0, round(z_score, 2)

    def check_and_record(self, metric: str, value: float) -> tuple[bool, float]:
        """Check for anomaly then record the value."""
        is_anomaly, z_score = self.check(metric, value)
        self.record(metric, value)
        return is_anomaly, z_score


# Module-level singleton so state persists across cycles within a process.
_detector = AnomalyDetector()


def check_cycle_anomalies(
    signals_count: int,
    exec_time_s: float,
    rejections: int,
    api_errors: int,
) -> list[str]:
    """Check for anomalies after a trading cycle.

    Returns a list of human-readable anomaly descriptions (empty if none).
    """
    anomalies: list[str] = []

    checks = [
        ("signals_per_cycle", signals_count, "signals generated"),
        ("exec_time_s", exec_time_s, "execution time (s)"),
        ("rejections", rejections, "risk rejections"),
        ("api_errors", api_errors, "API errors"),
    ]

    for metric, value, label in checks:
        is_anomaly, z_score = _detector.check_and_record(metric, value)
        if is_anomaly:
            severity = "WARNING" if z_score < 3.0 else "CRITICAL"
            msg = f"[{severity}] Anomaly in {label}: {value} (z-score: {z_score})"
            anomalies.append(msg)
            if z_score >= 3.0:
                logger.critical(msg)
            else:
                logger.warning(msg)

    return anomalies


def log_anomalies(anomalies: list[str]):
    """Persist anomalies to action_log for dashboard visibility."""
    if not anomalies:
        return
    try:
        from trading.db.store import log_action

        for a in anomalies:
            log_action("anomaly", a)
    except Exception as e:
        logger.warning("Failed to log anomalies: %s", e)
