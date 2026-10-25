import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Callable, Dict, List, Literal, Optional, Sequence

import numpy as np
import pandas as pd

from src.backtest import BacktestConfig, WalkForwardBacktester
from src.calibration import CalibrationSummary, PredictionLogRepository
from src.config import BotSettings

RiskFlagLevel = Literal["normal", "elevated", "high", "critical"]


@dataclass(frozen=True)
class BoundingBox:
    """Geographic bounding box around the Strait of Hormuz."""
    lat_min: float = 25.50
    lat_max: float = 27.00
    lon_min: float = 55.50
    lon_max: float = 57.50

    def contains(self, lat: float, lon: float) -> bool:
        return (self.lat_min <= lat <= self.lat_max) and (self.lon_min <= lon <= self.lon_max)

    def to_aisstream_format(self) -> List[List[List[float]]]:
        """Returns the nested coordinate list expected by aisstream.io subscription messages."""
        return [[[self.lat_min, self.lon_min], [self.lat_max, self.lon_max]]]


@dataclass(frozen=True)
class AISTankerReport:
    """Normalized AIS position report for a vessel inside or near the Strait of Hormuz."""
    mmsi: str
    timestamp_utc: datetime
    lat: float
    lon: float
    sog_knots: float          # Speed Over Ground (knots)
    cog_degrees: float        # Course Over Ground (0-360 degrees)
    ship_type: int = 80       # AIS ship types 80-89 correspond to Tankers
    vessel_name: str = ""

    @property
    def is_tanker(self) -> bool:
        return 80 <= self.ship_type <= 89


@dataclass(frozen=True)
class AISRiskStatus:
    """
    Non-directional risk output from the Hormuz AIS service.
    Feeds the jump-diffusion simulation engine and the UI 'Why' risk drivers panel.
    """
    timestamp_utc: datetime
    risk_flag: RiskFlagLevel
    anomaly_score: float             # [0.0, 1.0]
    transits_last_hour: int
    rolling_baseline_per_hr: float
    transit_ratio_vs_baseline: float
    loitering_tanker_count: int
    route_diversion_count: int
    sudden_transit_drop: bool
    loitering_cluster_anomaly: bool
    route_change_anomaly: bool
    jump_intensity_add: float        # Feeds Merton jump intensity (annualized)
    vol_multiplier_add: float        # Modest volatility scaling accompanying jump risk
    driver_summary: str


def angular_difference_degrees(angle_a: float, angle_b: float) -> float:
    """Computes the smallest absolute angle (0 to 180 degrees) between two compass headings."""
    diff = abs((angle_a - angle_b) % 360.0)
    return min(diff, 360.0 - diff)


class AISStreamConsumer:
    """
    WebSocket client & parser for aisstream.io with a bounding box around the Strait of Hormuz.
    Supports both live async WebSocket consumption and deterministic JSON payload parsing.
    """

    ws_url: str = "wss://stream.aisstream.io/v0/stream"

    def __init__(
        self,
        api_key: str = "",
        bbox: Optional[BoundingBox] = None,
    ):
        self.api_key = api_key
        self.bbox = bbox or BoundingBox()

    def build_subscription_payload(self) -> str:
        if not self.api_key:
            raise ValueError("AISstream API key is required to build subscription payload.")
        payload = {
            "APIKey": self.api_key,
            "BoundingBoxes": self.bbox.to_aisstream_format(),
            "FilterMessageTypes": ["PositionReport"],
        }
        return json.dumps(payload)

    def parse_message(
        self,
        raw_message: str | Dict[str, object],
        default_ship_type: int = 80,
        fallback_utc: Optional[datetime] = None,
    ) -> Optional[AISTankerReport]:
        """
        Parses an aisstream.io PositionReport JSON message and filters by the Hormuz bounding box.
        Returns None if the message is not a PositionReport or lies outside the bounding box.
        """
        data = json.loads(raw_message) if isinstance(raw_message, str) else raw_message
        msg_type = data.get("MessageType")
        if msg_type != "PositionReport":
            return None

        meta = data.get("MetaData") or {}
        pos = (data.get("Message") or {}).get("PositionReport") or {}

        lat = pos.get("Latitude", meta.get("latitude"))
        lon = pos.get("Longitude", meta.get("longitude"))
        if lat is None or lon is None:
            return None
        lat_f, lon_f = float(lat), float(lon)
        if not self.bbox.contains(lat_f, lon_f):
            return None

        mmsi = str(meta.get("MMSI") or pos.get("UserID") or "").strip()
        if not mmsi:
            return None

        sog = float(pos.get("Sog", 0.0))
        cog = float(pos.get("Cog", 0.0)) % 360.0
        ship_type = int(meta.get("ShipType", default_ship_type))
        vessel_name = str(meta.get("ShipName") or "").strip()

        time_str = str(meta.get("time_utc") or "").strip()
        dt = fallback_utc or datetime.now(timezone.utc)
        if time_str:
            try:
                # Handle standard ISO or aisstream '2026-10-25 12:00:00.000000 +0000 UTC'
                clean_ts = time_str.replace(" UTC", "").strip()
                dt = datetime.fromisoformat(clean_ts).astimezone(timezone.utc)
            except Exception:
                dt = fallback_utc or datetime.now(timezone.utc)

        return AISTankerReport(
            mmsi=mmsi,
            timestamp_utc=dt,
            lat=lat_f,
            lon=lon_f,
            sog_knots=sog,
            cog_degrees=cog,
            ship_type=ship_type,
            vessel_name=vessel_name,
        )

    async def stream_reports(self) -> AsyncIterator[AISTankerReport]:
        """Connects to wss://stream.aisstream.io/v0/stream and yields filtered AISTankerReports."""
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("The 'websockets' package is required for live AIS streaming.") from exc

        sub_json = self.build_subscription_payload()
        async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(sub_json)
            async for raw_msg in ws:
                report = self.parse_message(raw_msg)
                if report is not None and report.is_tanker:
                    yield report


