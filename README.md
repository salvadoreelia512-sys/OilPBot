# Oil, simplified

A minimal WTI oil target-probability app. Enter a **target price**, choose **long or
short**, and select **leverage**. The app returns the estimated probability of touching
the target in **1 hour, 3 hours, 5 hours, 1 calendar day, 1 calendar week and 1 calendar month**, a
probability chart, and a projected median / 10–90% price range.

Leverage affects the implied return at the target, not the market's touch probability.
There are no user inputs for OVX, required return, or data mode.

## Data and model

- The app displays the exact **TVC:USOIL** price received by its backend. It no
  longer embeds a separate widget that can be blank or inaccessible to forecast code.
- The backend connects to TradingView's public widget quote WebSocket for the exact
  **TVC:USOIL** instrument. It uses the source's last price, quote timestamp and
  session high/low; the stale public-page scrape is no longer used in production.
  One shared connection per process receives updates and reconnects with bounded
  exponential backoff (1–30 seconds). No login, API key or paid service is required.
  This public widget protocol is unofficial and may change or become unavailable;
  it is not a supported backend data-service contract. The app never replaces it
  silently with another instrument or invents a current timestamp.
- Both forecast calculations and `/api/market` use that shared stream. The browser
  receives updates through `/api/live` WebSocket, with one-second HTTP polling as
  fallback (including hosts that cannot proxy WebSockets). A separate TradingView website or widget connection can differ briefly due to
  transport latency. No zero-delay
  guarantee is possible. Forecasts run only when Calculate probability is pressed. Quotes, news, elapsed
  time and edited inputs do not recalculate or erase the last successful snapshot.
  Its original target, reference price, calculation time and horizon end dates remain
  visible. Input changes are marked until the user explicitly calculates again.
  No cosmetic smoothing hides changes in a newly requested forecast.
- During scheduled trading, quotes older than two minutes are refused automatically.
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
The frontend has no external JavaScript, CSS, chart, font or widget dependencies.
The backend subscribes to the public widget quote service; the frontend receives
that same data through its own server. The exact forecast reference price and source
timestamp are shown alongside the model, and stale quotes are rejected.

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

- `GET /api/news`: independently refreshed, dated oil news and per-source availability.
  News remains visible even when quotes or forecasts are unavailable.
- `GET /api/market`: timestamped TVC:USOIL streaming quote, automatic freshness/closure status.
- `WS /api/live`: pushes timestamped quote updates to browsers without creating a
  separate upstream connection per visitor. HTTP polling remains the fallback.
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

BBC World, EIA Today in Energy and OilPrice RSS are fetched without keys, cached for one
minute, filtered for oil-relevant dated articles, clustered, scored and decayed.
Failed feeds retry after 15 seconds on the next request. The browser requests news
every minute, independently of forecasting. Only articles published in the last 24 hours are returned or displayed. Old, future
or undated articles are excluded, including cached articles after a failed refresh.
The browser removes expired articles even between successful refreshes. Real publisher
headlines are deduplicated and ranked by potential oil relevance plus recency; source,
publication time and potential relevance are shown. Ranking is a transparent heuristic,
not a measured price-impact forecast, and no fictional headlines are generated. No matching recent article
is distinct from an unreachable feed.
News raises volatility/jump uncertainty; headlines do not automatically imply a
price direction. Feed availability and actual headlines are visible under Evidence.
This limited rule-based coverage is not comprehensive geopolitical intelligence.
There are no new paid services, API keys, AI providers, or AIS subscriptions.

Distinct TVC quote observations accumulate in the selected database when market
requests are served. The model reads the latest quote per minute over up to 1,000
minutes, so dense live ticks do not shrink its four-hour volatility warmup window.
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

## Historical context and forecast stability

The versioned `src/data/history.json` archive covers December 2024 onward: 470 EIA
WTI spot observations, 475 Yahoo S&P 500 daily observations, and 688 daily geopolitical
risk index observations from Caldara and Iacoviello's published GPR dataset. Exact
coverage, sources, SHA-256 source fingerprints and validation are available at
`/api/historical`. This endpoint and forecasts synchronize all 1,633 observations to
Neon once per archive version. It is not exact TVC history or an archive of all news.
Archive endpoints vary: WTI through 2026-10-20, stocks through 2026-10-23, GPR through
2026-10-19. The frozen archive does not refresh automatically.

