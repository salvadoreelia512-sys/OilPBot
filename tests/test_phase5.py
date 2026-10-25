from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import pytest

from src.ais import (
    AISStreamConsumer,
    AISTankerReport,
    BoundingBox,
    HormuzMonitor,
    angular_difference_degrees,
    evaluate_ais_layer_ab,
)
from src.backtest import BacktestConfig
from src.config import BotSettings
from src.simulation import SimulationEngine


def _generate_hormuz_jump_sessions(
    n_sessions: int = 24,
    bars_per_session: int = 240,
    seed: int = 3030,
) -> tuple[list[pd.DataFrame], list[AISTankerReport]]:
    """
    Generates intraday sessions where even-indexed sessions experience a Strait of Hormuz
    tanker blockade/diversion anomaly just before minute 60, triggering Poisson price jumps
    and elevated volatility over the remainder of the session (minutes 60..240).
    """
    rng = np.random.default_rng(seed)
    sub_ticks = 10
    dt_sub = 1.0 / (252.0 * 1380.0 * sub_ticks)
    base_date = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)

    sessions: list[pd.DataFrame] = []
    ais_reports: list[AISTankerReport] = []
    current_price = 75.0

    for s_idx in range(n_sessions):
        session_start = base_date + timedelta(days=s_idx)
        is_anomaly_day = (s_idx >= 1) and (s_idx % 2 == 0)
        shock_bar = 60
        eval_time = session_start + timedelta(minutes=shock_bar - 1)

        # Populate prior normal baseline transits (6 moving tankers/hr in hours -4..-1)
        for hr_back in range(2, 6):
            t_hr = eval_time - timedelta(hours=hr_back, minutes=15)
            for v_idx in range(6):
                ais_reports.append(
                    AISTankerReport(
                        mmsi=f"2000{v_idx}",
                        timestamp_utc=t_hr,
                        lat=26.20 + 0.02 * v_idx,
                        lon=56.30 + 0.02 * v_idx,
                        sog_knots=11.5,
                        cog_degrees=95.0,
                        ship_type=80,
                        vessel_name=f"TANKER_{v_idx}",
                    )
                )

        sigmas = np.full(bars_per_session * sub_ticks, 0.28)
        if is_anomaly_day:
            sigmas[shock_bar * sub_ticks :] = 0.46
            # During the anomaly hour: transits drop to 0 moving, 5 loiter in cluster, 3 reverse course
            for v_idx in range(5):
                t_start = eval_time - timedelta(minutes=35)
                t_end = eval_time - timedelta(minutes=2)
                ais_reports.append(
                    AISTankerReport(
                        mmsi=f"9000{v_idx}",
                        timestamp_utc=t_start,
                        lat=26.25 + 0.01 * v_idx,
                        lon=56.40 + 0.01 * v_idx,
                        sog_knots=10.0,
                        cog_degrees=90.0,
                        ship_type=82,
                    )
                )
                ais_reports.append(
                    AISTankerReport(
                        mmsi=f"9000{v_idx}",
                        timestamp_utc=t_end,
                        lat=26.26 + 0.01 * v_idx,
                        lon=56.41 + 0.01 * v_idx,
                        sog_knots=0.4,  # Anchored / loitering
                        cog_degrees=250.0 if v_idx < 3 else 95.0,  # 160-deg U-turn for first 3
                        ship_type=82,
                    )
                )
        else:
            # Normal hour: 6 tankers moving smoothly at 11.5 knots
            for v_idx in range(6):
                ais_reports.append(
                    AISTankerReport(
                        mmsi=f"3000{v_idx}",
                        timestamp_utc=eval_time - timedelta(minutes=10),
                        lat=26.20 + 0.03 * v_idx,
                        lon=56.35 + 0.03 * v_idx,
                        sog_knots=11.5,
                        cog_degrees=92.0,
                        ship_type=80,
                    )
                )

        sigma_sub = sigmas * np.sqrt(dt_sub)
        drift_sub = -0.5 * (sigmas ** 2) * dt_sub
        shocks = drift_sub + sigma_sub * rng.standard_normal(bars_per_session * sub_ticks)
        log_path = np.log(current_price) + np.cumsum(shocks)
        prices_sub = np.exp(log_path).reshape(bars_per_session, sub_ticks)

        opens = np.empty(bars_per_session)
        opens[0] = current_price
        opens[1:] = prices_sub[:-1, -1]
        closes = prices_sub[:, -1]
        highs = np.maximum(opens, np.max(prices_sub, axis=1))
        lows = np.minimum(opens, np.min(prices_sub, axis=1))
        current_price = float(closes[-1])

        idx = pd.date_range(session_start, periods=bars_per_session, freq="1min", tz="UTC")
        sessions.append(
            pd.DataFrame(
                {"open": opens, "high": highs, "low": lows, "close": closes},
                index=idx,
            )
        )

    return sessions, ais_reports


