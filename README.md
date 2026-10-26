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
- The central estimate uses a daily-volatility, zero-drift baseline evaluated on
  held-out daily TVC:USOIL high/low target touches. More complex regime and
  inventory-weighted candidates did not clear the upgrade gate. Their assumptions
  are not presented as established predictive improvements. Live intraday-origin
  calibration and a profitable directional edge remain unvalidated.
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

## Strategic baseline and separate risk scenarios

The central estimate uses 60 daily return observations with exponentially weighted
variance (0.97), anchored to completed daily TVC bars rather than individual
live ticks or headlines. It calculates GBM touch probabilities analytically, with
no new Monte Carlo noise. Higher cumulative touch probabilities at longer horizons
are expected. News is not assumed to create a directional edge.

Temporary headline and US supply stress are separate, explicitly unvalidated
scenarios. A shared 6,000-path jump-diffusion simulation uses a six-calendar-hour
headline half-life and a five-trading-day fundamental-stress half-life. It does not
extrapolate a current news shock unchanged for a month. Common random streams keep
like-for-like stress comparisons reproducible. These decay rates and stress weights
are assumptions, not measured effects or forecast confidence bounds.

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
News raises volatility/jump uncertainty in the separate stress scenario; headlines do not automatically imply a
price direction. Feed availability and actual headlines are visible under Evidence.
This limited rule-based coverage is not comprehensive geopolitical intelligence.
There are no new paid services, API keys, AI providers, or AIS subscriptions.

Distinct TVC quote observations accumulate in the selected database when market
requests are served. The model reads the latest quote per minute over up to 1,000
minutes, so dense live ticks do not shrink its four-hour volatility warmup window.
Live history supports future exact-instrument outcome validation; it is no longer
used to turn a few hours of momentum into a directional recommendation. Free Render
sleep and missing observations still limit uninterrupted outcome collection.

The experimental outlook can suggest long/short, a one-week reference target, or
WAIT. No directional model has passed independent validation, so setups remain conditional
scenarios in the user-selected direction. No measured profitable edge is claimed. Leverage is a 1× reference, not personalized sizing.
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

The older spot/stock/GPR archive remains available for comparison and compatibility.
It no longer determines the central forecast volatility. The strategic model instead
uses `src/data/strategic.json` and its matching validation report.

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

The former calculation-linked setup is replaced by an independent live technical
panel below the form. Probability calculations remain frozen and manual-only;
the live panel never reads the target price, direction or leverage fields.


## Reproducible strategic validation

`src/data/strategic.json` contains 4,231 TVC:USOIL daily OHLC bars from 2010
through 2026-10-23, plus 929 weekly observations each for EIA commercial crude stocks
and US production. Source fingerprints, units, availability lags and limitations are
exposed at `/api/strategy`. The public service resolved `pro_name=TVC:USOIL`, `America/New_York`, and
`1800-1700`. Bar dates denote the session end day; the current unfinished session is
excluded. These are provider historical bars, not point-in-time vintages. Nonpositive-price outcome windows are explicitly
excluded because the positive-price GBM cannot represent them.

```bash
python scripts/build_strategic_data.py --refresh
python scripts/evaluate_strategic.py
```

Development selection ends before 2019. Later, non-overlapping 5/21-session outcome
windows test three fixed target distances in each direction using subsequent daily
highs/lows. Each forecast uses only earlier known daily prices; EIA values have a
conservative seven-calendar-day availability lag. Six-origin block resampling clusters
all targets from an origin. Reports include Brier scores, reliability bins, aggregate
calibration gaps, directional splits and a development-frequency benchmark. Revised
source histories are not point-in-time vintages; release revisions and retrospective provider history
prevent interpreting this as an unbiased real-time trading backtest.