class HormuzMonitor:
    """
    Phase 5 Hormuz AIS Anomaly Detector & Jump-Intensity Hook.
    Tracks:
      1. Tanker transits per hour vs. rolling 24-hour baseline
      2. Sudden transit drops (blockade / avoidance proxy)
      3. Loitering / anchoring tanker clusters inside the strait
      4. Sharp route changes / heading reversals (>= 65 deg course change)
    Outputs a strictly non-directional risk flag that feeds the simulation jump intensity.
    """

    def __init__(
        self,
        settings: Optional[BotSettings] = None,
        bbox: Optional[BoundingBox] = None,
        enabled: bool = True,
    ):
        self.settings = settings or BotSettings()
        self.bbox = bbox or BoundingBox(
            lat_min=self.settings.hormuz_lat_min,
            lat_max=self.settings.hormuz_lat_max,
            lon_min=self.settings.hormuz_lon_min,
            lon_max=self.settings.hormuz_lon_max,
        )
        self.enabled = enabled
        self._reports: List[AISTankerReport] = []

    def ingest_reports(self, reports: Sequence[AISTankerReport]) -> int:
        kept = 0
        for r in reports:
            if r.is_tanker and self.bbox.contains(r.lat, r.lon):
                self._reports.append(r)
                kept += 1
        self._reports.sort(key=lambda x: x.timestamp_utc)
        return kept

    def prune_older_than(self, cutoff_utc: datetime) -> None:
        self._reports = [r for r in self._reports if r.timestamp_utc >= cutoff_utc]

    def evaluate_risk(self, now_utc: Optional[datetime] = None) -> AISRiskStatus:
        ref_now = now_utc or datetime.now(timezone.utc)
        if not self.enabled:
            return AISRiskStatus(
                timestamp_utc=ref_now,
                risk_flag="normal",
                anomaly_score=0.0,
                transits_last_hour=0,
                rolling_baseline_per_hr=self.settings.ais_default_baseline_transits_per_hr,
                transit_ratio_vs_baseline=1.0,
                loitering_tanker_count=0,
                route_diversion_count=0,
                sudden_transit_drop=False,
                loitering_cluster_anomaly=False,
                route_change_anomaly=False,
                jump_intensity_add=0.0,
                vol_multiplier_add=1.0,
                driver_summary="Hormuz AIS monitor disabled.",
            )

        # Slice strictly causal reports in the 24 hours up to ref_now
        window_24h_start = ref_now - timedelta(hours=24)
        window_1h_start = ref_now - timedelta(hours=1)

        causal_24h = [
            r for r in self._reports if window_24h_start <= r.timestamp_utc <= ref_now
        ]
        recent_1h = [
            r for r in causal_24h if r.timestamp_utc >= window_1h_start
        ]

        # 1. Rolling baseline transits per hour (hours -24 to -1)
        prior_23h = [r for r in causal_24h if r.timestamp_utc < window_1h_start]
        if prior_23h:
            hourly_counts: Dict[int, set[str]] = {}
            for r in prior_23h:
                if r.sog_knots >= self.settings.ais_loitering_speed_knots:
                    hr_bucket = int((ref_now - r.timestamp_utc).total_seconds() // 3600)
                    hourly_counts.setdefault(hr_bucket, set()).add(r.mmsi)
            if len(hourly_counts) >= 3:
                baseline_per_hr = float(np.mean([len(v) for v in hourly_counts.values()]))
            else:
                baseline_per_hr = self.settings.ais_default_baseline_transits_per_hr
        else:
            baseline_per_hr = self.settings.ais_default_baseline_transits_per_hr

        baseline_per_hr = max(1.0, baseline_per_hr)

        # 2. Current hour moving transits vs loitering tankers
        by_mmsi_1h: Dict[str, List[AISTankerReport]] = {}
        for r in recent_1h:
            by_mmsi_1h.setdefault(r.mmsi, []).append(r)

        moving_mmsis: set[str] = set()
        loitering_positions: List[tuple[float, float]] = []
        route_diversions = 0

        for mmsi, vessel_reports in by_mmsi_1h.items():
            latest = vessel_reports[-1]
            if latest.sog_knots < self.settings.ais_loitering_speed_knots:
                loitering_positions.append((latest.lat, latest.lon))
            else:
                moving_mmsis.add(mmsi)

            # Check heading changes across the vessel's track in the last hour
            if len(vessel_reports) >= 2:
                first_cog = vessel_reports[0].cog_degrees
                last_cog = vessel_reports[-1].cog_degrees
                turn_deg = angular_difference_degrees(first_cog, last_cog)
                if turn_deg >= self.settings.ais_route_turn_degrees_threshold:
                    route_diversions += 1

        transits_last_hr = len(moving_mmsis)
        transit_ratio = transits_last_hr / baseline_per_hr

        # Anomaly 1: Sudden drop in transits (only trigger if we have active AIS feed in the last 24h)
        has_feed_activity = len(causal_24h) > 0
        sudden_drop = bool(
            has_feed_activity
            and baseline_per_hr >= 2.0
            and transit_ratio <= self.settings.ais_transit_drop_ratio_threshold
        )

        # Anomaly 2: Loitering cluster detection (tankers anchored/slow within proximity)
        loitering_count = len(loitering_positions)
        loitering_anomaly = False
        if loitering_count >= self.settings.ais_loitering_cluster_min_vessels:
            lats = np.array([p[0] for p in loitering_positions])
            lons = np.array([p[1] for p in loitering_positions])
            # Check spatial clustering around median centroid (within ~0.25 deg)
            med_lat, med_lon = float(np.median(lats)), float(np.median(lons))
            clustered_count = int(
                np.sum(np.hypot(lats - med_lat, lons - med_lon) <= 0.25)
            )
            if clustered_count >= self.settings.ais_loitering_cluster_min_vessels:
                loitering_anomaly = True

        # Anomaly 3: Route change / course diversion anomaly
        route_anomaly = bool(route_diversions >= 2)

        # Composite anomaly score in [0.0, 1.0]
        score = 0.0
        if sudden_drop:
            drop_severity = min(1.0, max(0.0, 1.0 - transit_ratio))
            score += 0.45 * drop_severity
        if loitering_anomaly:
            loiter_severity = min(
                1.0, loitering_count / (self.settings.ais_loitering_cluster_min_vessels * 2.0)
            )
            score += 0.35 * max(0.6, loiter_severity)
        if route_anomaly:
            divert_severity = min(1.0, route_diversions / 4.0)
            score += 0.35 * max(0.6, divert_severity)

        score = float(np.clip(score, 0.0, 1.0))

        if score >= 0.75:
            flag: RiskFlagLevel = "critical"
        elif score >= 0.45:
            flag = "high"
        elif score >= 0.20:
            flag = "elevated"
        else:
            flag = "normal"

        jump_add = round(score * self.settings.ais_max_jump_intensity, 4)
        vol_mult_add = round(
            1.0 + score * max(0.0, self.settings.ais_vol_multiplier_cap - 1.0), 4
        )

        reasons: List[str] = []
        if sudden_drop:
            reasons.append(f"transits dropped to {transits_last_hr}/hr vs {baseline_per_hr:.1f} baseline")
        if loitering_anomaly:
            reasons.append(f"{loitering_count} tankers loitering/anchored")
        if route_anomaly:
            reasons.append(f"{route_diversions} tankers made sharp route diversions")
        if not reasons:
            reasons.append(f"normal flow ({transits_last_hr}/hr vs {baseline_per_hr:.1f} baseline)")

        summary_str = f"Hormuz AIS [{flag.upper()}]: " + "; ".join(reasons)

        return AISRiskStatus(
            timestamp_utc=ref_now,
            risk_flag=flag,
            anomaly_score=round(score, 4),
            transits_last_hour=transits_last_hr,
            rolling_baseline_per_hr=round(baseline_per_hr, 2),
            transit_ratio_vs_baseline=round(transit_ratio, 4),
            loitering_tanker_count=loitering_count,
            route_diversion_count=route_diversions,
            sudden_transit_drop=sudden_drop,
            loitering_cluster_anomaly=loitering_anomaly,
            route_change_anomaly=route_anomaly,
            jump_intensity_add=jump_add,
            vol_multiplier_add=vol_mult_add,
            driver_summary=summary_str,
        )

    def as_backtest_hook(
        self,
        base_news_hook: Optional[Callable[[pd.Series, datetime], tuple[float, float]]] = None,
    ) -> Callable[[pd.Series, datetime], tuple[float, float]]:
        """
        Returns a `RiskHook` for `WalkForwardBacktester.run_walk_forward()`.
        Can run standalone or combine additively with the Phase 4 NewsRiskAdjuster hook.
        """
        def _hook(closes_1m: pd.Series, now_utc: datetime) -> tuple[float, float]:
            if base_news_hook is not None:
                news_vol_mult, news_jump = base_news_hook(closes_1m, now_utc)
            else:
                news_vol_mult, news_jump = 1.0, 0.0

            status = self.evaluate_risk(now_utc=now_utc)
            combined_vol_mult = news_vol_mult * status.vol_multiplier_add
            combined_jump = news_jump + status.jump_intensity_add
            return round(combined_vol_mult, 4), round(combined_jump, 4)

        return _hook


def evaluate_ais_layer_ab(
    sessions: Sequence[pd.DataFrame],
    hormuz_monitor: HormuzMonitor,
    settings: Optional[BotSettings] = None,
    config: Optional[BacktestConfig] = None,
    ovx_index: float = 32.0,
    seed: int = 42,
) -> tuple[CalibrationSummary, CalibrationSummary, bool]:
    """
    Phase 5 Out-of-Sample A/B Calibration Gate:
    Compares the walk-forward baseline against the Hormuz AIS jump-intensity hook.
    Keeps `hormuz_monitor.enabled = True` ONLY if out-of-sample calibration beats the baseline.
    """
    cfg_settings = settings or BotSettings()
    base_cfg = config or BacktestConfig(model_version="baseline_v1")
    ais_cfg = BacktestConfig(
        model_version="hormuz_ais_v1",
        symbol=base_cfg.symbol,
        lookback_bars=base_cfg.lookback_bars,
        session_bars=base_cfg.session_bars,
        checkpoint_fractions=base_cfg.checkpoint_fractions,
        target_z_multipliers=base_cfg.target_z_multipliers,
        default_leverage=base_cfg.default_leverage,
        default_required_return=base_cfg.default_required_return,
        mc_paths=base_cfg.mc_paths,
    )

    repo = PredictionLogRepository(db_path=cfg_settings.sqlite_db_path)

    # 1. Baseline walk-forward run
    bt_base = WalkForwardBacktester(
        settings=cfg_settings,
        config=base_cfg,
        log_repo=repo,
        seed=seed,
    )
    baseline_summary = bt_base.run_walk_forward(
        sessions=sessions,
        ovx_index=ovx_index,
        risk_hook=None,
        save_as_benchmark=True,
    )

    # 2. AIS-aware walk-forward run
    hormuz_monitor.enabled = True
    bt_ais = WalkForwardBacktester(
        settings=cfg_settings,
        config=ais_cfg,
        log_repo=repo,
        seed=seed,
    )
    ais_summary = bt_ais.run_walk_forward(
        sessions=sessions,
        ovx_index=ovx_index,
        risk_hook=hormuz_monitor.as_backtest_hook(),
        save_as_benchmark=False,
    )

    keep_ais = ais_summary.beats_baseline(baseline_summary)
    hormuz_monitor.enabled = keep_ais
    if keep_ais:
        repo.save_benchmark(ais_summary)

    return baseline_summary, ais_summary, keep_ais
