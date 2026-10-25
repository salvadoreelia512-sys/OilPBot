from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import pytest

from src.config import BotSettings
from src.prices import (
    BarRepository,
    PriceAdapter,
    PriceService,
    PriceTick,
    StaleDataError,
)
from src.volatility import VolatilityEngine
from src.simulation import (
    SimulationEngine,
    closed_form_barrier_touch_prob,
    compute_return_goal_price,
)


class MockFailingAdapter(PriceAdapter):
    name = "mock_fail"

    def fetch_latest_tick(self, symbol: str) -> PriceTick:
        raise ConnectionError("Primary feed timeout")


class MockStaticAdapter(PriceAdapter):
    def __init__(self, name: str, price: float, timestamp_utc: datetime):
        self.name = name
        self.price = price
        self.timestamp_utc = timestamp_utc

    def fetch_latest_tick(self, symbol: str) -> PriceTick:
        return PriceTick(
            symbol=symbol,
            price=self.price,
            timestamp_utc=self.timestamp_utc,
            source=self.name,
        )


def test_failover_recovers_from_primary_outage(tmp_path):
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    settings = BotSettings(
        max_staleness_seconds=60.0,
        sqlite_db_path=str(tmp_path / "test_bars.db"),
    )
    adapters = [
        MockFailingAdapter(),
        MockStaticAdapter("yahoo", 75.40, now - timedelta(seconds=12)),
    ]
    service = PriceService(adapters=adapters, settings=settings)

    tick = service.get_fresh_price(now_utc=now)
    assert tick.source == "yahoo"
    assert tick.price == 75.40
    report = service.health_report(now_utc=now)
    assert report["status"] == "ok"
    assert report["sources"]["mock_fail"]["is_healthy"] is False
    assert report["sources"]["yahoo"]["is_healthy"] is True


def test_stale_data_refuses_output(tmp_path):
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    settings = BotSettings(
        max_staleness_seconds=60.0,
        sqlite_db_path=str(tmp_path / "test_stale.db"),
        market_symbol="USO",
    )
    stale_adapter = MockStaticAdapter("finnhub", 75.10, now - timedelta(seconds=125))
    service = PriceService(adapters=[stale_adapter], settings=settings)

    with pytest.raises(StaleDataError, match="REFUSED"):
        service.get_fresh_price(now_utc=now)


def test_bar_repository_aggregates_ohlc(tmp_path):
    repo = BarRepository(str(tmp_path / "ohlc.db"))
    base_time = datetime(2026, 10, 25, 12, 0, 5, tzinfo=timezone.utc)

    repo.record_tick_as_bar(PriceTick("CL=F", 74.00, base_time, "yahoo"))
    repo.record_tick_as_bar(PriceTick("CL=F", 74.50, base_time + timedelta(seconds=15), "yahoo"))
    repo.record_tick_as_bar(PriceTick("CL=F", 73.80, base_time + timedelta(seconds=30), "yahoo"))
    repo.record_tick_as_bar(PriceTick("CL=F", 74.20, base_time + timedelta(seconds=50), "yahoo"))

    closes = repo.get_recent_closes("CL=F", limit=10)
    assert len(closes) == 1
    assert closes.iloc[-1] == pytest.approx(74.20)


def test_leverage_to_target_conversion():
    long_goal = compute_return_goal_price(spot_price=75.0, direction="long", leverage=5.0, required_return=0.20)
    short_goal = compute_return_goal_price(spot_price=75.0, direction="short", leverage=5.0, required_return=0.20)

    assert long_goal == pytest.approx(78.0)
    assert short_goal == pytest.approx(72.0)


def test_volatility_engine_ewma_garch_ovx_blend():
    rng = np.random.default_rng(42)
    n_bars = 240
    annual_minutes = 252 * 1380
    sigma_1m = 0.30 / np.sqrt(annual_minutes)
    rets = rng.normal(0.0, sigma_1m, size=n_bars)
    prices = 75.0 * np.exp(np.cumsum(rets))
    idx = pd.date_range("2026-10-21 10:00", periods=n_bars, freq="1min", tz="UTC")
    series = pd.Series(prices, index=idx)

    engine = VolatilityEngine()
    eia_time = datetime(2026, 10, 21, 14, 30, tzinfo=timezone.utc)
    est = engine.estimate_volatility(series, ovx_index=32.0, now_utc=eia_time)

    assert 0.20 <= est.ewma_annual <= 0.45
    assert 0.20 <= est.garch_annual <= 0.45
    assert est.ovx_annual == pytest.approx(0.32)
    assert est.seasonality_multiplier == pytest.approx(1.35)
    assert est.blended_annual_vol > est.ovx_annual


def test_monte_carlo_matches_closed_form_and_latency():
    sim = SimulationEngine(n_paths=50_000, cross_check_tolerance=0.025, seed=12345)
    res = sim.run(
        spot_price=75.0,
        target_price=76.20,
        direction="long",
        leverage=4.0,
        required_return=0.10,
        sigma_annual=0.35,
        t_years=1.0 / 252.0,
        n_steps=120,
        jump_intensity_annual=0.0,
    )

    assert res.cross_check_alert is False
    assert res.cross_check_diff < 0.025
    assert res.runtime_ms < 500.0
    assert res.high_bands.p05 <= res.high_bands.p50 <= res.high_bands.p95
    assert res.low_bands.p05 <= res.low_bands.p50 <= res.low_bands.p95


def test_jumps_increase_out_of_the_money_touch_probability():
    sim_base = SimulationEngine(n_paths=50_000, seed=99)
    sim_jump = SimulationEngine(n_paths=50_000, seed=99)

    base = sim_base.run(
        spot_price=75.0,
        target_price=78.50,
        direction="long",
        leverage=2.0,
        required_return=0.10,
        sigma_annual=0.30,
        t_years=0.5 / 252.0,
        jump_intensity_annual=0.0,
    )
    jumpy = sim_jump.run(
        spot_price=75.0,
        target_price=78.50,
        direction="long",
        leverage=2.0,
        required_return=0.10,
        sigma_annual=0.30,
        t_years=0.5 / 252.0,
        jump_intensity_annual=40.0,
        jump_mean=0.0,
        jump_std=0.025,
    )

    assert jumpy.prob_touch_target > base.prob_touch_target
    assert jumpy.high_bands.p95 > base.high_bands.p95
