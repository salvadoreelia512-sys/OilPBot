from datetime import datetime, timezone
import pytest

from src.backtest import BacktestConfig, HistoricalDataLoader, WalkForwardBacktester
from src.calibration import (
    PredictionLogRepository,
    compute_brier_score,
    compute_reliability_curve,
)
from src.config import BotSettings
from src.simulation import SimulationEngine


def test_brier_score_and_reliability_curve_math():
    probs = [0.05, 0.15, 0.15, 0.50, 0.50, 0.85, 0.85, 0.95]
    outcomes = [0, 0, 1, 0, 1, 1, 1, 1]

    brier = compute_brier_score(probs, outcomes)
    assert 0.0 <= brier <= 0.25

    bins, ece = compute_reliability_curve(probs, outcomes, n_bins=5)
    assert len(bins) == 5
    assert sum(b.count for b in bins) == len(probs)
    assert 0.0 <= ece <= 1.0


def test_prediction_log_repository_roundtrip(tmp_path):
    db_file = str(tmp_path / "pred_log.db")
    repo = PredictionLogRepository(db_path=db_file)
    sim = SimulationEngine(n_paths=10_000, seed=7)

    res = sim.run(
        spot_price=75.0,
        target_price=76.5,
        direction="long",
        leverage=5.0,
        required_return=0.20,
        sigma_annual=0.32,
        t_years=0.5 / 252.0,
    )

    pred_id = repo.log_simulation(
        sim=res,
        timestamp_utc=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
        session_date="2026-10-22",
        model_version="baseline_v1",
        symbol="CL=F",
        sigma_annual=0.32,
        t_years=0.5 / 252.0,
    )
    assert pred_id == 1

    # Resolve session with high touching 76.80 (>= 76.50 target, < 78.00 return goal)
    updated = repo.resolve_session_outcomes(
        session_date="2026-10-22",
        actual_high=76.80,
        actual_low=74.40,
        actual_close=76.10,
    )
    assert updated == 1

    records = repo.get_records(model_version="baseline_v1", only_resolved=True)
    assert len(records) == 1
    assert records[0].touched_target == 1
    assert records[0].touched_return_goal == 0


def test_walk_forward_baseline_calibration_and_90_band_coverage(tmp_path):
    db_file = str(tmp_path / "walk_forward.db")
    settings = BotSettings(
        sqlite_db_path=db_file,
        default_ovx_level=32.0,
    )
    config = BacktestConfig(
        model_version="baseline_v1",
        lookback_bars=180,
        session_bars=240,
        checkpoint_fractions=(0.25, 0.50, 0.75),
        target_z_multipliers=(0.50, 0.85, 1.15, 1.45, 1.80),
        mc_paths=10_000,
    )

    sessions = HistoricalDataLoader.generate_synthetic_sessions(
        n_sessions=30,
        bars_per_session=240,
        spot_init=75.0,
        sigma_annual=0.32,
        seed=42,
    )

    repo = PredictionLogRepository(db_path=db_file)
    backtester = WalkForwardBacktester(
        settings=settings,
        config=config,
        log_repo=repo,
        seed=42,
    )

    summary = backtester.run_walk_forward(
        sessions=sessions,
        ovx_index=32.0,
        use_intraday_seasonality=False,
        save_as_benchmark=True,
    )

    # Verify benchmark persistence
    loaded_bench = repo.load_benchmark("baseline_v1")
    assert loaded_bench is not None
    assert loaded_bench.total_predictions == summary.total_predictions
    assert summary.total_predictions == (30 - 1) * 3 * 5 * 2  # 870 predictions

    # Calibration sanity checks against Success Criteria
    assert summary.brier_score < 0.22
    assert summary.expected_calibration_error < 0.05
    assert summary.bucket_10_20_count > 0
    # 10-20% bucket hit rate within a few points of stated rate
    assert summary.bucket_10_20_abs_error < 0.05
    # 90% band should contain real high, low, and close ~90% of the time
    assert 0.85 <= summary.high_90_band_coverage <= 0.95
    assert 0.85 <= summary.low_90_band_coverage <= 0.95
    assert 0.85 <= summary.close_90_band_coverage <= 0.95


def test_ab_comparison_beats_baseline_gate(tmp_path):
    db_file = str(tmp_path / "ab_gate.db")
    settings = BotSettings(sqlite_db_path=db_file, default_ovx_level=32.0)
    sessions = HistoricalDataLoader.generate_synthetic_sessions(
        n_sessions=10,
        bars_per_session=180,
        sigma_annual=0.32,
        seed=77,
    )

    repo = PredictionLogRepository(db_path=db_file)

    # 1. Run well-calibrated baseline
    base_cfg = BacktestConfig(model_version="baseline_v1", mc_paths=6_000)
    base_bt = WalkForwardBacktester(settings=settings, config=base_cfg, log_repo=repo, seed=1)
    base_summary = base_bt.run_walk_forward(sessions=sessions, ovx_index=32.0)

    # 2. Run a deliberately noisy "bad news model" (3x volatility distortion)
    bad_cfg = BacktestConfig(model_version="noisy_news_v1", mc_paths=6_000)
    bad_bt = WalkForwardBacktester(settings=settings, config=bad_cfg, log_repo=repo, seed=1)
    bad_summary = bad_bt.run_walk_forward(
        sessions=sessions,
        ovx_index=32.0,
        risk_hook=lambda closes, dt: (3.0, 50.0),
        save_as_benchmark=False,
    )

    # The noisy model must fail the `beats_baseline` gate
    assert bad_summary.beats_baseline(base_summary) is False