Weekly baseline Brier was approximately 0.192 over 392 origins (2,352 correlated
barrier cases); monthly approximately 0.191 over 93 origins (558 cases). Aggregate
calibration gaps were about 2.3 and 3.0 percentage points. These are diagnostics for
the evaluated daily-origin TVC cohort, not an accuracy guarantee or an error bound for a user's
forecast. Regime/fundamental candidates were not promoted: the weekly candidate was worse,
and the monthly improvement interval included zero. Weekly baseline Brier did not
beat the development-frequency benchmark; monthly baseline did. These comparisons
do not establish future skill. Arbitrary live-origin/hourly touch calibration, historical headline effects, global
supply/demand, futures curve, USD/rates and positioning remain incomplete.

A background refresh downloads free daily and EIA data when a newer completed trading
day is missing, with a 12-hour retry interval. It writes atomic caches under `/tmp`,
never repository source files, and recomputes the fixed validation protocol. Refresh
cannot automatically promote a new candidate. A fingerprint mismatch removes promotion;
central forecasts are refused if daily history is over ten calendar days old. Source
failures preserve the dated archive and report a refresh failure. Free instances can
sleep; collection and refresh do not run while asleep. The strategic archive starts
current but does not create a permanently guaranteed market data service.

Forecast records use model version `strategic-tvc-v1`. Live observation-based Brier
statistics are separated by version so earlier model results are not mixed into the
new model. Sparse quote samples still do not establish continuous-touch accuracy.


## Live technical agreement and watch levels

`GET /api/signal` combines completed daily/weekly TVC bars, current source quote and
last-24-hour headline risk without any probability-form inputs. The native gauge
uses six SMA/EMA comparisons plus RSI and MACD per timeframe (16 correlated votes).
Daily periods are 20/50/200; weekly 10/20/50; RSI is 14 and MACD 12/26/9. It is our
explicit rule, not TradingView's proprietary rating or a winning probability.

A net score of at least ±0.25 selects a technical direction; otherwise no entry is
suggested. Reference entry is the completed 20-day high/low plus/minus 0.1 Wilder
ATR(14). Invalidation is 1.5 ATR away; alternative week/month targets are 2/3 ATR.
These levels do not follow the user's chosen target. Daily/weekly references update
on completed sessions; live quote events update distance/crossing state immediately.
The full rule/news check refreshes every 15 seconds. A setup expires at the next
regular session close (New York 17:00). Holidays are not an authoritative calendar.
Post-close data refresh can now include a fully completed current-date session.

Closed markets, stale data/quotes, news outages, elevated headline risk, expired or
passed entries and ambiguous entry/invalidation history cause a stand-aside state.
The panel does not chase a price beyond 0.25 ATR past the trigger, assume an order was
executed, or claim the best action. A failed historical gate forces watch-only status.
Form calculations cannot overwrite the live panel, and live updates cannot recalculate
probabilities. New completed bars and news can change the panel independently.

Run `python scripts/evaluate_signal.py` to reproduce the fixed technical-rule report
in `src/data/signal_validation.json`. Later evaluation uses non-overlapping 5/21-session
windows from 2019, next-session entry, stop-first ambiguous daily ordering, worse
opening gap fills, and 5/10/25 basis-point round-trip cost assumptions. Funding,
real broker execution and the live news gate are not validated. Six-trade block
resampling is not a guarantee of independence or future profit.

The frozen rule FAILED its reliability gate: 52 weekly and 22 monthly triggered trades;
mean returns after 25 bps were about -0.58% and -2.08%. It is not a profitable strategy
claim. The site reports those results and keeps the levels watch-only. Changing a
threshold after inspecting this sample is not fresh out-of-sample validation.

The database records a deduplicated paper-signal ledger (`technical_signal_log`) of
rule/data version, first observed quote, levels, risk state and headline set. It logs
state/headline changes, not every price tick; it never places trades or fabricates
performance. This supplies future audit evidence without flooding free storage.
Runtime archive refreshes invalidate promotion against an unmatched frozen report;
old report results retain their own data-end date. Monitor retained database storage.