def test_aisstream_subscription_and_message_parsing():
    consumer = AISStreamConsumer(api_key="test_ais_key")
    sub_json = consumer.build_subscription_payload()
    assert "PositionReport" in sub_json
    assert "25.5" in sub_json and "57.5" in sub_json

    valid_msg = {
        "MessageType": "PositionReport",
        "MetaData": {
            "MMSI": 470123456,
            "ShipName": "GULF STAR",
            "ShipType": 80,
            "latitude": 26.35,
            "longitude": 56.45,
            "time_utc": "2026-10-25T11:30:00+00:00",
        },
        "Message": {
            "PositionReport": {
                "UserID": 470123456,
                "Latitude": 26.35,
                "Longitude": 56.45,
                "Sog": 12.4,
                "Cog": 88.5,
            }
        },
    }
    report = consumer.parse_message(valid_msg)
    assert report is not None
    assert report.mmsi == "470123456"
    assert report.is_tanker is True
    assert report.sog_knots == pytest.approx(12.4)

    # Outside Hormuz bounding box -> filtered out (None)
    outside_msg = dict(valid_msg)
    outside_msg["Message"] = {"PositionReport": {"Latitude": 10.0, "Longitude": 40.0, "Sog": 10.0, "Cog": 90.0}}
    assert consumer.parse_message(outside_msg) is None


def test_hormuz_anomaly_flags_and_non_directional_jump_hook():
    assert angular_difference_degrees(10.0, 350.0) == pytest.approx(20.0)
    assert angular_difference_degrees(90.0, 270.0) == pytest.approx(180.0)

    monitor = HormuzMonitor()
    t0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

    # 1. Seed 4 hours of normal baseline traffic (6 tankers/hr moving at 12 knots)
    baseline_reports = []
    for hr in range(2, 6):
        for i in range(6):
            baseline_reports.append(
                AISTankerReport(
                    mmsi=f"100{i}",
                    timestamp_utc=t0 - timedelta(hours=hr),
                    lat=26.2 + 0.01 * i,
                    lon=56.3 + 0.01 * i,
                    sog_knots=12.0,
                    cog_degrees=90.0,
                    ship_type=80,
                )
            )
    monitor.ingest_reports(baseline_reports)

    # 2. In the current hour, only 1 tanker moves, while 5 tankers loiter (<2 knots) and 3 reverse course
    current_reports = [
        AISTankerReport(
            mmsi="2000",
            timestamp_utc=t0 - timedelta(minutes=10),
            lat=26.20,
            lon=56.30,
            sog_knots=11.0,
            cog_degrees=90.0,
            ship_type=80,
        )
    ]
    for i in range(5):
        current_reports.append(
            AISTankerReport(
                mmsi=f"500{i}",
                timestamp_utc=t0 - timedelta(minutes=40),
                lat=26.30 + 0.01 * i,
                lon=56.40 + 0.01 * i,
                sog_knots=10.5,
                cog_degrees=85.0,
                ship_type=84,
            )
        )
        current_reports.append(
            AISTankerReport(
                mmsi=f"500{i}",
                timestamp_utc=t0 - timedelta(minutes=5),
                lat=26.31 + 0.01 * i,
                lon=56.41 + 0.01 * i,
                sog_knots=0.5,  # Loitering
                cog_degrees=260.0 if i < 3 else 90.0,  # 175-deg course reversal for 3 vessels
                ship_type=84,
            )
        )
    monitor.ingest_reports(current_reports)

    status = monitor.evaluate_risk(now_utc=t0)
    assert status.sudden_transit_drop is True
    assert status.loitering_cluster_anomaly is True
    assert status.route_change_anomaly is True
    assert status.risk_flag == "critical"
    assert status.jump_intensity_add > 35.0

    # Verify it acts as a non-directional tail-risk jump boost for BOTH long and short targets
    sim_normal = SimulationEngine(n_paths=30_000, seed=11)
    sim_ais = SimulationEngine(n_paths=30_000, seed=11)

    long_base = sim_normal.run(75.0, 78.0, "long", 5.0, 0.20, 0.30, 0.5 / 252.0, jump_intensity_annual=0.0)
    long_ais = sim_ais.run(
        75.0, 78.0, "long", 5.0, 0.20, 0.30 * status.vol_multiplier_add, 0.5 / 252.0,
        jump_intensity_annual=status.jump_intensity_add,
    )
    short_base = sim_normal.run(75.0, 72.0, "short", 5.0, 0.20, 0.30, 0.5 / 252.0, jump_intensity_annual=0.0)
    short_ais = sim_ais.run(
        75.0, 72.0, "short", 5.0, 0.20, 0.30 * status.vol_multiplier_add, 0.5 / 252.0,
        jump_intensity_annual=status.jump_intensity_add,
    )

    assert long_ais.prob_touch_target > long_base.prob_touch_target
    assert short_ais.prob_touch_target > short_base.prob_touch_target


def test_ais_ab_test_against_baseline(tmp_path):
    sessions, ais_reports = _generate_hormuz_jump_sessions(
        n_sessions=24,
        bars_per_session=240,
        seed=3030,
    )
    good_monitor = HormuzMonitor()
    good_monitor.ingest_reports(ais_reports)

    settings = BotSettings(
        sqlite_db_path=str(tmp_path / "ais_good.db"),
        default_ovx_level=28.0,
    )
    bt_cfg = BacktestConfig(
        lookback_bars=180,
        session_bars=240,
        checkpoint_fractions=(0.25,),
        target_z_multipliers=(0.60, 0.95, 1.30, 1.65),
        mc_paths=8_000,
    )

    base_sum, ais_sum, kept = evaluate_ais_layer_ab(
        sessions=sessions,
        hormuz_monitor=good_monitor,
        settings=settings,
        config=bt_cfg,
        ovx_index=28.0,
        seed=42,
    )

    assert kept is True
    assert good_monitor.enabled is True
    assert ais_sum.brier_score < base_sum.brier_score
