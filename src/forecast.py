"""Exact TVC:USOIL snapshots and transparent multi-horizon GBM estimates."""
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np
from fastapi import APIRouter, HTTPException, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from scipy.special import ndtri

from src.simulation import closed_form_barrier_touch_prob

SOURCE_URL = "https://www.tradingview.com/symbols/TVC-USOIL/"
MAX_QUOTE_AGE_SECONDS = 120
NY = ZoneInfo("America/New_York")
HORIZONS = [("1 hour", 1), ("3 hours", 3), ("5 hours", 5), ("1 day", 24), ("1 week", 168)]


def market_open(at):
    local = at.astimezone(NY)
    day, hour = local.weekday(), local.hour
    return (day == 6 and hour >= 18) or (day < 4 and hour != 17) or (day == 4 and hour < 17)


def trading_hours(start, end):
    """Regular WTI weekly schedule, UTC stepping preserves DST. Holidays not modeled."""
    seconds = 0.0
    while start < end:
        boundary = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        stop = min(boundary, end)
        if market_open(start):
            seconds += (stop - start).total_seconds()
        start = stop
    return seconds / 3600


@dataclass(frozen=True)
class OilSnapshot:
    price: float
    timestamp: datetime
    high: float
    low: float


class PageData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.inside = dict(attrs).get("type") == "application/prs.init-data+json"
            self.parts = []

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.inside:
            self.scripts.append("".join(self.parts))
            self.inside = False


def parse_snapshot(html):
    parser = PageData()
    parser.feed(html)

    def find(obj):
        if isinstance(obj, dict):
            if obj.get("pro_symbol") == "TVC:USOIL" and "daily_bar" in obj:
                bar = obj["daily_bar"]
                # Price and timestamp come from the same bar, never unrelated page quotes.
                result = OilSnapshot(float(bar["close"]), datetime.fromtimestamp(float(bar["data_update_time"]), timezone.utc), float(bar["high"]), float(bar["low"]))
                if not all(math.isfinite(v) and v > 0 for v in [result.price, result.high, result.low]) or not result.low <= result.price <= result.high:
                    raise ValueError("Source returned an invalid oil bar")
                return result
            for value in obj.values():
                match = find(value)
                if match:
                    return match
        elif isinstance(obj, list):
            for value in obj:
                match = find(value)
                if match:
                    return match
        return None

    for script in parser.scripts:
        try:
            result = find(json.loads(script))
        except (json.JSONDecodeError, KeyError, TypeError, OverflowError):
            continue
        if result:
            return result
    raise ValueError("TVC:USOIL quote and timestamp were not available from the source")


class OilFeed:
    def __init__(self, source=None):
        from src.live_quotes import stream
        self.stream = source if source is not None else stream

    @property
    def last_error(self):
        return self.stream.last_error

    def get(self):
        try:
            quote = self.stream.get()
        except RuntimeError:
            raise HTTPException(status_code=503, detail="Live TVC:USOIL quotes unavailable; reconnecting automatically. Retry shortly.") from None
        snapshot = OilSnapshot(quote.price, quote.timestamp, quote.high, quote.low)
        from src.intelligence import history
        history(snapshot)
        return snapshot


feed = OilFeed()
router = APIRouter()


class ForecastRequest(BaseModel):
    model_config = {"allow_inf_nan": False, "extra": "forbid"}
    target_price: float = Field(gt=0, le=1_000_000)
    direction: Literal["long", "short"]
    leverage: float = Field(default=5, ge=1, le=100)


def quote_payload(snapshot, now):
    age = max(0, (now - snapshot.timestamp).total_seconds())
    active_age = trading_hours(snapshot.timestamp, now)
    usable = age <= MAX_QUOTE_AGE_SECONDS or (age <= 96 * 3600 and active_age <= .25 and not market_open(now))
    return {
        "symbol": "TVC:USOIL", "name": "WTI crude oil", "price": snapshot.price,
        "source": "TradingView widget quote stream", "source_url": SOURCE_URL,
        "timestamp_utc": snapshot.timestamp.isoformat(), "age_seconds": round(age),
        "market_open": market_open(now), "usable": usable,
        "status": ("market_closed" if not market_open(now) else "fresh" if age <= 60 else "delayed") if usable else "unavailable",
        "checked_at_utc": now.isoformat(),
    }


