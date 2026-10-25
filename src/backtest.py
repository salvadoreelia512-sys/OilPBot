from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

from src.calibration import (
    CalibrationSummary,
    PredictionLogRepository,
    evaluate_records,
)
from src.config import BotSettings
from src.simulation import SimulationEngine
from src.volatility import VolatilityEngine


@dataclass(frozen=True)
class BacktestConfig:
    model_version: str = "baseline_v1"
    symbol: str = "CL=F"
    lookback_bars: int = 240
    session_bars: int = 360
    checkpoint_fractions: tuple[float, ...] = (0.25, 0.50, 0.75)
    # Target offsets in sigma*sqrt(T) units to sample across all probability buckets (including 10-20%)
    target_z_multipliers: tuple[float, ...] = (0.4, 0.75, 1.15, 1.45, 1.75, 2.15)
    default_leverage: float = 5.0
    default_required_return: float = 0.20
    mc_paths: int = 20_000


class HistoricalDataLoader:
    """
    Loads historical intraday 1-minute oil bars from CSV, Yahoo Finance,
    or generates synthetic multi-session intraday paths for walk-forward verification.
    """

    @staticmethod
    def load_csv(csv_path: str | Path) -> pd.DataFrame:
        df = pd.read_csv(csv_path)
        required = {"timestamp", "open", "high", "low", "close"}
        cols_lower = {c.lower(): c for c in df.columns}
        if not required.issubset(cols_lower.keys()):
            raise ValueError(f"CSV must contain columns: {required}")

        df = df.rename(columns={cols_lower[k]: k for k in required})
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").set_index("timestamp")
        return df[["open", "high", "low", "close"]].astype(float)

    @staticmethod
    def fetch_yahoo_intraday(
        symbol: str = "CL=F",
        period: str = "7d",
        interval: str = "1m",
    ) -> pd.DataFrame:
        if yf is None:
            raise RuntimeError("yfinance package is not installed")
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, interval=interval)
        if hist.empty:
            raise ValueError(f"No historical data returned from Yahoo for {symbol}")
        hist = hist.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
            }
        )
        if hist.index.tz is None:
            hist.index = hist.index.tz_localize("UTC")
        else:
            hist.index = hist.index.tz_convert("UTC")
        return hist[["open", "high", "low", "close"]].astype(float)

    @staticmethod
    def generate_synthetic_sessions(
        n_sessions: int = 25,
        bars_per_session: int = 360,
        spot_init: float = 75.0,
        sigma_annual: float = 0.32,
        trading_days_per_year: float = 252.0,
        trading_minutes_per_day: float = 1380.0,
        seed: int = 42,
    ) -> List[pd.DataFrame]:
        """
        Generates `n_sessions` of 1-minute OHLC bars with sub-minute micro-ticks so
        intra-minute highs and lows reflect continuous Brownian excursions.
        """
        rng = np.random.default_rng(seed)
        sub_ticks = 10
        dt_sub = 1.0 / (trading_days_per_year * trading_minutes_per_day * sub_ticks)
        sigma_sub = sigma_annual * np.sqrt(dt_sub)
        drift_sub = -0.5 * (sigma_annual ** 2) * dt_sub

        sessions: List[pd.DataFrame] = []
        current_price = spot_init
        base_date = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)

        for s_idx in range(n_sessions):
            session_start = base_date + timedelta(days=s_idx)
            n_total_sub = bars_per_session * sub_ticks
            shocks = drift_sub + sigma_sub * rng.standard_normal(n_total_sub)
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
            df = pd.DataFrame(
                {
                    "open": opens,
                    "high": highs,
                    "low": lows,
                    "close": closes,
                },
                index=idx,
            )
            sessions.append(df)

        return sessions


RiskHook = Callable[[pd.Series, datetime], tuple[float, float]]


