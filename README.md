# Oil Probability Bot

Intraday oil probability engine estimating the first-passage (barrier touch) probability of hitting a target price before the session cutoff, complete with multi-source price failover, staleness protection, EWMA/GARCH(1,1)/OVX blended volatility, a Merton Jump-Diffusion Monte Carlo simulator cross-checked against closed-form reflection formulas, a walk-forward calibration harness, and a news deduplication/scoring/decay risk layer.

## 1. What the Product Does
- **Inputs:** Target price, direction (`long` or `short`), leverage, and required return (e.g., `20%` return at `5x` leverage defines a `4%` underlying move).
- **Outputs:**
  - Probability of touching the target today before session cutoff
  - Percentile price bands (`p05`, `p25`, `p50`, `p75`, `p95`) for the day's High, Low, and Close
  - Nearest price level that meets your leveraged return goal + its touch probability
  - Data-freshness indicator and source health report
  - "Why" panel showing which news/AIS events are driving risk (Phases 4–5)

## 2. Architecture

```text
 DATA SOURCES                  INGESTION                    MODELS
┌───────────────┐        ┌────────────────┐        ┌──────────────────────┐
│ Prices (3+    │───────▶│ Price service  │───────▶│ Volatility engine    │
│ sources)      │        │ failover +     │        │ EWMA/GARCH + OVX     │
├───────────────┤        │ staleness check│        │ anchor               │
│ News: GDELT,  │        ├────────────────┤        ├──────────────────────┤
│ RSS, Finnhub  │───────▶│ News service   │───────▶│ Event scorer         │
├───────────────┤        │ dedupe+cluster │        │ (LLM / FinBERT)      │
│ AIS: Hormuz   │        ├────────────────┤        ├──────────────────────┤
│ tanker transits│──────▶│ AIS service    │───────▶│ Risk adjuster        │
├───────────────┤        │ transit counts │        │ vol multiplier +     │
│ OVX / VIX     │───────▶│ anomaly flags  │        │ jump intensity+decay │
└───────────────┘        └────────────────┘        └──────────┬───────────┘
                                                              ▼
                  ┌─────────────────┐            ┌──────────────────────┐
                  │ SQLite/Postgres │◀───────────│ Simulation engine    │
                  │ prediction log  │            │ jump-diffusion Monte │
                  │ + calibration   │            │ Carlo + closed-form  │
                  └────────┬────────┘            │ cross-check          │
                           │                     └──────────┬───────────┘
                           ▼                                ▼
                  ┌─────────────────┐            ┌──────────────────────┐
                  │ Calibration &   │            │ FastAPI + WebSocket  │
                  │ backtest jobs   │            └──────────┬───────────┘
                  └─────────────────┘                       ▼
                                                 ┌──────────────────────┐
                                                 │ Minimal single-page  │
                                                 │ UI                   │
                                                 └──────────────────────┘
```

## 3. Build Checklist

### Phase 0: Setup
- [ ] Choose paper accounts: Alpaca (Nasdaq, USO) and optionally Interactive Brokers (CL futures)
- [ ] Get free API keys: Finnhub, Twelve Data, AISstream, Alpaca
- [x] Repo, environment, config file, secrets in `.env.example`
- [x] Define the trading session window for "today" (Dubai-time cutoff)

### Phase 1: Prices and Reliability
- [x] Price adapters for each source (Finnhub, Twelve Data, Yahoo Finance, Alpaca)
- [x] Failover logic and staleness thresholds (`StaleDataError` hard refusal)
- [x] Health endpoint and logging (`PriceService.health_report()`)
- [x] Store 1-minute bars in SQLite for volatility estimation (`BarRepository`)

### Phase 2: Core Math
- [x] EWMA and GARCH(1,1) volatility + OVX anchor + intraday seasonality
- [x] Monte Carlo first-passage simulation (50,000 antithetic paths, Merton jump-diffusion)
- [x] Closed-form cross-check and unit tests (Broadie-Glasserman-Kou continuity correction)
- [x] Leverage-to-target conversion (`compute_return_goal_price`)
- [x] Percentile bands (5th, 25th, 50th, 75th, 95th for High, Low, Close)

### Phase 3: Backtest the Baseline
- [x] Historical oil data and a walk-forward test harness (`HistoricalDataLoader`, `WalkForwardBacktester`)
- [x] Measure calibration of the baseline: Brier score, reliability curve, 10–20% bucket accuracy, and 90% band coverage (`src/calibration.py`)
- [x] Set the benchmark every later feature must beat (`CalibrationSummary.beats_baseline()` + SQLite benchmark store)

### Phase 4: News Layer
- [x] GDELT (15m), RSS (30–60s), and Finnhub rate-limited pollers (`GDELTPoller`, `RSSPoller`, `FinnhubNewsPoller`)
- [x] Dedupe and event clustering so syndicated stories count as one event (`HeadlineClusterer`)
- [x] Event scorer (`event_type`, `severity`, `surprise`, `credibility`, fear velocity/alarm proxies) and half-life decay rules (`6h` Hormuz, `2h` OPEC, etc.)
- [x] Risk adjuster feeding `vol_multiplier`, `jump_intensity`, and the UI "Why" risk-driver panel (`NewsRiskAdjuster`)
- [x] Out-of-sample A/B calibration gate (`evaluate_news_layer_ab`) that automatically disables the news layer if it fails to beat the baseline

### Phase 5: Hormuz and AIS
- [x] AIS stream consumer and Strait of Hormuz bounding box (`AISStreamConsumer`, `BoundingBox`)
- [x] Transit-count rolling baseline and anomaly detection for sudden drops, loitering clusters, and route changes (`HormuzMonitor`)
- [x] Non-directional jump-intensity hook (`AISRiskStatus`, `HormuzMonitor.as_backtest_hook()`)
- [x] Out-of-sample A/B test against baseline (`evaluate_ais_layer_ab`)

### Phase 6: Interface
- [x] FastAPI REST and WebSocket endpoints (`src/main.py`)
- [x] Single-page UI with live freshness indicator, target inputs, probability readouts, percentile bands, and collapsible "Why" risk drivers (`templates/index.html`)
- [x] Hard error states (stale data refusal banner, source down alerts)

### Phase 7: Paper Trading and Validation
- [ ] Order execution on paper (limit orders only)
- [ ] Hard risk limits: max loss per trade, daily loss limit, kill switch
- [ ] Run live paper for 4 to 8 weeks with prediction log on

### Phase 8: Hardening
- [ ] Process supervision and auto-restart
- [ ] Alerts (Telegram or email) for outages and calibration drift
- [ ] Rate-limit handling and retry with backoff
- [ ] Deploy to a small cloud server (24/7)

## 4. Quickstart

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

# Run FastAPI server locally or deploy to Netlify / GitHub
pytest -v
```
