import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

import numpy as np

from src.simulation import SimulationResult


@dataclass(frozen=True)
class ReliabilityBin:
    bin_lower: float
    bin_upper: float
    count: int
    mean_predicted: float
    empirical_hit_rate: float
    abs_error: float


@dataclass(frozen=True)
class CalibrationSummary:
    model_version: str
    total_predictions: int
    brier_score: float
    expected_calibration_error: float
    bucket_10_20_count: int
    bucket_10_20_mean_pred: float
    bucket_10_20_hit_rate: float
    bucket_10_20_abs_error: float
    high_90_band_coverage: float
    low_90_band_coverage: float
    close_90_band_coverage: float
    bins: List[ReliabilityBin]

    def beats_baseline(self, baseline: "CalibrationSummary", tolerance: float = 0.0) -> bool:
        """
        Returns True if this model improves upon (lowers) the out-of-sample Brier score
        and does not degrade Expected Calibration Error beyond tolerance.
        Used in Phases 4 and 5 to gate news and AIS features.
        """
        brier_improved = self.brier_score < (baseline.brier_score - tolerance)
        ece_not_worse = self.expected_calibration_error <= (
            baseline.expected_calibration_error + 0.015
        )
        return bool(brier_improved and ece_not_worse)


@dataclass
class PredictionRecord:
    prediction_id: Optional[int]
    timestamp_utc: str
    session_date: str
    model_version: str
    symbol: str
    spot_price: float
    target_price: float
    direction: str
    leverage: float
    required_return: float
    goal_price_for_return: float
    sigma_annual: float
    t_years: float
    prob_touch_target: float
    prob_touch_return_goal: float
    closed_form_prob_target: float
    high_p05: float
    high_p50: float
    high_p95: float
    low_p05: float
    low_p50: float
    low_p95: float
    close_p05: float
    close_p50: float
    close_p95: float
    news_state_json: str = "{}"
    actual_high: Optional[float] = None
    actual_low: Optional[float] = None
    actual_close: Optional[float] = None
    touched_target: Optional[int] = None
    touched_return_goal: Optional[int] = None
    resolved_at_utc: Optional[str] = None