def estimate(snapshot, request, now):
    quote = quote_payload(snapshot, now)
    if not quote["usable"]:
        raise HTTPException(status_code=503, detail="The oil quote is too old for a new estimate. Waiting for a current source update.")
    spot, target = snapshot.price, request.target_price
    if (request.direction == "long" and target <= spot) or (request.direction == "short" and target >= spot):
        raise HTTPException(status_code=422, detail="Choose a target above the quote for long, or below it for short.")
    # Parkinson range estimator. One session is preliminary; no user OVX input or invented history.
    annual_vol = max(.10, min(1.50, math.log(snapshot.high / snapshot.low) * math.sqrt(252 / (4 * math.log(2)))))
    rows = []
    delay_hours = trading_hours(snapshot.timestamp, now)
    delay_years = delay_hours / (252 * 23)
    nodes, weights = np.polynomial.hermite.hermgauss(32)
    current_prices = spot * np.exp(-.5 * annual_vol ** 2 * delay_years + annual_vol * math.sqrt(2 * delay_years) * nodes)

    def touch_probability(future_years):
        values = []
        for price in current_prices:
            reached = price >= target if request.direction == "long" else price <= target
            values.append(1.0 if reached else closed_form_barrier_touch_prob(float(price), target, annual_vol, future_years))
        return float(np.dot(weights, values) / math.sqrt(math.pi))
    for label, hours in HORIZONS:
        end = now + timedelta(hours=hours)
        active_hours = trading_hours(now, end)
        years = active_hours / (252 * 23)
        probability = touch_probability(years)
        rows.append({"label": label, "hours": hours, "trading_hours": round(active_hours, 3), "probability": probability, "ends_at_utc": end.isoformat()})
    # A common clock yields nested touch events and monotone horizon probabilities.
    points = []
    for hours in np.linspace(0, 168, 169):
        future_hours = trading_hours(now, now + timedelta(hours=float(hours)))
        years = (future_hours + delay_hours) / (252 * 23)
        sigma_t = annual_vol * math.sqrt(years)
        median = spot * math.exp(-.5 * annual_vol ** 2 * years)
        points.append({"hours": float(hours), "median": median,
                       "low": median * math.exp(float(ndtri(.10)) * sigma_t),
                       "high": median * math.exp(float(ndtri(.90)) * sigma_t)})
    move = (target / spot - 1) * 100
    return {"quote": quote, "target_price": target, "direction": request.direction,
            "leverage": request.leverage, "price_change_percent": move,
            "leveraged_return_percent": abs(move) * request.leverage,
            "horizons": rows, "projection": points,
            "model": {"annual_volatility": annual_vol, "basis": "Single-session high/low range; preliminary, uncalibrated GBM estimate",
                      "interval": "10th–90th percentile model range, not a guaranteed path",
                      "assumptions": "Zero arithmetic drift; regular WTI weekly hours; holidays, gaps, fees and financing excluded. Delayed quotes propagate uncertainty to now before estimating future touches.",
                      "as_of_utc": now.isoformat(),
                      "delay_notice": "Estimates use the last quote, not an observed current price; delayed-period uncertainty is included." if delay_hours else None}}


@router.get("/api/market")
def market(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"quote": quote_payload(feed.get(), datetime.now(timezone.utc)),
            "source_warning": feed.last_error}


@router.websocket("/api/live")
async def live_market(socket: WebSocket):
    import asyncio
    await socket.accept()
    feed.stream.start()
    previous = None
    try:
        while True:
            current = feed.stream.latest
            if current is not None and current != previous:
                snapshot = OilSnapshot(current.price, current.timestamp, current.high, current.low)
                await socket.send_json({"quote": quote_payload(snapshot, datetime.now(timezone.utc)),
                                        "source_warning": feed.last_error})
                previous = current
            # Yield while waiting; one upstream connection serves every visitor.
            try:
                message = await asyncio.wait_for(socket.receive(), timeout=.25)
                if message["type"] == "websocket.disconnect":
                    break
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass


@router.get("/api/strategy")
def strategy_report(response: Response):
    from src.strategic import context
    response.headers["Cache-Control"] = "no-store"
    return context(datetime.now(timezone.utc))


@router.get("/api/news")
def news(response: Response):
    from src.intelligence import news_context
    response.headers["Cache-Control"] = "no-store"
    return news_context(datetime.now(timezone.utc))


@router.post("/api/forecast")
def forecast(request: ForecastRequest):
    snapshot = feed.get()
    from src.intelligence import enhance
    now = datetime.now(timezone.utc)
    from src.historical import sync_archive
    sync_archive()
    result = enhance(estimate(snapshot, request, now), snapshot, request, now)
    from src.storage import save_forecast
    result['forecast_id'] = save_forecast(result, now)
    return result


@router.get("/api/statistics")
def statistics_endpoint():
    from src.storage import statistics
    try:
        return statistics()
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable. Check DATABASE_URL and database connectivity in Render; credentials are never displayed.")


@router.get("/api/historical")
def historical_endpoint():
    from src.historical import sync_archive, summary
    return summary(sync_archive())
