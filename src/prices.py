import logging
import sqlite3
import time
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
    is_healthy: bool = True
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
        hist = ticker.history(period="1d", interval="1m")
        if hist.empty:
            raise ValueError(f"Yahoo Finance returned empty 1m bars for {symbol}")
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

    def get_fresh_price(
        self,
        symbol_map: Optional[Dict[str, str]] = None,
        now_utc: Optional[datetime] = None,
    ) -> PriceTick:
        """
        Walks the adapter chain until a fresh, non-stale tick is found.
        Raises StaleDataError if the best tick is older than max_staleness_seconds.
        """
        ref_now = now_utc or datetime.now(timezone.utc)
        symbol_map = symbol_map or {
            "finnhub": "USO",
            "twelvedata": "WTI/USD",
            "yahoo": "CL=F",
            "alpaca": "USO",
        }

        newest_candidate: Optional[PriceTick] = None

        for adapter in self.adapters:
            sym = symbol_map.get(adapter.name, "CL=F")
            t0 = time.perf_counter()
            try:
                tick = adapter.fetch_latest_tick(sym)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                age = tick.age_seconds(ref_now)

                # Update source health
                h = self.health[adapter.name]
                h.latency_ms = elapsed_ms
                h.last_success_utc = ref_now

                if age <= self.settings.max_staleness_seconds:
                    h.is_healthy = True
                    h.consecutive_failures = 0
                    h.last_error = None
                    self._last_good_tick = tick
                    self.bar_repo.record_tick_as_bar(tick)
                    return tick
                else:
                    h.is_healthy = False
                    h.last_error = f"Stale tick (age={age:.1f}s > {self.settings.max_staleness_seconds}s)"
                    if newest_candidate is None or tick.timestamp_utc > newest_candidate.timestamp_utc:
                        newest_candidate = tick

            except Exception as exc:
                h = self.health[adapter.name]
                h.is_healthy = False
                h.consecutive_failures += 1
                h.last_error = str(exc)
                logger.warning("Adapter %s failed: %s", adapter.name, exc)

        # Check if cached tick is still within staleness threshold
        if self._last_good_tick is not None:
            cached_age = self._last_good_tick.age_seconds(ref_now)
            if cached_age <= self.settings.max_staleness_seconds:
                return self._last_good_tick
            if newest_candidate is None or self._last_good_tick.timestamp_utc > newest_candidate.timestamp_utc:
                newest_candidate = self._last_good_tick

        if newest_candidate is not None:
            age = newest_candidate.age_seconds(ref_now)
            raise StaleDataError(
                f"REFUSED: Freshest price from '{newest_candidate.source}' is {age:.1f}s old "
                f"(limit={self.settings.max_staleness_seconds}s)."
            )

        raise AllSourcesDownError("REFUSED: All price sources failed and no cached tick is available.")

    def health_report(self, now_utc: Optional[datetime] = None) -> Dict[str, object]:
        ref_now = now_utc or datetime.now(timezone.utc)
        last_age = (
            self._last_good_tick.age_seconds(ref_now)
            if self._last_good_tick
            else None
        )
        return {
            "status": (
                "ok"
                if (last_age is not None and last_age <= self.settings.max_staleness_seconds)
                else "stale_or_down"
            ),
            "last_good_tick": self._last_good_tick,
            "last_tick_age_seconds": last_age,
            "sources": {
                name: {
                    "is_healthy": h.is_healthy,
                    "consecutive_failures": h.consecutive_failures,
                    "latency_ms": round(h.latency_ms, 2),
                    "last_error": h.last_error,
                }
                for name, h in self.health.items()
            },
        }
