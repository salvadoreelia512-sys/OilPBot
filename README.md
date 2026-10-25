# Oil, simplified

A minimal WTI oil target-probability app. Enter a **target price**, choose **long or
short**, and select **leverage**. The app returns the estimated probability of touching
the target in **1 hour, 3 hours, 5 hours, 1 calendar day, 1 calendar week and 1 calendar month**, a
probability chart, and a projected median / 10–90% price range.

Leverage affects the implied return at the target, not the market's touch probability.
There are no user inputs for OVX, required return, or data mode.

## Data and model

- The main app uses the exact **TVC:USOIL** instrument's public TradingView snapshot:
  https://www.tradingview.com/symbols/TVC-USOIL/ . This is not Nasdaq's oil futures
  listing or a Nasdaq-listed security. The interface credits the actual source and
  shows its timestamp. It never substitutes USO ETF or futures prices silently.
- Price and timestamp are extracted together from the same daily bar in the page's
  structured data. This public-page format is not a guaranteed API; failures or
  changed formats cause an explicit unavailable state, not invented prices.
- Quotes are refreshed at most once per minute per process; errors trigger exponential
  backoff capped at five minutes. Concurrent requests share the cache.
- During scheduled trading, quotes older than 15 minutes are refused automatically.
  During closures, the latest close is accepted only if no more than 15 minutes of
  scheduled trading have elapsed since its timestamp (and it is under 96 hours old).
  The UI explicitly labels it **Market closed · last session quote**.
- The model uses shared jump-diffusion Monte Carlo paths, a momentum drift gated by
  sampling uncertainty, and an automatic volatility blend. Its cold-start baseline is
  the single-session Parkinson high/low estimate, bounded to 10–150% annualized.
  This is preliminary and uncalibrated; percentages are model estimates, not measured
  accuracy. A price fan shows model uncertainty, not the actual future path.
- Horizons start at the request time. The regular WTI schedule excludes weekends and
  the daily 17:00–18:00 New York break, with DST handled. Holidays, exceptional closures,
  jumps, fees and financing are not modeled. Closed horizons have no modeled movement.
- For delayed quotes, uncertainty from the quote time to now is propagated into the
  current-price distribution; future touch probabilities are integrated over that
  distribution. Past touches in the unobserved interval are not counted as future events.
- A long target must be above the quoted price, and a short target below it.

## Run locally