def compute_brier_score(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Computes mean squared error between predicted probabilities [0,1] and binary outcomes {0,1}."""
    p = np.asarray(probs, dtype=np.float64)
    y = np.asarray(outcomes, dtype=np.float64)
    if len(p) == 0 or len(p) != len(y):
        raise ValueError("probs and outcomes must be non-empty and of equal length.")
    return float(np.mean((p - y) ** 2))


def compute_reliability_curve(
    probs: Sequence[float],
    outcomes: Sequence[int],
    n_bins: int = 10,
) -> tuple[List[ReliabilityBin], float]:
    """
    Groups predictions into `n_bins` across [0, 1] and returns:
    1. A list of ReliabilityBin objects
    2. Expected Calibration Error (ECE) weighted by bin count
    """
    p = np.asarray(probs, dtype=np.float64)
    y = np.asarray(outcomes, dtype=np.float64)
    if len(p) == 0 or len(p) != len(y):
        raise ValueError("probs and outcomes must be non-empty and of equal length.")

    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins: List[ReliabilityBin] = []
    weighted_error_sum = 0.0
    n_total = len(p)

    for i in range(n_bins):
        lo, hi = float(edges[i]), float(edges[i + 1])
        if i == n_bins - 1:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)

        cnt = int(np.sum(mask))
        if cnt > 0:
            mean_p = float(np.mean(p[mask]))
            hit_rate = float(np.mean(y[mask]))
            err = abs(mean_p - hit_rate)
            weighted_error_sum += (cnt / n_total) * err
        else:
            mean_p = 0.5 * (lo + hi)
            hit_rate = 0.0
            err = 0.0

        bins.append(
            ReliabilityBin(
                bin_lower=round(lo, 2),
                bin_upper=round(hi, 2),
                count=cnt,
                mean_predicted=round(mean_p, 4),
                empirical_hit_rate=round(hit_rate, 4),
                abs_error=round(err, 4),
            )
        )

    return bins, round(float(weighted_error_sum), 4)


def evaluate_records(
    records: Sequence[PredictionRecord],
    model_version: str = "baseline_v1",
    n_bins: int = 10,
) -> CalibrationSummary:
    """
    Computes Brier score, reliability curve, 10-20% bucket accuracy,
    and 90% band coverage from resolved PredictionRecords.
    """
    resolved = [r for r in records if r.touched_target is not None and r.actual_high is not None]
    if not resolved:
        raise ValueError("No resolved prediction records available for evaluation.")

    probs = np.array([r.prob_touch_target for r in resolved], dtype=np.float64)
    outcomes = np.array([int(r.touched_target) for r in resolved], dtype=np.int64)

    brier = compute_brier_score(probs, outcomes)
    bins, ece = compute_reliability_curve(probs, outcomes, n_bins=n_bins)

    # 10% to 20% probability bucket inspection (Success Criterion #1)
    mask_10_20 = (probs >= 0.10) & (probs < 0.20)
    cnt_10_20 = int(np.sum(mask_10_20))
    if cnt_10_20 > 0:
        mean_10_20 = float(np.mean(probs[mask_10_20]))
        hit_10_20 = float(np.mean(outcomes[mask_10_20]))
        err_10_20 = abs(mean_10_20 - hit_10_20)
    else:
        mean_10_20 = 0.15
        hit_10_20 = 0.0
        err_10_20 = 0.0

    # 90% band coverage (p05 to p95) for High, Low, Close (Success Criterion #2)
    high_in_band = [
        (r.high_p05 <= float(r.actual_high) <= r.high_p95) for r in resolved
    ]
    low_in_band = [
        (r.low_p05 <= float(r.actual_low) <= r.low_p95) for r in resolved
    ]
    close_in_band = [
        (r.close_p05 <= float(r.actual_close) <= r.close_p95) for r in resolved
    ]

    return CalibrationSummary(
        model_version=model_version,
        total_predictions=len(resolved),
        brier_score=round(brier, 5),
        expected_calibration_error=round(ece, 4),
        bucket_10_20_count=cnt_10_20,
        bucket_10_20_mean_pred=round(mean_10_20, 4),
        bucket_10_20_hit_rate=round(hit_10_20, 4),
        bucket_10_20_abs_error=round(err_10_20, 4),
        high_90_band_coverage=round(float(np.mean(high_in_band)), 4),
        low_90_band_coverage=round(float(np.mean(low_in_band)), 4),
        close_90_band_coverage=round(float(np.mean(close_in_band)), 4),
        bins=bins,
    )


class PredictionLogRepository:
    """
    SQLite store for prediction logging and outcome calibration.
    Stores every prediction with inputs, model version, news state, and eventual outcome.
    """

    def __init__(self, db_path: str = "oil_bot.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS prediction_log (
                    prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    spot_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    direction TEXT NOT NULL,
                    leverage REAL NOT NULL,
                    required_return REAL NOT NULL,
                    goal_price_for_return REAL NOT NULL,
                    sigma_annual REAL NOT NULL,
                    t_years REAL NOT NULL,
                    prob_touch_target REAL NOT NULL,
                    prob_touch_return_goal REAL NOT NULL,
                    closed_form_prob_target REAL NOT NULL,
                    high_p05 REAL NOT NULL,
                    high_p50 REAL NOT NULL,
                    high_p95 REAL NOT NULL,
                    low_p05 REAL NOT NULL,
                    low_p50 REAL NOT NULL,
                    low_p95 REAL NOT NULL,
                    close_p05 REAL NOT NULL,
                    close_p50 REAL NOT NULL,
                    close_p95 REAL NOT NULL,
                    news_state_json TEXT NOT NULL,
                    actual_high REAL,
                    actual_low REAL,
                    actual_close REAL,
                    touched_target INTEGER,
                    touched_return_goal INTEGER,
                    resolved_at_utc TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calibration_benchmarks (
                    model_version TEXT PRIMARY KEY,
                    created_at_utc TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def log_simulation(
        self,
        sim: SimulationResult,
        timestamp_utc: datetime,
        session_date: str,
        model_version: str,
        symbol: str,
        sigma_annual: float,
        t_years: float,
        news_state: Optional[Dict[str, object]] = None,
    ) -> int:
        news_json = json.dumps(news_state or {})
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO prediction_log (
                    timestamp_utc, session_date, model_version, symbol,
                    spot_price, target_price, direction, leverage, required_return,
                    goal_price_for_return, sigma_annual, t_years,
                    prob_touch_target, prob_touch_return_goal, closed_form_prob_target,
                    high_p05, high_p50, high_p95,
                    low_p05, low_p50, low_p95,
                    close_p05, close_p50, close_p95,
                    news_state_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp_utc.astimezone(timezone.utc).isoformat(),
                    session_date,
                    model_version,
                    symbol,
                    sim.spot_price,
                    sim.target_price,
                    sim.direction,
                    sim.leverage,
                    sim.required_return,
                    sim.goal_price_for_return,
                    sigma_annual,
                    t_years,
                    sim.prob_touch_target,
                    sim.prob_touch_return_goal,
                    sim.closed_form_prob_target,
                    sim.high_bands.p05,
                    sim.high_bands.p50,
                    sim.high_bands.p95,
                    sim.low_bands.p05,
                    sim.low_bands.p50,
                    sim.low_bands.p95,
                    sim.close_bands.p05,
                    sim.close_bands.p50,
                    sim.close_bands.p95,
                    news_json,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def resolve_session_outcomes(
        self,
        session_date: str,
        actual_high: float,
        actual_low: float,
        actual_close: float,
        resolved_at_utc: Optional[datetime] = None,
    ) -> int:
        """
        Resolves all pending predictions for `session_date` using the realized
        forward high, low, and close of that session.
        """
        resolved_ts = (resolved_at_utc or datetime.now(timezone.utc)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT prediction_id, direction, target_price, goal_price_for_return
                FROM prediction_log
                WHERE session_date = ? AND touched_target IS NULL
                """,
                (session_date,),
            ).fetchall()

            updated = 0
            for r in rows:
                direction = r["direction"]
                target = float(r["target_price"])
                goal = float(r["goal_price_for_return"])

                if direction == "long":
                    hit_target = 1 if actual_high >= target else 0
                    hit_goal = 1 if actual_high >= goal else 0
                else:
                    hit_target = 1 if actual_low <= target else 0
                    hit_goal = 1 if actual_low <= goal else 0

                conn.execute(
                    """
                    UPDATE prediction_log
                    SET actual_high = ?,
                        actual_low = ?,
                        actual_close = ?,
                        touched_target = ?,
                        touched_return_goal = ?,
                        resolved_at_utc = ?
                    WHERE prediction_id = ?
                    """,
                    (
                        actual_high,
                        actual_low,
                        actual_close,
                        hit_target,
                        hit_goal,
                        resolved_ts,
                        r["prediction_id"],
                    ),
                )
                updated += 1
            conn.commit()
            return updated

    def resolve_prediction_by_id(
        self,
        prediction_id: int,
        actual_high: float,
        actual_low: float,
        actual_close: float,
        resolved_at_utc: Optional[datetime] = None,
    ) -> None:
        """Resolves a single prediction using the exact forward window high/low/close from its timestamp."""
        resolved_ts = (resolved_at_utc or datetime.now(timezone.utc)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT direction, target_price, goal_price_for_return
                FROM prediction_log
                WHERE prediction_id = ?
                """,
                (prediction_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Prediction ID {prediction_id} not found.")

            direction = row["direction"]
            target = float(row["target_price"])
            goal = float(row["goal_price_for_return"])

            if direction == "long":
                hit_target = 1 if actual_high >= target else 0
                hit_goal = 1 if actual_high >= goal else 0
            else:
                hit_target = 1 if actual_low <= target else 0
                hit_goal = 1 if actual_low <= goal else 0

            conn.execute(
                """
                UPDATE prediction_log
                SET actual_high = ?,
                    actual_low = ?,
                    actual_close = ?,
                    touched_target = ?,
                    touched_return_goal = ?,
                    resolved_at_utc = ?
                WHERE prediction_id = ?
                """,
                (
                    actual_high,
                    actual_low,
                    actual_close,
                    hit_target,
                    hit_goal,
                    resolved_ts,
                    prediction_id,
                ),
            )
            conn.commit()

    def get_records(
        self,
        model_version: Optional[str] = None,
        only_resolved: bool = True,
    ) -> List[PredictionRecord]:
        query = "SELECT * FROM prediction_log WHERE 1=1"
        params: List[object] = []
        if model_version is not None:
            query += " AND model_version = ?"
            params.append(model_version)
        if only_resolved:
            query += " AND touched_target IS NOT NULL"
        query += " ORDER BY prediction_id ASC"

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()

        return [PredictionRecord(**dict(row)) for row in rows]

    def save_benchmark(self, summary: CalibrationSummary) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO calibration_benchmarks (model_version, created_at_utc, summary_json)
                VALUES (?, ?, ?)
                ON CONFLICT(model_version) DO UPDATE SET
                    created_at_utc = excluded.created_at_utc,
                    summary_json = excluded.summary_json
                """,
                (
                    summary.model_version,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(asdict(summary)),
                ),
            )
            conn.commit()

    def load_benchmark(self, model_version: str = "baseline_v1") -> Optional[CalibrationSummary]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT summary_json FROM calibration_benchmarks WHERE model_version = ?",
                (model_version,),
            ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        bins = [ReliabilityBin(**b) for b in data.pop("bins")]
        return CalibrationSummary(**data, bins=bins)
