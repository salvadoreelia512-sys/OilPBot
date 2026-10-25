import os
from datetime import datetime, timezone
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config import BotSettings
from src.prices import AlpacaAdapter, BarRepository, FinnhubAdapter, PriceService, StaleDataError, TwelveDataAdapter, YahooFinanceAdapter
from src.volatility import VolatilityEngine
from src.simulation import SimulationEngine, compute_return_goal_price
from src.news import GDELTPoller, RSSPoller, NewsRiskAdjuster, RawHeadline
from src.ais import HormuzMonitor, BoundingBox

app = FastAPI(
    title="Oil Probability Bot",
    description="Intraday Oil Price First-Passage Probability & Risk Engine",
    version="0.6.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

settings = BotSettings()

# Initialize price services with fallback chain
adapters = [
    FinnhubAdapter(api_key=settings.finnhub_api_key),
    TwelveDataAdapter(api_key=settings.twelvedata_api_key),
    YahooFinanceAdapter(),
    AlpacaAdapter(api_key=settings.alpaca_api_key, secret_key=settings.alpaca_secret_key),
]
bar_repo = BarRepository(settings.sqlite_db_path)
price_service = PriceService(adapters=adapters, settings=settings, bar_repo=bar_repo)
vol_engine = VolatilityEngine(settings)
sim_engine = SimulationEngine(n_paths=settings.mc_paths, cross_check_tolerance=settings.cross_check_tolerance)

# News and AIS risk layers
news_adjuster = NewsRiskAdjuster(settings=settings)
hormuz_monitor = HormuzMonitor(settings=settings)


class PredictionRequest(BaseModel):
    target_price: float = Field(..., gt=0.0)
    direction: str = Field(..., pattern="^(long|short)$")
    leverage: float = Field(5.0, gt=0.0)
    required_return: float = Field(0.20, ge=0.0)
    ovx_index: Optional[float] = Field(32.0, ge=0.0)


@app.get("/health")
def health_check():
    try:
        report = price_service.health_report()
        return {
            "status": report["status"],
            "server_time_utc": datetime.now(timezone.utc).isoformat(),
            "sources": report["sources"],
            "last_tick_age_seconds": report["last_tick_age_seconds"],
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc), "server_time_utc": datetime.now(timezone.utc).isoformat()}


@app.post("/api/predict")
def calculate_probability(req: PredictionRequest):
    now_utc = datetime.now(timezone.utc)
    try:
        tick = price_service.get_fresh_price(now_utc=now_utc)
    except StaleDataError as stale_exc:
        raise HTTPException(status_code=503, detail=str(stale_exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Price feed failure: {exc}")

    closes = bar_repo.get_recent_closes(tick.symbol, limit=390)
    if closes.empty or len(closes) < 5:
        # Fallback synthetic series if bar repository is warm-up sparse
        closes = pd.Series([tick.price] * 60, index=pd.date_range(now_utc - timedelta(hours=1), periods=60, freq="1min", tz="UTC"))

    # Compute news and AIS risk adjustments
    news_adj = news_adjuster.compute_adjustment(now_utc=now_utc)
    ais_status = hormuz_monitor.evaluate_risk(now_utc=now_utc)

    combined_vol_mult = news_adj.vol_multiplier * ais_status.vol_multiplier_add
    combined_jump = news_adj.jump_intensity_annual + ais_status.jump_intensity_add

    vol_est = vol_engine.estimate_volatility(
        closes_1m=closes,
        ovx_index=req.ovx_index,
        now_utc=now_utc,
        risk_vol_multiplier=combined_vol_mult,
    )

    t_years, rem_minutes = vol_engine.remaining_session_years(now_utc=now_utc)

    sim_res = sim_engine.run(
        spot_price=tick.price,
        target_price=req.target_price,
        direction=req.direction,
        leverage=req.leverage,
        required_return=req.required_return,
        sigma_annual=vol_est.blended_annual_vol,
        t_years=t_years,
        n_steps=min(rem_minutes, 180),
        jump_intensity_annual=combined_jump,
    )

    # Compile risk drivers for UI "Why" panel
    drivers = []
    for d in news_adj.drivers:
        drivers.append({
            "type": "news",
            "title": d.title,
            "event_type": d.event_type,
            "outlet_count": d.outlet_count,
            "age_minutes": d.age_minutes,
            "effective_impact": d.effective_impact,
            "summary": f"[{d.event_type.upper()}] {d.title} ({d.outlet_count} outlets, {d.age_minutes}m ago)",
        })
    drivers.append({
        "type": "ais",
        "title": ais_status.driver_summary,
        "event_type": "hormuz_ais",
        "outlet_count": ais_status.transits_last_hour,
        "age_minutes": 0.0,
        "effective_impact": ais_status.anomaly_score,
        "summary": ais_status.driver_summary,
    })

    return {
        "spot": {
            "symbol": tick.symbol,
            "price": tick.price,
            "source": tick.source,
            "timestamp_utc": tick.timestamp_utc.isoformat(),
            "age_seconds": round(tick.age_seconds(now_utc), 2),
        },
        "volatility": {
            "ewma_annual": vol_est.ewma_annual,
            "garch_annual": vol_est.garch_annual,
            "ovx_annual": vol_est.ovx_annual,
            "blended_annual_vol": vol_est.blended_annual_vol,
            "seasonality_multiplier": vol_est.seasonality_multiplier,
            "risk_multiplier": vol_est.risk_multiplier,
        },
        "simulation": {
            "spot_price": sim_res.spot_price,
            "target_price": sim_res.target_price,
            "direction": sim_res.direction,
            "leverage": sim_res.leverage,
            "required_return": sim_res.required_return,
            "goal_price_for_return": sim_res.goal_price_for_return,
            "prob_touch_target": sim_res.prob_touch_target,
            "prob_touch_return_goal": sim_res.prob_touch_return_goal,
            "closed_form_prob_target": sim_res.closed_form_prob_target,
            "cross_check_alert": sim_res.cross_check_alert,
            "high_bands": asdict_bands(sim_res.high_bands),
            "low_bands": asdict_bands(sim_res.low_bands),
            "close_bands": asdict_bands(sim_res.close_bands),
            "runtime_ms": sim_res.runtime_ms,
        },
        "risk_drivers": drivers,
    }


def asdict_bands(b):
    return {"p05": b.p05, "p25": b.p25, "p50": b.p50, "p75": b.p75, "p95": b.p95}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            now_utc = datetime.now(timezone.utc)
            try:
                tick = price_service.get_fresh_price(now_utc=now_utc)
                health = price_service.health_report(now_utc=now_utc)
                await websocket.send_json({
                    "type": "tick",
                    "symbol": tick.symbol,
                    "price": tick.price,
                    "source": tick.source,
                    "timestamp_utc": tick.timestamp_utc.isoformat(),
                    "age_seconds": round(tick.age_seconds(now_utc), 2),
                    "health": health["status"],
                })
            except Exception as exc:
                await websocket.send_json({
                    "type": "error",
                    "detail": str(exc),
                    "timestamp_utc": now_utc.isoformat(),
                })
            import asyncio
            await asyncio.sleep(2.0)
    except WebSocketDisconnect:
        pass


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    html_path = os.path.join("templates", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Oil Probability Bot API Running</h1><p>Visit <a href='/docs'>/docs</a> for Swagger UI.</p>"
