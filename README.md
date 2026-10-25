# Oil, simplified

A minimal WTI oil target-probability app. Enter a **target price**, choose **long or
short**, and select **leverage**. The app returns the estimated probability of touching
the target in **1 hour, 3 hours, 5 hours, 1 calendar day and 1 calendar week**, a
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
- The model uses geometric Brownian motion with zero arithmetic drift and an automatic
  single-session Parkinson high/low volatility estimate, bounded to 10–150% annualized.
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
  Returns five horizon estimates, projection points, model assumptions, underlying
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
