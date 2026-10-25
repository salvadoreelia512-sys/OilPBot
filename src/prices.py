import logging
import math
import sqlite3
import time
import threading
from zoneinfo import ZoneInfo
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

from src.config import BotSettings

logger = logging.getLogger("oil_bot.prices")


class StaleDataError(RuntimeError):
    """Raised when the freshest available price exceeds max_staleness_seconds."""


class AllSourcesDownError(RuntimeError):
    """Raised when every adapter in the failover chain fails."""


@dataclass(frozen=True)
class PriceTick:
    symbol: str
    price: float
    timestamp_utc: datetime
    source: str

    def age_seconds(self, now_utc: Optional[datetime] = None) -> float:
        ref = now_utc or datetime.now(timezone.utc)
        return max(0.0, (ref - self.timestamp_utc).total_seconds())


@dataclass
class SourceHealth:
    name: str
    is_healthy: bool = False
    last_success_utc: Optional[datetime] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    latency_ms: float = 0.0


class PriceAdapter(ABC):
    name: str

    @abstractmethod
    def fetch_latest_tick(self, symbol: str) -> PriceTick:
        """Fetch latest tick and return standardized PriceTick with UTC timestamp."""


class FinnhubAdapter(PriceAdapter):
    name = "finnhub"

    def __init__(self, api_key: str, timeout: float = 4.0):
        self.api_key = api_key
        self.timeout = timeout

    def fetch_latest_tick(self, symbol: str = "USO") -> PriceTick:
        if not self.api_key:
            raise ValueError("Finnhub API key not configured")
        url = "https://finnhub.io/api/v1/quote"
        resp = httpx.get(
            url,
            params={"symbol": symbol, "token": self.api_key},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        price = float(data.get("c", 0.0))
        ts = int(data.get("t", 0))
        if price <= 0 or ts <= 0:
            raise ValueError(f"Invalid Finnhub payload: {data}")
        return PriceTick(
            symbol=symbol,
            price=price,
            timestamp_utc=datetime.fromtimestamp(ts, tz=timezone.utc),
            source=self.name,
        )


class TwelveDataAdapter(PriceAdapter):
    name = "twelvedata"

    def __init__(self, api_key: str, timeout: float = 4.0):
        self.api_key = api_key
        self.timeout = timeout

    def fetch_latest_tick(self, symbol: str = "WTI/USD") -> PriceTick:
        if not self.api_key:
            raise ValueError("TwelveData API key not configured")
        url = "https://api.twelvedata.com/time_series"
        resp = httpx.get(
            url,
            params={
                "symbol": symbol,
                "interval": "1min",
                "outputsize": 1,
                "apikey": self.api_key,
                "timezone": "UTC",
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        values = body.get("values")
        if not values:
            raise ValueError(f"TwelveData returned no values: {body}")
        latest = values[0]
        dt = datetime.strptime(latest["datetime"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        return PriceTick(
            symbol=symbol,
            price=float(latest["close"]),
            timestamp_utc=dt,
            source=self.name,
        )


class YahooFinanceAdapter(PriceAdapter):
    name = "yahoo"

    def fetch_latest_tick(self, symbol: str = "CL=F") -> PriceTick:
        if yf is None:
            raise RuntimeError("yfinance package is not installed")
        ticker = yf.Ticker(symbol)
        try:
            hist = ticker.history(period="1d", interval="1m", timeout=4, raise_errors=True)
        except Exception as exc:
            name = type(exc).__name__
            if "RateLimit" in name or "429" in str(exc):
                raise RuntimeError("Yahoo rate limited requests; waiting before retry") from exc
            if "Timeout" in name or "Connection" in name:
                raise ConnectionError("Yahoo connection failed or timed out") from exc
            raise ValueError("Yahoo returned no usable minute bars (market closed or data unavailable)") from exc
        if hist.empty:
            raise ValueError("Yahoo returned no usable minute bars (market closed or data unavailable)")
        last_row = hist.iloc[-1]
        last_ts = hist.index[-1].to_pydatetime()
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        else:
            last_ts = last_ts.astimezone(timezone.utc)
        return PriceTick(
            symbol=symbol,
            price=float(last_row["Close"]),
            timestamp_utc=last_ts,
            source=self.name,
        )


class AlpacaAdapter(PriceAdapter):
    """Queries Alpaca Market Data v2 latest minute bars endpoint."""
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, timeout: float = 4.0):
        self.api_key = api_key
        self.secret_key = secret_key
        self.timeout = timeout

    def fetch_latest_tick(self, symbol: str = "USO") -> PriceTick:
        if not self.api_key or not self.secret_key:
            raise ValueError("Alpaca API credentials not configured")
        url = "https://data.alpaca.markets/v2/stocks/bars/latest"
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        }
        resp = httpx.get(
            url,
            params={"symbols": symbol, "feed": "iex"},
            headers=headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        bars = resp.json().get("bars", {})
        if symbol not in bars:
            raise ValueError(f"Alpaca returned no bar for {symbol}")
        bar = bars[symbol]
        ts_str = bar["t"].replace("Z", "+00:00")
        return PriceTick(
            symbol=symbol,
            price=float(bar["c"]),
            timestamp_utc=datetime.fromisoformat(ts_str).astimezone(timezone.utc),
            source=self.name,
        )


class BarRepository:
    """SQLite storage for 1-minute bars used by the Volatility Engine."""

    def __init__(self, db_path: str = "oil_bot.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bars_1m (
                    symbol TEXT NOT NULL,
                    bucket_utc INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY (symbol, bucket_utc)
                )
                """
            )
            conn.commit()

    def record_tick_as_bar(self, tick: PriceTick) -> None:
        bucket = int(tick.timestamp_utc.timestamp()) // 60 * 60
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO bars_1m (symbol, bucket_utc, open, high, low, close, source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, bucket_utc) DO UPDATE SET
                    high = MAX(bars_1m.high, excluded.high),
                    low = MIN(bars_1m.low, excluded.low),
                    close = excluded.close,
                    source = excluded.source
                """,
                (tick.symbol, bucket, tick.price, tick.price, tick.price, tick.price, tick.source),
            )
            conn.commit()

    def get_recent_closes(self, symbol: str, limit: int = 390) -> pd.Series:
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql_query(
                """
                SELECT bucket_utc, close
                FROM bars_1m
                WHERE symbol = ?
                ORDER BY bucket_utc DESC
                LIMIT ?
                """,
                conn,
                params=(symbol, limit),
            )
        if df.empty:
            return pd.Series(dtype=float)
        df = df.sort_values("bucket_utc")
        idx = pd.to_datetime(df["bucket_utc"], unit="s", utc=True)
        return pd.Series(df["close"].values, index=idx, name="close")


class PriceService:
    """
    Manages the failover chain, health tracking, last-good-value cache,
    and hard staleness refusal.
    """

    def __init__(
        self,
        adapters: List[PriceAdapter],
        settings: BotSettings,
        bar_repo: Optional[BarRepository] = None,
    ):
        self.adapters = adapters
        self.settings = settings
        self.bar_repo = bar_repo or BarRepository(settings.sqlite_db_path)
        self.health: Dict[str, SourceHealth] = {
            a.name: SourceHealth(name=a.name) for a in adapters
        }
        self._last_good_tick: Optional[PriceTick] = None
        self._attempt_after = {}
        self._provider_cache = {}
        self._lock = threading.Lock()

    def validate_tick(self, tick, now_utc, data_mode="strict"):
        if data_mode not in {"strict", "delayed"}:
            raise ValueError("Unknown data mode")
        limit = self.settings.max_staleness_seconds if data_mode == "strict" else 900.0
        local_now = now_utc.astimezone(ZoneInfo(self.settings.session_timezone))
        local_tick = tick.timestamp_utc.astimezone(ZoneInfo(self.settings.session_timezone))
        cutoff = local_now.replace(hour=self.settings.session_cutoff_hour,
                                   minute=self.settings.session_cutoff_minute, second=0, microsecond=0)
        if local_now >= cutoff or local_tick.date() != local_now.date():
            raise StaleDataError("REFUSED: Quote is outside today's active Dubai session.")
        if tick.age_seconds(now_utc) > limit:
            raise StaleDataError(f"REFUSED: Quote is older than the {limit:g}-second {data_mode} limit.")
        return tick

    def get_fresh_price(self, symbol_map=None, now_utc=None, data_mode="strict"):
        # Serialize refreshes so concurrent tabs do not multiply provider traffic.
        with self._lock:
            return self._get_price(symbol_map, now_utc, data_mode)

    def _get_price(
        self,
        symbol_map: Optional[Dict[str, str]] = None,
        now_utc: Optional[datetime] = None,
        data_mode: str = "strict",
    ) -> PriceTick:
        """Refresh compatible providers and apply the requested age/session policy."""
        ref_now = now_utc or datetime.now(timezone.utc)
        if symbol_map is None:
            # A failover must not substitute ETF shares for futures or spot oil.
            supported = {
                "CL=F": {"yahoo": "CL=F"},
                "USO": {name: "USO" for name in ("finnhub", "twelvedata", "yahoo", "alpaca")},
                "WTI/USD": {"twelvedata": "WTI/USD"},
            }
            symbol_map = supported[self.settings.market_symbol]

        newest_candidate: Optional[PriceTick] = None

        for adapter in self.adapters:
            if adapter.name in {"finnhub", "twelvedata", "yahoo", "alpaca"} and adapter.name not in symbol_map:
                self.health[adapter.name].last_error = "Provider does not support the configured instrument"
                continue
            sym = symbol_map.get(adapter.name, self.settings.market_symbol)
            t0 = time.perf_counter()
            try:
                key = (adapter.name, sym)
                fetched = False
                if time.monotonic() < self._attempt_after.get(key, 0):
                    tick = self._provider_cache.get(key)
                    if tick is None:
                        continue
                else:
                    try:
                        tick = adapter.fetch_latest_tick(sym)
                        fetched = True
                    except Exception:
                        failures = self.health[adapter.name].consecutive_failures
                        self._attempt_after[key] = time.monotonic() + min(300, 30 * 2 ** min(failures, 4))
                        raise
                    self._provider_cache[key] = tick
                    self._attempt_after[key] = time.monotonic() + 30
                if not math.isfinite(tick.price) or tick.price <= 0:
                    raise ValueError("Provider returned an invalid price")
                if tick.timestamp_utc.tzinfo is None or (tick.timestamp_utc - ref_now).total_seconds() > 60:
                    raise ValueError("Provider returned an invalid timestamp")
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                age = tick.age_seconds(ref_now)

                # Update source health
                h = self.health[adapter.name]
                h.latency_ms = elapsed_ms
                if fetched:
                    h.last_success_utc = ref_now

                try:
                    self.validate_tick(tick, ref_now, data_mode)
                    acceptable = True
                except StaleDataError as exc:
                    h.last_error = str(exc)
                    acceptable = False
                if acceptable:
                    h.is_healthy = True
                    if fetched:
                        h.consecutive_failures = 0
                        h.last_error = None
                    self._last_good_tick = tick
                    self.bar_repo.record_tick_as_bar(tick)
                    return tick
                else:
                    h.is_healthy = False
                    if newest_candidate is None or tick.timestamp_utc > newest_candidate.timestamp_utc:
                        newest_candidate = tick

            except Exception as exc:
                h = self.health[adapter.name]
                h.is_healthy = False
                h.latency_ms = (time.perf_counter() - t0) * 1000.0
                h.consecutive_failures += 1
                if adapter.name == "yahoo" and isinstance(exc, (ValueError, RuntimeError, ConnectionError)):
                    h.last_error = str(exc) if str(exc).startswith("Yahoo ") else f"Provider request failed ({type(exc).__name__})"
                else:
                    h.last_error = f"Provider request failed ({type(exc).__name__})"
                logger.warning("Adapter %s failed (%s)", adapter.name, type(exc).__name__)

        # Check if cached tick is still within staleness threshold
        if self._last_good_tick is not None:
            try:
                return self.validate_tick(self._last_good_tick, ref_now, data_mode)
            except StaleDataError:
                pass
            if newest_candidate is None or self._last_good_tick.timestamp_utc > newest_candidate.timestamp_utc:
                newest_candidate = self._last_good_tick

        if newest_candidate is not None:
            self.validate_tick(newest_candidate, ref_now, data_mode)
            return newest_candidate

        raise AllSourcesDownError("REFUSED: All price sources failed and no cached tick is available.")

    def health_report(self, now_utc: Optional[datetime] = None) -> Dict[str, object]:
        ref_now = now_utc or datetime.now(timezone.utc)
        last_age = (
            self._last_good_tick.age_seconds(ref_now)
            if self._last_good_tick
            else None
        )
        status = "stale_or_down"
        if self._last_good_tick:
            try:
                self.validate_tick(self._last_good_tick, ref_now, "delayed")
                status = "ok" if last_age <= self.settings.max_staleness_seconds else "delayed"
            except StaleDataError:
                pass
        return {
            "status": status,
            "last_good_tick": self._last_good_tick,
            "last_tick_age_seconds": last_age,
            "sources": {
                name: {
                    "is_healthy": h.is_healthy,
                    "consecutive_failures": h.consecutive_failures,
                    "latency_ms": round(h.latency_ms, 2),
                    "last_error": h.last_error,
                    "retry_after_seconds": round(max(0, self._attempt_after.get((name, self.settings.market_symbol), 0) - time.monotonic()), 1),
                }
                for name, h in self.health.items()
            },
        }
