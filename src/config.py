import os
from dataclasses import dataclass, field

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    HAS_PYDANTIC_SETTINGS = True
except ImportError:
    HAS_PYDANTIC_SETTINGS = False


if HAS_PYDANTIC_SETTINGS:
    class BotSettings(BaseSettings):
        # API Keys (loaded from .env)
        finnhub_api_key: str = ""
        twelvedata_api_key: str = ""
        alpaca_api_key: str = ""
        alpaca_secret_key: str = ""
        aisstream_api_key: str = ""

        # Phase 1: Reliability & Staleness
        max_staleness_seconds: float = 60.0
        http_timeout_seconds: float = 4.0
        sqlite_db_path: str = "oil_bot.db"

        # Session Window (Dubai timezone: UTC+4)
        session_timezone: str = "Asia/Dubai"
        session_cutoff_hour: int = 23
        session_cutoff_minute: int = 30
        trading_days_per_year: float = 252.0
        trading_minutes_per_day: float = 1380.0

        # Phase 2: Volatility & Simulation Defaults
        ewma_lambda: float = 0.94
        weight_ewma: float = 0.45
        weight_garch: float = 0.25
        weight_ovx: float = 0.30
        default_ovx_level: float = 32.0

        mc_paths: int = 50_000
        cross_check_tolerance: float = 0.025

        # Phase 4: News Layer Polling & Risk Adjuster Caps
        gdelt_poll_interval_seconds: float = 900.0   # 15 minutes
        rss_poll_interval_seconds: float = 45.0      # 30 to 60 seconds
        finnhub_news_poll_seconds: float = 60.0      # Rate-limit safe
        news_cluster_similarity_threshold: float = 0.42
        news_max_vol_multiplier: float = 2.25
        news_max_jump_intensity: float = 65.0

        # Phase 5: Hormuz AIS Bounding Box & Anomaly Thresholds
        hormuz_lat_min: float = 25.50
        hormuz_lat_max: float = 27.00
        hormuz_lon_min: float = 55.50
        hormuz_lon_max: float = 57.50
        ais_default_baseline_transits_per_hr: float = 6.0
        ais_transit_drop_ratio_threshold: float = 0.50
        ais_loitering_speed_knots: float = 2.0
        ais_loitering_cluster_min_vessels: int = 4
        ais_route_turn_degrees_threshold: float = 65.0
        ais_max_jump_intensity: float = 55.0
        ais_vol_multiplier_cap: float = 1.35

        model_config = SettingsConfigDict(env_file=".env", extra="ignore")
else:
    @dataclass
    class BotSettings:
        finnhub_api_key: str = field(default_factory=lambda: os.getenv("FINNHUB_API_KEY", ""))
        twelvedata_api_key: str = field(default_factory=lambda: os.getenv("TWELVEDATA_API_KEY", ""))
        alpaca_api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
        alpaca_secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
        aisstream_api_key: str = field(default_factory=lambda: os.getenv("AISSTREAM_API_KEY", ""))

        max_staleness_seconds: float = 60.0
        http_timeout_seconds: float = 4.0
        sqlite_db_path: str = "oil_bot.db"

        session_timezone: str = "Asia/Dubai"
        session_cutoff_hour: int = 23
        session_cutoff_minute: int = 30
        trading_days_per_year: float = 252.0
        trading_minutes_per_day: float = 1380.0

        ewma_lambda: float = 0.94
        weight_ewma: float = 0.45
        weight_garch: float = 0.25
        weight_ovx: float = 0.30
        default_ovx_level: float = 32.0

        mc_paths: int = 50_000
        cross_check_tolerance: float = 0.025

        gdelt_poll_interval_seconds: float = 900.0
        rss_poll_interval_seconds: float = 45.0
        finnhub_news_poll_seconds: float = 60.0
        news_cluster_similarity_threshold: float = 0.42
        news_max_vol_multiplier: float = 2.25
        news_max_jump_intensity: float = 65.0

        hormuz_lat_min: float = 25.50
        hormuz_lat_max: float = 27.00
        hormuz_lon_min: float = 55.50
        hormuz_lon_max: float = 57.50
        ais_default_baseline_transits_per_hr: float = 6.0
        ais_transit_drop_ratio_threshold: float = 0.50
        ais_loitering_speed_knots: float = 2.0
        ais_loitering_cluster_min_vessels: int = 4
        ais_route_turn_degrees_threshold: float = 65.0
        ais_max_jump_intensity: float = 55.0
        ais_vol_multiplier_cap: float = 1.35
