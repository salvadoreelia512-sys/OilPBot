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

# Run the application (GitHub Pages alone cannot run the Python backend)
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
# Run tests separately
pytest -v
```

## Run the application

Use Python 3.10 or newer (Python 3.12 is tested):

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env  # Only for a new checkout; preserve an existing .env.
.venv/bin/python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
```

Open the host running this service. FastAPI serves both the UI and JSON API; publishing
`templates/index.html` alone does not run the prediction engine. SQLite needs a writable
path (`SQLITE_DB_PATH`); mount persistent storage when deploying the backend.
A Dockerfile is provided for Python/container hosts, listening on port 8000. The image
uses `/data/oil_bot.db`; mount a writable volume at `/data` for persistence. The Docker
image was built and validated with local HTTP checks, a controlled-feed prediction,
and non-root SQLite writes. This does not verify a production deployment.

### Netlify frontend

Netlify hosts the static frontend and the Node proxy in `netlify/functions/api.mjs`.
Deploy the Python service above separately, then set **OILPBOT_API_URL** in Netlify to
its HTTPS origin, for example `https://your-api.example.com` (no path or credentials).
Redeploy the Netlify site after setting this variable. It must point to the backend,
not back to the Netlify frontend. Netlify's proxy preserves JSON error responses and
returns a readable configuration error if the backend is missing. Python files in
Netlify's functions directory are not a supported Python server deployment.

The frontend uses `/api/predict` and polls `/api/quote`, so it works with both FastAPI
and Netlify without requiring WebSocket support. The backend's `/ws` remains available
to direct ASGI clients. A static-only preview needs an API reverse proxy; otherwise,
run the full FastAPI app on a host that supports Python.

### Prices and honest status

`MARKET_SYMBOL=CL=F` (default) uses Yahoo oil futures. `MARKET_SYMBOL=USO` selects
USO ETF shares and enables same-symbol Finnhub, Twelve Data, Yahoo, and Alpaca failover.
`MARKET_SYMBOL=WTI/USD` selects Twelve Data spot oil. These instruments have different
prices; choose a target appropriate to the selected instrument. The API never silently
substitutes USO shares for an oil futures contract. Configure provider keys securely in
host environment settings or an untracked `.env` file. Outbound access to the selected
provider must also be allowed. Yahoo typically needs `query1.finance.yahoo.com`,
`query2.finance.yahoo.com`, `fc.yahoo.com`, and `guce.yahoo.com`. Other supported price
API destinations are `finnhub.io`, `api.twelvedata.com`, and `data.alpaca.markets`.

The app starts disconnected, displays no invented price, and ages the last verified tick
locally. Closed markets, delayed quotes, missing keys, and provider outages can all prevent
a fresh quote. Prices older than 60 seconds are refused; the UI allows retries after a
failure. No sample data is substituted for a live price. Sparse history is explicitly
marked as warmup: the volatility engine uses a flat-price series with an OVX anchor.
Predictions after today's 23:30 Dubai cutoff return a session-closed response rather than
silently forecasting tomorrow. Predictions resume after midnight Dubai time; quote
freshness still determines whether a prediction is allowed.

News and AIS parsers, monitors, and calibration harnesses are implemented, but the web
server does not automatically start live news/AIS ingestion. Do not interpret an empty
risk panel as proof that external news or AIS feeds are running.

### Validation

```bash
python -m pytest -v
node --test tests/web.test.mjs
```

The Python suite covers the models, calibration, news/AIS logic, and API predictions
with controlled feeds, including empty history, invalid inputs, stale/down feed recovery,
instrument consistency, and session cutoff. Node's built-in test runner (Node 20+)
checks frontend state/error handling and the Netlify proxy. Controlled-feed tests are
not evidence of live market access or a successful production deployment.