Python 3.12 is tested; Python 3.10+ is supported.

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
```

Open the host running the app on port 8000. The same FastAPI service serves the HTML,
JavaScript, charts and API. No API key or environment-variable setup is needed for the
main TVC app. SQLite needs a writable path; `SQLITE_DB_PATH` optionally changes it.
The frontend has no external JavaScript, CSS, chart or font dependencies.

## Deploy on Render

Create a **Web Service** from this repository, branch `main`, runtime **Docker**.
Leave **Root Directory** blank and use `./Dockerfile`. The image listens on port 8000
and writes SQLite to `/data/oil_bot.db`. Use a persistent writable volume at `/data`
if retaining legacy bar history matters. No environment variables are required for the
main app. Free instances may sleep; the browser allows up to 70 seconds for wake-up.

Use the Render `.onrender.com` URL directly. Netlify is optional and not needed.
If retaining Netlify, deploy the Python backend separately and set `OILPBOT_API_URL`
in Netlify to the backend HTTPS origin. The Node function forwards `/api/market` and
`/api/forecast` as well as legacy API routes.

## Main API

- `GET /api/market`: timestamped TVC:USOIL quote, automatic freshness/closure status.
- `POST /api/forecast`: `{ "target_price": 105, "direction": "long", "leverage": 5 }`.
  Returns six horizon estimates, projection points, model assumptions, underlying
  move and implied leveraged return. Invalid inputs return 422, unavailable data 503.
- `GET /openapi.json`: complete API schema.

The older `/api/predict`, `/api/quote`, `/api/health` and `/ws` endpoints remain for
compatibility and existing model/calibration tests. They use the original configurable
provider chain and are **not the data source for the new interface**. Their health
status does not establish the TVC source's readiness; use `/api/market` instead.

## Validation

```bash
python -m pytest -v
node --test tests/web.test.mjs
```

Tests cover source identity/parsing, caching and recovery, stale-data guards, regular
market hours and DST, monotone horizon probabilities, leverage independence, input
validation, UI state and charts, proxy routes, and legacy simulation/calibration logic.
A controlled-feed test does not establish live-source access or probability calibration.

## Evidence-aware Monte Carlo update

The main forecast now runs 12,000 shared jump-diffusion paths through one month,
with common random numbers across requests, Brownian-bridge barrier corrections
(approximate when jumps occur), and 10th/50th/90th percentile scenarios. Higher
cumulative touch probabilities at longer horizons are expected, not instability.
The reported sampling margin describes Monte Carlo noise, not forecast accuracy.

BBC World and EIA Today in Energy RSS are fetched without keys, cached for ten
minutes, filtered for oil-relevant dated articles, clustered, scored and decayed.
News raises volatility/jump uncertainty; headlines do not automatically imply a
price direction. Feed availability and actual headlines are visible under Evidence.
This limited rule-based coverage is not comprehensive geopolitical intelligence.
There are no new paid services, API keys, AI providers, or AIS subscriptions.

Distinct TVC quote observations accumulate in SQLite when the source refreshes.
After at least 20 return intervals spanning four trading hours, empirical volatility
is blended with session-range volatility. Shrunk momentum is used only when its
approximate sampling interval excludes zero. Sparse observations are not minute bars;
weekend gaps are not treated as continuous trading. Render free-instance restarts
may lose this history without a persistent volume, and an inactive service does not
collect uninterrupted data.

The experimental outlook can suggest long/short, a one-week reference target, or
WAIT. It waits for insufficient history, incomplete feed coverage, closed markets,
or inconclusive momentum. Leverage is a 1× reference, not personalized sizing.
No calibrated hit rate, profitable edge, safe leverage, or precise geopolitical
price effect is claimed. The news adjustment has not passed an out-of-sample gate;
existing calibration harnesses remain available for future validation. Live inventory
surprises, USD/rates, positioning, term structure, AIS and holiday schedules remain
unmodeled. A percentile line is not a worst/best possible bound or a coherent path.

## Persistent Neon PostgreSQL

Set `DATABASE_URL` securely in Render to the Neon **pooled** PostgreSQL connection
string, keeping the supplied SSL settings. Do not commit the value. No extra app
hosting service is needed. The application automatically creates its own tables:

- `tvc_observations`: distinct timestamped TVC quote observations.
- `oil_news`: dated headlines associated with served forecasts, deduplicated.
- `oil_forecasts`: model version, quote, target, direction, compact model/evidence record.
- `oil_outcomes`: six horizon probabilities, due dates, observation-based outcomes.

With DATABASE_URL present there is no silent SQLite fallback. Without it, local
SQLite remains available for development. Legacy model/bar APIs still use SQLite;
the main TVC history, forecast, news archive and statistics use the selected backend.
Existing observations already in the chosen SQLite database are retained locally;
they are not automatically copied to Neon. Data lost by earlier Render redeployments
cannot be recovered by adding Neon.

`GET /api/statistics` verifies the selected backend and reports record counts and
matured outcome statuses. It never displays the connection string or credentials.
Statistics evaluations are batched (up to 100 due outcomes per request). A recorded
target crossing counts as an observed touch. A sampled miss requires no scheduled
trading gaps exceeding five minutes; larger gaps are marked insufficient coverage.
Even dense point samples may miss brief intraminute touches. The observation-based
Brier diagnostics are therefore not validated continuous-touch calibration, and
correlated overlapping forecasts must not be interpreted as independent experiments.

Neon makes new stored records survive Render redeployments. It does not stop Render
free instances sleeping, backfill historical data, or provide uninterrupted collection.
Records are retained without automatic age deletion; monitor Neon free-tier storage
and export/back up or archive records before reaching its limits. Never share a
PostgreSQL URL in chat, logs, screenshots or source control.
