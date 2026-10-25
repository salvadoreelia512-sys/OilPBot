import time
from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
from scipy.special import ndtr  # Fast C-level standard normal CDF

Direction = Literal["long", "short"]

# Broadie-Glasserman-Kou (1997) continuity correction constant: -zeta(0.5) / sqrt(2*pi)
BGK_BETA: float = 0.582597


@dataclass(frozen=True)
class PercentileBands:
    p05: float
    p25: float
    p50: float
    p75: float
    p95: float


@dataclass(frozen=True)
class SimulationResult:
    spot_price: float
    target_price: float
    direction: Direction
    leverage: float
    required_return: float
    goal_price_for_return: float
    prob_touch_target: float
    prob_touch_return_goal: float
    closed_form_prob_target: float
    cross_check_diff: float
    cross_check_alert: bool
    high_bands: PercentileBands
    low_bands: PercentileBands
    close_bands: PercentileBands
    runtime_ms: float


def compute_return_goal_price(
    spot_price: float,
    direction: Direction,
    leverage: float,
    required_return: float,
) -> float:
    """
    Converts a leveraged return target (e.g. required_return=0.20 at leverage=5.0 -> 4% move)
    into the exact underlying oil price level needed.
    """
    if spot_price <= 0:
        raise ValueError("spot_price must be positive.")
    if leverage <= 0:
        raise ValueError("leverage must be positive.")
    if required_return < 0:
        raise ValueError("required_return must be non-negative.")

    unleveraged_move = required_return / leverage
    if direction == "long":
        return float(spot_price * (1.0 + unleveraged_move))
    elif direction == "short":
        if unleveraged_move >= 1.0:
            raise ValueError("Unleveraged short move cannot be >= 100%.")
        return float(spot_price * (1.0 - unleveraged_move))
    else:
        raise ValueError(f"Unsupported direction: {direction}")


def closed_form_barrier_touch_prob(
    spot_price: float,
    barrier_price: float,
    sigma_annual: float,
    t_years: float,
    mu_annual: float = 0.0,
) -> float:
    """
    Exact continuous-time first-passage probability for Geometric Brownian Motion
    hitting `barrier_price` before `t_years` using the Reflection Principle.
    """
    if spot_price <= 0 or barrier_price <= 0:
        raise ValueError("Prices must be strictly positive.")
    if abs(barrier_price - spot_price) < 1e-12:
        return 1.0
    if t_years <= 0 or sigma_annual <= 0:
        return 0.0

    log_ratio = np.log(barrier_price / spot_price)
    a = abs(log_ratio)
    direction_sign = 1.0 if log_ratio > 0 else -1.0

    # Drift in log-space toward the barrier
    mu_eff = direction_sign * (mu_annual - 0.5 * (sigma_annual ** 2))
    vol_sqrt_t = sigma_annual * np.sqrt(t_years)

    z1 = (-a + mu_eff * t_years) / vol_sqrt_t
    z2 = (-a - mu_eff * t_years) / vol_sqrt_t

    # Guard exponent against overflow when sigma is tiny
    exponent = np.clip((2.0 * mu_eff * a) / (sigma_annual ** 2), -100.0, 100.0)
    prob = float(ndtr(z1) + np.exp(exponent) * ndtr(z2))
    return float(np.clip(prob, 0.0, 1.0))