class WalkForwardBacktester:
    """
    Phase 3 Walk-Forward Test Harness.
    Strictly separates historical estimation bars (<= t_eval) from future realization
    bars (> t_eval to session cutoff) so zero lookahead bias can occur.
    """

    def __init__(
        self,
        settings: Optional[BotSettings] = None,
        config: Optional[BacktestConfig] = None,
        log_repo: Optional[PredictionLogRepository] = None,
        seed: int = 1234,
    ):
        self.settings = settings or BotSettings()
        self.config = config or BacktestConfig()
        self.log_repo = log_repo or PredictionLogRepository(self.settings.sqlite_db_path)
        self.vol_engine = VolatilityEngine(self.settings)
        self.sim_engine = SimulationEngine(
            n_paths=self.config.mc_paths,
            cross_check_tolerance=self.settings.cross_check_tolerance,
            seed=seed,
        )

    def run_walk_forward(
        self,
        sessions: Sequence[pd.DataFrame],
        ovx_index: Optional[float] = 32.0,
        use_intraday_seasonality: bool = False,
        risk_hook: Optional[RiskHook] = None,
        save_as_benchmark: bool = True,
    ) -> CalibrationSummary:
        """
        Walks forward through `sessions`.
        At each checkpoint fraction of session `i` (for `i >= 1` so prior session lookback exists):
          1. Slice strictly prior 1-minute closes up to `t_eval`.
          2. Estimate annualized volatility (EWMA + GARCH + OVX).
          3. Run Monte Carlo simulation for both long and short target offsets.
          4. Log predictions and immediately resolve against the true forward window
             `(t_eval, session_end]`.
        """
        if len(sessions) < 2:
            raise ValueError("At least 2 sessions required (1 for warm-up lookback, 1+ for evaluation).")

        annual_minutes = (
            self.settings.trading_days_per_year * self.settings.trading_minutes_per_day
        )

        for s_idx in range(1, len(sessions)):
            prev_df = sessions[s_idx - 1]
            curr_df = sessions[s_idx]
            n_bars = len(curr_df)
            session_date = curr_df.index[0].strftime("%Y-%m-%d")

            for frac in self.config.checkpoint_fractions:
                eval_idx = int(round(frac * n_bars))
                eval_idx = max(10, min(n_bars - 15, eval_idx))

                # Strictly causal history: previous session + current session up to eval_idx
                history_closes = pd.concat(
                    [prev_df["close"], curr_df["close"].iloc[:eval_idx]]
                ).iloc[-self.config.lookback_bars :]

                now_utc = curr_df.index[eval_idx - 1].to_pydatetime()
                spot = float(curr_df["close"].iloc[eval_idx - 1])

                # Forward realization window: strictly AFTER eval_idx-1 to end of session
                forward_df = curr_df.iloc[eval_idx:]
                rem_minutes = len(forward_df)
                t_years = rem_minutes / annual_minutes

                actual_high = max(spot, float(forward_df["high"].max()))
                actual_low = min(spot, float(forward_df["low"].min()))
                actual_close = float(forward_df["close"].iloc[-1])

                # Optional risk hook for Phase 4 (News) & Phase 5 (AIS) A/B comparisons
                if risk_hook is not None:
                    risk_vol_mult, jump_intensity = risk_hook(history_closes, now_utc)
                else:
                    risk_vol_mult, jump_intensity = 1.0, 0.0

                vol_est = self.vol_engine.estimate_volatility(
                    closes_1m=history_closes,
                    ovx_index=ovx_index,
                    now_utc=now_utc,
                    risk_vol_multiplier=risk_vol_mult,
                )

                if use_intraday_seasonality:
                    sigma_use = vol_est.blended_annual_vol
                else:
                    sigma_use = vol_est.blended_annual_vol / max(1e-6, vol_est.seasonality_multiplier)

                # Anchor evaluation target levels to the unadjusted reference volatility
                # so A/B tests (Baseline vs. News/AIS) evaluate the exact same target prices
                unadjusted_sigma = sigma_use / max(1e-6, vol_est.risk_multiplier)
                vol_horizon = unadjusted_sigma * np.sqrt(t_years)

                for z_mult in self.config.target_z_multipliers:
                    for direction in ("long", "short"):
                        if direction == "long":
                            target_price = round(spot * np.exp(z_mult * vol_horizon), 4)
                        else:
                            target_price = round(spot * np.exp(-z_mult * vol_horizon), 4)

                        sim_res = self.sim_engine.run(
                            spot_price=spot,
                            target_price=target_price,
                            direction=direction,
                            leverage=self.config.default_leverage,
                            required_return=self.config.default_required_return,
                            sigma_annual=sigma_use,
                            t_years=t_years,
                            n_steps=min(rem_minutes, 180),
                            jump_intensity_annual=jump_intensity,
                        )

                        pred_id = self.log_repo.log_simulation(
                            sim=sim_res,
                            timestamp_utc=now_utc,
                            session_date=session_date,
                            model_version=self.config.model_version,
                            symbol=self.config.symbol,
                            sigma_annual=sigma_use,
                            t_years=t_years,
                            news_state={
                                "risk_vol_multiplier": risk_vol_mult,
                                "jump_intensity": jump_intensity,
                            },
                        )

                        self.log_repo.resolve_prediction_by_id(
                            prediction_id=pred_id,
                            actual_high=actual_high,
                            actual_low=actual_low,
                            actual_close=actual_close,
                            resolved_at_utc=curr_df.index[-1].to_pydatetime(),
                        )

        records = self.log_repo.get_records(
            model_version=self.config.model_version,
            only_resolved=True,
        )
        summary = evaluate_records(records, model_version=self.config.model_version)
        if save_as_benchmark:
            self.log_repo.save_benchmark(summary)
        return summary