The expanding-window daily-return regression uses only earlier training rows, with
training-only feature scaling, a conservative 7-day GPR availability lag and 1-day
stock lag. On 364 held-out daily cases, model MSE was 0.001110 versus the zero-drift
baseline's 0.001075; directional accuracy was 51.9%. This candidate was NOT promoted.
Revised historical datasets are not point-in-time vintages; this is not an unbiased
real-time strategy simulation or validation of intraday barrier-touch accuracy.

The last 60 WTI spot returns form an EWMA annual volatility prior. Cold-start volatility
blends 80% of this proxy prior with 20% of current TVC session range volatility. Live volatility is admitted gradually with observation count and trading-time coverage,
capped at 70% weight, avoiding an abrupt switch at the warmup threshold.
Stock/GPR return coefficients are not used to create a directional edge that failed
validation. Current news still adjusts jump/volatility risk immediately.

Each simulation hour now uses separately seeded diffusion/bridge and jump streams.
Minor news changes do not scramble all subsequent diffusion draws. No probability
clipping or cosmetic smoothing hides real price/risk changes. Percentages remain
uncalibrated estimates, and accumulating independent outcomes is still necessary.

The chart displays local date/time ticks for the selected horizon. Its guide marks
the earliest interpolated crossing of any of the displayed percentile curves, with
a point and short local tangent. It does not forecast an actual first-passage time;
when no curve crosses, the chart explicitly says so. Watch-only long/short breakout
triggers and volatility-based targets accompany WAIT; they are conditional scenarios,
not an instruction to enter before the evidence gate passes.

To rebuild the archive, install optional builder dependencies `xlrd`, `yfinance` and
`httpx`, then run `python scripts/build_history.py`. Cached public source files in
`/tmp` are reused; absent files are fetched with normal TLS verification. Inspect
coverage and rerun validation before publishing a changed archive.


## Network access for development and deployment

Allow outbound HTTPS to `feeds.bbci.co.uk`, `www.eia.gov` and `oilprice.com` for server-side RSS,
and secure WebSockets to `widgetdata.tradingview.com` for backend quotes. Keep TLS
verification enabled. A cloud environment's egress settings do not change Render or
visitors' browser settings. Allow `oilpbot.onrender.com` in the cloud network policy
when checking that deployment.

Check `/api/news` for reachable sources and dated articles, and `/api/market` for
reference timestamp and usability. A reachable feed may have no new oil articles;
EIA does not publish continuously. Confirm `/api/live` sends advancing source quote
timestamps and that both long/short forecasts return six horizons in a real browser.
Price HTTP refresh and displayed source age update every second; new upstream quote
events are pushed immediately when received. A repeated displayed price retains its
real source timestamp. Polling every second cannot guarantee a new market tick or
remove upstream latency. The backend directly subscribes to public widget quotes rather than reading an
iframe. A stale reference still returns 503 for forecasts; no alternate instrument
or invented timestamp is substituted. The app uses one upstream stream per process,
not one per browser. Free Render instances still sleep when idle; stream collection
cannot continue while the instance is asleep. No paid add-on is needed.


## Manual forecast snapshots and directional setup

A calculation fixes all six horizon probabilities and charts to its calculation time.
One-week and one-month horizons retain their original end dates; they do not silently
roll forward with the live clock. Another click uses current price and evidence and
can legitimately produce different probabilities. Failed requests retain the previous
successful calculation. Monte Carlo margins describe sampling noise, not prediction
accuracy. The model already shares deterministic random streams for repeatable results
with the same inputs and evidence; freezing the display does not establish calibration.

The directional setup appears below the calculation form, showing LONG or SHORT,
an entry reference and separate one-week/one-month volatility-based target levels.
When history or momentum does not support a direction, it explicitly shows a conditional
breakout scenario in the user's selected direction. It never turns missing evidence
into a supposedly validated recommendation. Its targets are distinct from the target
entered for touch-probability calculation. A supported experimental momentum signal can
select its own direction; all reference levels belong to that same frozen calculation.