class SimulationEngine:
    """
    Vectorized Merton Jump-Diffusion Monte Carlo engine with antithetic variates
    and automatic closed-form barrier cross-checking.
    """

    def __init__(
        self,
        n_paths: int = 50_000,
        cross_check_tolerance: float = 0.025,
        seed: Optional[int] = None,
    ):
        if n_paths % 2 != 0:
            n_paths += 1  # Ensure even path count for antithetic pairing
        self.n_paths = n_paths
        self.cross_check_tolerance = cross_check_tolerance
        self.rng = np.random.default_rng(seed)

    @staticmethod
    def _make_bands(arr: np.ndarray) -> PercentileBands:
        p05, p25, p50, p75, p95 = np.percentile(arr, [5, 25, 50, 75, 95])
        return PercentileBands(
            p05=round(float(p05), 4),
            p25=round(float(p25), 4),
            p50=round(float(p50), 4),
            p75=round(float(p75), 4),
            p95=round(float(p95), 4),
        )

    @staticmethod
    def _touch_probability_from_extrema(
        spot_price: float,
        level: float,
        max_log_paths: np.ndarray,
        min_log_paths: np.ndarray,
        sigma_step: float,
    ) -> float:
        """
        Evaluates barrier first-passage probability using Broadie-Glasserman-Kou
        continuity-corrected log thresholds so discrete time steps match continuous monitoring.
        """
        log_dist = np.log(level / spot_price)
        if abs(log_dist) < 1e-12:
            return 1.0

        # Continuity shift compensates for intra-step excursions between discrete grid points
        shift = BGK_BETA * sigma_step
        if log_dist > 0:
            eff_threshold = max(0.0, log_dist - shift)
            touched = max_log_paths >= eff_threshold
        else:
            eff_threshold = min(0.0, log_dist + shift)
            touched = min_log_paths <= eff_threshold

        return float(np.mean(touched))

    def run(
        self,
        spot_price: float,
        target_price: float,
        direction: Direction,
        leverage: float,
        required_return: float,
        sigma_annual: float,
        t_years: float,
        n_steps: int = 120,
        mu_annual: float = 0.0,
        jump_intensity_annual: float = 0.0,
        jump_mean: float = 0.0,
        jump_std: float = 0.015,
    ) -> SimulationResult:
        t0 = time.perf_counter()
        n_steps = max(10, int(n_steps))
        dt = t_years / n_steps
        sigma_step = sigma_annual * np.sqrt(dt)

        half_paths = self.n_paths // 2

        # 1. Antithetic standard normal draws: shape (half_paths, n_steps) -> (n_paths, n_steps)
        z_half = self.rng.standard_normal(size=(half_paths, n_steps), dtype=np.float64)
        z = np.concatenate([z_half, -z_half], axis=0)

        # 2. Jump-diffusion drift compensation
        if jump_intensity_annual > 0.0:
            k = np.exp(jump_mean + 0.5 * (jump_std ** 2)) - 1.0
        else:
            k = 0.0

        drift_step = (mu_annual - 0.5 * (sigma_annual ** 2) - jump_intensity_annual * k) * dt
        increments = drift_step + sigma_step * z

        # 3. Compound Poisson jumps (if jump_intensity_annual > 0)
        if jump_intensity_annual > 0.0:
            lam_step = jump_intensity_annual * dt
            n_jumps = self.rng.poisson(lam=lam_step, size=(self.n_paths, n_steps))
            if np.any(n_jumps > 0):
                j_normals = self.rng.standard_normal(size=(self.n_paths, n_steps))
                jump_component = n_jumps * jump_mean + np.sqrt(n_jumps) * jump_std * j_normals
                increments += jump_component

        # 4. Cumulative log-return paths and running extrema (including t=0 at 0.0)
        log_paths = np.cumsum(increments, axis=1)
        max_log = np.maximum(0.0, np.max(log_paths, axis=1))
        min_log = np.minimum(0.0, np.min(log_paths, axis=1))
        end_log = log_paths[:, -1]

        # 5. Goal price for required leveraged return
        goal_price = compute_return_goal_price(
            spot_price=spot_price,
            direction=direction,
            leverage=leverage,
            required_return=required_return,
        )

        # 6. First-passage probabilities for user target & return goal
        if (direction == "long" and target_price <= spot_price) or (
            direction == "short" and target_price >= spot_price
        ):
            prob_target = 1.0
            cf_prob = 1.0
        else:
            prob_target = self._touch_probability_from_extrema(
                spot_price, target_price, max_log, min_log, sigma_step
            )
            cf_prob = closed_form_barrier_touch_prob(
                spot_price=spot_price,
                barrier_price=target_price,
                sigma_annual=sigma_annual,
                t_years=t_years,
                mu_annual=mu_annual,
            )

        prob_goal = self._touch_probability_from_extrema(
            spot_price, goal_price, max_log, min_log, sigma_step
        )

        # 7. Closed-form cross-check alert
        diff = abs(prob_target - cf_prob)
        alert = bool(jump_intensity_annual == 0.0 and diff > self.cross_check_tolerance)

        # 8. Convert log extrema to price bands
        high_prices = spot_price * np.exp(max_log)
        low_prices = spot_price * np.exp(min_log)
        close_prices = spot_price * np.exp(end_log)

        runtime_ms = (time.perf_counter() - t0) * 1000.0

        return SimulationResult(
            spot_price=spot_price,
            target_price=target_price,
            direction=direction,
            leverage=leverage,
            required_return=required_return,
            goal_price_for_return=round(goal_price, 4),
            prob_touch_target=round(prob_target, 4),
            prob_touch_return_goal=round(prob_goal, 4),
            closed_form_prob_target=round(cf_prob, 4),
            cross_check_diff=round(diff, 4),
            cross_check_alert=alert,
            high_bands=self._make_bands(high_prices),
            low_bands=self._make_bands(low_prices),
            close_bands=self._make_bands(close_prices),
            runtime_ms=round(runtime_ms, 2),
        )
