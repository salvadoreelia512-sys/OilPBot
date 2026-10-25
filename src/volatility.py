from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    from arch import arch_model
except ImportError:
    arch_model = None

from src.config import BotSettings


@dataclass(frozen=True)
class VolatilityEstimate:
    ewma_annual: float
    garch_annual: float
    ovx_annual: float
    seasonality_multiplier: float
    risk_multiplier: float
    blended_annual_vol: float


class VolatilityEngine:
    def __init__(self, settings: Optional[BotSettings] = None):
        self.settings = settings or BotSettings()
        self.annualization_factor = np.sqrt(
            self.settings.trading_days_per_year * self.settings.trading_minutes_per_day
        )

    def compute_ewma_vol(self, closes_1m: pd.Series) -> float:
        """Computes annualized EWMA volatility from 1-minute close prices."""
        log_rets = np.log(closes_1m / closes_1m.shift(1)).dropna().values
        if len(log_rets) < 5:
            raise ValueError("At least 5 1-minute bars required for EWMA volatility.")

        lam = self.settings.ewma_lambda
        var = float(np.var(log_rets[: min(15, len(log_rets))]))
        for r in log_rets:
            var = lam * var + (1.0 - lam) * (r * r)

        return float(np.sqrt(max(var, 1e-12)) * self.annualization_factor)

    def compute_garch_vol(self, closes_1m: pd.Series) -> float:
        """
        Fits a GARCH(1,1) model on 1-minute log returns (scaled by 1000 for optimizer
        stability) and outputs the 1-step-ahead annualized volatility forecast.
        """
        log_rets = np.log(closes_1m / closes_1m.shift(1)).dropna().values
        if len(log_rets) < 60 or arch_model is None:
            return float(np.std(log_rets, ddof=1) * self.annualization_factor)

        scale = 1000.0
        scaled_rets = log_rets * scale
        try:
            model = arch_model(
                scaled_rets,
                mean="Constant",
                vol="GARCH",
                p=1,
                q=1,
                dist="normal",
                rescale=False,
            )
            res = model.fit(disp="off", show_warning=False)
            forecast = res.forecast(horizon=1, reindex=False)
            var_1m = float(forecast.variance.values[-1, 0]) / (scale * scale)
            return float(np.sqrt(max(var_1m, 1e-12)) * self.annualization_factor)
        except Exception:
            return float(np.std(log_rets, ddof=1) * self.annualization_factor)

    @staticmethod
    def intraday_seasonality_multiplier(now_utc: datetime) -> float:
        """
        Captures known oil intraday volatility regimes in New York time (America/New_York):
        - Wednesday 10:15 to 11:00 ET (EIA Inventory Release at 10:30 ET): 1.35x
        - Daily 09:00 to 10:30 ET (NYMEX Pit Open liquidity surge): 1.20x
        - Overnight Asian session (20:00 to 02:00 ET): 0.85x
        - Standard hours: 1.00x
        """
        ny_time = now_utc.astimezone(ZoneInfo("America/New_York"))
        minutes_from_midnight = ny_time.hour * 60 + ny_time.minute

        if ny_time.weekday() == 2 and 615 <= minutes_from_midnight <= 660:
            return 1.35

        if 540 <= minutes_from_midnight <= 630:
            return 1.20

        if minutes_from_midnight >= 1200 or minutes_from_midnight <= 120:
            return 0.85

        return 1.00

    def estimate_volatility(
        self,
        closes_1m: pd.Series,
        ovx_index: Optional[float] = None,
        now_utc: Optional[datetime] = None,
        risk_vol_multiplier: float = 1.0,
    ) -> VolatilityEstimate:
        """
        Blends EWMA, GARCH(1,1), and the OVX anchor, then applies intraday
        seasonality and the news/Hormuz risk adjuster multiplier.
        """
        ref_now = now_utc or datetime.now(ZoneInfo("UTC"))
        ewma_ann = self.compute_ewma_vol(closes_1m)
        garch_ann = self.compute_garch_vol(closes_1m)

        ovx_val = ovx_index if (ovx_index is not None and ovx_index > 0) else self.settings.default_ovx_level
        ovx_ann = ovx_val / 100.0

        blended_var = (
            self.settings.weight_ewma * (ewma_ann ** 2)
            + self.settings.weight_garch * (garch_ann ** 2)
            + self.settings.weight_ovx * (ovx_ann ** 2)
        )
        base_vol = float(np.sqrt(blended_var))

        seasonality = self.intraday_seasonality_multiplier(ref_now)
        final_vol = base_vol * seasonality * max(0.1, risk_vol_multiplier)

        return VolatilityEstimate(
            ewma_annual=round(ewma_ann, 4),
            garch_annual=round(garch_ann, 4),
            ovx_annual=round(ovx_ann, 4),
            seasonality_multiplier=seasonality,
            risk_multiplier=risk_vol_multiplier,
            blended_annual_vol=round(final_vol, 4),
        )

    def remaining_session_years(self, now_utc: Optional[datetime] = None) -> tuple[float, int]:
        """
        Calculates time remaining until today's Dubai cutoff in both trading years (T)
        and remaining minutes (N_steps).
        """
        ref_now = (now_utc or datetime.now(ZoneInfo("UTC"))).astimezone(
            ZoneInfo(self.settings.session_timezone)
        )
        cutoff = ref_now.replace(
            hour=self.settings.session_cutoff_hour,
            minute=self.settings.session_cutoff_minute,
            second=0,
            microsecond=0,
        )
        if cutoff <= ref_now:
            return 0.0, 0

        rem_seconds = max(60.0, (cutoff - ref_now).total_seconds())
        rem_minutes = max(1, int(round(rem_seconds / 60.0)))
        total_annual_minutes = (
            self.settings.trading_days_per_year * self.settings.trading_minutes_per_day
        )
        t_years = rem_minutes / total_annual_minutes
        return t_years, rem_minutes
